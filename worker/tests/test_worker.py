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
from smt.asr import OpenAiAsr, judge, quality  # noqa: E402
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
        self.listed_subtitles: dict = {}  # what the extractor lists, before anything is downloaded

    caption_segments = staticmethod(fetch.caption_segments)

    def session(self, proxy, cookiefile, max_mb, keep_media, count, want_video=False):
        fake = self
        fake.wanted_video = want_video

        class Session:
            def __enter__(self):
                fake.sessions = getattr(fake, "sessions", 0) + 1
                return self

            def __exit__(self, *exc):
                return False

            def resolve(self, url):
                return fake.resolve(url, proxy, count, cookiefile)

            def download(self, info, tmp, captions_only=False):
                return fake.download(info, proxy, tmp, count, cookiefile, max_mb, keep_media, captions_only)

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
                 "timestamp": 1761800000, "formats": [{"url": "https://cdn.example/x.mp4"}],
                 "subtitles": self.listed_subtitles}
                for i in range(self.entries)]

    def download(self, info, proxy, tmp, count, cookiefile, max_mb, keep_media=True, captions_only=False):
        self._call("captions" if captions_only else "download", proxy, count)
        self.kept = keep_media
        if self.download_errors:
            raise self.download_errors.pop(0)
        video = None if captions_only else tmp / "media.mp4"
        if video:
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


