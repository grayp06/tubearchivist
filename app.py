"""A deliberately small Flask front end for yt-dlp."""

from __future__ import annotations

import os
import math
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    send_from_directory,
    url_for,
)
from features import (
    add_playlist_media,
    add_subscription,
    archive_stats,
    attach_download_to_playlist,
    cancel_all_queued,
    clear_download_history,
    create_playlist,
    create_snapshot,
    delete_playlist,
    delete_subscription,
    export_archive,
    get_playlist,
    list_playlist_media,
    list_playlists,
    list_snapshots,
    list_subscriptions,
    move_playlist_media,
    parse_subscriptions_opml,
    prioritize_download,
    queue_subscription_scan,
    remove_playlist_media,
    rescan_filesystem,
    retry_download,
    save_external_playlist,
    search_media,
    set_download_playlist,
    set_media_favorite,
    set_media_tags,
    subscriptions_opml,
    update_subscription,
)
from library import (
    add_ignored,
    cancel_download,
    claim_next_download,
    count_collection_items,
    delete_media,
    delete_media_and_ignore,
    delete_empty_channel,
    enqueue_collection_items,
    enqueue_download,
    get_channel,
    get_download,
    get_media,
    get_settings,
    initialize_database,
    is_download_cancelled,
    is_ignored,
    list_channel_media,
    list_channels,
    list_downloads,
    list_ignored,
    list_media,
    recover_interrupted_downloads,
    remove_ignored,
    save_media,
    seconds_until_next_download,
    set_media_watched,
    update_channel_art,
    update_download,
    update_media_progress,
    update_settings,
)
from yt_dlp import YoutubeDL
from storage import (
    archive_download,
    cache_channel_art,
    cache_known_artwork,
    cache_playlist_thumbnail,
    cache_video_thumbnail,
    channel_art_relative_path,
    delete_channel_art,
    delete_playlist_thumbnail,
    delete_video_thumbnail,
    migrate_storage_layout,
    playlist_thumb_relative_path,
    select_channel_art,
    sync_playlist_thumbnail,
    video_thumb_relative_path,
)


APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/youtube"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/cache"))
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "/data/library.db"))
SNAPSHOT_DIR = Path(os.environ.get("SNAPSHOT_DIR", "/data/snapshots"))

APP = Flask(__name__)
APP.config["MAX_CONTENT_LENGTH"] = 8192

QUALITY_OPTIONS = {
    "480": "480p",
    "720": "720p",
    "1080": "1080p",
    "1440": "1440p",
    "2160": "2160p (4K)",
    "best": "Best available",
}

QUEUE_WAKE = threading.Event()
WORKER_LOCK = threading.Lock()
WORKER_THREAD: threading.Thread | None = None


class DownloadCancelled(Exception):
    """Stop yt-dlp when the durable queue row has been cancelled."""


