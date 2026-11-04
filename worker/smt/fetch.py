"""Getting videos, captions and profile listings from the platforms.

yt-dlp does single videos on both platforms and TikTok profiles. Instagram
profiles go through Instaloader, because yt-dlp marks its own Instagram
profile extractor as broken (yt-dlp 2026.10.26, measured 2026-10-30).

Every HTTP request either library makes is counted for the job, so "a second
identical request costs the platform nothing" is a number in the result, not
a claim.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import shutil
from pathlib import Path

from . import captions
from .errors import JobError, classify

# Lower-cased extractor names, matched as regular expressions. Anything else
# — above all yt-dlp's generic extractor, which fetches any page — is off.
EXTRACTORS = ["tiktok", r"vm\.tiktok", "tiktok:user", "instagram", "vimeo"]

VIDEO_EXT = {".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3"}
CAPTION_EXT = {".vtt": "text/vtt", ".srt": "application/x-subrip", ".json": "application/json"}


def _ydl_class():
    import yt_dlp

    class Counting(yt_dlp.YoutubeDL):
        def __init__(self, params, count):
            super().__init__(params)
            self._count = count

        def urlopen(self, req):
            self._count()
            return super().urlopen(req)

    return Counting


def adapter_version() -> str:
    try:
        import yt_dlp.version
        return f"yt-dlp {yt_dlp.version.__version__}"
    except ImportError:
        return "yt-dlp (not installed)"


def _impersonate():
    """Every request as Chrome, not only the ones yt-dlp marks for it.

    TikTok's account listing (`/api/creator/item_list/`) goes out without
    impersonation, and from this Linux image it got a 537-byte HTML page back
    every time while the same request from Windows got the list — same address
    (measured 2026-10-31, 6 of 6). With Chrome's fingerprint on every request
    it works from the image too, and single TikToks and Reels still do.
    """
    try:
        import curl_cffi  # noqa: F401 — only checks that it is there
        from yt_dlp.networking.impersonate import ImpersonateTarget
        return ImpersonateTarget.from_str("chrome")
    except ImportError:
        return None


def _opts(proxy: str | None, cookiefile: Path | None, tmp: Path | None = None, max_mb: int = 500,
          keep_media: bool = True, want_video: bool = False) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "allowed_extractors": EXTRACTORS,
        # An empty string, not None: None lets yt-dlp pick up HTTP_PROXY from
        # the environment, and the route must be exactly the one chosen.
        "proxy": proxy or "",
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "extractor_retries": 0,
        "cachedir": False,
        "max_filesize": max_mb * 1024 * 1024,
    }
    target = _impersonate()
    if target:
        opts["impersonate"] = target
    if cookiefile:
        opts["cookiefile"] = str(cookiefile)
    if tmp:
        opts.update({
            "outtmpl": {"default": str(tmp / "media.%(ext)s")},
            # Kept: the video. Not kept: only what the transcript needs — the
            # audio track where there is one (Instagram), else the smallest file.
            # A video to be described: a small copy with picture and sound.
            "format": ("bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b" if keep_media
                       else "b[height<=480][vcodec!=none][acodec!=none]/bv*[height<=480]+ba/worst[vcodec!=none]/worst"
                       if want_video else "ba/worst"),
            "merge_output_format": "mp4",
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["all"],
            "subtitlesformat": "vtt/srt/json/best",
        })
    return opts


def cookie_copy(cookies_dir: Path | None, platform: str, tmp: Path) -> Path | None:
    """A copy of the platform's cookie file, because yt-dlp writes cookies back
    into the file it was given — and the session secret stays untouched."""
    if not cookies_dir:
        return None
    src = cookies_dir / f"{platform}.txt"
    if not src.is_file():
        return None
    dst = tmp / "cookies.txt"
    shutil.copyfile(src, dst)
    return dst


class Session:
    """One yt-dlp instance for a job's whole fetch: resolve, then download.

    Instagram ties a video's media addresses to the cookies its extraction
    received. Resolved in one instance and downloaded in another, every
    download from stardawneg64 failed with HTTP 403; in one instance it works
    (measured 2026-10-31; from a Starlink address Instagram did not check).
    """

    def __init__(self, proxy: str | None, cookiefile: Path | None, max_mb: int, keep_media: bool, count,
                 want_video: bool = False):
        self._ydl = _ydl_class()(_opts(proxy, cookiefile, Path("."), max_mb, keep_media, want_video), count)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._ydl.close()
        return False

    def resolve(self, url: str) -> list[dict]:
        """The video entries behind a post: one, or several for a carousel."""
        try:
            info = self._ydl.sanitize_info(self._ydl.extract_info(url, download=False))
        except Exception as exc:  # noqa: BLE001 — classified, not swallowed
            raise classify(exc) from exc
        entries = info.get("entries") if info.get("_type") == "playlist" else [info]
        videos = [e for e in entries or [] if e and (e.get("formats") or e.get("url"))]
        if not videos:
            raise JobError("unavailable", f"{url} has no video in it")
        return videos

    def remote_source(self, info: dict) -> dict:
        """Pass only a media URL and its scoped session headers to the relay."""
        import urllib.request
        from .relay import select_source
        source = select_source(info)
        request = urllib.request.Request(source["url"])
        self._ydl.cookiejar.add_cookie_header(request)
        if request.has_header("Cookie"):
            source["headers"]["Cookie"] = request.get_header("Cookie")
        return source

    def download(self, info: dict, tmp: Path, captions_only: bool = False) -> dict:
        """Downloads the video (or its audio) and the captions of an entry
        this session resolved, without asking for the page again. With
        captions_only, the creator's captions alone: no media at all."""
        params = self._ydl.params
        params["outtmpl"]["default"] = str(tmp / "media.%(ext)s")
        params["skip_download"] = captions_only
        params["writeautomaticsub"] = not captions_only
        try:
            self._ydl.process_ie_result(copy.deepcopy(info), download=True)
        except Exception as exc:  # noqa: BLE001
            raise classify(exc) from exc
        finally:
            params["skip_download"], params["writeautomaticsub"] = False, True
        video = None
        tracks = []
        for f in sorted(tmp.iterdir()):
            if f.name.startswith("media.") and f.suffix in VIDEO_EXT and f.name.count(".") == 1:
                video = f
            elif f.suffix in CAPTION_EXT and f.name.startswith("media."):
                # media.<language>.<ext>
                tracks.append({"language_raw": f.name.split(".")[1], "ext": f.suffix[1:], "path": f})
        if captions_only:
            return {"video": None, "captions": tracks}
        if video is None:
            # yt-dlp skips a file over max_filesize without raising.
            raise JobError("too_large", f"no video file arrived (over {self._ydl.params['max_filesize'] // 2**20} MB, "
                                        "or nothing to download)")
        return {"video": video, "captions": tracks}