class FakeUnderstand:
    """Stands in for understand.py: no model is called."""

    provider = staticmethod(__import__("smt.understand", fromlist=["provider"]).provider)

    def __init__(self):
        self.images, self.videos, self.fail_video = [], [], []

    def describe_image(self, model, data, mime, keys):
        self.images.append((model, mime, len(data)))
        return f"Shown: slide {len(self.images)}\nText: -"

    def describe_video(self, model, data, mime, keys):
        self.videos.append((model, mime, len(data)))
        if self.fail_video:
            raise self.fail_video.pop(0)
        return "0:00 Ein Mann zeigt einen Grundriss."


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
        port=0, bind="127.0.0.1", routes={"direct": None, "warp": "http://warp:1080"}, share_direct=(), direct_off=False,
        default_route={"tiktok": "direct", "instagram": "direct", "vimeo": "direct"},
        gap_seconds={"tiktok": 0, "instagram": 0, "vimeo": 0},
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
            "https://vimeo.com/76979871": ("vimeo", "video", "76979871"),
            "https://vimeo.com/channels/staffpicks/76979871": ("vimeo", "video", "76979871"),
            "https://player.vimeo.com/video/76979871?h=8272103f6e": ("vimeo", "video", "76979871:8272103f6e"),
        }
        for url, want in cases.items():
            t = guard.parse(url)
            self.assertEqual((t.platform, t.kind, t.platform_id), want, url)
        self.assertEqual(guard.parse(IG).canonical_url, "https://www.instagram.com/p/DNoIGNAu8fy/")
        # An unlisted Vimeo video's hash is part of its address.
        self.assertEqual(guard.parse("https://player.vimeo.com/video/76979871?h=8272103f6e").canonical_url,
                         "https://vimeo.com/76979871/8272103f6e")
        self.assertEqual(guard.parse("vimeo.com/76979871/8272103f6e").canonical_url,
                         "https://vimeo.com/76979871/8272103f6e")

    def test_refused(self):
        for url in ("https://youtube.com/watch?v=x", "http://169.254.169.254/latest", "https://tiktok.com.evil.io/@a",
                    "https://www.instagram.com/explore/", "file:///etc/passwd", "https://www.tiktok.com/foryou",
                    "https://vimeo.com/someuser", "https://vimeo.com/showcase/123", "https://vimeo.com.evil.io/1"):
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
            "ERROR: [vimeo] 76979871: This video is protected by a password, use the --video-password option": "private",
            "ERROR: [vimeo] 1: Cannot download embed-only video without embedding URL": "private",
            "ERROR: This format is DRM protected; Try selecting another format with --format": "drm",
            "TikTok is requiring login for access to this content": "challenge",
            "You do not have permission to view this post. Log into an account that has access": "private",
            "Restricted Video: You must be 18 years old. Use --cookies": "login_required",
            "Unable to connect to proxy: [Errno 111] Connection refused": "route_down",
            "HTTP Error 429: Too Many Requests": "rate_limited",
            "<urlopen error timed out>": "network",
            "HTTP Error 403: Forbidden": "expired_link",
            "ERROR: Unsupported URL: https://x": "parser",
            "something odd": "internal",
        }
        for message, want in cases.items():
            self.assertEqual(classify(RuntimeError(message)).cls, want, message)

    def test_feedback_required_stops_the_route_even_with_a_wait_message(self):
        observed = ('400 Bad Request - "fail" status, message "feedback_required" '
                    'when accessing https://www.instagram.com/graphql/query')
        for message in (observed, observed + ". Please wait a few minutes"):
            with self.subTest(message=message):
                self.assertEqual(classify(RuntimeError(message)).cls, "challenge")

    def test_feedback_required_in_a_link_or_video_id_cannot_stop_the_route(self):
        cases = {
            "ERROR: [Instagram] feedback_required: Requested content is not available": "unavailable",
            "ERROR: Unsupported URL: https://www.instagram.com/feedback_required/": "parser",
        }
        for message, want in cases.items():
            with self.subTest(message=message):
                self.assertEqual(classify(RuntimeError(message)).cls, want)

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
        engine.understand = self.understand = FakeUnderstand()
        engine.model_keys = {"gemini": "g", "anthropic": "a"}
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

    def test_jobs_on_direct_take_turns_with_a_route_that_shares_it(self):
        engine = self.make(routes={"direct": None, "warp": "http://warp:1080", "hetzner": "http://h:1080"},
                           share_direct=("hetzner",))
        engine.fetch = self.fake
        picks = [engine.routes.pick("instagram", "direct") for _ in range(4)]
        self.assertEqual(sorted(set(picks)), ["direct", "hetzner"], picks)
        self.assertNotEqual(picks[0], picks[1], "two jobs in a row leave from two addresses")
        self.assertEqual(engine.routes.pick("instagram", "warp"), "warp", "an explicit route is kept")
        engine.routes.record("instagram", "hetzner", JobError("rate_limited", "429", retry_after=600))
        self.assertEqual({engine.routes.pick("instagram", "direct") for _ in range(3)}, {"direct"},
                         "a route that cools down is left out")
        engine.db.conn.close()

    def test_with_the_own_line_off_direct_jobs_only_ever_leave_through_the_shared_route(self):
        engine = self.make(routes={"direct": None, "warp": "http://warp:1080", "hetzner": "http://h:1080"},
                           share_direct=("hetzner",), direct_off=True)
        self.assertEqual({engine.routes.pick("instagram", "direct") for _ in range(5)}, {"hetzner"})
        engine.routes.record("instagram", "hetzner", JobError("rate_limited", "429", retry_after=600))
        self.assertEqual(engine.routes.pick("instagram", "direct"), "hetzner",
                         "a paused hetzner makes the job wait, it never falls back to the own line")
        engine.db.conn.close()

    def test_a_post_that_needs_a_login_is_not_fetched_again_the_same_day(self):
        job, _ = self.engine.submit_video("starbase", IG)
        self.engine.db.run("update job set status = 'failed', stage = null, error_class = 'login_required',"
                           " finished_at = ? where id = ?", (time.time(), job["id"]))
        again, existing = self.engine.submit_video("starbase", IG)
        self.assertEqual((again["id"], again["status"], existing), (job["id"], "failed", True))
        self.engine.db.run("update job set finished_at = ? where id = ?", (time.time() - 25 * 3600, job["id"]))
        later, _ = self.engine.submit_video("starbase", IG)
        self.assertEqual(later["status"], "queued", "a day later it is tried again")
        self.engine.db.run("update job set status = 'failed', error_class = 'parser', finished_at = ? where id = ?",
                           (time.time(), job["id"]))
        fixed, _ = self.engine.submit_video("starbase", IG)
        self.assertEqual(fixed["status"], "queued", "a failure we can fix on our side is tried again at once")

    def test_an_instagram_video_carries_its_own_mp4_link(self):
        from smt.engine import _video_link
        info = {"formats": [
            {"url": "https://cdn.example/v.m3u8", "vcodec": "avc1", "acodec": "mp4a", "protocol": "m3u8_native", "height": 1920},
            {"url": "https://cdn.example/video-only.mp4", "vcodec": "avc1", "acodec": "none", "height": 1920},
            {"url": "https://cdn.example/small.mp4", "vcodec": "avc1", "acodec": "mp4a", "protocol": "https", "height": 640},
            {"url": "https://cdn.example/big.mp4", "vcodec": "avc1", "acodec": "mp4a", "protocol": "https", "height": 1280},
        ]}
        self.assertEqual(_video_link(info), "https://cdn.example/big.mp4")
        self.assertIsNone(_video_link({"formats": [{"url": "http://plain/a.mp4", "vcodec": "a", "acodec": "b"}]}))
        # What Instagram really sends (2026-11-14): audio-only and video-only dash
        # parts with codecs, and the whole files without any codec named.
        real = {"formats": [
            {"url": "https://cdn.example/a.m4a", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.5", "protocol": "https"},
            {"url": "https://cdn.example/1.mp4", "ext": "mp4", "vcodec": None, "acodec": None, "protocol": "https"},
            {"url": "https://cdn.example/3.mp4", "ext": "mp4", "vcodec": None, "acodec": None, "protocol": "https"},
            {"url": "https://cdn.example/v.mp4", "ext": "mp4", "vcodec": "vp09", "acodec": "none", "protocol": "https", "height": 1920},
        ]}
        self.assertEqual(_video_link(real), "https://cdn.example/3.mp4")
        self.assertEqual(self.engine._meta(info, "instagram")["video_url"], "https://cdn.example/big.mp4")
        self.assertNotIn("video_url", self.engine._meta(info, "tiktok"))

    def test_asking_for_the_mp4_link_reads_the_post_again_without_downloading(self):
        plain = self.fake.resolve

        def resolve(url, proxy, count, cookiefile=None):
            infos = plain(url, proxy, count, cookiefile)
            for i in infos:
                i["formats"] = [{"url": "https://cdn.example/whole.mp4", "vcodec": "avc1", "acodec": "mp4a",
                                 "protocol": "https", "height": 1280}]
            return infos
        self.fake.resolve = resolve
        job, _ = self.engine.submit_video("alice", IG)
        self.drain()
        self.engine.db.run("update media set meta = json_remove(meta, '$.video_url', '$.video_url_read_at')")
        self.fake.calls.clear()
        linked, _ = self.engine.submit_video("alice", IG, {"video_link": True})
        self.assertNotEqual(linked["id"], job["id"], "asking for the link is a job of its own")
        self.drain()
        self.assertEqual(self.fake.calls, ["resolve"], "one read of the post, no download")
        self.assertEqual(self.engine.result(self.job(linked["id"]))["media"]["video_url"], "https://cdn.example/whole.mp4")
        self.fake.calls.clear()
        again, _ = self.engine.submit_video("alice", IG, {"video_link": True}, idem="next-ask")
        self.drain()
        self.assertEqual(self.job(again["id"])["status"], "completed")
        self.assertEqual(self.fake.calls, [], "a link read in the last 12 hours is given as it is")
        self.engine.db.run("update media set meta = json_set(meta, '$.video_url_read_at', '2020-01-01T00:00:00Z')")
        self.fake.calls.clear()
        same, existing = self.engine.submit_video("alice", IG, {"video_link": True}, idem="next-ask")
        self.assertEqual((same["id"], same["status"]), (again["id"], "queued"), "an old link: the same job reads again")
        self.drain()
        self.assertEqual(self.fake.calls, ["resolve"])

    def test_a_member_is_kept_but_is_not_part_of_the_key(self):
        a, _ = self.engine.submit_video("awghub", TT, {"member": "user:ann"})
        b, existing = self.engine.submit_video("awghub", TT, {"member": "user:bob"})
        self.assertEqual((b["id"], existing), (a["id"], True))
        self.assertEqual(self.job(a["id"])["member"], "user:ann")
        for bad in ("ann bob", "../x", "x" * 121, 7):
            with self.assertRaises(JobError):
                self.engine.submit_video("awghub", TT, {"member": bad})

    def test_the_videos_of_an_account_run_for_the_member_who_asked(self):
        self.fake.profile_items = [{"id": "5", "url": "https://www.tiktok.com/@someone/video/5",
                                    "taken_at": dt.datetime(2026, 10, 29, tzinfo=dt.timezone.utc)}]
        job, _ = self.engine.submit_profile("awghub", "https://www.tiktok.com/@someone",
                                            {"max_items": 1, "member": "user:ann"})
        self.drain()
        children = self.engine.db.all("select member from job where parent_id = ?", (job["id"],))
        self.assertEqual([c["member"] for c in children], ["user:ann"])

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

    def test_videos_are_fetched_just_in_time_for_whisper(self):
        engine = self.make(asr_backlog=2)
        a, _ = engine.submit_video("alice", "https://www.tiktok.com/@x/video/1")
        b, _ = engine.submit_video("alice", "https://www.tiktok.com/@x/video/2")
        c, _ = engine.submit_video("alice", "https://www.tiktok.com/@x/video/3")
        pic, _ = engine.submit_images("alice", "https://www.instagram.com/p/PIC9/")
        engine.run_once(("fetch",))
        engine.run_once(("fetch",))  # two files now wait for Whisper
        self.assertEqual([self.job(j["id"])["stage"] for j in (a, b, c)], ["asr", "asr", "fetch"])
        claimed = engine.claim(("fetch",))
        self.assertEqual(claimed["id"], pic["id"])  # pictures are not held back; the third video is
        engine.run_once(("asr",))  # one transcribed, its file gone
        self.drain(engine)
        self.assertEqual(self.job(c["id"])["status"], "completed")

    def test_someone_waiting_goes_before_bulk_work(self):
        engine = self.make(asr_backlog=1)
        bulk = [engine.submit_video("alice", f"https://www.tiktok.com/@x/video/{i}")[0] for i in (1, 2, 3)]
        engine.run_once(("fetch",))  # one file waits for Whisper: the backlog is full
        waiting, _ = engine.submit_video("bob", "https://www.tiktok.com/@x/video/9", {"priority": "high"})
        claimed = engine.claim(("fetch",))
        self.assertEqual(claimed["id"], waiting["id"])  # first, and past the backlog rule
        engine.db.run("update job set lease_until = null, attempts = 0 where id = ?", (claimed["id"],))
        engine.run_once(("fetch",))
        self.assertEqual(engine.claim(("asr",))["id"], waiting["id"])  # and first for Whisper
        self.assertEqual(self.job(bulk[2]["id"])["stage"], "fetch")
        with self.assertRaises(JobError):
            engine.submit_video("bob", TT, {"priority": "urgent"})

    def test_asking_with_priority_lifts_a_bulk_job(self):
        bulk, _ = self.engine.submit_video("alice", TT)
        again, existing = self.engine.submit_video("alice", TT, {"priority": "high"})
        self.assertEqual((again["id"], existing, again["priority"]), (bulk["id"], True, 1))
        self.assertEqual(self.engine.submit_video("alice", TT)[0]["priority"], 1)  # never lowered

    def test_captions_answer_without_any_download(self):
        engine = self.make(keep_media=False)
        self.fake.listed_subtitles = {"en": [{"ext": "vtt"}]}
        self.fake.captions = [("en", "vtt", "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nGood evening\n")]
        job, _ = engine.submit_video("alice", "https://vimeo.com/76979871")
        self.drain(engine)
        result = engine.result(self.job(job["id"]))
        self.assertEqual((result["status"], result["transcript"]["source"]), ("completed", "native_captions"))
        self.assertEqual(result["transcript"]["text"], "Good evening")
        self.assertEqual(self.fake.calls, ["resolve", "captions"])  # no media at all
        self.assertEqual(self.asr.calls, 0)
        # Several languages listed: nothing says which is spoken, so the audio comes.
        self.fake.calls, self.fake.listed_subtitles = [], {"en": [], "de": []}
        engine.submit_video("alice", "https://vimeo.com/2")
        self.drain(engine)
        self.assertEqual(self.fake.calls, ["resolve", "download"])

    def test_copy_protected_audio_points_to_the_captions(self):
        engine = self.make(keep_media=False)
        self.fake.listed_subtitles = {"de": [], "en": []}  # two languages: the audio would be needed
        self.fake.download_errors = [classify(Exception("ERROR: This format is DRM protected; Try selecting another"))]
        job, _ = engine.submit_video("alice", "https://vimeo.com/76979871")
        self.drain(engine)
        row = self.job(job["id"])
        self.assertEqual((row["status"], row["error_class"]), ("failed", "drm"))
        self.assertIn("captions in de, en", row["error"])
        again, _ = engine.submit_video("alice", "https://vimeo.com/76979871", {"language": "de"})
        self.fake.captions = [("de", "vtt", "WEBVTT\n\n00:00:00.000 --> 00:00:01.500\nHier bei Vimeo\n")]
        self.drain(engine)
        self.assertEqual(engine.result(self.job(again["id"]))["transcript"]["text"], "Hier bei Vimeo")

    def test_an_unlisted_vimeo_video_is_not_handed_out_by_its_number_alone(self):
        engine = self.make(keep_media=False)
        owner, _ = engine.submit_video("alice", "https://vimeo.com/76979871/8272103f6e")
        self.drain(engine)
        self.assertEqual(self.job(owner["id"])["status"], "completed")
        self.fake.calls = []
        guess, _ = engine.submit_video("bob", "https://vimeo.com/76979871")
        self.drain(engine)
        self.assertEqual(self.fake.calls[:1], ["resolve"])  # fetched for bob, not taken from alice's
        self.assertNotIn("8272103f6e", json.dumps(engine.result(self.job(guess["id"]))))

    def test_captions_that_say_nothing_fall_back_to_the_audio(self):
        engine = self.make(keep_media=False)
        self.fake.listed_subtitles = {"en": [{"ext": "vtt"}]}
        self.fake.captions = [("en", "vtt", "WEBVTT\n\n")]  # listed, but empty
        job, _ = engine.submit_video("alice", "https://vimeo.com/3")
        self.drain(engine)
        result = engine.result(self.job(job["id"]))
        self.assertEqual(self.fake.calls, ["resolve", "captions", "download"])
        self.assertEqual((result["status"], result["transcript"]["source"]), ("completed", "asr"))

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

    def test_only_a_callers_size_of_import_goes_first(self):
        import smt.engine as engine_module
        self.fake.profile_items = [{"id": str(v), "url": f"https://www.tiktok.com/@someone/video/{v}"} for v in (1, 2, 3)]
        small, _ = self.engine.submit_profile("alice", "https://www.tiktok.com/@someone", {"max_items": 3, "priority": "high"})
        self.engine.run_once(("profile",))
        kids = self.engine.db.all("select priority from job where parent_id = ?", (small["id"],))
        self.assertEqual([k["priority"] for k in kids], [1, 1, 1])
        saved, engine_module.MAX_PRIORITY_ITEMS = engine_module.MAX_PRIORITY_ITEMS, 2
        try:
            big, _ = self.engine.submit_profile("bob", "https://www.tiktok.com/@someone", {"max_items": 3, "priority": "high"})
            self.engine.run_once(("profile",))
        finally:
            engine_module.MAX_PRIORITY_ITEMS = saved
        kids = self.engine.db.all("select priority from job where parent_id = ?", (big["id"],))
        self.assertEqual([k["priority"] for k in kids], [0, 0, 0])  # bulk-sized: no head start

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
        self.assertEqual(len(result["media"]["image_urls"]), 2)  # where the pictures are, for a caller to show
        self.assertTrue(all(not f.exists() for f in self.fake.downloaded))  # every picture deleted
        self.assertEqual(self.job(job["id"])["platform_requests"], 3)  # the post + two pictures
        again, _ = self.engine.submit_images("bob", "https://www.instagram.com/p/CAROUSEL1/")
        self.drain()
        self.assertEqual(self.job(again["id"])["platform_requests"], 0)
        with self.assertRaises(JobError):
            self.engine.submit_images("alice", TT)  # TikTok has no picture jobs here

    def test_a_waiting_caller_gets_fresh_picture_links_without_reading_again(self):
        self.engine.submit_images("alice", "https://www.instagram.com/p/CAROUSEL2/")
        self.drain()
        self.engine.db.run("update media set meta = json_set(meta, '$.image_links_at', 0)")  # days old
        self.fake.calls = []
        bulk, _ = self.engine.submit_images("bob", "https://www.instagram.com/p/CAROUSEL2/")
        self.drain()
        self.assertEqual(self.fake.calls, [])  # bulk work takes the text as it is
        waiting, _ = self.engine.submit_images("carol", "https://www.instagram.com/p/CAROUSEL2/", {"priority": "high"})
        self.drain()
        self.assertEqual(self.fake.calls, ["post"])  # the links once more, no picture read again
        result = self.engine.result(self.job(waiting["id"]))
        self.assertEqual((result["status"], result["transcript"]["text"]), ("completed", "Tipp 1: Nießbrauch prüfen"))
        self.assertEqual(len(result["media"]["image_urls"]), 2)

    def test_pictures_are_described_by_the_chosen_model(self):
        job, _ = self.engine.submit_images("alice", "https://www.instagram.com/p/CAROUSEL3/",
                                           {"describe_images": "claude-haiku-5-5"})
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["transcript"]["source"], "ocr")  # the text read off stays as it was
        u = result["understanding"]
        self.assertEqual((u["source"], u["model"], len(u["segments"])), ("vision", "claude-haiku-5-5", 2))
        self.assertIn("Slide 1:", u["text"])
        self.assertEqual([m for m, _, _ in self.understand.images], ["claude-haiku-5-5"] * 2)
        self.assertTrue(all(not f.exists() for f in self.fake.downloaded))  # pictures deleted after

    def test_a_model_without_credit_leaves_the_text_of_the_pictures(self):
        def broke(*_a):
            raise JobError("internal", "the model provider's credit is used up - top it up, then ask again")
        self.understand.describe_image = broke
        job, _ = self.engine.submit_images("alice", "https://www.instagram.com/p/CAROUSEL5/",
                                           {"describe_images": "gemini-3.8-flash"})
        self.drain()
        row = self.job(job["id"])
        result = self.engine.result(row)
        self.assertEqual((result["status"], result["transcript"]["text"]), ("completed", "Tipp 1: Nießbrauch prüfen"))
        self.assertNotIn("understanding", result)
        self.assertTrue(any("credit is used up" in w for w in result["warnings"]))

    def test_a_description_asked_for_later_reads_no_text_again(self):
        self.engine.submit_images("alice", "https://www.instagram.com/p/CAROUSEL4/")
        self.drain()
        calls_before = list(self.fake.calls)
        job, _ = self.engine.submit_images("alice", "https://www.instagram.com/p/CAROUSEL4/",
                                           {"describe_images": "gemini-3.8-flash"})  # another job: options differ
        self.drain()
        result = self.engine.result(self.job(job["id"]))
        self.assertEqual(result["understanding"]["model"], "gemini-3.8-flash")
        self.assertEqual(self.fake.calls[len(calls_before):], ["post", "image", "image"])  # fetched again for the model
        self.assertEqual(result["transcript"]["text"], "Tipp 1: Nießbrauch prüfen")  # OCR not run a second time

    def test_a_reel_is_watched_by_gemini_and_then_deleted(self):
        engine = self.make(keep_media=False)
        job, _ = engine.submit_video("alice", TT, {"describe_video": "gemini-3.8-flash"})
        self.drain(engine)
        result = engine.result(self.job(job["id"]))
        self.assertEqual(result["status"], "completed")
        self.assertTrue(self.fake.wanted_video)  # a small copy with picture, not only the audio
        self.assertEqual(result["understanding"]["text"], "0:00 Ein Mann zeigt einen Grundriss.")
        self.assertEqual(result["transcript"]["source"], "asr")
        self.assertEqual([f for f in self.data.joinpath("media").rglob("*") if f.is_file()], [])

    def test_a_transcribed_reel_gets_its_description_without_a_second_transcript(self):
        engine = self.make(keep_media=False)
        engine.submit_video("alice", TT)
        self.drain(engine)
        job, _ = engine.submit_video("alice", TT, {"describe_video": "gemini-3.8-flash"})
        self.drain(engine)
        self.assertEqual(self.asr.calls, 1)
        self.assertEqual(engine.result(self.job(job["id"]))["understanding"]["model"], "gemini-3.8-flash")
        self.assertEqual(self.fake.calls, ["resolve", "download", "resolve", "download"])

    def test_a_busy_model_is_asked_again_later(self):
        engine = self.make(keep_media=False)
        self.understand.fail_video = [JobError("network", "model API HTTP 429: busy")]
        job, _ = engine.submit_video("alice", TT, {"describe_video": "gemini-3.8-flash"})
        engine.run_once(("fetch",))
        engine.run_once(("asr",))
        self.assertEqual(self.job(job["id"])["status"], "retry_wait")
        engine.db.run("update job set next_at = 0 where id = ?", (job["id"],))
        engine.run_once(("asr",))
        self.assertEqual(self.job(job["id"])["status"], "completed")
        self.assertEqual(self.asr.calls, 1)  # the transcript was kept, only the model asked again
        self.assertEqual(len(self.understand.videos), 2)

    def test_understanding_options_are_checked(self):
        for options in ({"describe_video": "claude-sonnet-5-5"},  # cannot watch videos
                        {"describe_images": "gpt-6-luna"},  # no OpenAI key on this worker
                        {"describe_images": "gemini; rm -rf /"}):
            with self.assertRaises(JobError, msg=str(options)):
                self.engine.submit_video("alice", TT, options)
        plain, _ = self.engine.submit_video("alice", TT)
        described, _ = self.engine.submit_video("alice", TT, {"describe_video": "gemini-3.8-flash"})
        self.assertNotEqual(plain["id"], described["id"])
        self.assertEqual(self.engine.submit_video("alice", TT)[0]["id"], plain["id"])  # old keys unchanged

    def test_an_account_import_gives_each_post_the_model_that_fits_it(self):
        self.fake.profile_items = [
            {"id": "REEL7", "url": "https://www.instagram.com/p/REEL7/", "kind": "reel"},
            {"id": "PIC7", "url": "https://www.instagram.com/p/PIC7/", "kind": "image"},
        ]
        job, _ = self.engine.submit_profile("alice", "https://www.instagram.com/someone/", {
            "max_items": 2, "include_images": True,
            "describe_images": "claude-haiku-5-5", "describe_video": "gemini-3.8-flash"})
        self.engine.run_once(("profile",))
        kids = {r["kind"]: json.loads(r["options"]) for r in
                self.engine.db.all("select kind, options from job where parent_id = ?", (job["id"],))}
        self.assertEqual((kids["video"].get("describe_video"), kids["video"].get("describe_images")),
                         ("gemini-3.8-flash", None))
        self.assertEqual((kids["images"].get("describe_images"), kids["images"].get("describe_video")),
                         ("claude-haiku-5-5", None))
        # The same post asked for on its own is the same job.
        alone, existing = self.engine.submit_images("alice", "https://www.instagram.com/p/PIC7/",
                                                    {"describe_images": "claude-haiku-5-5"})
        self.assertTrue(existing)

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