def validate_url(value: object) -> str:
    """Return a normalized HTTP(S) URL or raise a user-facing error."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Enter a video URL.")
    url = value.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Enter a valid http:// or https:// video URL.")
    if len(url) > 2048:
        raise ValueError("The URL is too long.")
    return url


def format_duration(seconds: int | None) -> str:
    """Format seconds for compact cards."""
    if not seconds:
        return ""
    hours, remainder = divmod(int(seconds), 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{remaining_seconds:02d}"
    return f"{minutes}:{remaining_seconds:02d}"


def format_size(byte_count: int | None) -> str:
    """Format a byte count as MiB."""
    if byte_count is None:
        return ""
    return f"{byte_count / 1024 / 1024:.2f} MiB"


APP.jinja_env.filters["duration"] = format_duration
APP.jinja_env.filters["filesize"] = format_size


def page_url(page: int) -> str:
    """Build a pagination URL while replacing, rather than duplicating, page."""
    values = dict(request.view_args or {})
    values.update(request.args.to_dict(flat=True))
    values["page"] = page
    return url_for(request.endpoint, **values)


APP.jinja_env.globals["page_url"] = page_url


def cached_video_thumbnail(video_id: object) -> str | None:
    relative = video_thumb_relative_path(video_id)
    return f"/cache/{relative.as_posix()}" if (CACHE_DIR / relative).is_file() else None


def cached_channel_art(channel_id: object, art_type: str = "thumb") -> str | None:
    relative = channel_art_relative_path(channel_id, art_type)
    return f"/cache/{relative.as_posix()}" if (CACHE_DIR / relative).is_file() else None


def cached_playlist_thumbnail(playlist_id: object) -> str | None:
    relative = playlist_thumb_relative_path(playlist_id)
    return f"/cache/{relative.as_posix()}" if (CACHE_DIR / relative).is_file() else None


APP.jinja_env.globals.update(
    video_thumbnail_url=cached_video_thumbnail,
    channel_art_url=cached_channel_art,
    playlist_thumbnail_url=cached_playlist_thumbnail,
)


def get_downloaded_file(info: dict, downloader: YoutubeDL) -> Path:
    """Resolve the exact file yt-dlp wrote to disk."""
    candidates = [
        info.get("filepath"),
        info.get("_filename"),
        downloader.prepare_filename(info),
    ]
    candidates.extend(
        download.get("filepath") for download in info.get("requested_downloads") or []
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    video_id = str(info.get("id") or "")
    video_marker = f"[{video_id}]"
    download_cache = CACHE_DIR / "download"
    matches = [
        path
        for path in download_cache.iterdir()
        if path.is_file()
        and (path.stem == video_id or video_marker in path.name)
        and path.suffix not in {".part", ".ytdl"}
    ]
    if matches:
        return max(matches, key=lambda path: path.stat().st_mtime)
    raise FileNotFoundError("yt-dlp completed but its output file was not found.")


def delete_media_files(file_path: Path) -> None:
    """Delete one archived media file and its same-stem yt-dlp sidecars."""
    prefix = f"{file_path.stem}."
    for candidate in file_path.parent.iterdir():
        if candidate.is_file() and (
            candidate.name == file_path.name or candidate.name.startswith(prefix)
        ):
            candidate.unlink(missing_ok=True)


def quality_format(quality: str) -> str:
    """Select conservative H.264/AAC streams that mobile browsers can play."""
    video = "bestvideo[vcodec^=avc1][ext=mp4]"
    audio = "bestaudio[acodec^=mp4a][ext=m4a]"
    combined = "best[vcodec^=avc1][ext=mp4]"
    if quality == "best":
        return f"{video}+{audio}/{combined}"
    height = int(quality)
    return (
        f"{video}[height<={height}]+{audio}/"
        f"{combined}[height<={height}]"
    )


def validate_settings(collection_limit: object, quality: object) -> dict:
    """Validate editable settings and return normalized values."""
    try:
        limit = int(collection_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("Collection limit must be a whole number.") from exc
    if not 1 <= limit <= 10000:
        raise ValueError("Collection limit must be between 1 and 10,000.")
    quality_value = str(quality or "")
    if quality_value not in QUALITY_OPTIONS:
        raise ValueError("Choose a valid download quality.")
    return {"collection_limit": limit, "download_quality": quality_value}


def validate_advanced_settings(form: object) -> dict:
    """Validate optional low-memory downloader and library settings."""
    values = validate_settings(form.get("collection_limit"), form.get("download_quality"))
    try:
        page_size = int(form.get("page_size", 24))
    except (TypeError, ValueError) as exc:
        raise ValueError("Page size must be a whole number.") from exc
    if not 6 <= page_size <= 100:
        raise ValueError("Page size must be between 6 and 100.")
    try:
        subscription_delay = int(form.get("subscription_download_delay", 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("Subscription download delay must be a whole number.") from exc
    if not 0 <= subscription_delay <= 86400:
        raise ValueError(
            "Subscription download delay must be between 0 and 86,400 seconds."
        )
    languages = str(form.get("subtitle_languages", "en")).strip() or "en"
    if len(languages) > 100:
        raise ValueError("Subtitle language list is too long.")
    categories = str(form.get("sponsorblock_categories", "")).strip()
    rate_limit = str(form.get("rate_limit", "")).strip()
    if rate_limit:
        try:
            if int(rate_limit) < 1:
                raise ValueError
        except ValueError as exc:
            raise ValueError("Rate limit must be blank or a positive KiB/s value.") from exc
    output_template = str(
        form.get("output_template", "%(title).180B [%(id)s].%(ext)s")
    ).strip()
    if not output_template or any(marker in output_template for marker in ("/", "\\", "..")):
        raise ValueError("Output template must be a filename without folders.")
    cookie_path = Path(
        str(form.get("cookie_path", DATABASE_PATH.parent / "cookies.txt")).strip()
    ).resolve()
    try:
        cookie_path.relative_to(DATABASE_PATH.parent.resolve())
    except ValueError as exc:
        raise ValueError(
            "Cookie file must be stored inside the application data folder."
        ) from exc
    values.update(
        {
            "page_size": page_size,
            "download_subtitles": int(form.get("download_subtitles") == "1"),
            "subtitle_languages": languages,
            "write_info_json": int(form.get("write_info_json") == "1"),
            "sponsorblock_categories": categories,
            "cookie_path": str(cookie_path),
            "rate_limit": rate_limit,
            "subscription_download_delay": subscription_delay,
            "output_template": output_template,
            "download_comments": int(form.get("download_comments") == "1"),
        }
    )
    return values


def yt_dlp_download_options(download_id: int, state: dict[str, float]) -> dict:
    """Build yt-dlp options from SQLite settings only when work is claimed."""
    settings = get_settings(DATABASE_PATH)
    (CACHE_DIR / "download").mkdir(parents=True, exist_ok=True)
    options: dict = {
        "format": quality_format(settings["download_quality"]),
        "merge_output_format": "mp4",
        "format_sort": [
            "vcodec:h264",
            "lang",
            "quality",
            "res",
            "fps",
            "hdr:12",
            "acodec:aac",
        ],
        "noplaylist": True,
        "outtmpl": str(CACHE_DIR / "download" / "%(id)s.mp4"),
        "progress_hooks": [lambda data: progress_hook(download_id, state, data)],
        "postprocessor_hooks": [lambda _data: progress_hook(download_id, state, {})],
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "getcomments": settings["download_comments"],
    }
    if settings["download_subtitles"]:
        options.update(
            {
                "writesubtitles": True,
                "writeautomaticsub": True,
                "subtitleslangs": [
                    item.strip()
                    for item in settings["subtitle_languages"].split(",")
                    if item.strip()
                ],
                "subtitlesformat": "vtt/best",
            }
        )
    cookie_path = Path(settings["cookie_path"])
    if cookie_path.is_file():
        options["cookiefile"] = str(cookie_path)
    if settings["rate_limit"]:
        options["ratelimit"] = int(settings["rate_limit"]) * 1024
    postprocessors = []
    if settings["write_info_json"]:
        postprocessors.append({"key": "FFmpegMetadata", "add_chapters": True})
    categories = [
        item.strip()
        for item in settings["sponsorblock_categories"].split(",")
        if item.strip()
    ]
    if categories:
        postprocessors.extend(
            [
                {"key": "SponsorBlock", "categories": categories},
                {"key": "ModifyChapters", "remove_sponsor_segments": categories},
            ]
        )
    if postprocessors:
        options["postprocessors"] = postprocessors
    return options


def hydrate_missing_channel_thumbnails(limit: int = 5) -> None:
    """Fetch and cache TubeArchivist's three channel artwork variants."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 0,
        "skip_download": True,
    }
    channels = list_channels(DATABASE_PATH)
    attempted = 0
    for channel in channels:
        expected = [
            CACHE_DIR / channel_art_relative_path(channel["channel_id"], art_type)
            for art_type in ("thumb", "banner", "tvart")
        ]
        if (
            all(path.is_file() for path in expected)
            and channel.get("thumbnail_url")
            and channel.get("banner_url")
            and channel.get("tvart_url")
        ):
            continue
        if attempted >= limit:
            break
        attempted += 1
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(channel["source_url"], download=False)
            art = select_channel_art(info)
            update_channel_art(
                DATABASE_PATH,
                channel["id"],
                thumbnail_url=art["thumb"],
                banner_url=art["banner"],
                tvart_url=art["tvart"],
                description=info.get("description"),
                subscriber_count=info.get("channel_follower_count") or 0,
                tags=info.get("tags") or [],
            )
            cache_channel_art(
                CACHE_DIR,
                channel["channel_id"],
                art["thumb"],
                art["banner"],
                art["tvart"],
                skip_existing=False,
            )
        except Exception as exc:
            print(
                f"Unable to fetch thumbnail for channel {channel['id']}: {exc}",
                flush=True,
            )


