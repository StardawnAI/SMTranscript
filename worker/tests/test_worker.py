"""Worker tests without the network: the platforms and Whisper are stand-ins,
the video is a real two-second clip made by ffmpeg, the database is real.

    cd worker && python -m unittest discover -s tests -v
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smt import captions, config, fetch, guard  # noqa: E402
from smt.api import serve  # noqa: E402
from smt.asr import judge, quality  # noqa: E402
from smt.engine import Engine, MAX_ATTEMPTS  # noqa: E402
from smt.errors import JobError, classify  # noqa: E402

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
TT = "https://www.tiktok.com/@someone/video/7702581353048624387"
IG = "https://www.instagram.com/reel/DNoIGNAu8fy/"


def make_clip(path: Path, audio: bool = True):
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=2"]
    if audio:
        args += ["-f", "lavfi", "-i", "sine=f=440:d=2", "-shortest"]
    subprocess.run(args + ["-pix_fmt", "yuv420p", str(path)], check=True)


class FakeFetch:
    """Stands in for fetch.py. Every call counts as one platform request."""

    def __init__(self, clip: Path):
        self.clip = clip
        self.calls: list[str] = []
        self.proxies: list[str | None] = []
        self.resolve_errors: list[Exception] = []
        self.download_errors: list[Exception] = []
        self.captions: list[tuple[str, str, str]] = []
        self.entries = 1
        self.duration = 2
        self.profile_items: list[dict] = []

    caption_segments = staticmethod(fetch.caption_segments)

    def session(self, proxy, cookiefile, max_mb, keep_media, count):
        fake = self

        class Session:
            def __enter__(self):
                fake.sessions = getattr(fake, "sessions", 0) + 1
                return self

            def __exit__(self, *exc):
                return False

            def resolve(self, url):
                return fake.resolve(url, proxy, count, cookiefile)

            def download(self, info, tmp):
                return fake.download(info, proxy, tmp, count, cookiefile, max_mb, keep_media)

        return Session()

    def cookie_copy(self, *_a):
        return None

    def _call(self, name, proxy, count):
        self.calls.append(name)
        self.proxies.append(proxy)
        count()

    def resolve(self, url, proxy, count, cookiefile=None):
        self._call("resolve", proxy, count)
        if self.resolve_errors:
            raise self.resolve_errors.pop(0)
        return [{"id": f"v{i}", "title": "A clip", "uploader": "someone", "duration": self.duration,
                 "timestamp": 1761800000, "formats": [{"url": "https://cdn.example/x.mp4"}]}
                for i in range(self.entries)]

    def download(self, info, proxy, tmp, count, cookiefile, max_mb, keep_media=True):
        self._call("download", proxy, count)
        self.kept = keep_media
        if self.download_errors:
            raise self.download_errors.pop(0)
        video = tmp / "media.mp4"
        shutil.copyfile(self.clip, video)
        tracks = []
        for lang, ext, data in self.captions:
            f = tmp / f"media.{lang}.{ext}"
            f.write_text(data, encoding="utf-8")
            tracks.append({"language_raw": lang, "ext": ext, "path": f})
        return {"video": video, "captions": tracks}

    def tiktok_profile(self, account, limit, proxy, count, cookiefile=None):
        self._call("profile", proxy, count)
        return self.profile_items[:limit]

    def instagram_profile(self, account, limit, since, proxy, count, cursor=None, session=None, user=None,
                          on_item=lambda item, cursor: None, include_images=False):
        self._call("profile", proxy, count)
        self.include_images = include_images
        self.cursors_given = getattr(self, "cursors_given", []) + [cursor]
        # Resuming continues after the cursor, as the real timeline does.
        items = self.profile_items[cursor["node_count"]:] if cursor else self.profile_items
        out = []
        for i, item in enumerate(items[:limit]):
            out.append(item)
            on_item(item, {"node_count": (cursor or {}).get("node_count", 0) + i + 1, "remaining_data": None})
            if self.resolve_errors:
                # Part of the way through, as when Instagram cuts a listing off.
                raise self.resolve_errors.pop(0)
        return out


    def instagram_images(self, shortcode, proxy, count, session=None, user=None):
        self._call("post", proxy, count)
        return {"images": ["https://scontent.cdninstagram.com/1.jpg", "https://scontent.cdninstagram.com/2.jpg"],
                "videos": 1, "caption": "3 Tipps", "author": "immo.dommi",
                "taken_at": dt.datetime(2026, 10, 20, tzinfo=dt.timezone.utc), "kind": "carousel"}

    def download_image(self, url, dest, proxy, count, max_mb=20):
        self._call("image", proxy, count)
        dest.write_bytes(b"jpeg")
        self.downloaded = getattr(self, "downloaded", []) + [dest]
        return dest


def fake_ocr(path):
    return {"1.jpg": "Tipp 1: Nießbrauch prüfen", "2.jpg": ""}[path.name]


class FakeAsr:
    def __init__(self):
        self.calls = 0
        self.fail = 0
        self.text = "Hallo und willkommen."

    def transcribe(self, wav):
        self.calls += 1
        assert Path(wav).stat().st_size > 1000, "the audio was not extracted"
        if self.fail:
            self.fail -= 1
            raise RuntimeError("CUDA out of memory")
        segs = [{"start_ms": 0, "end_ms": 1800, "text": self.text}] if self.text else []
        return {"status": "available" if segs else "no_speech", "language": "de", "segments": segs,
                "text": self.text, "quality": {"flags": [], "language_probability": 0.99},
                "model": "large-v3-turbo", "model_revision": "abc123", "params": {"device": "cpu"}}


def settings(data: Path, **over) -> config.Settings:
    base = dict(
        data=data, keys=config.parse_keys(f"alice:{'a' * 21},bob:{'b' * 20},root:{'r' * 20}:admin"),
        port=0, bind="127.0.0.1", routes={"direct": None, "warp": "http://warp:1080"},
        default_route={"tiktok": "direct", "instagram": "direct"}, gap_seconds={"tiktok": 0, "instagram": 0},
        fetch_workers=1, asr_workers=1, max_video_mb=50, max_duration_s=600, max_profile_items=50,
        min_free_mb=0, asr_model="large-v3-turbo", asr_device="cpu", cookies_dir=None,
        instaloader_session=None, instagram_user=None, asr_models=("small",),
    )
    base.update(over)
    return config.Settings(**base)


class GuardTest(unittest.TestCase):
    def test_links(self):
        cases = {
            "https://www.tiktok.com/@a.b/video/123?is_from_webapp=1": ("tiktok", "video", "123"),
            "tiktok.com/@x/photo/77": ("tiktok", "video", "77"),
            "https://vm.tiktok.com/ZMabc/": ("tiktok", "short", None),
            "https://www.tiktok.com/t/ZTabc/": ("tiktok", "short", None),
            "https://www.tiktok.com/@tagesschau": ("tiktok", "profile", "tagesschau"),
            "https://www.instagram.com/reel/DNoIGNAu8fy/?igsh=x": ("instagram", "video", "DNoIGNAu8fy"),
            "https://instagram.com/someone/reel/ABC-1/": ("instagram", "video", "ABC-1"),
            "https://www.instagram.com/p/XYZ/": ("instagram", "video", "XYZ"),
            "https://www.instagram.com/share/reel/BAd/": ("instagram", "short", None),
            "https://www.instagram.com/tagesschau/": ("instagram", "profile", "tagesschau"),
            "https://www.instagram.com/tagesschau/reels/": ("instagram", "profile", "tagesschau"),
        }
        for url, want in cases.items():
            t = guard.parse(url)
            self.assertEqual((t.platform, t.kind, t.platform_id), want, url)
        self.assertEqual(guard.parse(IG).canonical_url, "https://www.instagram.com/p/DNoIGNAu8fy/")

    def test_refused(self):
        for url in ("https://youtube.com/watch?v=x", "http://169.254.169.254/latest", "https://tiktok.com.evil.io/@a",
                    "https://www.instagram.com/explore/", "file:///etc/passwd", "https://www.tiktok.com/foryou"):
            with self.assertRaises(guard.InputError, msg=url):
                guard.parse(url)

    def test_private_addresses(self):
        with self.assertRaises(guard.InputError):
            guard.public_host("localhost")


class ErrorsTest(unittest.TestCase):
    def test_classes(self):
        cases = {
            "ERROR: [Instagram] X: The webpage request was redirected to the login page. You have exceeded "
            "the rate-limit for accessing posts anonymously": "rate_limited",
            "ERROR: [TikTok] 1: Your IP address is blocked from accessing this post": "unavailable",
            "TikTok is requiring login for access to this content": "challenge",
            "You do not have permission to view this post. Log into an account that has access": "private",
            "Restricted Video: You must be 18 years old. Use --cookies": "login_required",
            "Unable to connect to proxy: [Errno 111] Connection refused": "route_down",
            "<urlopen error timed out>": "network",
            "HTTP Error 403: Forbidden": "expired_link",
            "ERROR: Unsupported URL: https://x": "parser",
            "something odd": "internal",
        }
        for message, want in cases.items():
            self.assertEqual(classify(RuntimeError(message)).cls, want, message)

    def test_a_chosen_video_id_cannot_fake_a_class(self):
        for message in ("ERROR: [Instagram] rate-limit: Requested content is not available",
                        "ERROR: [TikTok] 1: Unable to extract https://www.tiktok.com/@rate-limit/video/1"):
            self.assertNotEqual(classify(RuntimeError(message)).cls, "rate_limited", message)
        self.assertEqual(classify(RuntimeError("ERROR: [Instagram] abc: Requested content is not available, "
                                               "rate-limit reached or login required")).cls, "rate_limited")

    def test_retry_after_from_the_chain(self):
        class Resp:
            headers = {"Retry-After": "120"}

        class HTTPError(Exception):
            response = Resp()

        try:
            try:
                raise HTTPError("HTTP Error 429: Too Many Requests")
            except HTTPError as inner:
                raise RuntimeError("ERROR: HTTP Error 429: Too Many Requests") from inner
        except RuntimeError as exc:
            err = classify(exc)
        self.assertEqual((err.cls, err.retry_after), ("rate_limited", 120.0))


class CaptionsTest(unittest.TestCase):
    def test_vtt_srt_json(self):
        vtt = "WEBVTT\n\n00:00:00.500 --> 00:00:02.000\n<c>Hallo</c> Welt\n\n00:00:02.000 --> 00:00:03.250\nHallo Welt\n\n" \
              "00:00:03.300 --> 00:00:04.000\nZweiter Satz\n"
        self.assertEqual(captions.parse(vtt, "vtt"), [
            {"start_ms": 500, "end_ms": 3250, "text": "Hallo Welt"},
            {"start_ms": 3300, "end_ms": 4000, "text": "Zweiter Satz"}])
        srt = "1\n00:00:01,000 --> 00:00:02,500\nOne\n\n2\n01:00:00,000 --> 01:00:01,000\nTwo\n"
        self.assertEqual(captions.parse(srt, "srt")[1], {"start_ms": 3600000, "end_ms": 3601000, "text": "Two"})
        js = json.dumps({"utterances": [{"start_time": 10, "end_time": 900, "text": "Hi"}]})
        self.assertEqual(captions.parse(js, "json"), [{"start_ms": 10, "end_ms": 900, "text": "Hi"}])
        self.assertEqual([captions.language(c) for c in ("deu-DE", "eng-US", "de", "pt_BR", None)],
                         ["de", "en", "de", "pt", None])


class QualityTest(unittest.TestCase):
    def test_flags(self):
        same = [{"text": "Thanks for watching!", "compression_ratio": 1.0}] * 4
        self.assertIn("repeated_segments", quality(same, 0.9, 10))
        self.assertEqual(quality([{"text": "a", "compression_ratio": 1.2}], 0.95, 5), [])
        self.assertIn("suspect_repetition", quality([{"text": "a", "compression_ratio": 3.1}], 0.95, 5))
        self.assertIn("text_with_little_speech", quality([{"text": "a"}], 0.95, 0.2))
        self.assertIn("uncertain_language", quality([{"text": "a"}], 0.3, 5))

    def test_stock_words_over_music_are_not_speech(self):
        kept, flags, discarded = judge([{"start_ms": 0, "end_ms": 900, "text": "You"}], 0.23, 0.86)
        self.assertEqual((kept, discarded), ([], "You"))
        self.assertIn("discarded_as_no_speech", flags)
        speech = [{"start_ms": 0, "end_ms": 900, "text": "Ja, genau so ist es."}]
        self.assertEqual(judge(speech, 0.9, 0.9)[0], speech)  # five words: kept, flagged
        self.assertEqual(judge(speech[:1], 0.99, 12.0)[2], None)


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class EngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        cls.clip = cls.root / "clip.mp4"
        cls.silent = cls.root / "silent.mp4"
        make_clip(cls.clip)
        make_clip(cls.silent, audio=False)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def setUp(self):
        self.data = Path(tempfile.mkdtemp(dir=self.root))
        self.fake = FakeFetch(self.clip)
        self.asr = FakeAsr()
        self.engine = self.make()

    def make(self, **over):
        engine = Engine(settings(self.data, **over), asr_factory=lambda _m: self.asr, ocr=fake_ocr)
        engine.fetch = self.fake
        return engine

    def drain(self, engine=None):
        engine = engine or self.engine
        for _ in range(50):
            if not (engine.run_once(("fetch", "profile")) or engine.run_once(("asr",))):
                return

    def job(self, job_id):
        return self.engine.db.one("select * from job where id = ?", (job_id,))

    # -- accepting --------------------------------------------------------

    def test_idempotent_submit(self):
        a, existing_a = self.engine.submit_video("alice", TT)
        b, existing_b = self.engine.submit_video("alice", TT + "?lang=en")
        c, _ = self.engine.submit_video("alice", TT, {"language": "en"})
        d, _ = self.engine.submit_video("bob", TT)
        self.assertEqual((existing_a, existing_b, a["id"]), (False, True, b["id"]))
        self.assertNotEqual(a["id"], c["id"])
        self.assertNotEqual(a["id"], d["id"])
        with self.assertRaises(JobError):
            self.engine.submit_video("alice", TT, {"route": "tor"})
        with self.assertRaises(JobError):  # not on the list: would load a third model
            self.engine.submit_video("alice", TT, {"asr_model": "large-v3"})
        with self.assertRaises(JobError):
            self.engine.submit_video("alice", "https://www.tiktok.com/@someone")

    # -- the whole way ----------------------------------------------------

    def test_video_then_asr(self):
        job, _ = self.engine.submit_video("alice", TT)
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["transcript"]["source"], "asr")
        self.assertEqual(result["transcript"]["text"], "Hallo und willkommen.")
        self.assertEqual(result["platform_requests"], 2)  # resolve + download
        video = next(a for a in result["artifacts"] if a["kind"] == "video")
        path, _art = self.engine.artifact("alice", video["artifact_id"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), video["sha256"])
        self.assertNotIn(str(self.data), json.dumps(result))  # no paths in answers
        self.assertEqual(result["provenance"]["asr_model_revision"], "abc123")

    def test_cached_video_costs_nothing(self):
        first, _ = self.engine.submit_video("alice", TT)
        self.drain()
        second, _ = self.engine.submit_video("bob", TT)
        self.drain()
        result = self.engine.result(self.job(second["id"]))
        self.assertEqual((result["status"], result["platform_requests"]), ("completed", 0))
        self.assertEqual(self.asr.calls, 1)
        self.assertEqual(self.fake.calls, ["resolve", "download"])

    def test_another_model_reuses_the_file(self):
        self.engine.submit_video("alice", TT)
        self.drain()
        job, _ = self.engine.submit_video("alice", TT, {"asr_model": "small"})
        self.drain()
        self.assertEqual(self.job(job["id"])["platform_requests"], 0)
        self.assertEqual(self.asr.calls, 2)

    def test_single_caption_track_skips_asr(self):
        self.fake.captions = [("deu-DE", "vtt", "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nGuten Abend\n")]
        job, _ = self.engine.submit_video("alice", TT)
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual((result["transcript"]["source"], result["transcript"]["language"]), ("native_captions", "de"))
        self.assertEqual(result["transcript"]["origin"], "unknown")
        self.assertEqual(self.asr.calls, 0)

    def test_several_caption_languages_transcribe_the_speech(self):
        vtt = "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nText\n"
        self.fake.captions = [("deu-DE", "vtt", vtt), ("eng-US", "vtt", vtt)]
        job, _ = self.engine.submit_video("alice", TT)
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual(result["transcript"]["source"], "asr")
        self.assertEqual(len(result["tracks"]), 3)
        en, _ = self.engine.submit_video("alice", TT, {"language": "en"})
        self.drain()
        self.assertEqual(self.engine.result(self.job(en["id"]))["transcript"]["source"], "native_captions")

    def test_no_audio_is_a_result(self):
        self.fake.clip = self.silent
        job, _ = self.engine.submit_video("alice", TT)
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual((result["status"], result["transcript"]["status"]), ("completed", "no_audio"))
        self.assertEqual(self.asr.calls, 0)

    def test_asr_failing_twice_keeps_the_video(self):
        self.asr.fail = 2
        job, _ = self.engine.submit_video("alice", TT)
        self.engine.run_once(("fetch",))
        self.engine.run_once(("asr",))
        self.engine.db.run("update job set next_at = 0 where id = ?", (job["id"],))
        self.engine.run_once(("asr",))
        row = self.job(job["id"])
        self.assertEqual(row["status"], "partial")
        self.assertTrue(any(a["kind"] == "video" for a in self.engine.result(row)["artifacts"]))
        self.assertEqual(self.fake.calls, ["resolve", "download"])  # nothing fetched again

    def test_without_keep_media_nothing_stays_but_the_text(self):
        engine = self.make(keep_media=False)
        job, _ = engine.submit_video("alice", TT)
        self.drain(engine)
        self.assertFalse(self.fake.kept)  # asked for audio only
        result = engine.result(self.job(job["id"]))
        self.assertEqual((result["status"], result["transcript"]["text"]), ("completed", "Hallo und willkommen."))
        self.assertEqual(result["artifacts"], [])
        self.assertEqual([f for f in self.data.joinpath("media").rglob("*") if f.is_file()], [])
        again, _ = engine.submit_video("bob", TT, {"language": "fr"})
        self.drain(engine)
        self.assertEqual(self.job(again["id"])["platform_requests"], 0)  # the transcript answers
        self.assertEqual(self.fake.calls, ["resolve", "download"])

    def test_asking_again_after_a_failure_retries_with_what_is_stored(self):
        self.asr.fail = 2
        job, _ = self.engine.submit_video("alice", TT)
        self.engine.run_once(("fetch",))
        self.engine.run_once(("asr",))
        self.engine.db.run("update job set next_at = 0 where id = ?", (job["id"],))
        self.engine.run_once(("asr",))
        self.assertEqual(self.job(job["id"])["status"], "partial")
        again, existing = self.engine.submit_video("alice", TT)
        self.assertEqual((again["id"], existing, again["status"]), (job["id"], True, "queued"))
        self.drain()
        row = self.job(job["id"])
        self.assertEqual((row["status"], row["platform_requests"]), ("completed", 2))  # nothing fetched again
        self.assertEqual(self.fake.calls, ["resolve", "download"])

    def test_carousel_parts(self):
        self.fake.entries = 2
        job, _ = self.engine.submit_video("alice", IG)
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual(len(result["parts"]), 2)
        self.assertEqual(self.asr.calls, 2)

    # -- when it goes wrong -----------------------------------------------

    def test_429_cools_the_route_down_across_a_restart(self):
        self.fake.resolve_errors = [JobError("rate_limited", "HTTP Error 429", retry_after=300)]
        job, _ = self.engine.submit_video("alice", TT)
        self.drain()
        row = self.job(job["id"])
        self.assertEqual(row["status"], "retry_wait")
        self.assertAlmostEqual(row["next_at"] - time.time(), 300, delta=5)
        restarted = self.make()
        with self.assertRaises(JobError) as cm:
            restarted.routes.check("tiktok", "direct")
        self.assertEqual(cm.exception.cls, "rate_limited")
        restarted.routes.check("tiktok", "warp")  # the other route is not affected
        restarted.routes.check("instagram", "direct")

    def test_challenge_blocks_until_unblocked(self):
        self.fake.resolve_errors = [JobError("challenge", "TikTok is requiring login")]
        a, _ = self.engine.submit_video("alice", TT)
        self.drain()
        b, _ = self.engine.submit_video("alice", "https://www.tiktok.com/@x/video/99")
        self.drain()
        self.assertEqual([self.job(a["id"])["status"], self.job(b["id"])["status"]], ["blocked", "blocked"])
        self.assertEqual(self.fake.calls, ["resolve"])  # the second job never reached the platform
        self.assertEqual(self.engine.unblock("tiktok", "direct"), 2)
        self.drain()
        self.assertEqual(self.job(b["id"])["status"], "completed")

    def test_network_errors_retry_then_fail(self):
        self.fake.resolve_errors = [JobError("network", "timed out")] * MAX_ATTEMPTS
        job, _ = self.engine.submit_video("alice", TT)
        for _ in range(MAX_ATTEMPTS):
            self.engine.routes.unblock("tiktok", "direct")  # keep the breaker out of this test
            self.engine.db.run("update job set next_at = 0 where id = ?", (job["id"],))
            self.engine.run_once(("fetch",))
        row = self.job(job["id"])
        self.assertEqual((row["status"], row["error_class"]), ("failed", "network"))

    def test_three_network_failures_cool_the_route_down(self):
        self.fake.resolve_errors = [JobError("network", "timed out")] * 3
        for vid in (1, 2, 3, 4):
            self.engine.submit_video("alice", f"https://www.tiktok.com/@x/video/{vid}")
        for _ in range(4):
            self.engine.run_once(("fetch",))
        self.assertEqual(self.fake.calls, ["resolve"] * 3)  # the fourth never went out
        state = {s["route"]: s for s in self.engine.routes.states() if s["platform"] == "tiktok"}
        self.assertGreater(state["direct"]["cooling_down_s"], 500)

    def test_proxy_down_never_falls_back_to_direct(self):
        self.fake.resolve_errors = [JobError("route_down", "Unable to connect to proxy")]
        job, _ = self.engine.submit_video("alice", TT, {"route": "warp"})
        self.drain()
        self.assertEqual(self.job(job["id"])["status"], "retry_wait")
        self.assertEqual(self.fake.proxies, ["http://warp:1080"])

    def test_a_refused_download_is_tried_again_in_a_fresh_session(self):
        self.fake.download_errors = [JobError("expired_link", "HTTP Error 403: Forbidden")]
        job, _ = self.engine.submit_video("alice", TT)
        self.engine.run_once(("fetch",))
        self.assertEqual(self.job(job["id"])["status"], "retry_wait")
        self.engine.db.run("update job set next_at = 0 where id = ?", (job["id"],))
        self.drain()
        self.assertEqual(self.job(job["id"])["status"], "completed")
        # Resolved again, in a new session, before the second download.
        self.assertEqual(self.fake.calls, ["resolve", "download", "resolve", "download"])
        self.assertEqual(self.fake.sessions, 2)

    def test_limits(self):
        self.fake.duration = 4000
        job, _ = self.engine.submit_video("alice", TT)
        self.drain()
        self.assertEqual(self.job(job["id"])["error_class"], "too_large")
        full = self.make(min_free_mb=10 ** 9)
        job, _ = full.submit_video("alice", "https://www.tiktok.com/@x/video/5")
        full.run_once(("fetch",))
        self.assertEqual(self.job(job["id"])["error_class"], "disk_full")

    def test_a_dead_workers_job_is_taken_over(self):
        job, _ = self.engine.submit_video("alice", TT)
        claimed = self.engine.claim(("fetch",))  # ... and the worker dies here
        self.assertEqual(claimed["id"], job["id"])
        other = self.make()
        self.assertIsNone(other.claim(("fetch",)))  # the lease still holds
        self.engine.db.run("update job set lease_until = ? where id = ?", (time.time() - 1, job["id"]))
        self.drain(other)
        self.assertEqual(self.job(job["id"])["status"], "completed")

    def test_cancel(self):
        job, _ = self.engine.submit_video("alice", TT)
        self.engine.cancel(self.job(job["id"]))
        self.drain()
        self.assertEqual(self.job(job["id"])["status"], "cancelled")
        self.assertEqual(self.fake.calls, [])

    def test_tenants_do_not_see_each_other(self):
        job, _ = self.engine.submit_video("alice", TT)
        self.drain()
        art = self.engine.result(self.job(job["id"]))["artifacts"][0]["artifact_id"]
        self.assertIsNone(self.engine.artifact("bob", art))
        self.assertIsNotNone(self.engine.artifact("alice", art))

    # -- profiles -----------------------------------------------------------

    def test_profile_import(self):
        def item(vid, day):
            return {"id": str(vid), "url": f"https://www.tiktok.com/@someone/video/{vid}",
                    "taken_at": dt.datetime(2026, 10, day, tzinfo=dt.timezone.utc)}
        # A pinned old video first, then newest first.
        self.fake.profile_items = [item(1, 1), item(5, 29), item(4, 28), item(3, 27), item(2, 20)]
        job, _ = self.engine.submit_profile("alice", "https://www.tiktok.com/@someone",
                                            {"max_items": 3, "since": "2026-10-25"})
        self.drain()
        row = self.job(job["id"])
        result = self.engine.result(row)
        self.assertEqual([v["id"] for v in result["videos"]], ["5", "4", "3"])
        self.assertEqual(row["status"], "completed")
        self.assertTrue(all(v["status"] == "completed" for v in result["videos"]))
        # A second import of the same account reuses the videos already done.
        again, _ = self.engine.submit_profile("alice", "https://www.tiktok.com/@someone", {"max_items": 4})
        self.drain()
        self.assertEqual(self.fake.calls.count("download"), 4)  # three before, one new
        self.assertEqual(self.engine.status(self.job(again["id"]))["videos"]["total"], 4)

    def test_an_interrupted_instagram_listing_resumes_at_its_cursor(self):
        self.fake.resolve_errors = [JobError("network", "Connection reset")]
        self.fake.profile_items = [{"id": code, "url": f"https://www.instagram.com/p/{code}/", "kind": "reel",
                                    "taken_at": dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc)}
                                   for code in ("AAA", "BBB", "CCC")]
        job, _ = self.engine.submit_profile("alice", "https://www.instagram.com/someone/", {"max_items": 5})
        self.engine.run_once(("profile",))  # cut off after the first post
        self.assertEqual(self.job(job["id"])["status"], "retry_wait")
        self.engine.db.run("update job set next_at = 0 where id = ?", (job["id"],))
        self.drain()
        self.assertEqual(self.fake.cursors_given, [None, {"node_count": 1, "remaining_data": None}])
        result = self.engine.result(self.job(job["id"]))
        # What was found before the cut is kept, and nothing comes twice.
        self.assertEqual([v["id"] for v in result["videos"]], ["AAA", "BBB", "CCC"])
        self.assertEqual(self.job(job["id"])["status"], "completed")

    def test_instagram_timeline_nodes_need_no_extra_query(self):
        from smt.fetch import _describe_node
        reel = _describe_node({"code": "DbIK-PeI9WZ", "media_type": 2, "product_type": "clips",
                               "pk": "3753590431575330393", "caption": {"text": "Steuern sparen"}})
        self.assertEqual((reel["kind"], reel["caption"], reel["url"]),
                         ("reel", "Steuern sparen", "https://www.instagram.com/p/DbIK-PeI9WZ/"))
        self.assertEqual(reel["taken_at"].year, 2025)  # the time sits inside the id
        self.assertEqual(_describe_node({"code": "C1", "media_type": 8, "caption": None, "pk": "1"})["kind"], "carousel")
        self.assertEqual(_describe_node({"code": "I1", "media_type": 1, "pk": "x"})["kind"], "image")

    def test_pictures_are_read_and_deleted(self):
        job, _ = self.engine.submit_images("alice", "https://www.instagram.com/p/CAROUSEL1/")
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["transcript"]["source"], result["transcript"]["status"]), ("ocr", "available"))
        self.assertEqual(result["transcript"]["text"], "Tipp 1: Nießbrauch prüfen")
        self.assertEqual([s["image"] for s in result["transcript"]["segments"]], [1, 2])
        self.assertEqual((result["media"]["post_kind"], result["media"]["videos_not_read"]), ("carousel", 1))
        self.assertTrue(all(not f.exists() for f in self.fake.downloaded))  # every picture deleted
        self.assertEqual(self.job(job["id"])["platform_requests"], 3)  # the post + two pictures
        again, _ = self.engine.submit_images("bob", "https://www.instagram.com/p/CAROUSEL1/")
        self.drain()
        self.assertEqual(self.job(again["id"])["platform_requests"], 0)
        with self.assertRaises(JobError):
            self.engine.submit_images("alice", TT)  # TikTok has no picture jobs here

    def test_an_account_with_pictures(self):
        self.fake.profile_items = [
            {"id": "REEL1", "url": "https://www.instagram.com/p/REEL1/", "kind": "reel", "caption": "a",
             "taken_at": dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc)},
            {"id": "PIC1", "url": "https://www.instagram.com/p/PIC1/", "kind": "image", "caption": "b",
             "taken_at": dt.datetime(2026, 10, 28, tzinfo=dt.timezone.utc)},
        ]
        job, _ = self.engine.submit_profile("alice", "https://www.instagram.com/someone/",
                                            {"max_items": 5, "include_images": True})
        self.drain()
        self.assertTrue(self.fake.include_images)
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual([(v["id"], v["kind"], v["status"]) for v in result["videos"]],
                         [("REEL1", "reel", "completed"), ("PIC1", "image", "completed")])
        kinds = {r["kind"] for r in self.engine.db.all("select kind from job where parent_id = ?", (job["id"],))}
        self.assertEqual(kinds, {"video", "images"})

    def test_profile_limits(self):
        with self.assertRaises(JobError):
            self.engine.submit_profile("alice", "https://www.tiktok.com/@someone", {"max_items": 500})
        with self.assertRaises(JobError):
            self.engine.submit_profile("alice", TT)


@unittest.skipUnless(FFMPEG, "needs ffmpeg and ffprobe")
class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        clip = cls.root / "clip.mp4"
        make_clip(clip)
        cls.fake = FakeFetch(clip)
        cls.engine = Engine(settings(cls.root / "data"), asr_factory=lambda _m: FakeAsr())
        cls.engine.fetch = cls.fake
        cls.server = serve(cls.engine)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        shutil.rmtree(cls.root, ignore_errors=True)

    def call(self, method, path, body=None, key="a" * 21):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw), resp.headers
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), exc.headers

    def test_flow(self):
        self.assertEqual(self.call("GET", "/healthz", key="")[0], 200)
        self.assertEqual(self.call("GET", "/v1/routes", key="wrong" * 4)[0], 401)
        status, body, _ = self.call("POST", "/v1/jobs", {"url": TT})
        self.assertEqual(status, 202)
        job = body["job_id"]
        status, body, headers = self.call("GET", f"/v1/jobs/{job}/result")
        self.assertEqual(status, 202)
        self.assertIn("Retry-After", headers)
        self.assertEqual(self.call("GET", f"/v1/jobs/{job}", key="b" * 20)[0], 404)  # bob
        for _ in range(5):
            self.engine.run_once(("fetch",)) or self.engine.run_once(("asr",))
        status, result, _ = self.call("GET", f"/v1/jobs/{job}/result")
        self.assertEqual((status, result["status"]), (200, "completed"))
        art = result["artifacts"][0]
        status, raw, headers = self.call("GET", f"/v1/artifacts/{art['artifact_id']}")
        self.assertEqual((status, hashlib.sha256(raw).hexdigest()), (200, art["sha256"]))
        self.assertEqual(self.call("GET", f"/v1/artifacts/{art['artifact_id']}", key="b" * 20)[0], 404)
        self.assertEqual(self.call("POST", "/v1/jobs", {"url": "http://127.0.0.1/x"})[0], 400)
        self.assertEqual(self.call("POST", "/v1/jobs", {"url": TT})[0], 200)  # the same job again
        self.assertEqual(self.call("POST", "/v1/routes/unblock", {"platform": "tiktok", "route": "direct"})[0], 403)
        self.assertEqual(self.call("POST", "/v1/routes/unblock", {"platform": "tiktok", "route": "direct"},
                                   key="r" * 20)[0], 200)

    def test_negative_length_is_refused(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.putrequest("POST", "/v1/jobs")
        conn.putheader("Authorization", "Bearer " + "a" * 21)
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 400)
        conn.close()

    def test_route_errors_only_for_admins(self):
        self.engine.routes.record("tiktok", "warp", JobError("challenge", "someone else's link"))
        _, body, _ = self.call("GET", "/v1/routes")
        self.assertTrue(all(r["last_error"] is None for r in body["routes"]))
        _, body, _ = self.call("GET", "/v1/routes", key="r" * 20)
        self.assertIn("someone else's link", json.dumps(body))
        self.engine.routes.unblock("tiktok", "warp")


if __name__ == "__main__":
    unittest.main()
