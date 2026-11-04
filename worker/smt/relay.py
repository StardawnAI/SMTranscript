"""Metadata-only client for the Cloudflare/Groq streaming transcription relay."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .errors import JobError

MODEL = "groq:whisper-large-v3-turbo"
FORMATS = {"mp4", "m4a", "mp3", "wav", "webm", "ogg", "flac"}


def select_source(info: dict) -> dict:
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
    duration = info.get("duration") or 0
    if not 0 < duration <= 900:
        raise JobError("too_large", "Streaming transcription requires a known duration of at most 900 seconds")
    headers = {**info.get("http_headers", {}), **fmt.get("http_headers", {})}
    allowed = {"user-agent", "referer", "cookie", "accept", "accept-language"}
    return {"url": fmt["url"], "duration": duration, "ext": fmt["ext"],
            "headers": {k: v for k, v in headers.items() if k.lower() in allowed}}


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

    def transcribe(self, source: dict, identity: str, language: str | None) -> dict:
        payload = json.dumps({"id": identity, "source": source, "language": language}).encode()
        request = urllib.request.Request(self.url + "/v1/transcribe", data=payload,
            headers={"Authorization": "Bearer " + self.token, "X-Groq-Key": self.key,
                     "Content-Type": "application/json", "User-Agent": "SMTranscript/3.2.1"})
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
            raise JobError("invalid_input", f"Streaming relay returned HTTP {exc.code}; no local media fallback") from None
        except (OSError, ValueError) as exc:
            raise JobError("invalid_input", "Streaming relay did not return a verified result; no automatic paid retry") from None
        flags = ["transcription_budget_half_used"] if data.get("budget_alert") else []
        return {"text": data["text"], "language": data.get("language"), "segments": data["segments"],
                "status": "available" if data["text"] else "no_speech", "model": MODEL, "model_revision": None,
                "quality": {"flags": flags}, "params": {"transport": "cloudflare-stream",
                "media_bytes": data.get("media_bytes"), "reserved_eur": data.get("reserved_eur"),
                "duration_seconds": data.get("duration_seconds"), "cached": data.get("cached", False)}}