def progress_hook(download_id: int, state: dict[str, float], data: dict) -> None:
    """Persist throttled yt-dlp progress updates in SQLite."""
    if is_download_cancelled(DATABASE_PATH, download_id):
        raise DownloadCancelled("Download cancelled by user.")
    if data.get("status") != "downloading":
        return
    now = time.monotonic()
    if now - state["last_write"] < 0.5:
        return
    state["last_write"] = now
    total = data.get("total_bytes") or data.get("total_bytes_estimate")
    downloaded = data.get("downloaded_bytes") or 0
    progress = round(downloaded / total * 100, 1) if total else 0
    update_download(
        DATABASE_PATH,
        download_id,
        progress=progress,
        percent=data.get("_percent_str", "").strip(),
        speed=data.get("_speed_str", "").strip(),
        eta=data.get("_eta_str", "").strip(),
    )


def looks_like_single_video(url: str) -> bool:
    """Recognize YouTube URL shapes that should bypass collection expansion."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    if host == "youtu.be" and path:
        return True
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "/watch":
            return "list" not in parse_qs(parsed.query)
        return path.startswith(("/shorts/", "/live/", "/embed/"))
    return False


def video_identity(url: str) -> tuple[str, str | None]:
    """Return a stable extractor identity for recognized YouTube video URLs."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    path_parts = [part for part in parsed.path.split("/") if part]
    if host == "youtu.be" and path_parts:
        return "Youtube", path_parts[0]
    if host in {"youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path.rstrip("/") == "/watch":
            return "Youtube", (parse_qs(parsed.query).get("v") or [None])[0]
        if len(path_parts) >= 2 and path_parts[0] in {"shorts", "live", "embed"}:
            return "Youtube", path_parts[1]
    return "url", None


def collection_kind(url: str) -> str:
    """Classify a collection URL for persistent queue display."""
    path = urlparse(url).path.lower()
    if path == "/playlist":
        return "playlist"
    if any(marker in path for marker in ("/channel/", "/user/", "/c/", "/@")):
        return "channel"
    return "playlist"


def collection_probe_url(url: str) -> str:
    """Point bare YouTube channel URLs at their uploads tab."""
    if collection_kind(url) != "channel":
        return url
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    if path.endswith(("/videos", "/streams", "/shorts")):
        return url
    return parsed._replace(path=f"{path}/videos").geturl()


def collection_entry_url(entry: dict) -> str | None:
    """Resolve a flat yt-dlp collection entry to a downloadable HTTP URL."""
    for key in ("webpage_url", "original_url", "url"):
        value = entry.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    video_id = entry.get("id")
    extractor = str(entry.get("extractor_key") or entry.get("extractor") or "")
    if video_id and "youtube" in extractor.lower():
        return f"https://www.youtube.com/watch?v={video_id}"
    return None


def download_video(download: dict) -> None:
    """Download one known video queue row and persist its media record."""
    download_id = int(download["id"])
    url = str(download["source_url"])
    progress_state = {"last_write": 0.0}
    options = yt_dlp_download_options(download_id, progress_state)
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        if is_download_cancelled(DATABASE_PATH, download_id):
            raise DownloadCancelled("Download cancelled by user.")
        downloaded_path = get_downloaded_file(info, downloader).resolve()
        file_path = archive_download(
            downloaded_path,
            DOWNLOAD_DIR,
            info.get("channel_id") or info.get("uploader_id"),
            info.get("id"),
        )
        media_id = save_media(DATABASE_PATH, url, info, file_path)
        attach_download_to_playlist(DATABASE_PATH, download_id, media_id)
        cache_video_thumbnail(CACHE_DIR, info.get("id"), info.get("thumbnail"))
    update_download(
        DATABASE_PATH,
        download_id,
        kind="video",
        status="complete",
        progress=100,
        percent="100%",
        speed=None,
        eta=None,
        title=info.get("title") or url,
        filename=file_path.name,
        file_path=str(file_path),
        media_id=media_id,
        error=None,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    hydrate_missing_channel_thumbnails()


def expand_collection(download: dict, info: dict) -> None:
    """Expand a flat yt-dlp collection into durable child video rows."""
    download_id = int(download["id"])
    kind = collection_kind(str(download["source_url"]))
    title = str(info.get("title") or info.get("playlist_title") or download["source_url"])
    settings = get_settings(DATABASE_PATH)
    limit = settings["collection_limit"]
    defer_seconds = (
        settings["subscription_download_delay"]
        if download.get("subscription_id") is not None
        else 0
    )
    update_download(
        DATABASE_PATH,
        download_id,
        kind=kind,
        title=title,
        percent="Expanding",
        progress=0,
    )
    if kind == "playlist":
        playlist_id = save_external_playlist(
            DATABASE_PATH,
            str(download["source_url"]),
            info,
        )
        set_download_playlist(DATABASE_PATH, download_id, playlist_id)
        playlist_external_id = str(info.get("id") or info.get("playlist_id") or "")
        if playlist_external_id:
            thumbnails = info.get("thumbnails") or []
            source = thumbnails[-1].get("url") if thumbnails else info.get("thumbnail")
            cache_playlist_thumbnail(CACHE_DIR, playlist_external_id, source)

    position = 0
    total_items = count_collection_items(DATABASE_PATH, download_id)
    batch: list[dict] = []
    for entry in info.get("entries") or []:
        if total_items >= limit:
            break
        position += 1
        if not isinstance(entry, dict):
            continue
        entry_url = collection_entry_url(entry)
        if entry_url is None:
            continue
        batch.append(
            {
                "source_url": entry_url,
                "source_position": position,
                "title": entry.get("title"),
                "extractor": str(
                    entry.get("extractor_key")
                    or entry.get("extractor")
                    or video_identity(entry_url)[0]
                ),
                "video_id": entry.get("id") or video_identity(entry_url)[1],
            }
        )
        if len(batch) >= min(100, limit - total_items):
            enqueue_collection_items(
                DATABASE_PATH,
                download_id,
                batch,
                defer_seconds=defer_seconds,
            )
            batch.clear()
            total_items = count_collection_items(DATABASE_PATH, download_id)
            update_download(
                DATABASE_PATH,
                download_id,
                item_count=total_items,
                percent=f"{total_items} new videos (limit {limit})",
            )
    if batch and total_items < limit:
        enqueue_collection_items(
            DATABASE_PATH,
            download_id,
            batch,
            defer_seconds=defer_seconds,
        )
    total_items = count_collection_items(DATABASE_PATH, download_id)

    update_download(
        DATABASE_PATH,
        download_id,
        status="complete",
        progress=100,
        percent=f"{total_items} new videos (limit {limit})",
        item_count=total_items,
        error=None,
        finished_at=datetime.now(timezone.utc).isoformat(),
    )
    QUEUE_WAKE.set()


def run_download(download: dict) -> None:
    """Dispatch one claimed SQLite row as a video or expandable collection."""
    download_id = int(download["id"])
    url = str(download["source_url"])
    try:
        if download.get("kind") == "video" or looks_like_single_video(url):
            download_video(download)
            return

        options = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": "in_playlist",
            "lazy_playlist": True,
            "skip_download": True,
        }
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(collection_probe_url(url), download=False)
        if info.get("_type") == "playlist" or info.get("entries") is not None:
            expand_collection(download, info)
        else:
            download_video(download)
    except DownloadCancelled:
        return
    except Exception as exc:  # yt-dlp may wrap hook exceptions as DownloadError
        if is_download_cancelled(DATABASE_PATH, download_id):
            return
        update_download(
            DATABASE_PATH,
            download_id,
            status="error",
            error=str(exc),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )


def download_worker() -> None:
    """Continuously claim SQLite queue rows in FIFO order."""
    cache_known_artwork(DATABASE_PATH, CACHE_DIR)
    hydrate_missing_channel_thumbnails()
    while True:
        QUEUE_WAKE.clear()
        download = claim_next_download(DATABASE_PATH)
        if download is None:
            QUEUE_WAKE.wait(timeout=seconds_until_next_download(DATABASE_PATH))
            continue
        run_download(download)


def start_download_worker() -> None:
    """Start one process-local worker; queue ownership remains in SQLite."""
    global WORKER_THREAD
    with WORKER_LOCK:
        if WORKER_THREAD is not None and WORKER_THREAD.is_alive():
            return
        WORKER_THREAD = threading.Thread(
            target=download_worker,
            name="sqlite-download-worker",
            daemon=True,
        )
        WORKER_THREAD.start()


def enqueue_url(value: object) -> dict:
    """Validate and append a URL to the SQLite queue."""
    url = validate_url(value)
    kind = "video" if looks_like_single_video(url) else "auto"
    if kind == "video":
        extractor, video_id = video_identity(url)
        if is_ignored(DATABASE_PATH, url, extractor, video_id):
            raise ValueError(
                "This video is ignored. Remove it from Settings to queue it again."
            )
    download = enqueue_download(DATABASE_PATH, url, kind=kind)
    QUEUE_WAKE.set()
    return download


@APP.after_request
def add_security_headers(response: Response) -> Response:
    """Apply the small app's cache and browser security policy."""
    if request.path.startswith("/api/media/") and response.status_code < 400:
        response.headers["Cache-Control"] = "private, max-age=3600"
    else:
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' https: data:; "
        "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "connect-src 'self'; media-src 'self'"
    )
    return response


