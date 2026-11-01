"""The job engine: from a link to a stored video and a transcript.

A job moves through stages, each run by its own kind of thread:

  fetch    resolve the link, download the video and any captions (platform requests)
  asr      transcribe the stored file (no platform requests, ever)
  profile  list an account's videos and queue one video job each

Threads claim work with a lease they renew while they hold it. A worker that
dies leaves a lease that runs out, and the next claim picks the job up again —
at the stage it was in, so a finished download is not repeated because the
transcription after it crashed. Delivery is at least once; every stage is
written so that running it twice does no harm.

Everything stored is keyed by platform and video ID, not by job. A second job
for a video that is already here — from the same caller or another — costs the
platform nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import mimetypes
import os
import random
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import understand as understand_engine
from . import VERSION, fetch, guard, ocr as ocr_engine
from .asr import Asr, extract_audio, has_audio
from .config import Settings
from .db import TERMINAL, Db
from .errors import ACTIONS, JobError, classify
from .routes import Routes

LOG = logging.getLogger("smt.engine")

LEASE_S = 90
# Instagram's picture links are signed and expire after some days; a caller who
# waits for the pictures gets links younger than this.
IMAGE_LINKS_FRESH_S = 6 * 3600
MAX_PRIORITY_ITEMS = 100
MAX_ATTEMPTS = 5
ACTIVE = ("queued", "resolving", "fetching", "processing", "retry_wait", "blocked")


def _now() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat().replace("+00:00", "Z") if ts else None


def _image_mime(path: Path) -> str:
    head = path.read_bytes()[:12]
    if head.startswith(b"\x89PNG"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _priority(options: dict | None) -> int:
    """"high": a caller is waiting for this one job, so it goes before bulk work."""
    value = (options or {}).get("priority") or "normal"
    if value not in ("normal", "high"):
        raise JobError("invalid_input", 'priority is "normal" or "high"')
    return 1 if value == "high" else 0


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _probe_streams(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if out.returncode != 0:
        raise JobError("parser", f"the downloaded file is not a readable video: {out.stderr.strip()[:300]}")
    data = json.loads(out.stdout or "{}")
    kinds = {s.get("codec_type") for s in data.get("streams", [])}
    duration = float((data.get("format") or {}).get("duration") or 0)
    if not kinds & {"video", "audio"} or duration <= 0:
        raise JobError("parser", "the downloaded file has no playable stream")
    return {"video": "video" in kinds, "audio": "audio" in kinds, "duration": duration}


class Engine:
    def __init__(self, settings: Settings, db: Db | None = None, asr_factory=None, ocr=None):
        self.s = settings
        self.db = db or Db(settings.db_path)
        self.routes = Routes(self.db, settings.routes, settings.gap_seconds)
        for d in (settings.media_dir, settings.tmp_dir, settings.models_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._asr_factory = asr_factory or (lambda model: Asr(model, settings.asr_device, settings.models_dir))
        self._asr: dict[str, Asr] = {}
        self._asr_lock = threading.Lock()
        self._held: set[str] = set()
        self._held_lock = threading.Lock()
        self._stop = threading.Event()
        self.name = f"worker-{os.getpid()}-{secrets.token_hex(3)}"
        # Adapters are attributes so a test can stand in for the network.
        self.fetch = fetch
        self.ocr = ocr or ocr_engine.read_text
        self.understand = understand_engine
        self.model_keys = understand_engine.available()

    # ------------------------------------------------------------------
    # Accepting work
    # ------------------------------------------------------------------

    def _options(self, options: dict) -> dict:
        route = options.get("route")
        if route is not None and route not in self.s.routes:
            raise JobError("invalid_input", f"route {route!r} is not configured here (have: {', '.join(self.s.routes)})")
        model = options.get("asr_model") or self.s.asr_model
        allowed = {self.s.asr_model, *self.s.asr_models}
        if model not in allowed:
            raise JobError("invalid_input", f"asr_model must be one of {', '.join(sorted(allowed))} here")
        asr = options.get("asr") or "auto"
        if asr not in ("auto", "always"):
            raise JobError("invalid_input", 'asr is "auto" (captions first) or "always"')
        language = options.get("language")
        out = {"language": language.lower() if language else None, "route": route, "asr_model": model, "asr": asr}
        # Understanding: only when asked, so a job without it keeps its old key.
        for name in ("describe_images", "describe_video"):
            wanted = options.get(name)
            if not wanted:
                continue
            if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,80}", str(wanted)):
                raise JobError("invalid_input", f"{name} is a model name like gemini-3.8-flash")
            prov = understand_engine.provider(wanted)
            if prov not in self.model_keys:
                raise JobError("invalid_input", f"{name}: this worker has no key for {prov}")
            if name == "describe_video" and prov != "gemini":
                raise JobError("invalid_input", "describe_video needs a Gemini model: only Gemini watches videos")
            out[name] = wanted
        return out

    def _insert(self, tenant: str, kind: str, target: guard.Target, url: str, options: dict,
                idem: str | None, parent: str | None = None, priority: int = 0) -> tuple[dict, bool]:
        route = options["route"] or self.s.default_route[target.platform]
        key = idem or hashlib.sha256(json.dumps(
            {"kind": kind, "url": target.canonical_url, **options}, sort_keys=True).encode()).hexdigest()
        existing = self.db.one("select * from job where tenant = ? and idem_key = ?", (tenant, key))
        if existing and existing["status"] in ("failed", "partial", "cancelled"):
            # Asking again after a failure means: try again. What is stored
            # already is kept, so the second try only does what is missing.
            self.db.run(
                "update job set status = 'queued', stage = ?, attempts = 0, next_at = 0, error_class = null,"
                " error = null, warnings = '[]', finished_at = null, updated_at = ? where id = ?",
                ("profile" if kind == "profile" else "fetch", _now(), existing["id"]))
            self.db.run("delete from profile_cursor where job_id = ?", (existing["id"],))
            self.db.audit("retry", tenant, existing["id"])
            existing = self.db.one("select * from job where id = ?", (existing["id"],))
        if existing:
            if priority > existing["priority"]:
                # Someone now waits for a job that was queued in bulk.
                self.db.run("update job set priority = ? where id = ?", (priority, existing["id"]))
                existing = self.db.one("select * from job where id = ?", (existing["id"],))
            return existing, True
        job_id = "job_" + secrets.token_hex(12)
        now = _now()
        self.db.run(
            "insert into job (id, tenant, kind, idem_key, input_url, platform, platform_id, canonical_url, options,"
            " status, stage, route, parent_id, priority, created_at, updated_at)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?) on conflict (tenant, idem_key) do nothing",
            (job_id, tenant, kind, key, url, target.platform, target.platform_id if kind != "profile" else None,
             target.canonical_url, json.dumps(options), "profile" if kind == "profile" else "fetch",
             route, parent, priority, now, now),
        )
        job = self.db.one("select * from job where tenant = ? and idem_key = ?", (tenant, key))
        created = job["id"] == job_id
        if created:
            self.db.audit("submit", tenant, job_id, kind=kind, url=target.canonical_url)
        return job, not created

    def submit_video(self, tenant: str, url: str, options: dict | None = None, idem: str | None = None,
                     parent: str | None = None) -> tuple[dict, bool]:
        target = guard.parse(url)
        if target.kind == "profile":
            raise JobError("invalid_input", "this is an account, not a video — use a profile import")
        return self._insert(tenant, "video", target, url, self._options(options or {}), idem, parent,
                            _priority(options))

    def submit_images(self, tenant: str, url: str, options: dict | None = None, idem: str | None = None,
                      parent: str | None = None) -> tuple[dict, bool]:
        """A picture post or carousel: the text on its pictures."""
        target = guard.parse(url)
        if target.platform != "instagram" or target.kind != "video":
            raise JobError("invalid_input", "pictures are read from Instagram post links (instagram.com/p/...)")
        return self._insert(tenant, "images", target, url, self._options(options or {}), idem, parent,
                            _priority(options))

    def submit_profile(self, tenant: str, url: str, options: dict | None = None,
                       idem: str | None = None) -> tuple[dict, bool]:
        target = guard.parse(url)
        if target.kind != "profile":
            raise JobError("invalid_input", "this is not an account link (https://www.tiktok.com/@name, "
                                            "https://www.instagram.com/name/)")
        options = options or {}
        opts = self._options(options)
        limit = int(options.get("max_items") or 25)
        if not 1 <= limit <= self.s.max_profile_items:
            raise JobError("invalid_input", f"max_items is between 1 and {self.s.max_profile_items}")
        for key in ("since", "until"):
            if options.get(key):
                try:
                    dt.date.fromisoformat(options[key])
                except ValueError as exc:
                    raise JobError("invalid_input", f"{key} is a date like 2026-10-01") from exc
        opts.update(max_items=limit, since=options.get("since"), until=options.get("until"),
                    include_images=bool(options.get("include_images")) and target.platform == "instagram")
        return self._insert(tenant, "profile", target, url, opts, idem, priority=_priority(options))

    def cancel(self, job: dict) -> dict:
        if job["status"] not in TERMINAL:
            now = _now()
            self.db.run(
                "update job set status = 'cancelled', stage = null, lease_owner = null, lease_until = null,"
                " error_class = 'cancelled', error = 'cancelled by request', updated_at = ?, finished_at = ?"
                " where id = ? or (parent_id = ? and status not in ('completed','partial','failed','cancelled'))",
                (now, now, job["id"], job["id"]),
            )
            self.db.audit("cancel", job["tenant"], job["id"])
        return self.db.one("select * from job where id = ?", (job["id"],))

    def unblock(self, platform: str, route: str) -> int:
        """A person lifted a block: put the jobs that waited for it back in line."""
        self.routes.unblock(platform, route)
        n = self.db.run(
            "update job set status = 'queued', next_at = 0, updated_at = ? "
            "where status = 'blocked' and platform = ? and route = ?", (_now(), platform, route))
        self.db.audit("unblock", None, None, platform=platform, route=route, jobs=n)
        return n

    # ------------------------------------------------------------------
    # Claiming
    # ------------------------------------------------------------------

    def claim(self, stages: tuple[str, ...]) -> dict | None:
        now = _now()
        marks = ",".join("?" * len(stages))
        # A video is fetched only when Whisper is about to need it: with more
        # than asr_backlog files already waiting for transcription, video
        # fetches wait. So downloaded audio is never stockpiled on disk, and
        # an account's requests to the platform spread out over the run.
        # A job someone waits for (priority 1, an MCP call) goes first and its
        # video is fetched at once: it is one file, not an account's worth.
        job = self.db.one(
            f"update job set lease_owner = ?, lease_until = ?, attempts = attempts + 1, updated_at = ? "
            f"where id = (select id from job where stage in ({marks}) and next_at <= ? "
            f" and status in ('queued','resolving','fetching','processing','retry_wait') "
            f" and (lease_until is null or lease_until < ?) "
            f" and (stage != 'fetch' or kind != 'video' or priority > 0 "
            f"      or (select count(*) from job w where w.stage = 'asr') < ?) "
            f" order by priority desc, created_at limit 1) "
            f"returning *",
            (self.name, now + LEASE_S, now, *stages, now, now, self.s.asr_backlog),
        )
        if job:
            with self._held_lock:
                self._held.add(job["id"])
        return job

    def _release(self, job_id: str):
        with self._held_lock:
            self._held.discard(job_id)

    def heartbeat(self):
        with self._held_lock:
            held = list(self._held)
        if held:
            marks = ",".join("?" * len(held))
            self.db.run(f"update job set lease_until = ? where lease_owner = ? and id in ({marks})",
                        (_now() + LEASE_S, self.name, *held))
        self._sweep_parents()

    def _set(self, job_id: str, **fields):
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        # A cancelled job stays cancelled whatever a thread still running on it finishes.
        self.db.run(f"update job set {cols} where id = :id and status != 'cancelled'", {**fields, "id": job_id})

    def _count(self, job_id: str):
        def bump():
            self.db.run("update job set platform_requests = platform_requests + 1 where id = ?", (job_id,))
        return bump

    def _warn(self, job_id: str, warning: str):
        row = self.db.one("select warnings from job where id = ?", (job_id,))
        warnings = json.loads(row["warnings"]) if row else []
        if warning not in warnings:
            warnings.append(warning)
            self.db.run("update job set warnings = ? where id = ?", (json.dumps(warnings), job_id))

    # ------------------------------------------------------------------
    # Running
    # ------------------------------------------------------------------

    def run_once(self, stages: tuple[str, ...]) -> bool:
        job = self.claim(stages)
        if not job:
            return False
        try:
            if job["stage"] == "fetch":
                self._fetch(job)
            elif job["stage"] == "asr":
                self._transcribe(job)
            else:
                self._profile(job)
        except Exception as exc:  # noqa: BLE001 — every failure ends in the job's record
            self._failed(job, classify(exc))
        finally:
            self.db.run("update job set lease_owner = null, lease_until = null where id = ?", (job["id"],))
            self._release(job["id"])
        if job["parent_id"]:
            self._sweep_parents()  # the import ends with its last video, not at the next heartbeat
        return True

    def _failed(self, job: dict, err: JobError):
        LOG.warning("job %s: %s (%s)", job["id"], err.cls, str(err)[:200])
        base = {"error_class": err.cls, "error": str(err)[:2000]}
        if err.cls == "challenge":
            self._set(job["id"], status="blocked", next_at=_now() + 365 * 86400, **base)
        elif err.cls == "rate_limited":
            wait = err.retry_after or 15 * 60
            # A wait is not an attempt: the job did nothing wrong.
            self.db.run("update job set attempts = max(0, attempts - 1) where id = ?", (job["id"],))
            self._set(job["id"], status="retry_wait", next_at=_now() + wait, **base)
        elif not err.terminal and job["attempts"] < MAX_ATTEMPTS:
            delay = min(30 * 2 ** (job["attempts"] - 1), 1800) * random.uniform(0.8, 1.2)
            self._set(job["id"], status="retry_wait", next_at=_now() + delay, **base)
        else:
            current = self.db.one("select platform, platform_id from job where id = ?", (job["id"],)) or job
            has_video = current["platform_id"] and self.db.one(
                "select 1 from artifact a join media m on m.id = a.media_id where a.kind = 'video' "
                "and m.platform = ? and m.platform_id = ?", (current["platform"], current["platform_id"]))
            self._set(job["id"], status="partial" if has_video else "failed", stage=None,
                      finished_at=_now(), **base)

    # -- fetch -------------------------------------------------------------

    def _parts(self, platform: str, platform_id: str) -> list[dict]:
        return self.db.all("select * from media where platform = ? and platform_id = ? order by part",
                           (platform, platform_id))

    def _video(self, media_id: int) -> dict | None:
        return self.db.one("select * from artifact where media_id = ? and kind = 'video' order by created_at desc",
                           (media_id,))

    def _publish(self, media: dict, src: Path, kind: str, language: str | None = None) -> dict:
        """Moves a finished file into the store under a random name: partial
        files never appear there, and no name reveals a path or an account."""
        art_id = "art_" + secrets.token_hex(12)
        rel = Path(media["platform"]) / str(media["id"]) / f"{art_id}{src.suffix}"
        dst = self.s.media_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        size, digest = src.stat().st_size, _sha256(src)
        os.replace(src, dst)
        mime = mimetypes.guess_type(dst.name)[0] or ("text/vtt" if src.suffix == ".vtt" else "application/octet-stream")
        self.db.run(
            "insert into artifact (id, media_id, kind, path, mime, size, sha256, language, created_at)"
            " values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (art_id, media["id"], kind, rel.as_posix(), mime, size, digest, language, _now()),
        )
        return {"id": art_id, "path": dst}

    def _meta(self, info: dict) -> dict:
        ts = info.get("timestamp")
        return {
            "title": info.get("title") or info.get("fulltitle"),
            "description": info.get("description"),
            "author": info.get("uploader") or info.get("channel"),
            "author_id": info.get("uploader_id") or info.get("channel_id"),
            "published_at": _iso(ts) if ts else None,
            "duration_seconds": info.get("duration"),
        }

    def _media_rows(self, platform: str, platform_id: str, canonical: str, infos: list[dict]) -> list[dict]:
        for part, info in enumerate(infos):
            self.db.run(
                "insert into media (platform, platform_id, part, canonical_url, info, info_at, meta)"
                " values (?, ?, ?, ?, ?, ?, ?) on conflict (platform, platform_id, part) do update"
                " set info = excluded.info, info_at = excluded.info_at, meta = excluded.meta",
                (platform, platform_id, part, canonical, json.dumps(info), _now(), json.dumps(self._meta(info))),
            )
        return self._parts(platform, platform_id)

    def _fetch(self, job: dict):
        if job["kind"] == "images":
            return self._fetch_images(job)
        opts = json.loads(job["options"])
        count = self._count(job["id"])
        proxy = self.routes.proxy(job["route"])
        target = guard.parse(job["input_url"])

        if target.kind == "short":
            self._set(job["id"], status="resolving")
            with self.routes.request(target.platform, job["route"]):
                try:
                    target = guard.resolve_short(target, proxy, count)
                    self.routes.record(target.platform, job["route"], None)
                except Exception as exc:
                    err = classify(exc)
                    self.routes.record(target.platform, job["route"], err)
                    raise err from exc
            self._set(job["id"], platform_id=target.platform_id, canonical_url=target.canonical_url)

        parts = self._parts(target.platform, target.platform_id)
        # A transcript that exists answers the job: no download, even when the
        # file it was made from is long deleted.
        # A description still wanted needs the video once more.
        known = bool(parts) and all(self._chosen(p["id"], opts) and not self._needs_description(p["id"], opts)
                                    for p in parts)
        if not known and (not parts or any(self._video(p["id"]) is None for p in parts)):
            parts = self._download(job, target, parts, proxy, count)
        self._set(job["id"], media_id=parts[0]["id"])

        if known or all(self._chosen(p["id"], opts) and not self._needs_description(p["id"], opts) for p in parts):
            self._finish(job["id"], parts)
        else:
            # A new stage starts its own count of attempts.
            self._set(job["id"], status="processing", stage="asr", next_at=0, attempts=0)

    def _download(self, job: dict, target: guard.Target, parts: list[dict], proxy, count) -> list[dict]:
        free_mb = shutil.disk_usage(self.s.data).free // (1024 * 1024)
        if free_mb < self.s.min_free_mb:
            raise JobError("disk_full", f"only {free_mb} MB free, {self.s.min_free_mb} MB kept in reserve")
        platform, route = target.platform, job["route"]
        opts = json.loads(job["options"])
        tmp_root = self.s.tmp_dir / job["id"]
        cookie_dir = tmp_root / "session"
        cookie_dir.mkdir(parents=True, exist_ok=True)
        cookiefile = self.fetch.cookie_copy(self.s.cookies_dir, platform, cookie_dir)
        try:
            # Resolved and downloaded in one session: the media addresses only
            # work with the cookies their resolution got (see fetch.Session).
            with self.fetch.session(proxy, cookiefile, self.s.max_video_mb, self.s.keep_media, count,
                                    want_video=bool(opts.get("describe_video"))) as ses:
                infos = self._resolve(job, target, ses)
                parts = self._media_rows(platform, target.platform_id, target.canonical_url, infos)
                self._set(job["id"], status="fetching")
                for media, info in zip(parts, infos):
                    if self._video(media["id"]) or (self._chosen(media["id"], opts)
                                                    and not self._needs_description(media["id"], opts)):
                        continue
                    duration = info.get("duration") or 0
                    if duration > self.s.max_duration_s:
                        raise JobError("too_large", f"{int(duration)} s is over the {self.s.max_duration_s} s limit")
                    tmp = tmp_root / f"part{media['part']}"
                    shutil.rmtree(tmp, ignore_errors=True)
                    tmp.mkdir(parents=True)
                    if self._captions_first(info, opts):
                        got = self._download_one(platform, route, info, tmp, ses, captions_only=True)
                        for track in got["captions"]:
                            self._store_captions(media, track)
                        if self._chosen(media["id"], opts):
                            continue
                        # The captions did not settle it after all: the audio, for Whisper.
                    got = self._download_one(platform, route, info, tmp, ses)
                    probe = _probe_streams(got["video"])
                    # What the post is, not what was downloaded: without keep_media
                    # only its audio comes.
                    probe["video"] = probe["video"] or any(
                        (f.get("vcodec") or "none") != "none" for f in info.get("formats") or [])
                    self._publish(media, got["video"], "video")
                    meta = json.loads(media["meta"])
                    meta.update(has_video=probe["video"], has_audio=probe["audio"],
                                duration_seconds=meta.get("duration_seconds") or round(probe["duration"], 2))
                    self.db.run("update media set meta = ? where id = ?", (json.dumps(meta), media["id"]))
                    for track in got["captions"]:
                        self._store_captions(media, track)
            return self._parts(platform, target.platform_id)
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def _resolve(self, job, target, ses) -> list[dict]:
        self._set(job["id"], status="resolving")
        with self.routes.request(target.platform, job["route"]):
            try:
                infos = ses.resolve(target.fetch_url or target.canonical_url)
            except JobError as err:
                self.routes.record(target.platform, job["route"], err)
                raise
            self.routes.record(target.platform, job["route"], None)
        return infos

    def _captions_first(self, info: dict, opts: dict) -> bool:
        """Whether the creator's captions will answer, so that no media needs
        downloading: _chosen's rule, read from what the extractor listed."""
        from .captions import language
        if self.s.keep_media or opts["asr"] == "always" or opts.get("describe_video"):
            return False
        langs = {language(k) for k in info.get("subtitles") or {} if k != "live_chat"}
        return opts["language"] in langs if opts["language"] else len(langs) == 1

    def _download_one(self, platform, route, info, tmp, ses, captions_only=False) -> dict:
        with self.routes.request(platform, route):
            try:
                got = ses.download(info, tmp, captions_only)
            except JobError as err:
                self.routes.record(platform, route, err)
                langs = sorted(k for k in info.get("subtitles") or {} if k != "live_chat")
                if err.cls == "drm" and langs:
                    # Copy-protected media can still have readable captions (Vimeo).
                    raise JobError("drm", "the audio is copy-protected (DRM), but the video has captions in "
                                          f"{', '.join(langs)}: ask again with language set to one of them") from err
                raise
            self.routes.record(platform, route, None)
        return got

    def _store_captions(self, media: dict, track: dict):
        from .captions import language
        try:
            segments = self.fetch.caption_segments(track)
        except Exception as exc:  # noqa: BLE001 — a broken caption file is not a broken job
            LOG.warning("captions %s unreadable: %s", track["path"], exc)
            return
        lang = language(track["language_raw"])
        self._publish(media, Path(track["path"]), "captions", lang)
        self.db.run(
            "insert into track (media_id, source, language, origin, params, status, segments, text, created_at)"
            " values (?, 'native_captions', ?, 'unknown', ?, ?, ?, ?, ?)",
            (media["id"], lang, json.dumps({"platform_language": track["language_raw"], "format": track["ext"]}),
             "available" if segments else "no_speech", json.dumps(segments),
             " ".join(s["text"] for s in segments), _now()),
        )

    # -- choosing a transcript ----------------------------------------------

    def _chosen(self, media_id: int, opts: dict) -> dict | None:
        """The text track that answers this job, or None when ASR must run.

        Captions first, but only when it is clear they are the spoken words: a
        track in the language asked for, or the only track there is. Several
        tracks and no language asked for usually means an original and
        machine translations of it, and nothing says which is which — then the
        speech itself is transcribed.
        """
        tracks = self.db.all("select * from track where media_id = ? order by created_at desc", (media_id,))
        asr = [t for t in tracks if t["source"] == "asr" and t["model"] == opts["asr_model"]]
        native = [t for t in tracks if t["source"] == "native_captions" and t["status"] == "available"]
        if opts["asr"] != "always":
            if opts["language"]:
                match = [t for t in native if t["language"] == opts["language"]]
                if match:
                    return match[0]
            elif len({t["language"] for t in native}) == 1:
                return native[0]
        return asr[0] if asr else None

    # -- asr ------------------------------------------------------------------

    def _model(self, name: str) -> Asr:
        with self._asr_lock:
            if name not in self._asr:
                self._asr[name] = self._asr_factory(name)
            return self._asr[name]

    def preload(self):
        try:
            self._model(self.s.asr_model).load()
        except Exception:  # noqa: BLE001 — the first job will report it properly
            LOG.exception("could not load %s", self.s.asr_model)

    def asr_state(self) -> dict:
        model = self._asr.get(self.s.asr_model)
        if not model or not getattr(model, "device", None):
            return {"model": self.s.asr_model, "loaded": False}
        return {"model": model.name, "loaded": True, "device": model.device, "compute_type": model.compute_type,
                "revision": model.revision}

    def _transcribe(self, job: dict):
        opts = json.loads(job["options"])
        parts = self._parts(job["platform"], job["platform_id"])
        for media in parts:
            if self._chosen(media["id"], opts):
                continue
            video = self._video(media["id"])
            if not video:
                raise JobError("internal", "transcription reached without a stored video")
            path = self.s.media_dir / video["path"]
            model = self._model(opts["asr_model"])
            if not has_audio(path):
                result = {"status": "no_audio", "language": None, "segments": [], "text": "",
                          "quality": {"flags": []}, "model": opts["asr_model"], "model_revision": None, "params": {}}
            else:
                wav = self.s.tmp_dir / f"{job['id']}-{media['id']}.wav"
                try:
                    extract_audio(path, wav)
                    try:
                        result = model.transcribe(wav)
                    except Exception as exc:  # noqa: BLE001
                        if job["attempts"] < 2:
                            raise JobError("network", f"transcription failed once, trying again: {exc}") from exc
                        self._warn(job["id"], f"asr_failed: {exc}"[:300])
                        result = None
                finally:
                    wav.unlink(missing_ok=True)
            if result is None:
                self._drop_media(parts)
                self._set(job["id"], status="partial", stage=None, finished_at=_now(), error_class="internal",
                          error="transcription failed" + ("; the video is stored" if self.s.keep_media else ""))
                return
            self.db.run(
                "insert into track (media_id, source, language, origin, model, model_revision, params, status,"
                " segments, text, quality, created_at) values (?, 'asr', ?, 'speech', ?, ?, ?, ?, ?, ?, ?, ?)",
                (media["id"], result["language"], result["model"], result["model_revision"],
                 json.dumps(result["params"]), result["status"], json.dumps(result["segments"]), result["text"],
                 json.dumps(result["quality"]), _now()),
            )
            for flag in result["quality"].get("flags", []):
                self._warn(job["id"], flag)
        for media in parts:
            if self._needs_description(media["id"], opts):
                self._describe_video(job, media, opts["describe_video"])
        self._finish(job["id"], parts)

    # -- understanding ----------------------------------------------------------

    def _description(self, media_id: int, source: str, model: str) -> dict | None:
        return self.db.one("select * from track where media_id = ? and source = ? and model = ?"
                           " order by created_at desc limit 1", (media_id, source, model))

    def _needs_description(self, media_id: int, opts: dict) -> bool:
        model = opts.get("describe_video")
        return bool(model) and not self._description(media_id, "video_description", model)

    def _describe_video(self, job: dict, media: dict, model: str):
        """What happens in the video, from the copy fetched for it. A model
        that is busy or unreachable is asked again later; anything else is a
        warning, and the transcript stands."""
        video = self._video(media["id"])
        if not video:
            self._warn(job["id"], "video_description_skipped: no video copy to show the model")
            return
        path = self.s.media_dir / video["path"]
        try:
            text = self.understand.describe_video(model, path.read_bytes(), video["mime"] or "video/mp4",
                                                  self.model_keys)
        except JobError as err:
            if err.cls == "network" and job["attempts"] < MAX_ATTEMPTS:
                raise
            self._warn(job["id"], f"video_description_failed: {err}"[:300])
            return
        self.db.run(
            "insert into track (media_id, source, language, origin, model, params, status, segments, text, quality,"
            " created_at) values (?, 'video_description', null, 'pictures_and_sound', ?, '{}', ?, '[]', ?, '{}', ?)",
            (media["id"], model, "available" if text else "no_text", text, _now()))

    # -- pictures ---------------------------------------------------------------

    def _ocr_track(self, media_id: int) -> dict | None:
        return self.db.one("select * from track where media_id = ? and source = 'ocr' order by created_at desc limit 1",
                           (media_id,))

    def _fetch_images(self, job: dict):
        """Reads the text on a post's pictures. Each picture is deleted as
        soon as it is read; the text is what stays."""
        target = guard.parse(job["input_url"])
        want = json.loads(job["options"]).get("describe_images")
        parts = self._parts("instagram", target.platform_id)
        read = bool(parts and self._ocr_track(parts[0]["id"]))
        described = not want or bool(parts and self._description(parts[0]["id"], "vision", want))
        if read and described:
            fresh = _now() - json.loads(parts[0]["meta"]).get("image_links_at", 0) < IMAGE_LINKS_FRESH_S
            if not job["priority"] or fresh:
                self._set(job["id"], media_id=parts[0]["id"])
                return self._finish(job["id"])
            # Somebody waits for the pictures: their links once more, the text stays.
        count = self._count(job["id"])
        proxy = self.routes.proxy(job["route"])
        self._set(job["id"], status="resolving")
        with self.routes.request("instagram", job["route"]):
            try:
                post = self.fetch.instagram_images(target.platform_id, proxy, count,
                                                   self.s.instaloader_session, self.s.instagram_user)
            except JobError as err:
                self.routes.record("instagram", job["route"], err)
                raise
            self.routes.record("instagram", job["route"], None)
        meta = {"title": None, "description": post["caption"], "author": post["author"],
                "published_at": _iso(post["taken_at"].timestamp()) if post.get("taken_at") else None,
                "post_kind": post["kind"], "images": len(post["images"]), "videos_not_read": post["videos"],
                # Instagram's signed CDN addresses: they expire after some days.
                "image_urls": post["images"], "image_links_at": _now(),
                "has_video": False}
        self.db.run(
            "insert into media (platform, platform_id, part, canonical_url, meta) values ('instagram', ?, 0, ?, ?)"
            " on conflict (platform, platform_id, part) do update set meta = excluded.meta",
            (target.platform_id, target.canonical_url, json.dumps(meta)))
        media = self._parts("instagram", target.platform_id)[0]
        if read and described:
            self._set(job["id"], media_id=media["id"])
            return self._finish(job["id"])
        self._set(job["id"], status="fetching", media_id=media["id"])
        tmp = self.s.tmp_dir / job["id"]
        tmp.mkdir(parents=True, exist_ok=True)
        segments, descriptions = [], []
        try:
            # Pictures come from Instagram's CDN, not its API: no route budget,
            # but every one is counted.
            for i, url in enumerate(post["images"], 1):
                path = self.fetch.download_image(url, tmp / f"{i}.jpg", proxy, count)
                if not read:
                    segments.append({"image": i, "text": self.ocr(path)})
                if not described:
                    try:
                        descriptions.append({"image": i, "text": self.understand.describe_image(
                            want, path.read_bytes(), _image_mime(path), self.model_keys)})
                    except JobError as err:
                        if err.cls == "network" and job["attempts"] < MAX_ATTEMPTS:
                            raise
                        # The text read off the pictures still counts.
                        self._warn(job["id"], f"picture_description_failed: {err}"[:300])
                        described, descriptions = True, []
                path.unlink(missing_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if descriptions:
            self.db.run(
                "insert into track (media_id, source, language, origin, model, params, status, segments, text,"
                " quality, created_at) values (?, 'vision', null, 'pictures', ?, '{}', ?, ?, ?, '{}', ?)",
                (media["id"], want, "available" if any(d["text"] for d in descriptions) else "no_text",
                 json.dumps(descriptions), "\n\n".join(f"Slide {d['image']}:\n{d['text']}" for d in descriptions),
                 _now()))
        if read:
            return self._finish(job["id"])
        read = [seg for seg in segments if seg["text"]]
        self.db.run(
            "insert into track (media_id, source, language, origin, model, params, status, segments, text, quality,"
            " created_at) values (?, 'ocr', null, 'pictures', ?, '{}', ?, ?, ?, ?, ?)",
            (media["id"], ocr_engine.version() or "tesseract", "available" if read else "no_text",
             json.dumps(segments), "\n\n".join(seg["text"] for seg in read),
             json.dumps({"images": len(segments), "with_text": len(read)}), _now()))
        self._finish(job["id"])

    def _finish(self, job_id: str, parts: list[dict] | None = None):
        self._drop_media(parts or [])
        self._set(job_id, status="completed", stage=None, finished_at=_now(), error_class=None, error=None)

    def _drop_media(self, parts: list[dict]):
        """Without keep_media the downloaded file goes as soon as it has done
        its work; the text tracks stay."""
        if self.s.keep_media:
            return
        for media in parts:
            for art in self.db.all("select id, path from artifact where media_id = ? and kind = 'video'", (media["id"],)):
                (self.s.media_dir / art["path"]).unlink(missing_ok=True)
                self.db.run("delete from artifact where id = ?", (art["id"],))

    # -- profiles -----------------------------------------------------------

    def _profile(self, job: dict):
        opts = json.loads(job["options"])
        target = guard.parse(job["input_url"])
        proxy = self.routes.proxy(job["route"])
        count = self._count(job["id"])
        row = self.db.one("select * from profile_cursor where job_id = ?", (job["id"],))
        items = json.loads(row["seen"]) if row else []
        cursor = json.loads(row["cursor"]) if row and row["cursor"] else None
        since = dt.datetime.fromisoformat(opts["since"]).replace(tzinfo=dt.timezone.utc) if opts.get("since") else None
        until = (dt.datetime.fromisoformat(opts["until"]).replace(tzinfo=dt.timezone.utc) + dt.timedelta(days=1)
                 if opts.get("until") else None)

        def save(cur=None):
            self.db.run(
                "insert into profile_cursor (job_id, cursor, seen) values (?, ?, ?) on conflict (job_id)"
                " do update set cursor = coalesce(excluded.cursor, cursor), seen = excluded.seen",
                (job["id"], json.dumps(cur) if cur else None, json.dumps(items)))

        seen = {i["id"] for i in items}
        # A waiting caller's import goes first only while it is a caller's size (the
        # MCP tools take up to 100): a 1000-post import must not hold up everyone else.
        waiting = job["priority"] and opts["max_items"] <= MAX_PRIORITY_ITEMS
        child_opts = {"language": opts["language"], "route": opts["route"], "asr_model": opts["asr_model"],
                      "asr": opts["asr"], "priority": "high" if waiting else "normal",
                      **{k: opts[k] for k in ("describe_images", "describe_video") if opts.get(k)}}

        def take(item, cur=None):
            """One listed post: its own job, and kept with the cursor right away."""
            taken = item.get("taken_at")
            if len(items) >= opts["max_items"] or item["id"] in seen:
                return
            if taken and ((since and taken < since) or (until and taken >= until)):
                return
            kind = item.get("kind") or "reel"
            submit = self.submit_images if kind in ("image", "carousel") else self.submit_video
            child, _ = submit(job["tenant"], item["url"], child_opts, parent=job["id"])
            items.append({"id": item["id"], "url": item["url"], "job_id": child["id"], "kind": kind,
                          "caption": item.get("caption"), "taken_at": taken.isoformat() if taken else None})
            seen.add(item["id"])
            save(cur)

        self._set(job["id"], status="resolving")
        cookie_dir = self.s.tmp_dir / job["id"]
        cookie_dir.mkdir(parents=True, exist_ok=True)
        try:
            with self.routes.request(target.platform, job["route"]):
                try:
                    if target.platform == "tiktok":
                        cookiefile = self.fetch.cookie_copy(self.s.cookies_dir, "tiktok", cookie_dir)
                        # Listed with some room, because a date window drops some.
                        found = self.fetch.tiktok_profile(target.platform_id, opts["max_items"] * (3 if since else 1),
                                                          proxy, count, cookiefile)
                    else:
                        found = self.fetch.instagram_profile(
                            target.platform_id, opts["max_items"], since, proxy, count, cursor,
                            self.s.instaloader_session, self.s.instagram_user, on_item=take,
                            include_images=opts.get("include_images", False))
                    self.routes.record(target.platform, job["route"], None)
                except JobError as err:
                    self.routes.record(target.platform, job["route"], err)
                    raise
        finally:
            shutil.rmtree(cookie_dir, ignore_errors=True)

        for item in found:  # already taken one by one where the listing streams them
            take(item)
        save()
        if len(items) < opts["max_items"]:
            self._warn(job["id"], f"found {len(items)} of {opts['max_items']} videos asked for "
                                  "(the account has fewer in the window, or listing stopped early)")
        # Nothing left to do here; the sweep closes the job when its videos are done.
        self._set(job["id"], status="processing", stage=None)
        self._sweep_parents()

    def _sweep_parents(self):
        for parent in self.db.all("select id from job where kind = 'profile' and stage is null and status = 'processing'"):
            row = self.db.one("select seen from profile_cursor where job_id = ?", (parent["id"],))
            ids = [i["job_id"] for i in json.loads(row["seen"])] if row else []
            if ids:
                marks = ",".join("?" * len(ids))
                states = [r["status"] for r in self.db.all(f"select status from job where id in ({marks})", ids)]
            else:
                states = []
            if all(s in TERMINAL for s in states):
                self._set(parent["id"], status="completed" if all(s == "completed" for s in states) else "partial",
                          finished_at=_now())

    # ------------------------------------------------------------------
    # Answers
    # ------------------------------------------------------------------

    def status(self, job: dict) -> dict:
        out = {
            "job_id": job["id"], "kind": job["kind"], "status": job["status"],
            "platform": job["platform"], "platform_id": job["platform_id"], "url": job["canonical_url"],
            "route": job["route"], "attempts": job["attempts"], "platform_requests": job["platform_requests"],
            "warnings": json.loads(job["warnings"]), "created_at": _iso(job["created_at"]),
            "updated_at": _iso(job["updated_at"]), "finished_at": _iso(job["finished_at"]),
        }
        if job["error_class"]:
            out["error"] = {"class": job["error_class"], "message": job["error"],
                            "next_step": ACTIONS.get(job["error_class"])}
        if job["status"] in ("retry_wait", "queued") and job["next_at"] > _now():
            out["retry_after_s"] = int(job["next_at"] - _now())
        elif job["status"] not in TERMINAL:
            out["retry_after_s"] = 3 if job["kind"] == "video" else 10
        if job["kind"] == "profile":
            row = self.db.one("select seen from profile_cursor where job_id = ?", (job["id"],))
            items = json.loads(row["seen"]) if row else []
            ids = [i["job_id"] for i in items]
            counts: dict[str, int] = {}
            if ids:
                marks = ",".join("?" * len(ids))
                for r in self.db.all(f"select status, count(*) n from job where id in ({marks}) group by status", ids):
                    counts[r["status"]] = r["n"]
            out["videos"] = {"total": len(items), **counts}
        return out

    def _track_out(self, t: dict) -> dict:
        return {"source": t["source"], "language": t["language"], "origin": t["origin"], "status": t["status"],
                "model": t["model"], "model_revision": t["model_revision"], "text": t["text"],
                "quality": json.loads(t["quality"]), "segments": json.loads(t["segments"])}

    def _understanding_out(self, media: dict, opts: dict, kind: str) -> dict:
        source, model = (("vision", opts.get("describe_images")) if kind == "images"
                         else ("video_description", opts.get("describe_video")))
        found = self._description(media["id"], source, model) if model else None
        if not found:
            return {}
        return {"understanding": {"source": source, "model": model, "status": found["status"], "text": found["text"],
                                  "segments": json.loads(found["segments"])}}

    def _part_out(self, media: dict, opts: dict, kind: str = "video") -> dict:
        arts = self.db.all("select id, kind, mime, size, sha256, language from artifact where media_id = ?"
                           " order by created_at", (media["id"],))
        tracks = self.db.all("select * from track where media_id = ? order by created_at", (media["id"],))
        chosen = self._ocr_track(media["id"]) if kind == "images" else self._chosen(media["id"], opts)
        meta = json.loads(media["meta"])
        return {
            "media": {"platform": media["platform"], "platform_id": media["platform_id"], "part": media["part"],
                      "canonical_url": media["canonical_url"], "media_kind": "video" if meta.get("has_video", True)
                      else "audio", **{k: v for k, v in meta.items() if k not in ("has_video",)}},
            "artifacts": [{"artifact_id": a["id"], "kind": a["kind"], "mime_type": a["mime"], "size": a["size"],
                           "sha256": a["sha256"], **({"language": a["language"]} if a["language"] else {})}
                          for a in arts],
            "transcript": self._track_out(chosen) if chosen else {"status": "missing"},
            **self._understanding_out(media, opts, kind),
            # Every track there is, without its text: the chosen one is above.
            "tracks": [{"source": t["source"], "language": t["language"], "origin": t["origin"],
                        "status": t["status"], "model": t["model"], "characters": len(t["text"])} for t in tracks],
        }

    def result(self, job: dict) -> dict:
        opts = json.loads(job["options"])
        base = {"schema_version": "1.0", "job_id": job["id"], "kind": job["kind"], "status": job["status"],
                "platform_requests": job["platform_requests"], "warnings": json.loads(job["warnings"])}
        if job["error_class"]:
            base["error"] = {"class": job["error_class"], "message": job["error"]}
        if job["kind"] == "profile":
            row = self.db.one("select seen from profile_cursor where job_id = ?", (job["id"],))
            items = json.loads(row["seen"]) if row else []
            ids = [i["job_id"] for i in items]
            jobs = {}
            if ids:
                marks = ",".join("?" * len(ids))
                jobs = {r["id"]: r for r in self.db.all(f"select id, status from job where id in ({marks})", ids)}
            base["account"] = job["canonical_url"]
            base["videos"] = [{**i, "status": jobs.get(i["job_id"], {}).get("status")} for i in items]
            return base
        parts = self._parts(job["platform"], job["platform_id"]) if job["platform_id"] else []
        outs = [self._part_out(p, opts, job["kind"]) for p in parts]
        asr = next((t for p in outs for t in [p["transcript"]] if t.get("source") == "asr"), None)
        base["provenance"] = {
            "adapter": "instaloader+tesseract" if job["kind"] == "images" else "yt_dlp",
            "adapter_version": (ocr_engine.version() or "tesseract") if job["kind"] == "images"
            else fetch.adapter_version(),
            "worker_version": VERSION,
            "route": job["route"], "retrieved_at": _iso(job["finished_at"] or job["updated_at"]),
            **({"asr_model": asr["model"], "asr_model_revision": asr["model_revision"]} if asr else {}),
        }
        if outs:
            base.update(outs[0])
            if len(outs) > 1:
                base["parts"] = outs
        return base

    def artifact(self, tenant: str, art_id: str) -> tuple[Path, dict] | None:
        """The file, if this caller has a job for the video it belongs to."""
        art = self.db.one(
            "select a.*, m.platform, m.platform_id from artifact a join media m on m.id = a.media_id where a.id = ?",
            (art_id,))
        if not art:
            return None
        owns = self.db.one("select 1 from job where tenant = ? and platform = ? and platform_id = ?",
                           (tenant, art["platform"], art["platform_id"]))
        return (self.s.media_dir / art["path"], art) if owns else None

    # ------------------------------------------------------------------
    # Threads
    # ------------------------------------------------------------------

    def _loop(self, stages: tuple[str, ...]):
        while not self._stop.is_set():
            try:
                if not self.run_once(stages):
                    self._stop.wait(1.0)
            except Exception:  # noqa: BLE001 — a loop that dies takes its stage with it
                LOG.exception("worker loop for %s", stages)
                self._stop.wait(5.0)

    def _beat(self):
        while not self._stop.wait(20):
            try:
                self.heartbeat()
            except Exception:  # noqa: BLE001
                LOG.exception("heartbeat")

    def start(self):
        threads = [threading.Thread(target=self._beat, name="heartbeat", daemon=True)]
        for i in range(self.s.fetch_workers):
            threads.append(threading.Thread(target=self._loop, args=(("fetch", "profile"),), name=f"fetch-{i}", daemon=True))
        for i in range(self.s.asr_workers):
            threads.append(threading.Thread(target=self._loop, args=(("asr",),), name=f"asr-{i}", daemon=True))
        for t in threads:
            t.start()
        return threads

    def stop(self):
        self._stop.set()
