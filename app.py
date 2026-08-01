"""A deliberately small Flask front end for yt-dlp."""

from __future__ import annotations

import os
import math
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
    url_for,
)
from library import (
    add_ignored,
    cancel_download,
    claim_next_download,
    count_collection_items,
    delete_media_and_ignore,
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
    list_channels_missing_thumbnail,
    list_downloads,
    list_ignored,
    list_media,
    recover_interrupted_downloads,
    remove_ignored,
    save_media,
    set_media_watched,
    update_channel_thumbnail,
    update_download,
    update_media_progress,
    update_settings,
)
from yt_dlp import YoutubeDL


APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8080"))
DOWNLOAD_DIR = Path(os.environ.get("DOWNLOAD_DIR", "/downloads"))
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "/data/library.db"))

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
    video_marker = f"[{info.get('id')}]"
    matches = [
        path
        for path in DOWNLOAD_DIR.iterdir()
        if path.is_file()
        and video_marker in path.name
        and path.suffix not in {".part", ".ytdl"}
    ]
    if matches:
        return max(matches, key=lambda path: path.stat().st_mtime)
    raise FileNotFoundError("yt-dlp completed but its output file was not found.")


def quality_format(quality: str) -> str:
    """Translate a persisted quality preset into a yt-dlp format selector."""
    if quality == "best":
        return "bestvideo+bestaudio/best"
    height = int(quality)
    return (
        f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]"
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


def choose_channel_thumbnail(info: dict) -> str | None:
    """Choose the channel avatar rather than its wide banner."""
    thumbnails = info.get("thumbnails") or []
    for thumbnail in reversed(thumbnails):
        if "avatar" in str(thumbnail.get("id", "")) and thumbnail.get("url"):
            return str(thumbnail["url"])
    square = [
        thumbnail
        for thumbnail in thumbnails
        if thumbnail.get("url")
        and thumbnail.get("width")
        and thumbnail.get("width") == thumbnail.get("height")
    ]
    if square:
        return str(max(square, key=lambda item: item.get("width", 0))["url"])
    return None


def hydrate_missing_channel_thumbnails(limit: int = 5) -> None:
    """Use a metadata-only yt-dlp lookup to cache missing channel avatars."""
    options = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlistend": 0,
        "skip_download": True,
    }
    for channel in list_channels_missing_thumbnail(DATABASE_PATH, limit):
        try:
            with YoutubeDL(options) as downloader:
                info = downloader.extract_info(channel["source_url"], download=False)
            thumbnail_url = choose_channel_thumbnail(info)
            if thumbnail_url:
                update_channel_thumbnail(DATABASE_PATH, channel["id"], thumbnail_url)
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
    settings = get_settings(DATABASE_PATH)
    progress_state = {"last_write": 0.0}
    options = {
        "format": quality_format(settings["download_quality"]),
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": str(DOWNLOAD_DIR / "%(title).180B [%(id)s].%(ext)s"),
        "progress_hooks": [
            lambda data: progress_hook(download_id, progress_state, data)
        ],
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    with YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=True)
        if is_download_cancelled(DATABASE_PATH, download_id):
            raise DownloadCancelled("Download cancelled by user.")
        file_path = get_downloaded_file(info, downloader).resolve()
        media_id = save_media(DATABASE_PATH, url, info, file_path)
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
    limit = get_settings(DATABASE_PATH)["collection_limit"]
    update_download(
        DATABASE_PATH,
        download_id,
        kind=kind,
        title=title,
        percent="Expanding",
        progress=0,
    )

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
            enqueue_collection_items(DATABASE_PATH, download_id, batch)
            batch.clear()
            total_items = count_collection_items(DATABASE_PATH, download_id)
            update_download(
                DATABASE_PATH,
                download_id,
                item_count=total_items,
                percent=f"{total_items} new videos (limit {limit})",
            )
    if batch and total_items < limit:
        enqueue_collection_items(DATABASE_PATH, download_id, batch)
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
    hydrate_missing_channel_thumbnails()
    while True:
        QUEUE_WAKE.clear()
        download = claim_next_download(DATABASE_PATH)
        if download is None:
            QUEUE_WAKE.wait(timeout=5)
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
    return render_template("home.html", videos=list_media(DATABASE_PATH, limit=24))


@APP.get("/channels")
def channels_page() -> str:
    """Show normalized channel records and their library counts."""
    return render_template("channels.html", channels=list_channels(DATABASE_PATH))


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
    )


@APP.get("/downloads")
def downloads_page() -> str:
    """Show the durable queue grouped by state."""
    downloads = list_downloads(DATABASE_PATH)
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
    )


@APP.post("/downloads")
def add_download_page() -> tuple[str, int] | Response:
    """Append a form submission to the persistent queue."""
    try:
        download = enqueue_url(request.form.get("url"))
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


@APP.get("/settings")
def settings_page() -> str:
    """Show persistent downloader settings."""
    return render_template(
        "settings.html",
        settings=get_settings(DATABASE_PATH),
        quality_options=QUALITY_OPTIONS,
        ignored=list_ignored(DATABASE_PATH),
        saved=request.args.get("saved") == "1",
    )


@APP.post("/settings")
def update_settings_page() -> tuple[str, int] | Response:
    """Validate and persist downloader settings."""
    try:
        settings = validate_settings(
            request.form.get("collection_limit"),
            request.form.get("download_quality"),
        )
    except ValueError as exc:
        current = {
            "collection_limit": request.form.get("collection_limit", ""),
            "download_quality": request.form.get("download_quality", ""),
        }
        return (
            render_template(
                "settings.html",
                settings=current,
                quality_options=QUALITY_OPTIONS,
                ignored=list_ignored(DATABASE_PATH),
                saved=False,
                error=str(exc),
            ),
            400,
        )
    update_settings(DATABASE_PATH, settings)
    return redirect(url_for("settings_page", saved=1), code=303)


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
    return render_template("watch.html", video=video)


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
    file_path.unlink(missing_ok=True)
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


@APP.errorhandler(404)
def not_found(_error: object) -> tuple[Response, int] | tuple[str, int]:
    """Render page requests as HTML and API requests as JSON."""
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return render_template("not_found.html"), 404


def main() -> None:
    """Initialize storage, recover queue state, and run Flask."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    initialize_database(DATABASE_PATH)
    recovered = recover_interrupted_downloads(DATABASE_PATH)
    if recovered:
        print(f"Requeued {recovered} interrupted download(s).", flush=True)
    start_download_worker()
    APP.run(host=APP_HOST, port=APP_PORT, threaded=True)


if __name__ == "__main__":
    main()