@APP.get("/")
def home_page() -> str:
    """Show the most recently downloaded videos."""
    settings = get_settings(DATABASE_PATH)
    result = search_media(
        DATABASE_PATH,
        query=request.args.get("q", ""),
        watched=request.args.get("watched", "all"),
        favorite=request.args.get("favorite") == "1",
        video_type=request.args.get("type", "all"),
        sort=request.args.get("sort", "downloaded"),
        order=request.args.get("order", "desc"),
        page=request.args.get("page", 1, type=int),
        page_size=settings["page_size"],
    )
    return render_template(
        "home.html",
        videos=result["items"],
        result=result,
        playlists=list_playlists(DATABASE_PATH),
    )


@APP.get("/channels")
def channels_page() -> str:
    """Show normalized channel records and their library counts."""
    return render_template(
        "channels.html",
        channels=list_channels(DATABASE_PATH),
        subscriptions=list_subscriptions(DATABASE_PATH),
    )


@APP.get("/channels/<int:channel_id>")
def channel_page(channel_id: int) -> str:
    """Show only videos belonging to one normalized channel."""
    channel = get_channel(DATABASE_PATH, channel_id)
    if channel is None:
        abort(404)
    return render_template(
        "channel.html",
        channel=channel,
        videos=list_channel_media(DATABASE_PATH, channel_id),
        playlists=list_playlists(DATABASE_PATH),
    )