def session(proxy: str | None, cookiefile: Path | None, max_mb: int, keep_media: bool, count,
            want_video: bool = False) -> Session:
    return Session(proxy, cookiefile, max_mb, keep_media, count, want_video)


def caption_segments(track: dict) -> list[dict]:
    return captions.parse(Path(track["path"]).read_text(encoding="utf-8", errors="replace"), track["ext"])


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def tiktok_time(video_id: str) -> dt.datetime | None:
    """A TikTok video ID starts with its upload time: the top 32 bits are Unix
    seconds. So a date window needs no request per video."""
    try:
        return dt.datetime.fromtimestamp(int(video_id) >> 32, dt.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def tiktok_profile(account: str, limit: int, proxy: str | None, count, cookiefile=None) -> list[dict]:
    """The newest `limit` videos of an account, newest first."""
    opts = {**_opts(proxy, cookiefile), "extract_flat": "in_playlist", "playlistend": limit}
    try:
        with _ydl_class()(opts, count) as ydl:
            info = ydl.extract_info(f"https://www.tiktok.com/@{account}", download=False)
    except Exception as exc:  # noqa: BLE001
        raise classify(exc) from exc
    out = []
    for e in info.get("entries") or []:
        vid = str(e.get("id") or "")
        if vid.isdigit():
            out.append({"id": vid, "url": f"https://www.tiktok.com/@{account}/video/{vid}",
                        "taken_at": tiktok_time(vid)})
    return out


def _instaloader(proxy: str | None, count, session: Path | None, user: str | None):
    import instaloader
    from instaloader.instaloadercontext import RateController

    class Refuse(RateController):
        # Every query Instagram is asked passes here, whichever session sends
        # it, so this is where they are counted.
        def wait_before_query(self, query_type):
            count()
            return super().wait_before_query(query_type)

        # Instaloader's own answer to a 429 is to sleep, for many minutes,
        # inside the call. The wait belongs to the route budget instead.
        def handle_429(self, query_type):
            raise JobError("rate_limited", f"Instagram answered 429 to {query_type}")

    loader = instaloader.Instaloader(
        quiet=True, download_pictures=False, download_videos=False, download_video_thumbnails=False,
        save_metadata=False, compress_json=False, max_connection_attempts=1,
        rate_controller=lambda ctx: Refuse(ctx),
    )
    if session and user and session.is_file():
        loader.load_session_from_file(user, str(session))
    # Loading saved cookies replaces the HTTP session; configure the final one.
    _route_instaloader_requests(loader.context, proxy)
    query = loader.context.doc_id_graphql_query

    def metadata_query(doc_id, variables, referer=None, web_api=False):
        # 4.16 migrated timelines but left authenticated profile/post metadata
        # on the old endpoint. Use its existing web-token handling for these too.
        if loader.context.is_logged_in and doc_id in {"27937681195819736", "27128499623469141"}:
            web_api = True
        return query(doc_id, variables, referer=referer, web_api=web_api)

    loader.context.doc_id_graphql_query = metadata_query
    return loader


def _route_instaloader_requests(context, proxy: str | None):
    """Instaloader 4.16 copies sessions without their route settings."""
    def routed(session):
        session.proxies = {"http": proxy, "https": proxy} if proxy else {}
        session.trust_env = False
        return session

    get_json, get_page = context.get_json, context._get_page

    def routed_json(path, params, host="www.instagram.com", session=None,
                    response_headers=None, use_post=False):
        return get_json(path, params, host=host, session=routed(session or context._session),
                        response_headers=response_headers, use_post=use_post)

    def routed_page(session, path):
        return get_page(routed(session), path)

    routed(context._session)
    context.get_json, context._get_page = routed_json, routed_page


def _post_kind(post) -> str:
    if post.is_video:
        return "reel"
    return "carousel" if post.typename == "GraphSidecar" else "image"


def _describe_post(post) -> dict:
    return {"id": post.shortcode, "url": f"https://www.instagram.com/p/{post.shortcode}/", "kind": _post_kind(post),
            "caption": (post.caption or "")[:2000], "taken_at": post.date_utc.replace(tzinfo=dt.timezone.utc)}


# Instagram media ids carry their time: milliseconds since this epoch, above bit 23.
_IG_EPOCH_MS = 1314220021721


def _describe_node(node: dict) -> dict:
    """A post from a node of the logged-out profile timeline, without asking
    Instagram for anything more."""
    media_type = node.get("media_type")
    if media_type == 2 or node.get("product_type") == "clips":
        kind = "reel"
    elif media_type == 8 or node.get("product_type") == "carousel_container":
        kind = "carousel"
    else:
        kind = "image"
    caption = node.get("caption")
    text = caption.get("text") if isinstance(caption, dict) else caption
    try:
        taken = dt.datetime.fromtimestamp(((int(node.get("pk") or node["id"].split("_")[0]) >> 23) + _IG_EPOCH_MS) / 1000,
                                          dt.timezone.utc)
    except (KeyError, ValueError, TypeError):
        taken = None
    return {"id": node["code"], "url": f"https://www.instagram.com/p/{node['code']}/", "kind": kind,
            "caption": (text or "")[:2000], "taken_at": taken}


def _logged_out_timeline(context, account: str):
    """The account's timeline as the logged-out web app pages through it, its
    nodes kept as they come.

    Instaloader 4.16 turns every node into a full Post with one more query per
    post ("the nodes lack most of the post's metadata"), and after 200 of them
    in 11 minutes its rate limiter sleeps: listing immo.dommi stopped at post
    199 (measured 2026-10-31). What the nodes do have — code, media type,
    caption, and the time inside the id — is all a listing needs, so an
    account of 366 posts costs about 31 queries instead of 397.
    """
    from instaloader.nodeiterator import NodeIterator

    page = context.get_page_data(f"{account}/")
    if any((d.get("xig_user_by_username") or {}).get("is_private") for d in page):
        raise JobError("private", f"the account {account} is private")
    user = next((d["xig_user_by_username"] for d in page
                 if "polaris_ordered_timeline_connection" in (d.get("xig_user_by_username") or {})), None)
    if user is None:
        raise JobError("unavailable", f"no posts on the profile page of {account}")
    return NodeIterator(
        context=context,
        edge_extractor=lambda d: d["data"]["node"]["polaris_ordered_timeline_connection"],
        node_wrapper=lambda n: n,
        query_variables={"id": user["id"], "__relay_internal__pv__PolarisSEOIsCrawlerrelayprovider": False},
        query_referer=f"https://www.instagram.com/{account}/",
        doc_id="28904005159232689",
        query_hash=None,
        first_data=user["polaris_ordered_timeline_connection"],
        web_api=True,
    )


def instagram_profile(account: str, limit: int, since: dt.datetime | None, proxy: str | None, count,
                      cursor: dict | None = None, session: Path | None = None, user: str | None = None,
                      on_item=lambda item, cursor: None, include_images: bool = False) -> list[dict]:
    """Up to `limit` posts of an account, newest first: its reels, and with
    `include_images` its picture posts and carousels too, each with its kind
    and caption. `on_item` gets every post as it is found, with the cursor to
    resume after it, so a listing cut off half way has kept what it found.

    Pinned posts come first whatever their age, so one post older than `since`
    does not end the listing: only a run of them does — Instagram pins three
    at most.
    """
    import instaloader
    from instaloader.nodeiterator import FrozenNodeIterator

    try:
        loader = _instaloader(proxy, count, session, user)
        if loader.context.is_logged_in:
            owner_id = loader.context._session.cookies.get_dict().get("ds_user_id", "")
            if account.lower() == loader.context.username.lower() and owner_id.isdigit():
                # from_username first fetches an anonymous page, which may require
                # login even though the owner already supplied a valid session.
                profile = instaloader.Profile.from_id(loader.context, int(owner_id))
                if profile.username.lower() != account.lower():
                    raise JobError("login_required", "the Instagram session belongs to a different account")
            else:
                profile = instaloader.Profile.from_username(loader.context, account)
            posts, describe = profile.get_posts(), _describe_post
        else:
            posts, describe = _logged_out_timeline(loader.context, account), _describe_node
        if cursor:
            posts.thaw(FrozenNodeIterator(**cursor))
        out, older = [], 0
        for raw in posts:
            item = describe(raw)
            taken = item["taken_at"]
            if since and taken and taken < since:
                older += 1
                if older > 3:
                    break
                continue
            older = 0
            if item["kind"] == "reel" or include_images:
                out.append(item)
                on_item(item, json.loads(json.dumps(posts.freeze()._asdict(), default=str)))
            if len(out) >= limit:
                break
        return out
    except JobError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _instaloader_error(exc) from exc


def _instaloader_error(exc: Exception) -> JobError:
    name = type(exc).__name__
    if name == "ProfileNotExistsException":
        return JobError("unavailable", str(exc))
    if name == "PrivateProfileNotFollowedException":
        return JobError("private", str(exc))
    if name in ("LoginRequiredException", "LoginException"):
        return JobError("login_required", str(exc))
    return classify(exc)


def instagram_images(shortcode: str, proxy: str | None, count, session: Path | None = None,
                     user: str | None = None) -> dict:
    """The pictures of a picture post or carousel: their addresses, and what
    the post says about itself. Videos inside a carousel are counted, not
    fetched — they are not what a picture job reads."""
    import instaloader

    try:
        loader = _instaloader(proxy, count, session, user)
        post = instaloader.Post.from_shortcode(loader.context, shortcode)
        nodes = list(post.get_sidecar_nodes()) if post.typename == "GraphSidecar" else []
        if nodes:
            urls = [n.display_url for n in nodes if not n.is_video]
            videos = sum(1 for n in nodes if n.is_video)
        else:
            urls, videos = ([] if post.is_video else [post.url]), int(post.is_video)
        return {
            "images": urls[:20], "videos": videos, "caption": post.caption or "",
            "author": post.owner_username, "taken_at": post.date_utc.replace(tzinfo=dt.timezone.utc),
            "kind": _post_kind(post),
        }
    except JobError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _instaloader_error(exc) from exc


def download_image(url: str, dest: Path, proxy: str | None, count, max_mb: int = 20) -> Path:
    """One picture from Instagram's CDN, size-capped."""
    import urllib.request

    host = (urllib.request.urlparse(url).hostname or "").lower()
    if not (host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net")):
        raise JobError("parser", f"a picture address outside Instagram's CDN: {host}")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))
    count()
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as resp:
            data = resp.read(max_mb * 1024 * 1024 + 1)
    except Exception as exc:  # noqa: BLE001
        raise classify(exc) from exc
    if len(data) > max_mb * 1024 * 1024:
        raise JobError("too_large", f"a picture over {max_mb} MB")
    dest.write_bytes(data)
    return dest
