"""Metadata-only client for the Cloudflare/Groq streaming transcription relay."""
from __future__ import annotations

import datetime as dt
import json
import os
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .errors import JobError

MODEL = "groq:whisper-large-v3-turbo"
FORMATS = {"mp4", "m4a", "mp3", "wav", "webm", "ogg", "flac"}
# Enough of the file's head for its container to say how long it is: the
# relay needs a duration, and Instagram often leaves it out of the metadata.
# A file whose index sits at its end gets its last TAIL_BYTES read instead.
HEAD_BYTES = 64 * 1024
TAIL_BYTES = 256 * 1024


def _ranged(opener, url: str, headers: dict, byte_range: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={**headers, "Range": f"bytes={byte_range}"})
    try:
        with opener.open(request, timeout=20) as response:
            return response.read(limit)
    except (OSError, ValueError):
        return b""


def _probe_bytes(data: bytes) -> float:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "head"
        path.write_bytes(data)
        try:
            out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                                 capture_output=True, text=True, timeout=20)
            return float((json.loads(out.stdout or "{}").get("format") or {}).get("duration") or 0)
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0.0


def mvhd_duration(data: bytes) -> float:
    """The duration an MP4 movie header (mvhd) states, wherever it is in data."""
    at = data.find(b"mvhd")
    if at < 0 or len(data) < at + 32:
        return 0.0
    body = data[at + 4:]
    if body[0] == 1:
        if len(body) < 32:
            return 0.0
        timescale, duration = struct.unpack(">IQ", body[20:32])
    else:
        timescale, duration = struct.unpack(">II", body[12:20])
    return duration / timescale if timescale else 0.0


def head_duration(url: str, headers: dict, proxy: str | None = None, opener=None) -> float:
    """The media's duration from the first 64 KB of the file, or from its
    last 256 KB when the index is at the end; never the whole file. 0 when
    neither says."""
    if opener is None:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    head = _ranged(opener, url, headers, f"0-{HEAD_BYTES - 1}", HEAD_BYTES)
    seconds = _probe_bytes(head) if head else 0.0
    if seconds <= 0:
        seconds = mvhd_duration(head) or mvhd_duration(_ranged(opener, url, headers, f"-{TAIL_BYTES}", TAIL_BYTES))
    return seconds


def _until_next_utc_day() -> float:
    now = dt.datetime.now(dt.timezone.utc)
    tomorrow = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds() + 60


def select_source(info: dict, probe=None) -> dict:
    """Select a single progressive audio/video resource, never a manifest."""
    candidates = []
    for fmt in info.get("formats") or [info]:
        if (urlparse(fmt.get("url") or "").scheme != "https" or fmt.get("ext") not in FORMATS
                or fmt.get("protocol", "https") not in ("https", "http")
                or fmt.get("fragments") or fmt.get("has_drm") or fmt.get("acodec") == "none"):
            continue
        size = fmt.get("filesize") or fmt.get("filesize_approx") or 0
        if size > 25 * 1024 * 1024:
            continue
        candidates.append(fmt)
    if not candidates:
        raise JobError("invalid_input", "No progressive audio/video resource within the relay size limit is available")
    fmt = min(candidates, key=lambda f: (f.get("vcodec") != "none",
              f.get("filesize") or f.get("filesize_approx") or float(f.get("tbr") or 1e9) * 1000))
    headers = {**info.get("http_headers", {}), **fmt.get("http_headers", {})}
    allowed = {"user-agent", "referer", "cookie", "accept", "accept-language"}
    headers = {k: v for k, v in headers.items() if k.lower() in allowed}
    duration = info.get("duration") or fmt.get("duration") or 0
    if not duration and probe is not None:
        duration = probe(fmt["url"], headers)
    if not 0 < duration <= 900:
        raise JobError("too_large", "Streaming transcription requires a known duration of at most 900 seconds")
    return {"url": fmt["url"], "duration": duration, "ext": fmt["ext"], "headers": headers}


class RemoteAsr:
    def __init__(self, url: str, token: str, key: str):
        self.url, self.token, self.key = url.rstrip("/"), token, key

    @classmethod
    def from_env(cls):
        values = [os.environ.get(k) for k in ("SMT_TRANSCRIPT_RELAY_URL", "SMT_TRANSCRIPT_RELAY_TOKEN", "SMT_GROQ_API_KEY")]
        if not all(values):
            return None
        url = urlparse(values[0])
        if url.scheme != "https" or url.username or url.password or url.query or url.fragment:
            raise ValueError("SMT_TRANSCRIPT_RELAY_URL must be an HTTPS endpoint without credentials")
        return cls(*values)

    def transcribe(self, source: dict, identity: str, language: str | None, member: str | None = None) -> dict:
        payload = json.dumps({"id": identity, "source": source, "language": language,
                              **({"member": member} if member else {})}).encode()
        request = urllib.request.Request(self.url + "/v1/transcribe", data=payload,
            headers={"Authorization": "Bearer " + self.token, "X-Groq-Key": self.key,
                     "Content-Type": "application/json", "User-Agent": "SMTranscript/3.5.0"})
        try:
            # paid-call: Cloudflare Budget atomically reserves euros across all callers;
            # stable identity prevents a second provider call after an uncertain response.
            with urllib.request.urlopen(request, timeout=150) as response:
                raw = response.read(512 * 1024 + 1)
                if len(raw) > 512 * 1024:
                    raise ValueError("oversized transcript")
                data = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Do not include request URLs, session headers or provider keys in errors.
            try:
                detail = json.loads(exc.read(4096))
            except (ValueError, OSError):
                detail = {}
            alert = "; daily transcription budget is at least half used" if detail.get("budget_alert") else ""
            if exc.code == 429:
                # The day's euros are spent and nothing was reserved: the job
                # waits for the next UTC day instead of failing.
                whose = "this user's" if detail.get("member_budget") else "the"
                raise JobError("rate_limited", f"{whose} daily transcription budget is used up; "
                               "transcription resumes after 00:00 UTC", retry_after=_until_next_utc_day()) from None
            raise JobError("invalid_input", f"Streaming relay returned HTTP {exc.code}; no local media fallback{alert}") from None
        except (OSError, ValueError) as exc:
            raise JobError("invalid_input", "Streaming relay did not return a verified result; no automatic paid retry") from None
        flags = ["transcription_budget_half_used"] if data.get("budget_alert") else []
        return {"text": data["text"], "language": data.get("language"), "segments": data["segments"],
                "status": "available" if data["text"] else "no_speech", "model": MODEL, "model_revision": None,
                "quality": {"flags": flags}, "params": {"transport": "cloudflare-stream",
                "media_bytes": data.get("media_bytes"), "reserved_eur": data.get("reserved_eur"),
                "duration_seconds": data.get("duration_seconds"), "cached": data.get("cached", False)}}