def _delete_channel(channel_id: int, *, ignore: bool) -> Response:
    """Delete a channel and its media, optionally adding videos to ignore."""
    channel = get_channel(DATABASE_PATH, channel_id)
    if channel is None:
        abort(404)
    videos = list_channel_media(DATABASE_PATH, channel_id)
    download_root = DOWNLOAD_DIR.resolve()
    paths = []
    for video in videos:
        path = Path(video["file_path"]).resolve()
        try:
            path.relative_to(download_root)
        except ValueError:
            abort(400)
        paths.append((video, path))
    for video, path in paths:
        delete_operation = delete_media_and_ignore if ignore else delete_media
        delete_operation(DATABASE_PATH, video["id"])
        delete_media_files(path)
        delete_video_thumbnail(CACHE_DIR, video["video_id"])
    if not delete_empty_channel(DATABASE_PATH, channel_id):
        abort(409)
    delete_channel_art(CACHE_DIR, channel["channel_id"])
    return redirect(url_for("channels_page"), code=303)


@APP.post("/channels/<int:channel_id>/delete")
def delete_channel_only_page(channel_id: int) -> Response:
    """Delete a channel while allowing all of its videos to be downloaded again."""
    return _delete_channel(channel_id, ignore=False)


@APP.post("/channels/<int:channel_id>/delete-ignore")
def delete_channel_page(channel_id: int) -> Response:
    """Delete a channel and retain ignore identities for all of its videos."""
    return _delete_channel(channel_id, ignore=True)


@APP.get("/search")
def search_page() -> str:
    """Search archived metadata, tags, descriptions, and indexed subtitles."""
    settings = get_settings(DATABASE_PATH)
    result = search_media(
        DATABASE_PATH,
        query=request.args.get("q", ""),
        channel_id=request.args.get("channel", type=int),
        watched=request.args.get("watched", "all"),
        favorite=request.args.get("favorite") == "1",
        video_type=request.args.get("type", "all"),
        sort=request.args.get("sort", "downloaded"),
        order=request.args.get("order", "desc"),
        page=request.args.get("page", 1, type=int),
        page_size=settings["page_size"],
    )
    return render_template(
        "search.html",
        videos=result["items"],
        result=result,
        channels=list_channels(DATABASE_PATH),
        playlists=list_playlists(DATABASE_PATH),
    )


@APP.get("/playlists")
def playlists_page() -> str:
    return render_template("playlists.html", playlists=list_playlists(DATABASE_PATH))


@APP.post("/playlists")
def create_playlist_page() -> tuple[str, int] | Response:
    try:
        playlist = create_playlist(
            DATABASE_PATH,
            str(request.form.get("name", "")),
            str(request.form.get("description", "")),
        )
        sync_playlist_thumbnail(DATABASE_PATH, CACHE_DIR, playlist["id"])
    except ValueError as exc:
        return render_template("playlists.html", playlists=list_playlists(DATABASE_PATH), error=str(exc)), 400
    return redirect(url_for("playlists_page"), code=303)


@APP.get("/playlists/<int:playlist_id>")
def playlist_page(playlist_id: int) -> str:
    playlist = get_playlist(DATABASE_PATH, playlist_id)
    if playlist is None:
        abort(404)
    return render_template(
        "playlist.html",
        playlist=playlist,
        videos=list_playlist_media(DATABASE_PATH, playlist_id),
    )


@APP.post("/playlists/<int:playlist_id>/delete")
def delete_playlist_page(playlist_id: int) -> Response:
    playlist = get_playlist(DATABASE_PATH, playlist_id)
    if playlist is None:
        abort(404)
    if not delete_playlist(DATABASE_PATH, playlist_id):
        abort(404)
    delete_playlist_thumbnail(CACHE_DIR, playlist["playlist_id"])
    return redirect(url_for("playlists_page"), code=303)


@APP.post("/playlists/<int:playlist_id>/videos/<int:media_id>")
def add_to_playlist_page(playlist_id: int, media_id: int) -> Response:
    if not add_playlist_media(DATABASE_PATH, playlist_id, media_id):
        abort(404)
    sync_playlist_thumbnail(DATABASE_PATH, CACHE_DIR, playlist_id)
    target = request.form.get("next") or url_for("playlist_page", playlist_id=playlist_id)
    return redirect(target if target.startswith("/") and not target.startswith("//") else "/", code=303)


@APP.post("/playlists/<int:playlist_id>/videos/<int:media_id>/remove")
def remove_from_playlist_page(playlist_id: int, media_id: int) -> Response:
    if not remove_playlist_media(DATABASE_PATH, playlist_id, media_id):
        abort(404)
    sync_playlist_thumbnail(DATABASE_PATH, CACHE_DIR, playlist_id)
    return redirect(url_for("playlist_page", playlist_id=playlist_id), code=303)


