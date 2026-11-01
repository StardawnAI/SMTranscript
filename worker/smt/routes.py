"""Routes out to the platforms, and the budget each one is spent from.

A route is a way out — `direct`, or `warp` through the Cloudflare WARP proxy.
Budgets are kept per platform *and* route, because that pair is what a
platform sees: one address asking for its pages.

  * one request at a time per pair, with a minimum gap between them
  * a cooldown after a 429, stored in the database, so a restart does not
    forget it and start asking again at once
  * a block after a login challenge, lifted only by a person
  * three network failures in a row cool the pair down for a while — one
    failure says little, three say the route is in trouble

A route that is down is never swapped for another one behind the caller's
back: the job waits and says why. Falling back from WARP to the direct
address silently would spend exactly the address WARP was there to protect.
"""

from __future__ import annotations

import random
import threading
import time

from .config import PLATFORMS
from .db import Db
from .errors import JobError

COOLDOWN_DEFAULT_S = 15 * 60
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 10 * 60


class Routes:
    def __init__(self, db: Db, routes: dict[str, str | None], gaps: dict[str, float]):
        self.db = db
        self.routes = routes
        self.gaps = gaps
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}
        self._guard = threading.Lock()

    def proxy(self, route: str) -> str | None:
        if route not in self.routes:
            raise JobError("invalid_input", f"route {route!r} is not configured here")
        return self.routes[route]

    def _state(self, key: str) -> dict:
        row = self.db.one("select * from route_state where key = ?", (key,))
        return row or {"key": key, "cooldown_until": 0, "blocked": 0, "failures": 0, "last_error": None}

    def check(self, platform: str, route: str) -> None:
        """Raises when this platform may not be asked on this route right now."""
        key = f"{platform}:{route}"
        state = self._state(key)
        if state["blocked"]:
            # Not the error text itself: it may name another caller's video.
            raise JobError("challenge", f"{key} is blocked after a login check until a person unblocks it")
        wait = state["cooldown_until"] - time.time()
        if wait > 0:
            raise JobError("rate_limited", f"{key} is cooling down for {int(wait)} s more", retry_after=wait)

    def request(self, platform: str, route: str):
        """Context manager around one platform request: order, gap, budget."""
        return _Slot(self, platform, route)

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def _upsert(self, key: str, **fields) -> None:
        state = {**self._state(key), **fields, "updated_at": time.time()}
        self.db.run(
            "insert into route_state (key, cooldown_until, blocked, failures, last_error, updated_at) "
            "values (:key, :cooldown_until, :blocked, :failures, :last_error, :updated_at) "
            "on conflict (key) do update set cooldown_until = excluded.cooldown_until, "
            "blocked = excluded.blocked, failures = excluded.failures, "
            "last_error = excluded.last_error, updated_at = excluded.updated_at",
            state,
        )

    def record(self, platform: str, route: str, error: JobError | None) -> None:
        """What a request's outcome means for the pair it was spent from."""
        key = f"{platform}:{route}"
        if error is None:
            self._upsert(key, failures=0)
            return
        if error.cls == "rate_limited":
            wait = error.retry_after or COOLDOWN_DEFAULT_S
            self._upsert(key, cooldown_until=time.time() + wait, last_error=str(error)[:500])
        elif error.cls == "challenge":
            self._upsert(key, blocked=1, last_error=str(error)[:500])
        elif error.cls in ("network", "route_down", "unavailable"):
            # unavailable counts too: TikTok answers a blocked address with the
            # same status as a deleted video, and a run of them is the tell.
            failures = self._state(key)["failures"] + 1
            fields = {"failures": failures, "last_error": str(error)[:500]}
            if failures >= BREAKER_FAILURES:
                fields.update(cooldown_until=time.time() + BREAKER_COOLDOWN_S, failures=0)
            self._upsert(key, **fields)

    def unblock(self, platform: str, route: str) -> None:
        self._upsert(f"{platform}:{route}", blocked=0, cooldown_until=0, failures=0, last_error=None)

    def states(self, admin: bool = False) -> list[dict]:
        """Pauses and blocks; the error texts behind them only for an admin,
        because they come from other callers' jobs."""
        rows = {r["key"]: r for r in self.db.all("select * from route_state")}
        out = []
        for platform in PLATFORMS:
            for route in self.routes:
                state = rows.get(f"{platform}:{route}") or self._state(f"{platform}:{route}")
                wait = max(0, state["cooldown_until"] - time.time())
                out.append({
                    "platform": platform, "route": route, "blocked": bool(state["blocked"]),
                    "cooling_down_s": int(wait), "failures_in_a_row": state["failures"],
                    "last_error": state["last_error"] if admin else None,
                })
        return out


class _Slot:
    def __init__(self, routes: Routes, platform: str, route: str):
        self.routes, self.platform, self.route = routes, platform, route
        self.key = f"{platform}:{route}"

    def __enter__(self):
        self.lock = self.routes._lock(self.key)
        self.lock.acquire()
        try:
            # Checked after waiting for the lock: the request ahead may just
            # have earned this pair a cooldown.
            self.routes.check(self.platform, self.route)
        except JobError:
            self.lock.release()
            raise
        gap = self.routes.gaps.get(self.platform, 2.0)
        # A little randomness, so two workers started together do not fall
        # into step with each other.
        wait = self.routes._last.get(self.key, 0) + gap * random.uniform(1.0, 1.5) - time.time()
        if wait > 0:
            time.sleep(wait)
        return self

    def __exit__(self, *exc):
        self.routes._last[self.key] = time.time()
        self.lock.release()
        return False
