"""The HTTP interface: small, and the same for the MCP server and StarBase.

  POST /v1/jobs                      a video link            -> 202 {job_id}
                                     ({"kind": "images"}: the text on a picture post's pictures)
  POST /v1/profile-imports           an account link + limit -> 202 {job_id}
  GET  /v1/jobs/{id}                 status, error class, when to ask again
  GET  /v1/jobs/{id}/result          the transcript and the stored files, or 202 while not done
  POST /v1/jobs/{id}/cancel
  GET  /v1/artifacts/{id}            a stored file (video, captions)
  GET  /v1/routes                    cooldowns and blocks per platform and route
  POST /v1/routes/unblock            {platform, route} — admin keys only
  GET  /healthz, /readyz

Every request but the two health checks needs `Authorization: Bearer <key>`.
A job or file of another caller answers 404, exactly like one that does not
exist. Cookies, proxy addresses and file paths never appear in an answer.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import VERSION
from .config import PLATFORMS, Key
from .engine import Engine
from .errors import JobError
from .guard import InputError

LOG = logging.getLogger("smt.api")
MAX_BODY = 64 * 1024


def make_handler(engine: Engine):
    keys: dict[str, Key] = engine.s.keys

    class Handler(BaseHTTPRequestHandler):
        server_version = f"smtranscript/{VERSION}"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # the access log never holds a key
            LOG.info("%s %s", self.command, self.path.split("?")[0])

        def _send(self, status: int, body: dict, headers: dict | None = None):
            data = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, code: str, message: str):
            self._send(status, {"error": {"code": code, "message": message}})

        def _key(self) -> Key | None:
            auth = self.headers.get("Authorization", "")
            return keys.get(auth[7:].strip()) if auth.startswith("Bearer ") else None

        def _body(self) -> dict:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError as exc:
                raise JobError("invalid_input", "Content-Length is not a number") from exc
            # A negative length would read to the end of the stream, past the limit.
            if not 0 <= length <= MAX_BODY:
                raise JobError("invalid_input", "request body too large, or a negative length")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError as exc:
                raise JobError("invalid_input", f"body is not JSON: {exc}") from exc
            if not isinstance(body, dict):
                raise JobError("invalid_input", "body must be a JSON object")
            return body

        def _job(self, key: Key, job_id: str) -> dict | None:
            return engine.db.one("select * from job where id = ? and tenant = ?", (job_id, key.tenant))

        def _dispatch(self, method: str):
            path = self.path.split("?")[0].rstrip("/") or "/"
            if method == "GET" and path == "/healthz":
                return self._send(200, {"ok": True, "version": VERSION})
            if method == "GET" and path == "/readyz":
                return self._ready()
            key = self._key()
            if key is None:
                return self._error(401, "unauthorized", "missing or unknown API key")
            try:
                if method == "POST" and path == "/v1/jobs":
                    body = self._body()
                    submit = engine.submit_images if body.get("kind") == "images" else engine.submit_video
                    job, existing = submit(key.tenant, str(body.get("url") or ""), body, body.get("idempotency_key"))
                    return self._send(200 if existing else 202, {**engine.status(job), "existing": existing})
                if method == "POST" and path == "/v1/profile-imports":
                    body = self._body()
                    job, existing = engine.submit_profile(key.tenant, str(body.get("url") or ""), body,
                                                          body.get("idempotency_key"))
                    return self._send(200 if existing else 202, {**engine.status(job), "existing": existing})
                if method == "GET" and path == "/v1/routes":
                    return self._send(200, {"routes": engine.routes.states(admin=key.admin)})
                if method == "POST" and path == "/v1/routes/unblock":
                    if not key.admin:
                        return self._error(403, "forbidden", "unblocking a route needs an admin key")
                    body = self._body()
                    platform, route = body.get("platform"), body.get("route")
                    if platform not in PLATFORMS or route not in engine.s.routes:
                        raise JobError("invalid_input", f"platform is one of {', '.join(PLATFORMS)}; route one of "
                                                        + ", ".join(engine.s.routes))
                    return self._send(200, {"requeued_jobs": engine.unblock(platform, route)})
                m = re.fullmatch(r"/v1/jobs/(job_[0-9a-f]{24})(/result|/cancel)?", path)
                if m:
                    job = self._job(key, m.group(1))
                    if not job:
                        return self._error(404, "not_found", "no such job")
                    if m.group(2) == "/cancel" and method == "POST":
                        return self._send(200, engine.status(engine.cancel(job)))
                    if m.group(2) == "/result" and method == "GET":
                        # A profile answers as soon as it is listed: its videos then
                        # finish one by one, each with its own result.
                        listed = job["kind"] == "profile" and job["stage"] is None
                        if job["status"] in ("completed", "partial", "failed", "cancelled") or listed:
                            return self._send(200, engine.result(job))
                        status = engine.status(job)
                        return self._send(202, status, {"Retry-After": str(status.get("retry_after_s", 5))})
                    if not m.group(2) and method == "GET":
                        return self._send(200, engine.status(job))
                m = re.fullmatch(r"/v1/artifacts/(art_[0-9a-f]{24})", path)
                if m and method == "GET":
                    found = engine.artifact(key.tenant, m.group(1))
                    if not found or not found[0].is_file():
                        return self._error(404, "not_found", "no such file")
                    return self._file(*found)
                return self._error(404, "not_found", f"no route {method} {path}")
            except (JobError, InputError) as exc:
                code = exc.cls if isinstance(exc, JobError) else "invalid_input"
                return self._error(400 if code == "invalid_input" else 409, code, str(exc))
            except Exception as exc:  # noqa: BLE001
                LOG.exception("%s %s", method, path)
                return self._error(500, "internal", f"{type(exc).__name__}: {exc}")

        def _file(self, path, art):
            self.send_response(200)
            self.send_header("Content-Type", art["mime"])
            self.send_header("Content-Length", str(art["size"]))
            self.send_header("X-Checksum-SHA256", art["sha256"])
            self.end_headers()
            with path.open("rb") as f:
                shutil.copyfileobj(f, self.wfile, 1 << 20)

        def _ready(self):
            checks = {"database": True, "ffmpeg": bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))}
            try:
                engine.db.one("select 1")
            except Exception:  # noqa: BLE001
                checks["database"] = False
            free_mb = shutil.disk_usage(engine.s.data).free // (1024 * 1024)
            checks["disk"] = free_mb >= engine.s.min_free_mb
            ok = all(checks.values())
            self._send(200 if ok else 503, {"ok": ok, **checks, "free_mb": free_mb, "version": VERSION,
                                            "routes": list(engine.s.routes), "asr": engine.asr_state()})

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def serve(engine: Engine) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((engine.s.bind, engine.s.port), make_handler(engine))
    server.daemon_threads = True
    return server