@APP.post("/playlists/<int:playlist_id>/videos/<int:media_id>/move")
def move_playlist_video_page(playlist_id: int, media_id: int) -> Response:
    if not move_playlist_media(
        DATABASE_PATH,
        playlist_id,
        media_id,
        str(request.form.get("direction", "")),
    ):
        abort(404)
    sync_playlist_thumbnail(DATABASE_PATH, CACHE_DIR, playlist_id)
    return redirect(url_for("playlist_page", playlist_id=playlist_id), code=303)


@APP.post("/subscriptions")
def add_subscription_page() -> Response:
    url = validate_url(request.form.get("url"))
    kind = collection_kind(url)
    add_subscription(DATABASE_PATH, url, kind, request.form.get("name"))
    QUEUE_WAKE.set()
    return redirect(url_for("channels_page"), code=303)


@APP.post("/subscriptions/<int:subscription_id>/toggle")
def toggle_subscription_page(subscription_id: int) -> Response:
    enabled = request.form.get("enabled") == "1"
    if not update_subscription(DATABASE_PATH, subscription_id, enabled=enabled):
        abort(404)
    return redirect(url_for("channels_page"), code=303)


@APP.post("/subscriptions/<int:subscription_id>/delete")
def delete_subscription_page(subscription_id: int) -> Response:
    if not delete_subscription(DATABASE_PATH, subscription_id):
        abort(404)
    return redirect(url_for("channels_page"), code=303)


@APP.post("/subscriptions/scan")
def scan_subscriptions_page() -> Response:
    queued = queue_subscription_scan(DATABASE_PATH)
    if queued:
        QUEUE_WAKE.set()
    return redirect(url_for("downloads_page", scanned=queued), code=303)


@APP.get("/stats")
def stats_page() -> str:
    return render_template("stats.html", stats=archive_stats(DATABASE_PATH))


@APP.get("/downloads")
def downloads_page() -> str:
    """Show the durable queue grouped by state."""
    downloads = list_downloads(DATABASE_PATH)
    queue_query = request.args.get("q", "").strip().casefold()
    if queue_query:
        downloads = [
            item
            for item in downloads
            if queue_query
            in f"{item.get('title') or ''} {item.get('source_url') or ''} {item.get('error') or ''}".casefold()
        ]
    return render_template(
        "downloads.html",
        active=[item for item in downloads if item["status"] == "downloading"],
        queued=[item for item in downloads if item["status"] == "queued"],
        history=[
            item
            for item in downloads
            if item["status"] in {"complete", "error", "cancelled"}
        ],
        queued_id=request.args.get("queued", type=int),
        scanned=request.args.get("scanned", type=int),
    )


@APP.post("/downloads")
def add_download_page() -> tuple[str, int] | Response:
    """Append a form submission to the persistent queue."""
    try:
        submitted_url = validate_url(request.form.get("url"))
        download = enqueue_url(submitted_url)
        if request.form.get("subscribe") == "1" and download["kind"] != "video":
            add_subscription(
                DATABASE_PATH,
                submitted_url,
                collection_kind(submitted_url),
            )
    except ValueError as exc:
        downloads = list_downloads(DATABASE_PATH)
        return (
            render_template(
                "downloads.html",
                active=[item for item in downloads if item["status"] == "downloading"],
                queued=[item for item in downloads if item["status"] == "queued"],
                history=[
                    item
                    for item in downloads
                    if item["status"] in {"complete", "error", "cancelled"}
                ],
                queued_id=None,
                scanned=None,
                error=str(exc),
            ),
            400,
        )
    return redirect(url_for("downloads_page", queued=download["id"]), code=303)


@APP.post("/downloads/<int:download_id>/cancel")
def cancel_download_page(download_id: int) -> Response:
    """Cancel one queued or active video without affecting its siblings."""
    if not cancel_download(DATABASE_PATH, download_id):
        abort(404)
    QUEUE_WAKE.set()
    return redirect(url_for("downloads_page"), code=303)


@APP.post("/downloads/<int:download_id>/ignore")
def ignore_download_page(download_id: int) -> Response:
    """Cancel a queued video and prevent future queue expansion from adding it."""
    download = get_download(DATABASE_PATH, download_id)
    if download is None or download["kind"] != "video":
        abort(404)
    if not cancel_download(DATABASE_PATH, download_id):
        abort(404)
    extractor, video_id = video_identity(str(download["source_url"]))
    add_ignored(
        DATABASE_PATH,
        str(download["source_url"]),
        extractor,
        video_id,
        download.get("title"),
    )
    QUEUE_WAKE.set()
    return redirect(url_for("downloads_page"), code=303)


@APP.post("/downloads/<int:download_id>/priority")
def prioritize_download_page(download_id: int) -> Response:
    if not prioritize_download(DATABASE_PATH, download_id):
        abort(404)
    QUEUE_WAKE.set()
    return redirect(url_for("downloads_page"), code=303)


@APP.post("/downloads/<int:download_id>/retry")
def retry_download_page(download_id: int) -> Response:
    if not retry_download(DATABASE_PATH, download_id):
        abort(404)
    QUEUE_WAKE.set()
    return redirect(url_for("downloads_page"), code=303)


@APP.post("/downloads/history/clear")
def clear_download_history_page() -> Response:
    clear_download_history(DATABASE_PATH)
    return redirect(url_for("downloads_page"), code=303)


@APP.post("/downloads/queued/cancel")
def cancel_all_downloads_page() -> Response:
    cancel_all_queued(DATABASE_PATH)
    QUEUE_WAKE.set()
    return redirect(url_for("downloads_page"), code=303)


@APP.get("/settings")
def settings_page() -> str:
    """Show persistent downloader settings."""
    return render_template(
        "settings.html",
        settings=get_settings(DATABASE_PATH),
        quality_options=QUALITY_OPTIONS,
        ignored=list_ignored(DATABASE_PATH),
        snapshots=list_snapshots(SNAPSHOT_DIR),
        stats=archive_stats(DATABASE_PATH),
        saved=request.args.get("saved") == "1",
        action=request.args.get("action"),
    )


