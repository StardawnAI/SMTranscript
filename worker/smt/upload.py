"""Handing a downloaded file to the library worker, which keeps it for one hour
behind a link and then deletes it.

  POST {base}/v1/downloads              {filename, mime, size, title, source_url} -> {id, part_size}
  PUT  {base}/v1/downloads/<id>/parts/<n>   raw bytes, n from 1, exactly part_size (the last may be shorter)
  POST {base}/v1/downloads/<id>/complete    -> {url, expires_at}
  POST {base}/v1/downloads/<id>/abort       on any failure, so no half upload is left behind

Every request carries `Authorization: Bearer <upload token>`. A part is read
from disk while it is sent, so a 2 GB file never sits in memory. Each request
is tried at most ATTEMPTS times, further apart each time; a refusal (4xx) is
not tried again, because asking again changes nothing.
"""

from __future__ import annotations

import http.client
import json
import logging
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import VERSION
from .errors import JobError

LOG = logging.getLogger("smt.upload")

ATTEMPTS = 3
BACKOFF_S = 2.0
TIMEOUT_S = 120
MAX_ANSWER = 64 * 1024

# Our own service: never through a proxy from the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class _Part:
    """One part of the file, read from disk as http.client sends it."""

    def __init__(self, f, offset: int, length: int):
        f.seek(offset)
        self.f, self.left = f, length

    def read(self, n: int = -1) -> bytes:
        if self.left <= 0:
            return b""
        data = self.f.read(self.left if n is None or n < 0 else min(n, self.left))
        self.left -= len(data)
        return data


def _send(method: str, url: str, token: str, body: dict | None = None, part: tuple | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}", "User-Agent": f"smtranscript/{VERSION}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    elif part is not None:
        f, offset, length = part
        data = _Part(f, offset, length)
        headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(length)})
    else:
        headers["Content-Length"] = "0"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with _OPENER.open(request, timeout=TIMEOUT_S) as resp:
        raw = resp.read(MAX_ANSWER)
    try:
        return json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        raise JobError("internal", f"the download store answered {method} with something that is not JSON") from exc


def _refused(what: str, status: int, detail: str) -> JobError:
    if status in (401, 403):
        return JobError("internal", f"the download store refused {what} (HTTP {status}): check SMT_DOWNLOAD_TOKEN")
    if status == 413:
        return JobError("too_large", f"the download store does not take this file (HTTP 413): {detail}")
    return JobError("internal", f"the download store refused {what} (HTTP {status}): {detail}")


def _tried(what: str, call, attempts: int, backoff_s: float):
    """`call` at most `attempts` times; network trouble and 5xx/408/429 are
    tried again after 1, 2, 4 ... times backoff_s, a refusal is not."""
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except urllib.error.HTTPError as exc:
            detail = (exc.read(300) or b"").decode("utf-8", "replace").strip()
            if exc.code < 500 and exc.code not in (408, 429):
                raise _refused(what, exc.code, detail) from exc
            last = f"HTTP {exc.code} {detail}".strip()
        except (OSError, http.client.HTTPException) as exc:  # URLError, timeouts, resets
            last = f"{type(exc).__name__}: {exc}"
        if attempt < attempts:
            time.sleep(backoff_s * 2 ** (attempt - 1))
    raise JobError("network", f"{what} failed {attempts} times; the last time: {last[:300]}")


def upload(path: Path, base_url: str, token: str, *, filename: str, mime: str, title: str | None,
           source_url: str, attempts: int | None = None, backoff_s: float | None = None) -> dict:
    """Uploads the file in parts and returns {url, expires_at, filename}. The file
    itself is left where it is: deleting it is the caller's job."""
    attempts = ATTEMPTS if attempts is None else attempts
    backoff_s = BACKOFF_S if backoff_s is None else backoff_s
    base = base_url.rstrip("/")
    size = path.stat().st_size
    created = _tried("starting the upload", lambda: _send("POST", f"{base}/v1/downloads", token, {
        "filename": filename, "mime": mime, "size": size, "title": title or "", "source_url": source_url,
    }), attempts, backoff_s)
    upload_id, part_size = created.get("id"), created.get("part_size")
    if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", upload_id):
        raise JobError("internal", "the download store answered without a usable upload id")
    if not isinstance(part_size, int) or isinstance(part_size, bool) or part_size < 1:
        raise JobError("internal", f"the download store answered an unusable part_size: {part_size!r}")
    at = f"{base}/v1/downloads/{upload_id}"
    try:
        with path.open("rb") as f:
            for number, offset in enumerate(range(0, size, part_size), 1):
                length = min(part_size, size - offset)
                _tried(f"part {number}", lambda o=offset, n=number, ln=length: _send(
                    "PUT", f"{at}/parts/{n}", token, part=(f, o, ln)), attempts, backoff_s)
        done = _tried("finishing the upload", lambda: _send("POST", f"{at}/complete", token), attempts, backoff_s)
        if not isinstance(done.get("url"), str) or not isinstance(done.get("expires_at"), str):
            raise JobError("internal", "the download store finished the upload without a url and expires_at")
        # The store may make the name plainer; the link carries its version.
        stored = next((n for n in (done.get("filename"), created.get("filename")) if isinstance(n, str) and n), filename)
        return {"url": done["url"], "expires_at": done["expires_at"], "filename": stored}
    except BaseException:
        try:
            _tried("aborting the upload", lambda: _send("POST", f"{at}/abort", token), attempts, backoff_s)
        except Exception as exc:  # noqa: BLE001 — the first failure is the one to report
            LOG.warning("upload %s not aborted: %s", upload_id, exc)
        raise
