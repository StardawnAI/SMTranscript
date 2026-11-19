"""Downloads (job kind "download") without the network: yt-dlp is a stand-in,
the library worker is a small HTTP server on localhost, the database is real.

    cd worker && python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from smt import config, fetch, guard, upload  # noqa: E402
from smt.api import serve  # noqa: E402
from smt.engine import Engine  # noqa: E402
from smt.errors import JobError  # noqa: E402

TOKEN = "upload-token-for-tests-0123456789"
YT = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class FakeLibrary:
    """The library worker's upload routes, recording what arrives.

    `fail` maps a path suffix ("parts/2", "complete", ...) to a list of
    statuses answered in turn before the request succeeds."""

    def __init__(self, part_size: int = 1000):
        self.part_size = part_size
        self.requests: list[tuple[str, str, int, str]] = []  # method, path, body length, auth
        self.parts: dict[int, bytes] = {}
        self.fail: dict[str, list[int]] = {}
        self.created: dict | None = None
        lib = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body=None):
                data = json.dumps(body or {}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                lib.requests.append((self.command, self.path, len(raw), self.headers.get("Authorization", "")))
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._answer(401, {"error": "unauthorized"})
                for suffix, statuses in lib.fail.items():
                    if self.path.endswith(suffix) and statuses:
                        return self._answer(statuses.pop(0), {"error": "planned"})
                if self.command == "POST" and self.path == "/v1/downloads":
                    lib.created = json.loads(raw)
                    return self._answer(200, {"id": "dl_0123456789abcdef", "part_size": lib.part_size,
                                              **({"filename": lib.stored_name} if lib.stored_name else {})})
                if self.command == "PUT" and "/parts/" in self.path:
                    lib.parts[int(self.path.rsplit("/", 1)[1])] = raw
                    return self._answer(200, {"ok": True})
                if self.path.endswith("/complete"):
                    return self._answer(200, {"url": "https://lib.example/d/dl_0123456789abcdef/a.mp4",
                                              "expires_at": lib.expires_at})
                if self.path.endswith("/abort"):
                    return self._answer(200, {"ok": True})
                return self._answer(404)

            do_POST = do_PUT = _handle

        self.expires_at = "2099-01-01T00:00:00Z"
        self.stored_name = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def paths(self, method=None):
        return [p for m, p, _n, _a in self.requests if method in (None, m)]


class FakeFiles:
    """Stands in for fetch.file_session: resolve, then write a file of `size` bytes."""

    def __init__(self):
        self.size = 2500
        self.download_errors: list[Exception] = []
        self.written: list[Path] = []
        self.entries = [{"id": "dQw4w9WgXcQ", "title": "Ein Lied – über Füße!", "formats": [{"url": "x"}]}]
        self.max_mb = None

    def cookie_copy(self, *_a):
        return None

    def file_session(self, proxy, cookiefile, max_mb, count):
        fake = self
        fake.max_mb = max_mb

        class Session:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def resolve(self, url):
                count()
                fake.resolved = url
                return fake.entries

            def download(self, info, tmp, captions_only=False):
                count()
                if fake.download_errors:
                    # Half a file, as yt-dlp leaves one when a download breaks off.
                    part = tmp / "media.mp4.part"
                    part.write_bytes(b"half")
                    fake.written.append(part)
                    raise fake.download_errors.pop(0)
                path = tmp / "media.mp4"
                path.write_bytes(bytes(range(256)) * (fake.size // 256) + b"x" * (fake.size % 256))
                fake.written.append(path)
                return {"video": path, "captions": []}

        return Session()


def settings(data: Path, **over) -> config.Settings:
    routes = {"tiktok": "direct", "instagram": "direct", "vimeo": "direct", "youtube": "direct"}
    base = dict(
        data=data, keys=config.parse_keys(f"alice:{'a' * 21},bob:{'b' * 20}"), port=0, bind="127.0.0.1",
        routes={"direct": None}, share_direct=(), direct_off=False, default_route=routes,
        gap_seconds={p: 0 for p in routes}, fetch_workers=1, asr_workers=1, max_video_mb=50, max_duration_s=600,
        max_profile_items=50, min_free_mb=0, asr_model="large-v3-turbo", asr_device="cpu", cookies_dir=None,
        instaloader_session=None, instagram_user=None, download_token=TOKEN,
    )
    base.update(over)
    return config.Settings(**base)


class YoutubeLinkTest(unittest.TestCase):
    def test_youtube_forms(self):
        canonical = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube.com/watch?v=dQw4w9WgXcQ&t=42s",
                    "https://www.youtube.com/watch?feature=share&v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ?si=abc",
                    "https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/live/dQw4w9WgXcQ?feature=x",
                    "https://m.youtube.com/watch?v=dQw4w9WgXcQ", "http://m.youtube.com/shorts/dQw4w9WgXcQ/"):
            with self.subTest(url=url):
                t = guard.parse(url)
                self.assertEqual((t.platform, t.kind, t.platform_id, t.canonical_url),
                                 ("youtube", "video", "dQw4w9WgXcQ", canonical))

    def test_not_a_youtube_video(self):
        for url in ("https://youtube.com/watch?v=x", "https://www.youtube.com/@channel",
                    "https://www.youtube.com/playlist?list=PL123", "https://youtu.be/", "https://www.youtube.com/",
                    "https://www.youtube.com/watch?v=dQw4w9WgXcQ0", "https://youtube.com.evil.io/watch?v=dQw4w9WgXcQ",
                    "https://www.youtube.com/channel/UC1234567890"):
            with self.subTest(url=url), self.assertRaises(guard.InputError):
                guard.parse(url)


class FileSessionTest(unittest.TestCase):
    def test_options_ask_for_one_mp4_and_nothing_else(self):
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            self.skipTest("needs yt-dlp")
        with fetch.file_session(None, None, 2048, lambda: None) as ses:
            p = ses._ydl.params
            self.assertEqual(p["format"], fetch.DOWNLOAD_FORMAT)
            self.assertEqual(p["merge_output_format"], "mp4")
            self.assertEqual(p["max_filesize"], 2048 * 1024 * 1024)
            self.assertIn("youtube", p["allowed_extractors"])
            self.assertFalse(p["writesubtitles"] or p["writeautomaticsub"])
            self.assertEqual(p["proxy"], "")


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.lib = FakeLibrary(part_size=1000)
        self.addCleanup(self.lib.close)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.file = self.dir / "a.mp4"
        self.data = bytes(range(256)) * 9 + b"tail"  # 2308 bytes
        self.file.write_bytes(self.data)

    def send(self, **kw):
        return upload.upload(self.file, self.lib.url + "/", TOKEN, filename="a.mp4", mime="video/mp4",
                             title="A", source_url=YT, backoff_s=0, **kw)

    def test_parts_of_exactly_part_size_then_complete(self):
        got = self.send()
        self.assertEqual(got, {"url": "https://lib.example/d/dl_0123456789abcdef/a.mp4",
                               "expires_at": "2099-01-01T00:00:00Z", "filename": "a.mp4"})
        self.assertEqual(self.lib.created, {"filename": "a.mp4", "mime": "video/mp4", "size": 2308, "title": "A",
                                            "source_url": YT})
        self.assertEqual([len(self.lib.parts[n]) for n in sorted(self.lib.parts)], [1000, 1000, 308])
        self.assertEqual(b"".join(self.lib.parts[n] for n in (1, 2, 3)), self.data)
        self.assertEqual(self.lib.paths(), ["/v1/downloads", "/v1/downloads/dl_0123456789abcdef/parts/1",
                                            "/v1/downloads/dl_0123456789abcdef/parts/2",
                                            "/v1/downloads/dl_0123456789abcdef/parts/3",
                                            "/v1/downloads/dl_0123456789abcdef/complete"])
        self.assertTrue(all(auth == f"Bearer {TOKEN}" for *_x, auth in self.lib.requests))
        self.assertTrue(self.file.exists(), "deleting the file is the caller's job")

    def test_a_refused_part_aborts_and_is_not_tried_again(self):
        self.lib.fail["parts/2"] = [400]
        with self.assertRaises(JobError) as caught:
            self.send()
        self.assertEqual(caught.exception.cls, "internal")
        self.assertEqual(self.lib.paths("PUT").count("/v1/downloads/dl_0123456789abcdef/parts/2"), 1)
        self.assertIn("/v1/downloads/dl_0123456789abcdef/abort", self.lib.paths())
        self.assertNotIn("/v1/downloads/dl_0123456789abcdef/complete", self.lib.paths())

    def test_a_failing_part_is_tried_three_times_then_aborted(self):
        self.lib.fail["parts/2"] = [500] * 10
        with self.assertRaises(JobError) as caught:
            self.send()
        self.assertEqual(caught.exception.cls, "network")
        self.assertEqual(self.lib.paths("PUT").count("/v1/downloads/dl_0123456789abcdef/parts/2"), upload.ATTEMPTS)
        self.assertEqual(upload.ATTEMPTS, 3)
        self.assertNotIn("/v1/downloads/dl_0123456789abcdef/parts/3", self.lib.paths())
        self.assertEqual(self.lib.paths()[-1], "/v1/downloads/dl_0123456789abcdef/abort")

    def test_the_name_the_store_kept_is_the_one_answered(self):
        self.lib.stored_name = "a-plainer.mp4"
        self.assertEqual(self.send()["filename"], "a-plainer.mp4")

    def test_a_passing_hiccup_is_retried_with_the_same_bytes(self):
        self.lib.fail["parts/1"] = [503]
        self.lib.fail["complete"] = [502]
        self.send()
        self.assertEqual(self.lib.parts[1], self.data[:1000])
        self.assertEqual(self.lib.paths("PUT").count("/v1/downloads/dl_0123456789abcdef/parts/1"), 2)
        self.assertNotIn("/v1/downloads/dl_0123456789abcdef/abort", self.lib.paths())

    def test_a_wrong_token_says_so_without_showing_it(self):
        with self.assertRaises(JobError) as caught:
            upload.upload(self.file, self.lib.url, "wrong-token", filename="a.mp4", mime="video/mp4", title=None,
                          source_url=YT, backoff_s=0)
        self.assertIn("SMT_DOWNLOAD_TOKEN", str(caught.exception))
        self.assertNotIn("wrong-token", str(caught.exception))
        self.assertEqual(len(self.lib.requests), 1)

    def test_an_unreachable_store_is_a_network_error_after_three_tries(self):
        dead = FakeLibrary()
        url = dead.url
        dead.close()
        with self.assertRaises(JobError) as caught:
            upload.upload(self.file, url, TOKEN, filename="a.mp4", mime="video/mp4", title=None, source_url=YT,
                          backoff_s=0)
        self.assertEqual(caught.exception.cls, "network")


class DownloadJobTest(unittest.TestCase):
    def setUp(self):
        self.lib = FakeLibrary(part_size=1000)
        self.addCleanup(self.lib.close)
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, True)
        self.files = FakeFiles()
        self.engine = self.make()
        # No waiting between upload tries in tests.
        backoff = upload.BACKOFF_S
        upload.BACKOFF_S = 0
        self.addCleanup(setattr, upload, "BACKOFF_S", backoff)

    def make(self, **over):
        engine = Engine(settings(self.data, **{"download_url": self.lib.url, **over}), asr_factory=lambda _m: None)
        engine.fetch = self.files
        return engine

    def drain(self):
        for _ in range(20):
            if not self.engine.run_once(("fetch", "profile")):
                return

    def job(self, job_id):
        return self.engine.db.one("select * from job where id = ?", (job_id,))

    def assert_nothing_left(self, job_id):
        self.assertTrue(self.files.written, "a file was written")
        for path in self.files.written:
            self.assertFalse(path.exists(), path)
        self.assertFalse((self.engine.s.tmp_dir / job_id).exists())
        self.assertEqual(list(self.engine.s.media_dir.iterdir()), [])

    def test_a_youtube_video_becomes_a_link_and_leaves_nothing_behind(self):
        job, existing = self.engine.submit_download("alice", "https://youtu.be/dQw4w9WgXcQ", {"priority": "high"})
        self.assertFalse(existing)
        self.drain()
        done = self.job(job["id"])
        self.assertEqual(done["status"], "completed", done["error"])
        result = self.engine.result(done)
        self.assertEqual(result["download"], {"url": "https://lib.example/d/dl_0123456789abcdef/a.mp4",
                                              "expires_at": "2099-01-01T00:00:00Z",
                                              "filename": "Ein-Lied-uber-Fue.mp4", "size": 2500,
                                              "title": "Ein Lied – über Füße!"})
        self.assertEqual((result["kind"], result["source_url"]), ("download", YT))
        self.assertNotIn("transcript", result)
        self.assertEqual(self.files.resolved, YT)
        self.assertEqual(self.files.max_mb, 2048)
        self.assertEqual(self.lib.created["mime"], "video/mp4")
        self.assertEqual([len(self.lib.parts[n]) for n in sorted(self.lib.parts)], [1000, 1000, 500])
        self.assertEqual(self.engine.db.one("select count(*) n from track")["n"], 0)
        self.assert_nothing_left(job["id"])

    def test_asked_again_while_the_link_lives_answers_the_same_link(self):
        job, _ = self.engine.submit_download("alice", YT)
        self.drain()
        again, existing = self.engine.submit_download("alice", "https://www.youtube.com/shorts/dQw4w9WgXcQ")
        self.assertEqual((existing, again["id"], again["status"]), (True, job["id"], "completed"))
        self.assertEqual(len(self.files.written), 1)

    def test_asked_again_after_the_link_ran_out_downloads_anew(self):
        self.lib.expires_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
        job, _ = self.engine.submit_download("alice", YT)
        self.drain()
        self.lib.expires_at = "2099-01-01T00:00:00Z"
        again, _ = self.engine.submit_download("alice", YT)
        self.assertEqual((again["id"], again["status"], again["result"]), (job["id"], "queued", None))
        self.drain()
        self.assertEqual(self.engine.result(self.job(job["id"]))["download"]["expires_at"], "2099-01-01T00:00:00Z")
        self.assertEqual(len(self.files.written), 2)

    def test_a_failed_upload_deletes_the_file_and_aborts(self):
        self.lib.fail["parts/2"] = [500] * 20
        job, _ = self.engine.submit_download("alice", YT)
        self.drain()
        failed = self.job(job["id"])
        self.assertEqual(failed["error_class"], "network")
        self.assertIn("/v1/downloads/dl_0123456789abcdef/abort", self.lib.paths())
        self.assertIsNone(failed["result"])
        self.assert_nothing_left(job["id"])

    def test_a_failed_download_leaves_no_file(self):
        self.files.download_errors = [
            RuntimeError("ERROR: [youtube] dQw4w9WgXcQ: Video unavailable. This video has been removed")]
        job, _ = self.engine.submit_download("alice", YT)
        self.drain()
        self.assertEqual((self.job(job["id"])["status"], self.job(job["id"])["error_class"]), ("failed", "unavailable"))
        self.assertEqual(self.lib.requests, [])
        self.assert_nothing_left(job["id"])

    def test_over_the_download_limit_is_too_large_and_never_uploaded(self):
        self.engine = self.make(download_max_mb=1)
        self.files.size = 1024 * 1024 + 1
        job, _ = self.engine.submit_download("alice", YT)
        self.drain()
        self.assertEqual(self.job(job["id"])["error_class"], "too_large")
        self.assertEqual(self.lib.requests, [])
        self.assert_nothing_left(job["id"])

    def test_a_running_live_stream_is_refused(self):
        self.files.entries = [{**self.files.entries[0], "is_live": True}]
        job, _ = self.engine.submit_download("alice", YT)
        self.drain()
        self.assertEqual(self.job(job["id"])["error_class"], "invalid_input")
        self.assertEqual(self.files.written, [])

    def test_without_the_download_settings_nothing_crashes(self):
        engine = self.make(download_url=None)
        with self.assertRaises(JobError) as caught:
            engine.submit_download("alice", YT)
        self.assertEqual(caught.exception.cls, "invalid_input")
        self.assertIn("SMT_DOWNLOAD_URL", str(caught.exception))
        # A job queued before the settings went away fails the same way.
        job, _ = self.engine.submit_download("alice", YT)
        self.engine.s.download_token = None
        self.drain()
        self.assertEqual((self.job(job["id"])["status"], self.job(job["id"])["error_class"]), ("failed", "invalid_input"))

    def test_what_a_download_takes(self):
        for url in ("https://www.tiktok.com/@a/video/123", "https://www.instagram.com/reel/ABC/",
                    "https://vimeo.com/76979871"):
            self.assertEqual(self.engine.submit_download("alice", url)[0]["kind"], "download")
        for url in ("https://www.tiktok.com/@someone", "https://www.instagram.com/someone/"):
            with self.assertRaises(JobError):
                self.engine.submit_download("alice", url)
        with self.assertRaises(JobError) as caught:
            self.engine.submit_video("alice", YT)
        self.assertIn("download", str(caught.exception))

    def test_over_the_api(self):
        server = serve(self.engine)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"

        def call(method, path, body=None):
            req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                         headers={"Authorization": f"Bearer {'a' * 21}"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read())

        status, body = call("POST", "/v1/jobs", {"url": YT, "kind": "download"})
        self.assertEqual((status, body["kind"], body["platform"]), (202, "download", "youtube"))
        self.drain()
        status, result = call("GET", f"/v1/jobs/{body['job_id']}/result")
        self.assertEqual((status, result["status"]), (200, "completed"))
        self.assertEqual(set(result["download"]), {"url", "expires_at", "filename", "size", "title"})


if __name__ == "__main__":
    unittest.main()
