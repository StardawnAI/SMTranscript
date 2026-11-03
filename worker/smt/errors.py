"""What went wrong decides what happens next.

A 429 and a deleted video both arrive as "the download failed", and treating
them alike is how a scraper ends up hammering a platform that asked it to
wait, or retrying a video that no longer exists. Each class here carries the
one action that fits it; engine.py carries it out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# class -> (terminal for the job, counts against the route, what to do)
ACTIONS = {
    "rate_limited": "pause this platform on this route until the wait is over",
    "challenge": "stop this platform on this route until a person unblocks it",
    "network": "retry a few times, further apart each time",
    "route_down": "wait for the route — never switch to another one unasked",
    "expired_link": "resolve the video once more, then download again",
    "parser": "keep the message for a report; another try changes nothing",
    "unavailable": "the video is gone or never existed",
    "private": "the video is private or only for followers",
    "login_required": "the video needs a logged-in session",
    "drm": "the video's media is copy-protected; only its captions can be read",
    "too_large": "over the size or duration limit",
    "disk_full": "stop downloading until space is free",
    "invalid_input": "not a link this service takes",
    "cancelled": "cancelled by request",
    "internal": "a bug in this service",
}

TERMINAL = {"parser", "unavailable", "private", "login_required", "drm", "too_large", "invalid_input", "cancelled",
            "internal"}


class JobError(Exception):
    def __init__(self, cls: str, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.cls = cls
        self.retry_after = retry_after

    @property
    def terminal(self) -> bool:
        return self.cls in TERMINAL


# Checked in order; the first match wins. The texts are yt-dlp's and
# Instaloader's own messages (yt-dlp 2026.10.26, Instaloader 4.16).
_RULES = [
    ("challenge", r"\bfeedback_required\b"),
    ("rate_limited", r"HTTP Error 429|Too Many Requests|rate-?limit|Please wait a few minutes"),
    ("challenge", r"TikTok is requiring login|checkpoint|challenge_required"),
    ("route_down", r"Unable to connect to proxy|ProxyError|SOCKS|Tunnel connection failed|proxy.*refused"),
    ("drm", r"DRM protected"),
    ("private", r"[Pp]rivate|do not have permission|who follow this account|protected by a password|embed-only"),
    ("login_required", r"Restricted Video|registered users|login required|log ?in|--cookies"),
    ("unavailable", r"not available|no longer available|has been removed|deleted|HTTP Error 404|"
                    r"IP address is blocked from accessing this post|does not exist|Not Found"),
    ("expired_link", r"HTTP Error 403|HTTP Error 410"),
    ("too_large", r"larger than max-filesize|File is larger|does not pass filter"),
    ("disk_full", r"No space left on device"),
    ("network", r"timed out|Timeout|Connection reset|Connection aborted|Connection refused|"
                r"Temporary failure in name resolution|Network is unreachable|HTTP Error 5\d\d|"
                r"IncompleteRead|EOF occurred|Remote end closed"),
    ("parser", r"Unable to extract|Unable to download JSON|Unsupported URL|KeyError|regex|JSONDecodeError"),
]


def _retry_after(exc: BaseException) -> float | None:
    """Seconds from a Retry-After header anywhere in the exception chain."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
        value = headers.get("Retry-After") if headers is not None else None
        if value and str(value).strip().isdigit():
            return float(value)
        # yt-dlp wraps: DownloadError.exc_info -> ExtractorError.cause -> HTTPError
        exc_info = getattr(exc, "exc_info", None)
        exc = (getattr(exc, "cause", None) or (exc_info[1] if exc_info else None)
               or exc.__cause__ or exc.__context__)
    return None


def _platform_words(message: str) -> str:
    """The message without what the caller chose: links, and the video id
    yt-dlp puts in front ("ERROR: [Instagram] <shortcode>: ..."). Otherwise a
    shortcode like `rate-limit` would read as a 429 and cool the route down
    for every caller (security audit 2026-10-31, lead 1)."""
    message = re.sub(r"https?://\S+", "<link>", message)
    return re.sub(r"\[[\w:.]+\] [\w.@-]+: ", "", message)


def classify(exc: BaseException) -> JobError:
    if isinstance(exc, JobError):
        return exc
    message = str(exc) or type(exc).__name__
    if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
        return JobError("disk_full", message)
    words = _platform_words(message)
    for cls, pattern in _RULES:
        if re.search(pattern, words):
            return JobError(cls, message, _retry_after(exc) if cls == "rate_limited" else None)
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return JobError("network", message)
    return JobError("internal", f"{type(exc).__name__}: {message}")