@APP.post("/settings")
def update_settings_page() -> tuple[str, int] | Response:
    """Validate and persist downloader settings."""
    try:
        settings = validate_advanced_settings(request.form)
    except ValueError as exc:
        current = get_settings(DATABASE_PATH)
        for key in current:
            if key in request.form:
                current[key] = request.form.get(key, "")
        for key in ("download_subtitles", "write_info_json", "download_comments"):
            current[key] = request.form.get(key) == "1"
        return (
            render_template(
                "settings.html",
                settings=current,
                quality_options=QUALITY_OPTIONS,
                ignored=list_ignored(DATABASE_PATH),
                snapshots=list_snapshots(SNAPSHOT_DIR),
                stats=archive_stats(DATABASE_PATH),
                saved=False,
                error=str(exc),
            ),
            400,
        )
    update_settings(DATABASE_PATH, settings)
    return redirect(url_for("settings_page", saved=1), code=303)


@APP.post("/settings/cookies")
def upload_cookies_page() -> Response:
    upload = request.files.get("cookies")
    if upload is None or not upload.filename:
        abort(400)
    content = upload.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        abort(413)
    cookie_path = Path(get_settings(DATABASE_PATH)["cookie_path"])
    cookie_path.parent.mkdir(parents=True, exist_ok=True)
    cookie_path.write_bytes(content)
    return redirect(url_for("settings_page", action="cookies"), code=303)


@APP.post("/settings/rescan")
def rescan_page() -> Response:
    result = rescan_filesystem(DATABASE_PATH, DOWNLOAD_DIR)
    return redirect(
        url_for(
            "settings_page",
            action=f"rescan:{result['added']}:{result['removed_missing']}",
        ),
        code=303,
    )


@APP.post("/settings/snapshots")
def create_snapshot_page() -> Response:
    create_snapshot(DATABASE_PATH, SNAPSHOT_DIR)
    return redirect(url_for("settings_page", action="snapshot"), code=303)


@APP.get("/settings/snapshots/<path:name>")
def download_snapshot_page(name: str) -> Response:
    if not name.startswith("library-") or not name.endswith(".db"):
        abort(404)
    return send_from_directory(SNAPSHOT_DIR, name, as_attachment=True)


@APP.get("/settings/export.json")
def export_archive_page() -> Response:
    payload = json.dumps(export_archive(DATABASE_PATH), ensure_ascii=False, default=str)
    return Response(
        payload,
        mimetype="application/json",
        headers={"Content-Disposition": "attachment; filename=tubearchivist-lite-export.json"},
    )


@APP.get("/settings/subscriptions.opml")
def export_subscriptions_page() -> Response:
    return Response(
        subscriptions_opml(DATABASE_PATH),
        mimetype="text/xml",
        headers={"Content-Disposition": "attachment; filename=subscriptions.opml"},
    )


@APP.post("/settings/subscriptions.opml")
def import_subscriptions_page() -> Response:
    upload = request.files.get("opml")
    if upload is None or not upload.filename:
        abort(400)
    content = upload.read(1024 * 1024 + 1)
    if len(content) > 1024 * 1024:
        abort(413)
    try:
        items = parse_subscriptions_opml(content)
        for item in items:
            add_subscription(
                DATABASE_PATH,
                validate_url(item["source_url"]),
                item["kind"],
                item["name"],
            )
    except ValueError:
        abort(400)
    return redirect(url_for("settings_page", action=f"opml:{len(items)}"), code=303)


@APP.post("/settings/ignored/<int:ignored_id>/remove")
def remove_ignored_page(ignored_id: int) -> Response:
    """Allow a video from the ignore list to be queued again."""
    if not remove_ignored(DATABASE_PATH, ignored_id):
        abort(404)
    return redirect(url_for("settings_page"), code=303)


@APP.get("/watch/<int:media_id>")
def watch_page(media_id: int) -> str:
    """Show the player for one downloaded video."""
    video = get_media(DATABASE_PATH, media_id)
    if video is None:
        abort(404)
    return render_template(
        "watch.html",
        video=video,
        playlists=list_playlists(DATABASE_PATH),
    )


@APP.post("/videos/<int:media_id>/watched")
def watched_api(media_id: int) -> Response:
    """Toggle watched state and return to a safe local page."""
    watched = request.form.get("watched") == "1"
    if not set_media_watched(DATABASE_PATH, media_id, watched):
        abort(404)
    target = request.form.get("next") or url_for("home_page")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("home_page")
    return redirect(target, code=303)


@APP.post("/videos/<int:media_id>/favorite")
def favorite_video_page(media_id: int) -> Response:
    if not set_media_favorite(DATABASE_PATH, media_id, request.form.get("favorite") == "1"):
        abort(404)
    target = request.form.get("next") or url_for("home_page")
    return redirect(target if target.startswith("/") and not target.startswith("//") else "/", code=303)


@APP.post("/videos/<int:media_id>/tags")
def tag_video_page(media_id: int) -> Response:
    if not set_media_tags(DATABASE_PATH, media_id, str(request.form.get("tags", ""))):
        abort(404)
    return redirect(url_for("watch_page", media_id=media_id), code=303)


