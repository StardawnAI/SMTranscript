"""What may come in: links to TikTok, Instagram and Vimeo, nothing else.

A job service that fetches whatever URL it is handed is a way into the network
it runs in. So the input is matched against the two platforms' own hosts, a
short link is followed one hop at a time with every hop checked again, and an
address that resolves into a private, loopback or link-local network is
refused even when its name looks right. yt-dlp is then limited to the
platforms' extractors (see fetch.py), so its generic extractor never runs.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

TIKTOK_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com", "vm.tiktok.com", "vt.tiktok.com"}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
VIMEO_HOSTS = {"vimeo.com", "www.vimeo.com", "player.vimeo.com"}
HOSTS = TIKTOK_HOSTS | INSTAGRAM_HOSTS | VIMEO_HOSTS

# Instagram paths that are not account names.
_IG_RESERVED = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "share", "direct", "about", "legal"}


class InputError(ValueError):
    """A link this service does not take."""


@dataclass(frozen=True)
class Target:
    platform: str  # tiktok | instagram | vimeo
    kind: str  # video | profile | short
    platform_id: str | None  # video id / shortcode, or the account name for a profile
    canonical_url: str
    fetch_url: str | None = None  # where the extractor is pointed, when not the canonical address


def _clean(url: str) -> tuple[str, str]:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in HOSTS:
        raise InputError(f"not a TikTok, Instagram or Vimeo link: {host or url!r}")
    return host, parsed.path


def _vimeo(host: str, url: str, path: str) -> Target:
    """Single videos only. An unlisted video's hash (vimeo.com/<id>/<hash>
    or ?h=<hash>) is part of its address and is kept."""
    m = (re.match(r"^/video/(\d+)/?$", path) if host == "player.vimeo.com" else None) or re.match(
        r"^/(?:channels/[\w-]+/|groups/[\w-]+/videos/|showcase/\d+/video/)?(\d+)(?:/([0-9a-f]{6,}))?/?$", path)
    if not m:
        raise InputError(f"no Vimeo video in {url!r} (accounts and showcases are not taken)")
    video_id = m.group(1)
    hash_ = m.group(2) if m.re.groups >= 2 else None
    query = re.search(r"[?&]h=([0-9a-f]{6,})", url)
    hash_ = hash_ or (query.group(1) if query else None)
    # vimeo.com pages need a login in yt-dlp 2026.10.26; the player address does
    # not (measured 2026-11-01, logged out, from the worker image).
    # An unlisted video's hash is part of its id: stored media is keyed by the id,
    # and a caller who knows only the number must not get what was fetched with the hash.
    return Target("vimeo", "video", f"{video_id}:{hash_}" if hash_ else video_id,
                  f"https://vimeo.com/{video_id}" + (f"/{hash_}" if hash_ else ""),
                  f"https://player.vimeo.com/video/{video_id}" + (f"?h={hash_}" if hash_ else ""))


def parse(url: str) -> Target:
    """Platform, kind and id from the link alone — no network."""
    host, path = _clean(url)
    if host in VIMEO_HOSTS:
        return _vimeo(host, url, path)
    if host in TIKTOK_HOSTS:
        if host in ("vm.tiktok.com", "vt.tiktok.com") or path.startswith("/t/"):
            return Target("tiktok", "short", None, f"https://{host}{path}")
        m = re.match(r"^/@([\w.-]*)/(video|photo)/(\d+)", path)
        if m:
            return Target("tiktok", "video", m.group(3), f"https://www.tiktok.com/@{m.group(1)}/{m.group(2)}/{m.group(3)}")
        m = re.match(r"^/(?:embed(?:/v2)?|v)/(\d+)", path)
        if m:
            return Target("tiktok", "video", m.group(1), f"https://www.tiktok.com/@/video/{m.group(1)}")
        m = re.match(r"^/@([\w.-]+)/?$", path)
        if m:
            return Target("tiktok", "profile", m.group(1), f"https://www.tiktok.com/@{m.group(1)}")
        raise InputError(f"no TikTok video or account in {url!r}")

    if path.startswith("/share/"):
        return Target("instagram", "short", None, f"https://www.instagram.com{path}")
    m = re.match(r"^/(?:[\w.]+/)?(?:p|reels?|tv)/([\w-]+)", path)
    if m:
        code = m.group(1)
        return Target("instagram", "video", code, f"https://www.instagram.com/p/{code}/")
    m = re.match(r"^/([\w.]+)/?(?:reels/?)?$", path)
    if m and m.group(1).lower() not in _IG_RESERVED:
        return Target("instagram", "profile", m.group(1), f"https://www.instagram.com/{m.group(1)}/")
    raise InputError(f"no Instagram post, reel or account in {url!r}")


def public_host(host: str) -> None:
    """Refuses a host whose addresses are not all public."""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise InputError(f"cannot resolve {host}: {exc}") from exc
    for info in infos:
        addr = ipaddress.ip_address(info[4][0].split("%")[0])
        if not addr.is_global:
            raise InputError(f"{host} resolves to a non-public address ({addr})")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def resolve_short(target: Target, proxy: str | None, count=lambda: None, hops: int = 5) -> Target:
    """Follows a short link to the video it stands for, checking every hop.

    `count` is called once per request, for the job's request tally.
    """
    handlers = [_NoRedirect(), urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    url = target.canonical_url
    for _ in range(hops):
        host, _path = _clean(url)
        if not proxy:
            # Through a proxy the proxy resolves the name; directly, we do.
            public_host(host)
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "Mozilla/5.0"})
        count()
        try:
            with opener.open(req, timeout=20) as resp:
                status, location = resp.status, resp.headers.get("Location")
        except urllib.error.HTTPError as exc:
            status, location = exc.code, exc.headers.get("Location")
        if status in (301, 302, 303, 307, 308) and location:
            url = urljoin(url, location)
            nxt = parse(url)
            if nxt.kind != "short":
                return nxt
            continue
        raise InputError(f"short link {target.canonical_url} did not lead to a video (HTTP {status})")
    raise InputError(f"short link {target.canonical_url} redirects more than {hops} times")
