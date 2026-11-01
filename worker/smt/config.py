"""Settings, all from the environment, read once at start.

Every limit has a default, because a job service without a size, duration or
profile limit is one long video or one large account away from a full disk.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PLATFORMS = ("tiktok", "instagram", "vimeo")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


@dataclass
class Key:
    tenant: str
    admin: bool


@dataclass
class Settings:
    data: Path
    keys: dict[str, Key]
    port: int
    bind: str
    routes: dict[str, str | None]  # route name -> proxy URL (None = direct)
    default_route: dict[str, str]  # platform -> route name
    gap_seconds: dict[str, float]  # platform -> minimum gap between its requests on one route
    fetch_workers: int
    asr_workers: int
    max_video_mb: int
    max_duration_s: int
    max_profile_items: int
    min_free_mb: int
    asr_model: str
    asr_device: str
    cookies_dir: Path | None
    instaloader_session: Path | None
    instagram_user: str | None
    info_ttl_s: int = 3600
    # Models a caller may ask for besides asr_model. Each one asked for stays
    # loaded, so the list is the memory budget, not a menu.
    asr_models: tuple = ()
    # Off on a server: only the audio is fetched, and it is deleted as soon as the
    # transcript exists. What stays is the link and the text.
    keep_media: bool = True
    # Video fetches wait while this many files already wait for Whisper.
    asr_backlog: int = 3

    @property
    def db_path(self) -> Path:
        return self.data / "smt.db"

    @property
    def media_dir(self) -> Path:
        return self.data / "media"

    @property
    def tmp_dir(self) -> Path:
        return self.data / "tmp"

    @property
    def models_dir(self) -> Path:
        return self.data / "models"


def parse_keys(raw: str) -> dict[str, Key]:
    """`name:key[:admin]`, comma-separated. The name is the tenant."""
    keys = {}
    for part in (p.strip() for p in raw.split(",")):
        if not part:
            continue
        bits = part.split(":")
        if len(bits) < 2 or not bits[0] or len(bits[1]) < 16:
            raise ValueError("SMT_API_KEYS entries are name:key[:admin], keys at least 16 characters")
        keys[bits[1]] = Key(tenant=bits[0], admin=len(bits) > 2 and bits[2] == "admin")
    return keys


def load() -> Settings:
    keys = parse_keys(os.environ.get("SMT_API_KEYS", ""))
    if not keys:
        raise SystemExit("SMT_API_KEYS is empty: set at least one name:key")
    routes: dict[str, str | None] = {"direct": None}
    if os.environ.get("SMT_WARP_PROXY"):
        routes["warp"] = os.environ["SMT_WARP_PROXY"]
    fallback = os.environ.get("SMT_DEFAULT_ROUTE", "direct")
    default_route = {p: os.environ.get(f"SMT_ROUTE_{p.upper()}", fallback) for p in PLATFORMS}
    for platform, route in default_route.items():
        if route not in routes:
            raise SystemExit(f"route {route!r} for {platform} is not configured (set SMT_WARP_PROXY for warp)")
    cookies = os.environ.get("SMT_COOKIES_DIR")
    session = os.environ.get("SMT_INSTALOADER_SESSION")
    return Settings(
        data=Path(os.environ.get("SMT_DATA", "/data")),
        keys=keys,
        port=_int("SMT_PORT", 8765),
        bind=os.environ.get("SMT_BIND", "0.0.0.0"),
        routes=routes,
        default_route=default_route,
        # Not platform limits — nobody publishes those. Starting values that a
        # measured run may lower or raise.
        gap_seconds={
            "tiktok": _float("SMT_GAP_TIKTOK", 2.0),
            "instagram": _float("SMT_GAP_INSTAGRAM", 5.0),
            "vimeo": _float("SMT_GAP_VIMEO", 2.0),
        },
        fetch_workers=_int("SMT_FETCH_WORKERS", 2),
        asr_workers=_int("SMT_ASR_WORKERS", 1),
        max_video_mb=_int("SMT_MAX_VIDEO_MB", 500),
        max_duration_s=_int("SMT_MAX_DURATION", 1800),
        max_profile_items=_int("SMT_MAX_PROFILE_ITEMS", 1000),
        min_free_mb=_int("SMT_MIN_FREE_MB", 2048),
        asr_model=os.environ.get("SMT_ASR_MODEL", "large-v3-turbo"),
        asr_device=os.environ.get("SMT_ASR_DEVICE", "auto"),
        asr_models=tuple(m.strip() for m in os.environ.get("SMT_ASR_MODELS", "small").split(",") if m.strip()),
        keep_media=os.environ.get("SMT_KEEP_MEDIA", "1") != "0",
        asr_backlog=_int("SMT_ASR_BACKLOG", 3),
        cookies_dir=Path(cookies) if cookies else None,
        instaloader_session=Path(session) if session else None,
        instagram_user=os.environ.get("SMT_INSTAGRAM_USER") or None,
    )