@APP.post("/videos/<int:media_id>/refresh")
def refresh_video_page(media_id: int) -> Response:
    """Manually refresh one video's metadata without downloading it again."""
    video = get_media(DATABASE_PATH, media_id)
    if video is None:
        abort(404)
    file_path = Path(video["file_path"]).resolve()
    try:
        file_path.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        abort(400)
    options: dict = {"quiet": True, "no_warnings": True, "skip_download": True}
    cookie_path = Path(get_settings(DATABASE_PATH)["cookie_path"])
    if cookie_path.is_file():
        options["cookiefile"] = str(cookie_path)
    try:
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(video["webpage_url"] or video["source_url"], download=False)
        save_media(DATABASE_PATH, video["source_url"], info, file_path)
        cache_video_thumbnail(
            CACHE_DIR,
            info.get("id") or video["video_id"],
            info.get("thumbnail"),
        )
    except Exception as exc:
        return render_template(
            "watch.html",
            video=video,
            playlists=list_playlists(DATABASE_PATH),
            error=f"Metadata refresh failed: {exc}",
        ), 502
    return redirect(url_for("watch_page", media_id=media_id), code=303)


@APP.post("/videos/<int:media_id>/delete-ignore")
def delete_ignore_page(media_id: int) -> Response:
    """Delete one media file and library row while retaining its ignore identity."""
    video = get_media(DATABASE_PATH, media_id)
    if video is None:
        abort(404)
    file_path = Path(video["file_path"]).resolve()
    try:
        file_path.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        abort(400)
    if delete_media_and_ignore(DATABASE_PATH, media_id) is None:
        abort(404)
    delete_media_files(file_path)
    delete_video_thumbnail(CACHE_DIR, video["video_id"])
    target = request.form.get("next") or url_for("home_page")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("home_page")
    return redirect(target, code=303)


@APP.post("/videos/<int:media_id>/delete")
def delete_only_page(media_id: int) -> Response:
    """Delete one media file and library row without ignoring the video."""
    video = get_media(DATABASE_PATH, media_id)
    if video is None:
        abort(404)
    file_path = Path(video["file_path"]).resolve()
    try:
        file_path.relative_to(DOWNLOAD_DIR.resolve())
    except ValueError:
        abort(400)
    if delete_media(DATABASE_PATH, media_id) is None:
        abort(404)
    delete_media_files(file_path)
    delete_video_thumbnail(CACHE_DIR, video["video_id"])
    target = request.form.get("next") or url_for("home_page")
    if not target.startswith("/") or target.startswith("//"):
        target = url_for("home_page")
    return redirect(target, code=303)


@APP.post("/api/media/<int:media_id>/progress")
def media_progress_api(media_id: int) -> Response:
    """Persist event-driven player progress using server-side thresholds."""
    payload = request.get_json(silent=True)
    try:
        if not isinstance(payload, dict):
            raise ValueError
        current_time = float(payload["current_time"])
        duration = float(payload["duration"])
        if (
            not math.isfinite(current_time)
            or not math.isfinite(duration)
            or current_time < 0
            or duration <= 0
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify({"error": "Valid current_time and duration are required."}), 400

    progress = update_media_progress(
        DATABASE_PATH,
        media_id,
        current_time / duration * 100,
    )
    if progress is None:
        return jsonify({"error": "Video not found."}), 404
    return jsonify(progress)


@APP.get("/api/jobs")
def jobs_api() -> Response:
    """Retain the small JSON queue endpoint for diagnostics."""
    return jsonify(list_downloads(DATABASE_PATH))


@APP.get("/api/library")
def library_api() -> Response:
    """Return persistent downloaded-media records."""
    return jsonify(list_media(DATABASE_PATH))


@APP.get("/api/media/<int:media_id>")
def media_api(media_id: int) -> Response:
    """Serve a recorded download with Flask's conditional range support."""
    record = get_media(DATABASE_PATH, media_id)
    if record is None:
        return jsonify({"error": "Video not found."}), 404
    download_root = DOWNLOAD_DIR.resolve()
    file_path = Path(record["file_path"]).resolve()
    try:
        file_path.relative_to(download_root)
    except ValueError:
        return jsonify({"error": "Video file is unavailable."}), 404
    if not file_path.is_file():
        return jsonify({"error": "Video file is unavailable."}), 404
    return send_file(file_path, conditional=True, max_age=3600)


@APP.get("/cache/<path:filename>")
def cache_file(filename: str) -> Response:
    """Serve cached artwork from the same URL root as TubeArchivist."""
    cache_root = CACHE_DIR.resolve()
    path = (cache_root / filename).resolve()
    try:
        path.relative_to(cache_root)
    except ValueError:
        return jsonify({"error": "Artwork not found."}), 404
    if not path.is_file():
        return jsonify({"error": "Artwork not found."}), 404
    return send_file(path, conditional=True, max_age=86400)


@APP.get("/api/health")
def health_api() -> Response:
    """Return a cheap container health signal."""
    return jsonify({"status": "ok"})


@APP.post("/api/downloads")
def downloads_api() -> Response:
    """Append a JSON submission to the persistent queue."""
    try:
        length = request.content_length or 0
        if length <= 0 or length > APP.config["MAX_CONTENT_LENGTH"]:
            raise ValueError("Invalid request size.")
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("Enter a JSON object containing a video URL.")
        download = enqueue_url(payload.get("url"))
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(download), 202


from api_compat import register_ta_api

register_ta_api(APP)


@APP.errorhandler(404)
def not_found(_error: object) -> tuple[Response, int] | tuple[str, int]:
    """Render page requests as HTML and API requests as JSON."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return render_template("not_found.html"), 404


def main() -> None:
    """Initialize storage, recover queue state, and run Flask."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database(DATABASE_PATH)
    migrate_storage_layout(DATABASE_PATH, DOWNLOAD_DIR, CACHE_DIR)
    recovered = recover_interrupted_downloads(DATABASE_PATH)
    if recovered:
        print(f"Requeued {recovered} interrupted download(s).", flush=True)
    start_download_worker()
    APP.run(host=APP_HOST, port=APP_PORT, threaded=True)


if __name__ == "__main__":
    main()
