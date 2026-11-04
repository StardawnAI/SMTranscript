"""The remote transcription path must never download a local media file."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from smt.engine import Engine
from smt.errors import JobError
from smt.relay import RemoteAsr, select_source
from test_worker import FakeFetch, settings, TT


def info():
    return {"id": "remote", "duration": 30, "title": "Remote clip", "formats": [
        {"url": "https://video.cdninstagram.com/video.mp4", "ext": "mp4", "acodec": "aac", "vcodec": "h264", "tbr": 300},
        {"url": "https://video.cdninstagram.com/audio.m4a", "ext": "m4a", "acodec": "aac", "vcodec": "none", "tbr": 64}]}


class RelayTest(unittest.TestCase):
    def test_audio_only_is_preferred_and_manifests_are_rejected(self):
        self.assertEqual(select_source(info())["ext"], "m4a")
        data = info()
        data["formats"] = [{"url": "https://cdn.example/master.m3u8", "ext": "mp4", "protocol": "m3u8_native"}]
        with self.assertRaises(JobError):
            select_source(data)

    def test_no_local_download_or_media_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Engine(settings(Path(root), keep_media=False, asr_model="groq:whisper-large-v3-turbo"))
            remote = Mock()
            remote.transcribe.return_value = {"text": "Hello world", "language": "en",
                "model": "groq:whisper-large-v3-turbo", "model_revision": None, "params": {"transport": "cloudflare-stream"},
                "segments": [{"start_ms": 0, "end_ms": 2000, "text": "Hello world"}],
                "status": "available", "quality": {"flags": []}}
            engine.relay = remote
            session = Mock()
            session.resolve.return_value = [info()]
            session.remote_source.return_value = select_source(info())
            engine.fetch = Mock()
            engine.fetch.cookie_copy.return_value = None
            engine.fetch.session.return_value.__enter__ = Mock(return_value=session)
            engine.fetch.session.return_value.__exit__ = Mock(return_value=False)
            job, _ = engine.submit_video("alice", TT)
            engine.run_once(("fetch",))
            row = engine.db.one("select * from job where id = ?", (job["id"],))
            self.assertEqual(row["status"], "completed")
            session.download.assert_not_called()
            self.assertEqual(engine.result(row)["transcript"]["text"], "Hello world")
            self.assertFalse(list(Path(root, "media").rglob("*")))
            self.assertFalse(list(Path(root, "tmp").rglob("*")))
            engine.db.conn.close()

    def test_relay_mode_rejects_media_retention(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Engine(settings(Path(root), asr_model="groq:whisper-large-v3-turbo"))
            engine.relay = Mock()
            with self.assertRaises(JobError):
                engine.submit_video("alice", TT)
            engine.db.conn.close()

    def test_native_captions_do_not_call_the_paid_relay(self):
        with tempfile.TemporaryDirectory() as root:
            engine = Engine(settings(Path(root), keep_media=False, asr_model="groq:whisper-large-v3-turbo"))
            engine.relay = Mock()
            fake = FakeFetch(Path(root, "must-not-be-read.mp4"))
            fake.listed_subtitles = {"en": [{"ext": "vtt"}]}
            fake.captions = [("en", "vtt", "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nHello\n")]
            engine.fetch = fake
            job, _ = engine.submit_video("alice", TT)
            engine.run_once(("fetch",))
            row = engine.db.one("select * from job where id = ?", (job["id"],))
            self.assertEqual(row["status"], "completed")
            engine.relay.transcribe.assert_not_called()
            self.assertEqual(fake.calls, ["resolve", "captions"])
            engine.db.conn.close()

    def test_paid_provider_errors_never_fall_back_to_local_downloads(self):
        with patch.dict(os.environ, {"SMT_TRANSCRIPT_RELAY_URL": "https://relay.example",
             "SMT_TRANSCRIPT_RELAY_TOKEN": "test-token", "SMT_GROQ_API_KEY": "gsk_test"}):
            client = RemoteAsr.from_env()
            with patch("smt.relay.urllib.request.urlopen", side_effect=OSError("connection lost")) as call:
                with self.assertRaises(JobError):
                    client.transcribe(select_source(info()), "a" * 64, None)
                self.assertEqual(call.call_count, 1)


if __name__ == "__main__":
    unittest.main()