class OpenAiAsrTest(unittest.TestCase):
    """Reels to OpenAI: speech found here first, the local model when OpenAI
    cannot answer, and the tokens kept for what it cost."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.wav = self.tmp / "a.wav"
        self.wav.write_bytes(b"RIFF" + b"\0" * 2000)
        self.posts = []
        self.local = FakeAsr()
        self.local.transcribe = lambda wav, language=None: {
            "status": "available", "language": "de", "segments": [{"start_ms": 0, "end_ms": 900, "text": "lokal"}],
            "text": "lokal", "quality": {"flags": []}, "model": "large-v3-turbo", "model_revision": "abc", "params": {}}

    def asr(self, speech=12.0, answer=None, fail=None, key="sk-test"):
        def post(key, model, wav, language):
            self.posts.append((key, model, wav, language))
            if fail:
                raise fail
            return answer or {"text": " Hallo zusammen. ", "usage": {"type": "tokens", "input_tokens": 400, "output_tokens": 9}}
        return OpenAiAsr("openai:gpt-4o-mini-transcribe", key, lambda: self.local, post=post,
                         vad=lambda wav: (48.1, speech))

    def test_speech_goes_to_openai_as_one_segment_with_its_tokens(self):
        out = self.asr().transcribe(self.wav)
        self.assertEqual(self.posts[0][:2], ("sk-test", "gpt-4o-mini-transcribe"))
        self.assertEqual(out["status"], "available")
        self.assertEqual(out["segments"], [{"start_ms": 0, "end_ms": 48100, "text": "Hallo zusammen."}])
        self.assertEqual(out["model"], "openai:gpt-4o-mini-transcribe")
        self.assertEqual(out["quality"]["usage"]["input_tokens"], 400)
        self.assertEqual(out["quality"]["speech_seconds"], 12.0)
        self.assertEqual(out["params"]["backend"], "openai")

    def test_music_never_leaves_the_machine(self):
        out = self.asr(speech=0.4).transcribe(self.wav)
        self.assertEqual(self.posts, [])
        self.assertEqual((out["status"], out["text"], out["language"]), ("no_speech", "", None))

    def test_openai_failing_means_the_local_model_answers_and_says_why(self):
        out = self.asr(fail=RuntimeError("OpenAI HTTP 429: quota")).transcribe(self.wav)
        self.assertEqual(out["text"], "lokal")
        self.assertEqual(out["model"], "large-v3-turbo")
        self.assertEqual(out["quality"]["flags"], ["openai_failed: OpenAI HTTP 429: quota"])

    def test_without_a_key_or_with_long_audio_the_local_model_answers(self):
        self.assertEqual(self.asr(key=None).transcribe(self.wav)["text"], "lokal")
        self.wav.write_bytes(b"\0" * (24 * 1024 * 1024 + 1))
        out = self.asr().transcribe(self.wav)
        self.assertEqual((out["text"], out["quality"]["flags"]), ("lokal", ["too_long_for_openai"]))
        self.assertEqual(self.posts, [])

    def test_an_openai_model_is_refused_where_the_worker_has_no_key(self):
        data = self.tmp / "data"
        engine = Engine(settings(data, asr_model="openai:gpt-4o-mini-transcribe"), asr_factory=lambda _m: self.local)
        engine.model_keys = {}
        with self.assertRaises(JobError) as err:
            engine._options({})
        self.assertIn("no key for openai", str(err.exception))
        engine.model_keys = {"openai": "sk-test"}
        self.assertEqual(engine._options({})["asr_model"], "openai:gpt-4o-mini-transcribe")
        self.assertIsInstance(engine._make_asr("openai:gpt-4o-mini-transcribe"), OpenAiAsr)


if __name__ == "__main__":
    unittest.main()
