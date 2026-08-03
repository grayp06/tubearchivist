"""TubeArchivist v0.5.10 compatible API backed by the lightweight SQLite data."""

from __future__ import annotations

import json
import math
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse

from flask import Response, jsonify, redirect, render_template_string, request, send_file

from features import (
    add_playlist_media,
    add_subscription,
    archive_stats,
    create_playlist,
    create_snapshot,
    delete_playlist,
    list_playlist_media,
    list_playlists,
    list_snapshots,
    move_playlist_media,
    remove_playlist_media,
    rescan_filesystem,
)
from library import (
    add_ignored,
    connect,
    enqueue_download,
    get_settings,
    remove_ignored,
    set_media_watched,
    update_media_progress,
    update_settings,
)
from storage import (
    channel_art_relative_path,
    delete_channel_art,
    delete_playlist_thumbnail,
    delete_video_thumbnail,
    playlist_thumb_relative_path,
    sync_playlist_thumbnail,
    video_thumb_relative_path,
)


TA_VERSION = "v0.5.10"
VIDEO_TYPES = {"video": "videos", "short": "shorts", "stream": "streams"}
VIDEO_TYPES_REVERSE = {value: key for key, value in VIDEO_TYPES.items()}
TASK_CONFIG = {
    "update_subscribed": {"api_start": True, "api_stop": True},
    "download_pending": {"api_start": True, "api_stop": True},
    "extract_download": {"api_start": False, "api_stop": True},
    "check_reindex": {"api_start": False, "api_stop": False},
    "manual_import": {"api_start": False, "api_stop": False},
    "run_backup": {"api_start": True, "api_stop": False},
    "restore_backup": {"api_start": False, "api_stop": False},
    "rescan_filesystem": {"api_start": False, "api_stop": False},
    "thumbnail_check": {"api_start": True, "api_stop": False},
    "resync_metadata": {"api_start": True, "api_stop": False},
    "index_playlists": {"api_start": False, "api_stop": False},
    "subscribe_to": {"api_start": False, "api_stop": False},
    "version_check": {"api_start": False, "api_stop": False},
}


def _app_module():
    import app

    return app


def _database_path() -> Path:
    return _app_module().DATABASE_PATH


def _download_dir() -> Path:
    return _app_module().DOWNLOAD_DIR


def _snapshot_dir() -> Path:
    return _app_module().SNAPSHOT_DIR


def _cache_dir() -> Path:
    return _app_module().CACHE_DIR


def _cache_url(relative: Path) -> str | None:
    return f"/cache/{relative.as_posix()}" if (_cache_dir() / relative).is_file() else None


def _error(message: str, status: int = 400):
    return jsonify({"error": message}), status


def _json_object() -> dict | None:
    value = request.get_json(silent=True)
    return value if isinstance(value, dict) else None


def _as_bool(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _epoch(value) -> int:
    if not value:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _duration_str(seconds) -> str:
    seconds = int(seconds or 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _ensure_schema() -> None:
    with connect(_database_path()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_tasks (
                task_id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                result TEXT, traceback TEXT, args_json TEXT, kwargs_json TEXT,
                date_done TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_schedules (
                name TEXT PRIMARY KEY, schedule TEXT NOT NULL,
                config_json TEXT NOT NULL DEFAULT '{}',
                last_run_at TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS api_notifications (
                task_name TEXT NOT NULL, url TEXT NOT NULL,
                PRIMARY KEY(task_name, url)
            )
            """
        )
        token = connection.execute(
            "SELECT value FROM app_settings WHERE key='api_token'"
        ).fetchone()
        if token is None:
            connection.execute(
                "INSERT INTO app_settings (key, value) VALUES ('api_token', ?)",
                (secrets.token_hex(20),),
            )


def _setting(key: str, default=None):
    _ensure_schema()
    with connect(_database_path()) as connection:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key=?", (key,)
        ).fetchone()
    return row["value"] if row else default


def _set_setting(key: str, value) -> None:
    _ensure_schema()
    with connect(_database_path()) as connection:
        connection.execute(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                updated_at=CURRENT_TIMESTAMP
            """,
            (key, str(value)),
        )


def _task(name: str, result="done", status="SUCCESS", *, args=None, kwargs=None):
    _ensure_schema()
    task_id = str(uuid.uuid4())
    with connect(_database_path()) as connection:
        connection.execute(
            """
            INSERT INTO api_tasks
                (task_id, name, status, result, args_json, kwargs_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                name,
                status,
                None if result is None else json.dumps(result, default=str),
                json.dumps(args or []),
                json.dumps(kwargs or {}),
            ),
        )
    return task_id


def _task_dict(row) -> dict:
    result = row["result"]
    if result is not None:
        try:
            result = json.loads(result)
        except json.JSONDecodeError:
            pass
    if result is not None and not isinstance(result, str):
        result = json.dumps(result, separators=(",", ":"), default=str)
    return {
        "status": row["status"],
        "result": result,
        "traceback": row["traceback"],
        "date_done": row["date_done"],
        "name": row["name"],
        "args": json.loads(row["args_json"] or "[]"),
        "kwargs": json.loads(row["kwargs_json"] or "{}"),
        "task_id": row["task_id"],
    }


def _metadata(row) -> dict:
    try:
        return json.loads(row.get("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def _channel_subscribed(channel_id: str) -> bool:
    with connect(_database_path()) as connection:
        rows = connection.execute(
            "SELECT source_url FROM subscriptions WHERE kind='channel' AND enabled=1"
        ).fetchall()
    return any(channel_id in row["source_url"] for row in rows)


def _playlist_subscribed(playlist_id: str) -> bool:
    with connect(_database_path()) as connection:
        rows = connection.execute(
            "SELECT source_url FROM subscriptions WHERE kind='playlist' AND enabled=1"
        ).fetchall()
    return any(playlist_id in row["source_url"] for row in rows)


def _channel_json(row) -> dict:
    channel_id = str(row.get("channel_id") or row.get("id") or "unknown")
    try:
        overwrites = json.loads(_setting(f"channel_overwrites:{channel_id}", "{}"))
    except json.JSONDecodeError:
        overwrites = {}
    try:
        channel_tags = json.loads(row.get("tags_json") or "[]")
    except json.JSONDecodeError:
        channel_tags = []
    return {
        "channel_id": channel_id,
        "channel_active": True,
        "channel_banner_url": _cache_url(channel_art_relative_path(channel_id, "banner")) if row.get("banner_url") else None,
        "channel_thumb_url": _cache_url(channel_art_relative_path(channel_id, "thumb")) if row.get("thumbnail_url") else None,
        "channel_tvart_url": _cache_url(channel_art_relative_path(channel_id, "tvart")) if row.get("tvart_url") else None,
        "channel_description": row.get("description"),
        "channel_last_refresh": str(row.get("updated_at") or ""),
        "channel_name": str(row.get("name") or row.get("channel") or "Unknown channel"),
        "channel_overwrites": overwrites,
        "channel_subs": int(row.get("subscriber_count") or 0),
        "channel_subscribed": _channel_subscribed(str(row.get("channel_id") or "")),
        "channel_tags": channel_tags,
        "channel_tabs": ["videos", "shorts", "streams"],
    }


def _media_channel(row) -> dict:
    return _channel_json(
        {
            "id": row.get("channel_db_id"),
            "channel_id": row.get("channel_id"),
            "name": row.get("channel"),
            "thumbnail_url": row.get("channel_thumbnail_url"),
            "banner_url": row.get("channel_banner_url"),
            "tvart_url": row.get("channel_tvart_url"),
            "description": row.get("channel_description"),
            "subscriber_count": row.get("channel_subscriber_count"),
            "tags_json": row.get("channel_tags_json"),
            "updated_at": row.get("updated_at"),
        }
    )


def _streams(metadata: dict) -> list[dict]:
    formats = metadata.get("requested_formats") or []
    if not formats and metadata:
        formats = [metadata]
    output = []
    for index, item in enumerate(formats):
        if item.get("vcodec") and item.get("vcodec") != "none":
            stream_type, codec = "video", item.get("vcodec")
        elif item.get("acodec") and item.get("acodec") != "none":
            stream_type, codec = "audio", item.get("acodec")
        else:
            continue
        stream = {
            "bitrate": int((item.get("tbr") or item.get("abr") or 0) * 1000),
            "codec": str(codec),
            "index": index,
            "type": stream_type,
        }
        if item.get("height"):
            stream["height"] = int(item["height"])
        if item.get("width"):
            stream["width"] = int(item["width"])
        output.append(stream)
    return output


def _subtitle_items(row) -> list[dict]:
    file_path = Path(row["file_path"])
    items = []
    if not file_path.parent.exists():
        return items
    for sidecar in file_path.parent.iterdir():
        if not sidecar.is_file() or sidecar.suffix.lower() != ".vtt":
            continue
        if not sidecar.name.startswith(file_path.stem):
            continue
        suffix = sidecar.name[len(file_path.stem) :].lstrip(".")
        lang = suffix.rsplit(".", 1)[0] or "en"
        items.append(
            {
                "ext": "vtt",
                "lang": lang,
                "media_url": f"/youtube/{row.get('channel_id') or 'unknown'}/{sidecar.name}",
                "name": lang,
                "source": "auto",
                "url": None,
            }
        )
    return items


def _comment_items(comments) -> list[dict]:
    """Normalize yt-dlp comment metadata to TubeArchivist's API fields."""
    output = []
    for item in comments or []:
        if not isinstance(item, dict):
            continue
        timestamp = int(item.get("timestamp") or 0)
        output.append(
            {
                "comment_author": str(item.get("author") or item.get("comment_author") or ""),
                "comment_author_id": str(item.get("author_id") or item.get("comment_author_id") or ""),
                "comment_author_is_uploader": bool(item.get("author_is_uploader") or item.get("comment_author_is_uploader")),
                "comment_author_thumbnail": str(item.get("author_thumbnail") or item.get("comment_author_thumbnail") or "https://www.youtube.com/favicon.ico"),
                "comment_id": str(item.get("id") or item.get("comment_id") or ""),
                "comment_is_favorited": bool(item.get("is_favorited") or item.get("comment_is_favorited")),
                "comment_likecount": int(item.get("like_count") or item.get("comment_likecount") or 0),
                "comment_parent": str(item.get("parent") or item.get("comment_parent") or "root"),
                "comment_text": str(item.get("text") or item.get("comment_text") or ""),
                "comment_time_text": str(item.get("time_text") or item.get("comment_time_text") or ""),
                "comment_timestamp": timestamp,
                "comment_replies": _comment_items(item.get("replies") or item.get("comment_replies")),
            }
        )
    return output


def _video_json(row) -> dict:
    metadata = _metadata(row)
    duration = int(row.get("duration") or 0)
    progress = float(row.get("watch_progress") or 0)
    position = duration * progress / 100
    player = {
        "watched": bool(row.get("watched")),
        "duration": duration,
        "duration_str": _duration_str(duration),
        "progress": progress,
        "position": position,
    }
    if row.get("watched_at"):
        player["watched_date"] = _epoch(row["watched_at"])
    tags = row.get("tags") or ""
    if isinstance(tags, str):
        tags = [item.strip() for item in tags.split(",") if item.strip()]
    playlists = row.get("playlist_ids") or ""
    if isinstance(playlists, str):
        playlists = [item for item in playlists.split(",") if item]
    item = {
        "active": True,
        "category": list(metadata.get("categories") or []),
        "channel": _media_channel(row),
        "comment_count": metadata.get("comment_count"),
        "date_downloaded": _epoch(row.get("downloaded_at")),
        "description": row.get("description"),
        "media_size": int(row.get("file_size") or 0),
        "media_url": f"/youtube/{row.get('channel_id') or 'unknown'}/{Path(row['file_path']).name}",
        "player": player,
        "playlist": playlists,
        "published": row.get("upload_date") or "",
        "stats": {
            "like_count": int(metadata.get("like_count") or 0),
            "average_rating": float(metadata.get("average_rating") or 0),
            "view_count": int(metadata.get("view_count") or 0),
            "dislike_count": int(metadata.get("dislike_count") or 0),
        },
        "streams": _streams(metadata),
        "subtitles": _subtitle_items(row),
        "tags": tags,
        "title": row["title"],
        "vid_last_refresh": str(row.get("updated_at") or ""),
        "vid_thumb_url": _cache_url(video_thumb_relative_path(row["video_id"])) or "",
        "vid_type": VIDEO_TYPES.get(row.get("video_type"), "videos"),
        "youtube_id": row["video_id"],
    }
    return item


MEDIA_SELECT = """
SELECT m.*, c.thumbnail_url AS channel_thumbnail_url,
       c.banner_url AS channel_banner_url,
       c.tvart_url AS channel_tvart_url,
       c.description AS channel_description,
       c.subscriber_count AS channel_subscriber_count,
       c.tags_json AS channel_tags_json,
       GROUP_CONCAT(DISTINCT mt.tag) AS tags,
       GROUP_CONCAT(DISTINCT p.playlist_id) AS playlist_ids
FROM media m
LEFT JOIN channels c ON c.id=m.channel_db_id
LEFT JOIN media_tags mt ON mt.media_id=m.id
LEFT JOIN playlist_media pm ON pm.media_id=m.id
LEFT JOIN playlists p ON p.id=pm.playlist_id
"""


def _media_row(video_id: str):
    with connect(_database_path()) as connection:
        row = connection.execute(
            MEDIA_SELECT + " WHERE m.video_id=? GROUP BY m.id", (video_id,)
        ).fetchone()
    return dict(row) if row else None


def _pagination(total: int, page: int, page_size: int) -> dict:
    page_from = 0 if page in {0, 1} else (page - 1) * page_size
    max_pages = math.ceil(total / page_size) if total else 0
    prev_pages = None
    if page > 1:
        prev_pages = sorted(i for i in range(page - 1, page - 6, -1) if i > 1)
    last_page = max_pages if page < max_pages and max_pages > 1 else False
    next_pages = [i for i in range(page + 1, page + 6) if 1 < i < max_pages]
    params = request.args.to_dict(flat=True)
    params.pop("page", None)
    return {
        "page_size": page_size,
        "page_from": page_from,
        "prev_pages": prev_pages,
        "current_page": page,
        "max_hits": total >= 10000,
        "params": urlencode(params),
        "last_page": last_page,
        "next_pages": next_pages,
        "total_hits": total,
    }


def _paged_rows(base_sql: str, params: list, *, order_sql: str):
    settings = get_settings(_database_path())
    page_size = settings["page_size"]
    try:
        page = int(request.args.get("page", 0))
    except ValueError:
        return None, _error("Invalid page.")
    if page < 0:
        return None, _error("Invalid page.")
    offset = 0 if page in {0, 1} else (page - 1) * page_size
    with connect(_database_path()) as connection:
        total = int(connection.execute(f"SELECT COUNT(*) FROM ({base_sql})", params).fetchone()[0])
        rows = connection.execute(
            f"{base_sql} {order_sql} LIMIT ? OFFSET ?", [*params, page_size, offset]
        ).fetchall()
    if total and offset >= total:
        return None, _error("Not found.", 404)
    return ([dict(row) for row in rows], _pagination(total, page, page_size)), None


def _playlist_row(external_id: str):
    with connect(_database_path()) as connection:
        row = connection.execute(
            "SELECT * FROM playlists WHERE playlist_id=?", (external_id,)
        ).fetchone()
    return dict(row) if row else None


def _playlist_json(row) -> dict:
    videos = list_playlist_media(_database_path(), int(row["id"]))
    entries = [
        {
            "youtube_id": video["video_id"],
            "title": video["title"],
            "uploader": video.get("channel"),
            "idx": int(video.get("position") or index),
            "downloaded": True,
        }
        for index, video in enumerate(videos)
    ]
    return {
        "playlist_active": True,
        "playlist_channel": videos[0].get("channel") if videos else "",
        "playlist_channel_id": videos[0].get("channel_id") if videos else "",
        "playlist_description": row.get("description"),
        "playlist_entries": entries,
        "playlist_id": row["playlist_id"],
        "playlist_last_refresh": str(row.get("updated_at") or ""),
        "playlist_name": row["name"],
        "playlist_subscribed": _playlist_subscribed(row["playlist_id"]),
        "playlist_sort_order": row.get("playlist_sort_order") or "top",
        "playlist_thumbnail": _cache_url(playlist_thumb_relative_path(row["playlist_id"])) or "",
        "playlist_type": "custom" if row.get("custom") else "regular",
    }


def _video_id(value: str) -> str:
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return _app_module().video_identity(value)[1] or value
    return value


def _download_item_from_row(row, status="pending") -> dict:
    video_id = _video_id(row.get("source_url") or row.get("video_id") or "")
    metadata = {}
    if row.get("media_id"):
        media = _media_row(video_id)
        metadata = _metadata(media or {})
    return {
        "auto_start": bool(row.get("priority")),
        "channel_id": str(row.get("channel_id") or metadata.get("channel_id") or ""),
        "channel_indexed": bool(row.get("channel_id") or metadata.get("channel_id")),
        "channel_name": str(row.get("channel_name") or metadata.get("channel") or ""),
        "duration": str(row.get("duration") or metadata.get("duration") or ""),
        **({"message": row.get("error")} if row.get("error") else {}),
        "published": row.get("published") or metadata.get("upload_date"),
        "status": status,
        "timestamp": _epoch(row.get("created_at") or row.get("ignored_at")) or None,
        "title": str(row.get("title") or video_id),
        "vid_thumb_url": _cache_url(video_thumb_relative_path(video_id)),
        "vid_type": VIDEO_TYPES.get(row.get("video_type"), "videos"),
        "youtube_id": video_id,
    }


def _download_rows() -> list[dict]:
    output = []
    with connect(_database_path()) as connection:
        pending = connection.execute(
            """
            SELECT d.*, m.channel_id, m.channel AS channel_name, m.duration,
                   m.upload_date AS published, m.thumbnail_url, m.video_type
            FROM downloads d LEFT JOIN media m ON m.id=d.media_id
            WHERE d.kind='video' AND d.status NOT IN ('complete','cancelled')
            """
        ).fetchall()
        ignored = connection.execute("SELECT * FROM ignored_videos").fetchall()
    output.extend(_download_item_from_row(dict(row)) for row in pending)
    output.extend(_download_item_from_row(dict(row), "ignore") for row in ignored)
    return output


def _find_download(video_id: str):
    for item in _download_rows():
        if item["youtube_id"] == video_id:
            return item
    return None


def _delete_media_file(row) -> None:
    path = Path(row["file_path"]).resolve()
    try:
        path.relative_to(_download_dir().resolve())
    except ValueError:
        return
    _app_module().delete_media_files(path)
    delete_video_thumbnail(_cache_dir(), row["video_id"])


def _app_config() -> dict:
    settings = get_settings(_database_path())
    saved = json.loads(_setting("api_config_json", "{}"))
    base = {
        "subscriptions": {
            "channel_size": settings["collection_limit"],
            "live_channel_size": settings["collection_limit"],
            "shorts_channel_size": settings["collection_limit"],
            "playlist_size": settings["collection_limit"],
            "auto_start": True,
            "extract_flat": True,
        },
        "downloads": {
            "limit_speed": int(settings["rate_limit"]) if settings["rate_limit"] else None,
            "sleep_interval": settings["subscription_download_delay"],
            "autodelete_days": None,
            "format": settings["download_quality"],
            "format_sort": None,
            "add_metadata": settings["write_info_json"],
            "subtitle": settings["subtitle_languages"] if settings["download_subtitles"] else None,
            "subtitle_source": "auto" if settings["download_subtitles"] else None,
            "subtitle_index": settings["download_subtitles"],
            "comment_max": "all" if settings["download_comments"] else None,
            "comment_sort": "top" if settings["download_comments"] else None,
            "cookie_import": Path(settings["cookie_path"]).is_file(),
            "pot_provider_url": None,
            "throttledratelimit": None,
            "extractor_lang": None,
            "integrate_ryd": False,
            "integrate_sponsorblock": bool(settings["sponsorblock_categories"]),
        },
        "application": {"enable_snapshot": True, "enable_cast": False},
    }
    for section, values in saved.items():
        if section in base and isinstance(values, dict):
            base[section].update(values)
    return base


def _user_config() -> dict:
    settings = get_settings(_database_path())
    saved = json.loads(_setting("api_user_config", "{}"))
    base = {
        "stylesheet": "dark.css",
        "page_size": settings["page_size"],
        "sort_by": "downloaded",
        "sort_order": "desc",
        "view_style_home": "grid",
        "view_style_channel": "grid",
        "view_style_downloads": "grid",
        "view_style_playlist": "grid",
        "vid_type_filter": None,
        "grid_items": 4,
        "hide_watched": False,
        "hide_watched_channel": False,
        "hide_watched_playlist": False,
        "file_size_unit": "binary",
        "show_ignored_only": False,
        "show_subed_only": False,
        "show_subed_only_playlists": False,
        "show_help_text": True,
    }
    base.update(saved)
    return base


def _snapshot_item(path: Path) -> dict:
    stamp = int(path.stat().st_mtime)
    date = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    return {
        "id": path.stem,
        "state": "SUCCESS",
        "es_version": "sqlite",
        "start_date": date,
        "end_date": date,
        "end_stamp": stamp,
        "duration_s": 0,
    }


def _snapshot_path(snapshot_id: str) -> Path | None:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", snapshot_id):
        return None
    path = (_snapshot_dir() / f"{snapshot_id}.db").resolve()
    try:
        path.relative_to(_snapshot_dir().resolve())
    except ValueError:
        return None
    return path if path.is_file() else None


def register_ta_api(app) -> None:
    """Register the current TubeArchivist API paths on a Flask application."""

    @app.get("/api/ping/")
    def ta_ping():
        return jsonify({"response": "pong", "user": 1, "version": TA_VERSION, "ta_update": {"status": False, "version": TA_VERSION, "is_breaking": False}})

    @app.get("/api/health/")
    def ta_health():
        return jsonify("OK")

    @app.route("/api/video/", methods=["GET"])
    def ta_video_list():
        conditions, params = ["1=1"], []
        channel = request.args.get("channel")
        playlist = request.args.get("playlist")
        watch = request.args.get("watch")
        vid_type = request.args.get("type")
        if channel:
            conditions.append("m.channel_id=?"); params.append(channel)
        if playlist:
            conditions.append("EXISTS (SELECT 1 FROM playlist_media x JOIN playlists p2 ON p2.id=x.playlist_id WHERE x.media_id=m.id AND p2.playlist_id=?)"); params.append(playlist)
        if watch == "watched": conditions.append("m.watched=1")
        elif watch == "unwatched": conditions.append("m.watched=0")
        elif watch == "continue": conditions.append("m.watch_progress>=5 AND m.watch_progress<95")
        if vid_type:
            if vid_type not in VIDEO_TYPES_REVERSE: return _error("Invalid type.")
            conditions.append("m.video_type=?"); params.append(VIDEO_TYPES_REVERSE[vid_type])
        sort_map = {"published":"m.upload_date", "downloaded":"m.downloaded_at", "views":"m.id", "likes":"m.id", "duration":"m.duration", "filesize":"m.file_size"}
        sort = request.args.get("sort", "published")
        order = request.args.get("order", "desc")
        if sort not in sort_map or order not in {"asc","desc"}: return _error("Invalid sort or order.")
        base = MEDIA_SELECT + " WHERE " + " AND ".join(conditions) + " GROUP BY m.id"
        result, failure = _paged_rows(base, params, order_sql=f"ORDER BY {sort_map[sort]} {order.upper()}, m.id {order.upper()}")
        if failure: return failure
        rows, paginate = result
        return jsonify({"data": [_video_json(row) for row in rows], "paginate": paginate})

    @app.route("/api/video/<video_id>/", methods=["GET", "DELETE"])
    def ta_video(video_id):
        row = _media_row(video_id)
        if not row: return _error("video not found", 404)
        if request.method == "GET": return jsonify(_video_json(row))
        _delete_media_file(row)
        with connect(_database_path()) as connection:
            connection.execute("UPDATE downloads SET media_id=NULL WHERE media_id=?", (row["id"],))
            connection.execute("DELETE FROM media WHERE id=?", (row["id"],))
        return Response(status=204)

    @app.route("/api/video/<video_id>/progress/", methods=["POST", "DELETE"])
    def ta_video_progress(video_id):
        row = _media_row(video_id)
        if request.method == "DELETE":
            if row: update_media_progress(_database_path(), row["id"], 0)
            return Response(status=204)
        if not row: return _error("video not found", 404)
        data = _json_object()
        if data is None or not isinstance(data.get("position", 0), (int, float)): return _error("position must be a number")
        duration = int(row.get("duration") or 0)
        percentage = float(data.get("position", 0)) / duration * 100 if duration else 0
        update_media_progress(_database_path(), row["id"], percentage)
        return jsonify(_video_json(_media_row(video_id))["player"])

    @app.get("/api/video/<video_id>/comment/")
    def ta_video_comment(video_id):
        row = _media_row(video_id)
        if not row: return _error("video not found", 404)
        return jsonify(_comment_items(_metadata(row).get("comments")))

    @app.get("/api/video/<video_id>/nav/")
    def ta_video_nav(video_id):
        row = _media_row(video_id)
        if not row: return _error("video not found", 404)
        output = []
        with connect(_database_path()) as connection:
            playlists = connection.execute("SELECT p.* FROM playlists p JOIN playlist_media pm ON pm.playlist_id=p.id WHERE pm.media_id=?", (row["id"],)).fetchall()
        for playlist in playlists:
            videos = list_playlist_media(_database_path(), playlist["id"])
            index = next((i for i, item in enumerate(videos) if item["id"] == row["id"]), 0)
            def nav_item(item, idx):
                if item is None: return None
                return {"youtube_id":item["video_id"],"title":item["title"],"uploader":item.get("channel") or "","idx":idx,"downloaded":True,"vid_thumb":item.get("thumbnail_url") or ""}
            output.append({"playlist_meta":{"current_idx":index,"playlist_id":playlist["playlist_id"],"playlist_name":playlist["name"],"playlist_channel":videos[0].get("channel") if videos else ""},"playlist_previous":nav_item(videos[index-1], index-1) if index else None,"playlist_next":nav_item(videos[index+1], index+1) if index+1 < len(videos) else None})
        return jsonify(output)

    @app.get("/api/video/<video_id>/similar/")
    def ta_video_similar(video_id):
        row = _media_row(video_id)
        if not row: return jsonify([])
        with connect(_database_path()) as connection:
            rows = connection.execute(MEDIA_SELECT + " WHERE m.id<>? AND (m.channel_id=? OR m.title LIKE ?) GROUP BY m.id ORDER BY m.downloaded_at DESC LIMIT 6", (row["id"], row.get("channel_id"), f"%{row['title'].split()[0]}%")).fetchall()
        return jsonify([_video_json(dict(item)) for item in rows])

    @app.route("/api/channel/", methods=["GET", "POST"])
    def ta_channels():
        if request.method == "POST":
            data = _json_object()
            if data is None or not isinstance(data.get("data"), list): return jsonify({"message":"missing expected data key"}), 400
            for item in data["data"]:
                channel_id = str(item.get("channel_id", ""))
                if not channel_id: continue
                url = f"https://www.youtube.com/channel/{channel_id}"
                if item.get("channel_subscribed"):
                    add_subscription(_database_path(), url, "channel", channel_id)
                else:
                    with connect(_database_path()) as connection: connection.execute("DELETE FROM subscriptions WHERE kind='channel' AND source_url LIKE ?", (f"%{channel_id}%",))
            return jsonify(data)
        filter_value = request.args.get("filter")
        if filter_value not in {None,"subscribed","unsubscribed"}: return _error("Invalid filter.")
        base = "SELECT c.* FROM channels c"
        result, failure = _paged_rows(base, [], order_sql="ORDER BY c.name COLLATE NOCASE")
        if failure: return failure
        rows, paginate = result
        data = [_channel_json(row) for row in rows]
        if filter_value: data = [item for item in data if item["channel_subscribed"] == (filter_value == "subscribed")]
        paginate["total_hits"] = len(data) if filter_value else paginate["total_hits"]
        return jsonify({"data":data,"paginate":paginate})

    @app.get("/api/channel/search/")
    def ta_channel_search():
        query = request.args.get("q")
        if not query: return jsonify({"message":"missing expected q parameter","data":False}), 400
        channel_id = query.rstrip("/").rsplit("/",1)[-1]
        with connect(_database_path()) as connection:
            row = connection.execute("SELECT * FROM channels WHERE channel_id=? OR source_url=?", (channel_id, query)).fetchone()
        return jsonify(_channel_json(dict(row))) if row else _error(f"channel not found: {query}", 404)

    @app.route("/api/channel/<channel_id>/", methods=["GET", "POST", "DELETE"])
    def ta_channel(channel_id):
        with connect(_database_path()) as connection:
            row = connection.execute("SELECT * FROM channels WHERE channel_id=?", (channel_id,)).fetchone()
        if not row: return _error("channel not found", 404)
        if request.method == "GET": return jsonify(_channel_json(dict(row)))
        if request.method == "POST":
            data = _json_object() or {}
            if "channel_subscribed" in data:
                url = row["source_url"] or f"https://www.youtube.com/channel/{channel_id}"
                if data["channel_subscribed"]: add_subscription(_database_path(), url, "channel", row["name"])
                else:
                    with connect(_database_path()) as connection: connection.execute("DELETE FROM subscriptions WHERE kind='channel' AND source_url LIKE ?", (f"%{channel_id}%",))
            if "channel_overwrites" in data:
                if not isinstance(data["channel_overwrites"], dict):
                    return _error("channel_overwrites must be an object")
                _set_setting(
                    f"channel_overwrites:{channel_id}",
                    json.dumps(data["channel_overwrites"], separators=(",", ":")),
                )
            return jsonify(data)
        with connect(_database_path()) as connection:
            videos = connection.execute("SELECT * FROM media WHERE channel_db_id=?", (row["id"],)).fetchall()
        for video in videos: _delete_media_file(dict(video))
        with connect(_database_path()) as connection: connection.execute("DELETE FROM media WHERE channel_db_id=?", (row["id"],)); connection.execute("DELETE FROM channels WHERE id=?", (row["id"],))
        delete_channel_art(_cache_dir(), channel_id)
        return Response(status=204)

    @app.get("/api/channel/<channel_id>/aggs/")
    def ta_channel_aggs(channel_id):
        with connect(_database_path()) as connection:
            row = connection.execute("SELECT COUNT(*) items, COALESCE(SUM(file_size),0) size, COALESCE(SUM(duration),0) duration FROM media WHERE channel_id=?", (channel_id,)).fetchone()
        return jsonify({"total_items":{"value":row["items"]},"total_size":{"value":row["size"]},"total_duration":{"value":row["duration"],"value_str":_duration_str(row["duration"])}})

    @app.get("/api/channel/<channel_id>/nav/")
    def ta_channel_nav(channel_id):
        with connect(_database_path()) as connection:
            counts = {row["video_type"]:row["n"] for row in connection.execute("SELECT video_type,COUNT(*) n FROM media WHERE channel_id=? GROUP BY video_type", (channel_id,))}
            playlists = connection.execute("SELECT 1 FROM playlists p JOIN playlist_media pm ON pm.playlist_id=p.id JOIN media m ON m.id=pm.media_id WHERE m.channel_id=? LIMIT 1", (channel_id,)).fetchone()
            pending = connection.execute("SELECT 1 FROM downloads d LEFT JOIN media m ON m.id=d.media_id WHERE m.channel_id=? AND d.status IN ('queued','downloading') LIMIT 1", (channel_id,)).fetchone()
        return jsonify({"has_pending":bool(pending),"has_ignored":False,"has_playlists":bool(playlists),"has_videos":bool(counts.get("video")),"has_streams":bool(counts.get("stream")),"has_shorts":bool(counts.get("short"))})

    @app.route("/api/playlist/", methods=["GET", "POST"])
    def ta_playlists():
        if request.method == "POST":
            data = _json_object()
            if data is None or not isinstance(data.get("data"), list): return _error("nothing to subscribe")
            ids = [str(item.get("playlist_id")) for item in data["data"] if item.get("playlist_id")]
            if not ids: return _error("nothing to subscribe")
            for playlist_id in ids: add_subscription(_database_path(), f"https://www.youtube.com/playlist?list={playlist_id}", "playlist", playlist_id)
            return jsonify({"message":"playlist subscribe task started","task_id":_task("subscribe_to", ids)})
        rows = list_playlists(_database_path())
        channel = request.args.get("channel"); subscribed = _as_bool(request.args.get("subscribed")); kind = request.args.get("type")
        if kind not in {None,"regular","custom"}: return _error("Invalid type.")
        data = [_playlist_json(row) for row in rows]
        if channel: data = [item for item in data if item["playlist_channel_id"] == channel]
        if subscribed is not None: data = [item for item in data if item["playlist_subscribed"] == subscribed]
        if kind: data = [item for item in data if item["playlist_type"] == kind]
        page_size=get_settings(_database_path())["page_size"]
        try: page=int(request.args.get("page",0))
        except ValueError: return _error("Invalid page.")
        if page < 0: return _error("Invalid page.")
        offset=0 if page in {0,1} else (page-1)*page_size
        if data and offset >= len(data): return _error("Not found.",404)
        return jsonify({"data":data[offset:offset+page_size],"paginate":_pagination(len(data),page,page_size)})

    @app.post("/api/playlist/custom/")
    def ta_playlist_custom_create():
        data = _json_object() or {}
        if not data.get("playlist_name"): return _error("playlist_name is required")
        row = create_playlist(_database_path(), str(data["playlist_name"]))
        sync_playlist_thumbnail(_database_path(), _cache_dir(), row["id"])
        return jsonify(_playlist_json(row))

    @app.post("/api/playlist/custom/<playlist_id>/")
    def ta_playlist_custom_update(playlist_id):
        row = _playlist_row(playlist_id)
        if not row: return _error("playlist not found",404)
        if not row["custom"]: return _error(f"playlist with ID {playlist_id} is not custom")
        data=_json_object() or {}; action=data.get("action"); video=_media_row(str(data.get("video_id", "")))
        if action not in {"create","remove","up","down","top","bottom"}: return _error("Invalid action.")
        if not video: return _error(f"failed to add video {data.get('video_id')} to playlist")
        if action=="create": add_playlist_media(_database_path(),row["id"],video["id"])
        elif action=="remove": remove_playlist_media(_database_path(),row["id"],video["id"])
        elif action in {"up","down"}: move_playlist_media(_database_path(),row["id"],video["id"],action)
        else:
            with connect(_database_path()) as connection:
                position = -1 if action=="top" else 2147483647
                connection.execute("UPDATE playlist_media SET position=? WHERE playlist_id=? AND media_id=?",(position,row["id"],video["id"]))
        sync_playlist_thumbnail(_database_path(), _cache_dir(), row["id"])
        return jsonify(_playlist_json(_playlist_row(playlist_id)))

    @app.route("/api/playlist/<playlist_id>/", methods=["GET", "POST", "DELETE"])
    def ta_playlist(playlist_id):
        row=_playlist_row(playlist_id)
        if not row and request.method!="DELETE": return _error("playlist not found",404)
        if request.method=="GET": return jsonify(_playlist_json(row))
        if request.method=="POST":
            if row["custom"]: return _error(f"playlist with ID {playlist_id} is custom")
            data=_json_object() or {}
            if "playlist_subscribed" not in data and "playlist_sort_order" not in data: return _error("expect playlist_subscribed or playlist_sort_order")
            if "playlist_subscribed" in data:
                if data["playlist_subscribed"]: add_subscription(_database_path(),row["source_url"] or f"https://www.youtube.com/playlist?list={playlist_id}","playlist",row["name"])
                else:
                    with connect(_database_path()) as connection: connection.execute("DELETE FROM subscriptions WHERE kind='playlist' AND source_url LIKE ?",(f"%{playlist_id}%",))
            if data.get("playlist_sort_order") in {"top","bottom"}:
                with connect(_database_path()) as connection: connection.execute("UPDATE playlists SET playlist_sort_order=? WHERE id=?",(data["playlist_sort_order"],row["id"]))
            return jsonify(_playlist_json(_playlist_row(playlist_id)))
        if row:
            if _as_bool(request.args.get("delete_videos"),False):
                for video in list_playlist_media(_database_path(),row["id"]): _delete_media_file(video)
                with connect(_database_path()) as connection: connection.execute("DELETE FROM media WHERE id IN (SELECT media_id FROM playlist_media WHERE playlist_id=?)",(row["id"],))
            delete_playlist(_database_path(),row["id"])
            delete_playlist_thumbnail(_cache_dir(), playlist_id)
        return Response(status=204)

    @app.route("/api/download/", methods=["GET","POST","PATCH","DELETE"])
    def ta_downloads():
        if request.method=="GET":
            items=_download_rows(); status=request.args.get("filter"); channel=request.args.get("channel"); kind=request.args.get("vid_type"); query=request.args.get("q","").casefold(); error=_as_bool(request.args.get("error"))
            if status not in {None,"pending","ignore"}: return _error("Invalid filter.")
            if kind not in {None,"videos","shorts","streams"}: return _error("Invalid vid_type.")
            if status: items=[item for item in items if item["status"]==status]
            if channel: items=[item for item in items if item["channel_id"]==channel]
            if kind: items=[item for item in items if item["vid_type"]==kind]
            if query: items=[item for item in items if query in item["title"].casefold()]
            if error is not None: items=[item for item in items if bool(item.get("message"))==error]
            page_size=get_settings(_database_path())["page_size"]
            try: page=int(request.args.get("page",0))
            except ValueError: return _error("Invalid page.")
            if page < 0: return _error("Invalid page.")
            offset=0 if page in {0,1} else (page-1)*page_size
            if items and offset >= len(items): return _error("Not found.",404)
            return jsonify({"data":items[offset:offset+page_size],"paginate":_pagination(len(items),page,page_size)})
        if request.method=="POST":
            data=_json_object()
            if data is None or not isinstance(data.get("data"),list): return _error("data must be a list")
            auto_start=_as_bool(request.args.get("autostart"))
            force=_as_bool(request.args.get("force"),False)
            for item in data["data"]:
                video_id=str(item.get("youtube_id", "")); status=item.get("status")
                if not video_id or status not in {"pending","ignore-force"}: return _error("invalid download item")
                url=video_id if video_id.startswith("http") else f"https://www.youtube.com/watch?v={video_id}"
                if status=="ignore-force": add_ignored(_database_path(),url,"Youtube",_video_id(url))
                else:
                    if force:
                        with connect(_database_path()) as connection:
                            connection.execute("DELETE FROM ignored_videos WHERE video_id=? OR source_url=?",(_video_id(url),url))
                    queued=enqueue_download(_database_path(),url,kind="video")
                    if auto_start:
                        with connect(_database_path()) as connection:
                            connection.execute("UPDATE downloads SET priority=priority+1 WHERE id=?",(queued["id"],))
            _app_module().QUEUE_WAKE.set(); return jsonify({"message":"add to queue task started","task_id":_task("extract_download",data["data"])})
        if request.method=="PATCH":
            data=_json_object() or {}; new_status=data.get("status"); filter_status=request.args.get("filter")
            if new_status not in {"pending","ignore","priority","clear_error"} or filter_status not in {"pending","ignore","priority"}: return _error("invalid status")
            for item in list(_download_rows()):
                if filter_status!="priority" and item["status"]!=filter_status: continue
                if request.args.get("channel") and item["channel_id"] != request.args["channel"]: continue
                if request.args.get("vid_type") and item["vid_type"] != request.args["vid_type"]: continue
                error_filter = _as_bool(request.args.get("error"))
                if error_filter is not None and bool(item.get("message")) != error_filter: continue
                _update_download_status(item["youtube_id"],new_status)
            return Response(status=204)
        filter_status=request.args.get("filter")
        if filter_status not in {"pending","ignore"}: return _error("filter is required")
        for item in list(_download_rows()):
            if item["status"] != filter_status: continue
            if request.args.get("channel") and item["channel_id"] != request.args["channel"]: continue
            if request.args.get("vid_type") and item["vid_type"] != request.args["vid_type"]: continue
            with connect(_database_path()) as connection:
                if filter_status == "pending":
                    connection.execute(
                        "UPDATE downloads SET status='cancelled', error='Cancelled by API', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE source_url LIKE ? AND status IN ('queued','downloading','error')",
                        (f"%{item['youtube_id']}%",),
                    )
                else:
                    connection.execute("DELETE FROM ignored_videos WHERE video_id=?", (item["youtube_id"],))
        return Response(status=204)

    def _update_download_status(video_id, status):
        url=f"https://www.youtube.com/watch?v={video_id}"
        with connect(_database_path()) as connection:
            row=connection.execute("SELECT * FROM downloads WHERE source_url LIKE ? ORDER BY id DESC LIMIT 1",(f"%{video_id}%",)).fetchone()
            ignored=connection.execute("SELECT id FROM ignored_videos WHERE video_id=?",(video_id,)).fetchone()
        if status=="pending":
            if ignored: remove_ignored(_database_path(),ignored["id"])
            if row:
                with connect(_database_path()) as connection: connection.execute("UPDATE downloads SET status='queued',error=NULL WHERE id=?",(row["id"],))
            else: enqueue_download(_database_path(),url,kind="video")
            _app_module().QUEUE_WAKE.set()
        elif status=="ignore" or status=="ignore-force":
            if row:
                with connect(_database_path()) as connection: connection.execute("UPDATE downloads SET status='cancelled' WHERE id=?",(row["id"],))
            add_ignored(_database_path(),url,"Youtube",video_id,row["title"] if row else None)
        elif status=="priority" and row:
            with connect(_database_path()) as connection: connection.execute("UPDATE downloads SET status='queued',priority=priority+1,error=NULL WHERE id=?",(row["id"],))
            _app_module().QUEUE_WAKE.set()
        elif status=="clear_error" and row:
            with connect(_database_path()) as connection: connection.execute("UPDATE downloads SET error=NULL WHERE id=?",(row["id"],))

    @app.route("/api/download/<video_id>/", methods=["GET","POST","DELETE"])
    def ta_download(video_id):
        item=_find_download(video_id)
        if request.method=="GET": return jsonify(item) if item else _error("Download item not found",404)
        if request.method=="POST":
            data=_json_object() or {}; status=data.get("status")
            if status not in {"pending","ignore","ignore-force","priority"}: return _error("invalid status")
            if not item and status!="ignore-force": return _error("Download item not found",404)
            _update_download_status(video_id,status); return jsonify({"status":status})
        with connect(_database_path()) as connection:
            connection.execute(
                "UPDATE downloads SET status='cancelled', error='Cancelled by API', finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE source_url LIKE ? AND status IN ('queued','downloading','error')",
                (f"%{video_id}%",),
            )
            connection.execute("DELETE FROM ignored_videos WHERE video_id=?",(video_id,))
        return Response(status=204)

    @app.get("/api/download/aggs/")
    def ta_download_aggs():
        filter_view=request.args.get("filter")
        if filter_view not in {"pending","ignore"}: return _error("filter is required")
        grouped={}
        for item in _download_rows():
            if item["status"]!=filter_view: continue
            if request.args.get("channel") and item["channel_id"] != request.args["channel"]: continue
            if request.args.get("vid_type") and item["vid_type"] != request.args["vid_type"]: continue
            key=(item["channel_name"],item["channel_id"]); grouped[key]=grouped.get(key,0)+1
        return jsonify({"doc_count_error_upper_bound":0,"sum_other_doc_count":0,"buckets":[{"key":list(key),"key_as_string":"|".join(key),"doc_count":count} for key,count in sorted(grouped.items(),key=lambda pair:pair[1],reverse=True)]})

    @app.post("/api/watched/")
    def ta_watched():
        data=_json_object() or {}
        if not data.get("id") or not isinstance(data.get("is_watched"),bool): return _error("missing id or is_watched")
        identity=str(data["id"])
        with connect(_database_path()) as connection:
            media=connection.execute("SELECT id FROM media WHERE video_id=?",(identity,)).fetchall()
            if not media: media=connection.execute("SELECT m.id FROM media m LEFT JOIN channels c ON c.id=m.channel_db_id LEFT JOIN playlist_media pm ON pm.media_id=m.id LEFT JOIN playlists p ON p.id=pm.playlist_id WHERE c.channel_id=? OR p.playlist_id=?",(identity,identity)).fetchall()
        for row in media: set_media_watched(_database_path(),row["id"],data["is_watched"])
        return jsonify({"id":identity,"is_watched":data["is_watched"]})

    @app.get("/api/search/")
    def ta_search():
        query=request.args.get("query")
        if query is None: return jsonify({"message":"no search query specified"}),400
        query_type="simple"; term=query
        if ":" in query.split()[0]:
            prefix,term=query.split()[0].split(":",1)
            if prefix in {"video","channel","playlist","full"}: query_type=prefix; term=" ".join([term,*query.split()[1:]])
        token=f"%{term}%"
        with connect(_database_path()) as connection:
            videos=connection.execute(
                MEDIA_SELECT
                + " WHERE m.title LIKE ? OR EXISTS (SELECT 1 FROM media_tags mt2 WHERE mt2.media_id=m.id AND mt2.tag LIKE ?) GROUP BY m.id LIMIT 30",
                (token, token),
            ).fetchall() if query_type in {"simple","video"} else []
            channels=connection.execute("SELECT * FROM channels WHERE name LIKE ? LIMIT 30",(token,)).fetchall() if query_type in {"simple","channel"} else []
            playlists=connection.execute("SELECT * FROM playlists WHERE name LIKE ? OR description LIKE ? LIMIT 30",(token,token)).fetchall() if query_type in {"simple","playlist"} else []
            full=connection.execute("SELECT * FROM media WHERE subtitle_text LIKE ? LIMIT 30",(token,)).fetchall() if query_type in {"simple","full"} else []
        fulltext=[{"subtitle_channel":row["channel"] or "","subtitle_channel_id":row["channel_id"] or "","subtitle_end":"","subtitle_fragment_id":f"{row['video_id']}-0","subtitle_index":0,"subtitle_lang":"en","subtitle_last_refresh":_epoch(row["updated_at"]),"subtitle_line":row["subtitle_text"] or "","subtitle_source":"auto","subtitle_start":"","title":row["title"],"youtube_id":row["video_id"],"_index":"ta_subtitle"} for row in full]
        return jsonify({"results":{"video_results":[{**_video_json(dict(row)),"_index":"ta_video"} for row in videos],"channel_results":[{**_channel_json(dict(row)),"_index":"ta_channel"} for row in channels],"playlist_results":[{**_playlist_json(dict(row)),"_index":"ta_playlist"} for row in playlists],"fulltext_results":fulltext},"queryType":query_type})

    @app.get("/api/notification/")
    def ta_notification():
        if request.args.get("filter") not in {None,"download","settings","channel"}:
            return _error("Invalid filter.")
        return jsonify([])

    @app.route("/api/refresh/",methods=["GET","POST"])
    def ta_refresh():
        if request.method=="GET":
            refresh_type = request.args.get("type")
            refresh_id = request.args.get("id")
            if refresh_id and not refresh_type: return _error("specified id also needs type")
            if refresh_type not in {None, "video", "channel", "playlist"}: return _error("bad request")
            return jsonify({"state":"empty","total_queued":0})
        data=_json_object() or {}
        unknown = set(data) - {"video", "channel", "playlist"}
        if unknown or any(not isinstance(value, list) for value in data.values()):
            return _error("invalid refresh data")
        return jsonify({"message":"reindex task started","task_id":_task("check_reindex",data)})

    @app.route("/api/appsettings/config/",methods=["GET","POST"])
    def ta_config():
        if request.method=="GET": return jsonify(_app_config())
        data=_json_object()
        if data is None: return _error("Expected a JSON object.")
        current=json.loads(_setting("api_config_json","{}"))
        for section,values in data.items():
            if section not in {"subscriptions","downloads","application"} or not isinstance(values,dict): return _error(f"Unknown fields: {section}")
            current.setdefault(section,{}).update(values)
        translated={}
        subs=data.get("subscriptions",{}); downloads=data.get("downloads",{})
        if "channel_size" in subs and subs["channel_size"] is not None: translated["collection_limit"]=int(subs["channel_size"])
        if "limit_speed" in downloads: translated["rate_limit"]="" if downloads["limit_speed"] is None else str(downloads["limit_speed"])
        if "sleep_interval" in downloads and downloads["sleep_interval"] is not None:
            try:
                sleep_interval = int(downloads["sleep_interval"])
            except (TypeError, ValueError):
                return _error("sleep_interval must be a whole number")
            if not 0 <= sleep_interval <= 86400:
                return _error("sleep_interval must be between 0 and 86400")
            translated["subscription_download_delay"] = sleep_interval
        if "add_metadata" in downloads: translated["write_info_json"]=int(bool(downloads["add_metadata"]))
        if "subtitle" in downloads: translated["download_subtitles"]=int(downloads["subtitle"] is not None); translated["subtitle_languages"]=downloads["subtitle"] or "en"
        _set_setting("api_config_json",json.dumps(current))
        if translated: update_settings(_database_path(),translated)
        return jsonify(_app_config())

    @app.route("/api/appsettings/cookie/",methods=["GET","POST","PUT","DELETE"])
    def ta_cookie():
        path=Path(get_settings(_database_path())["cookie_path"])
        if request.method=="DELETE": path.unlink(missing_ok=True); return Response(status=204)
        if request.method=="PUT":
            data=_json_object() or {}; cookie=data.get("cookie")
            if not cookie: return _error("missing cookie key in request data")
            if "Netscape HTTP Cookie File" not in cookie: return _error("[cookie]: import failed, not valid")
            path.parent.mkdir(parents=True,exist_ok=True); path.write_text(cookie,encoding="utf-8")
        enabled=path.is_file(); now=int(datetime.now(timezone.utc).timestamp())
        return jsonify({"cookie_enabled":enabled,"status":enabled,"validated":now,"validated_str":datetime.fromtimestamp(now,timezone.utc).isoformat()})

    @app.route("/api/appsettings/token/",methods=["GET","DELETE"])
    def ta_token():
        if request.method=="DELETE": _set_setting("api_token",secrets.token_hex(20)); return Response(status=204)
        return jsonify({"token":_setting("api_token")})

    @app.route("/api/appsettings/snapshot/",methods=["GET","POST"])
    def ta_snapshots():
        if request.method=="POST": path=create_snapshot(_database_path(),_snapshot_dir()); return jsonify({"snapshot_name":path.stem})
        items=[_snapshot_item(_snapshot_dir() / item["name"]) for item in list_snapshots(_snapshot_dir())]
        return jsonify({"next_exec":0,"next_exec_str":"","expire_after":"never","snapshots":items})

    @app.route("/api/appsettings/snapshot/<snapshot_id>/",methods=["GET","POST","DELETE"])
    def ta_snapshot(snapshot_id):
        path=_snapshot_path(snapshot_id)
        if not path:
            if request.method=="GET": return _error("snapshot not found",404)
            return _error("failed to delete snapshot" if request.method=="DELETE" else "failed to restore snapshot")
        if request.method=="GET": return jsonify(_snapshot_item(path))
        if request.method=="DELETE": path.unlink(); return Response(status=204)
        source=sqlite3.connect(path); target=sqlite3.connect(_database_path())
        try: source.backup(target)
        finally: target.close(); source.close()
        return jsonify({"accepted":True})

    def backup_data(path): return {"filename":path.name,"file_path":str(path),"file_size":path.stat().st_size,"timestamp":datetime.fromtimestamp(path.stat().st_mtime,timezone.utc).isoformat(),"reason":"manual"}

    @app.route("/api/appsettings/backup/",methods=["GET","POST"])
    def ta_backups():
        if request.method=="GET": return jsonify([backup_data(_snapshot_dir() / item["name"]) for item in list_snapshots(_snapshot_dir())])
        path=create_snapshot(_database_path(),_snapshot_dir()); return jsonify({"message":"backup task started","task_id":_task("run_backup",path.name)})

    @app.route("/api/appsettings/backup/<path:filename>/",methods=["GET","POST","DELETE"])
    def ta_backup(filename):
        path=(_snapshot_dir()/filename).resolve()
        try: path.relative_to(_snapshot_dir().resolve())
        except ValueError: return _error("file not found",404)
        if not path.is_file(): return _error("file not found",404)
        if request.method=="GET": return jsonify(backup_data(path))
        if request.method=="DELETE": path.unlink(); return Response(status=204)
        return jsonify({"message":"backup restore task started","filename":filename,"task_id":_task("restore_backup",filename)})

    @app.post("/api/appsettings/rescan-filesystem/")
    def ta_rescan():
        data=_json_object() or {}
        if not all(key in data and isinstance(data[key],bool) for key in ("ignore_error","prefer_local")): return _error("ignore_error and prefer_local are required")
        result=rescan_filesystem(_database_path(),_download_dir()); return jsonify({"message":"rescan filesystem task completed","task_id":_task("rescan_filesystem",result),"status":"SUCCESS"})

    @app.post("/api/appsettings/manual-import/")
    def ta_manual_import():
        data=_json_object() or {}
        if not all(key in data and isinstance(data[key],bool) for key in ("ignore_error","prefer_local")): return _error("ignore_error and prefer_local are required")
        result=rescan_filesystem(_database_path(),_download_dir()); return jsonify({"message":"manual import task completed","task_id":_task("manual_import",result),"status":"SUCCESS"})

    def stat_item(condition="1=1"):
        with connect(_database_path()) as connection: row=connection.execute(f"SELECT COUNT(*) n,COALESCE(SUM(file_size),0) size,COALESCE(SUM(duration),0) duration FROM media WHERE {condition}").fetchone()
        return {"doc_count":row["n"],"media_size":row["size"],"duration":row["duration"],"duration_str":_duration_str(row["duration"])}

    @app.get("/api/stats/video/")
    def ta_stats_video():
        total=stat_item(); return jsonify({**total,"type_videos":stat_item("video_type='video'"),"type_shorts":stat_item("video_type='short'"),"type_streams":stat_item("video_type='stream'"),"active_true":total,"active_false":stat_item("0")})

    @app.get("/api/stats/channel/")
    def ta_stats_channel():
        with connect(_database_path()) as connection: total=connection.execute("SELECT COUNT(*) FROM channels").fetchone()[0]; subscribed=connection.execute("SELECT COUNT(*) FROM subscriptions WHERE kind='channel' AND enabled=1").fetchone()[0]
        return jsonify({"doc_count":total,"active_true":total,"active_false":0,"subscribed_true":subscribed,"subscribed_false":max(0,total-subscribed)})

    @app.get("/api/stats/playlist/")
    def ta_stats_playlist():
        with connect(_database_path()) as connection: total=connection.execute("SELECT COUNT(*) FROM playlists").fetchone()[0]; subscribed=connection.execute("SELECT COUNT(*) FROM subscriptions WHERE kind='playlist' AND enabled=1").fetchone()[0]
        return jsonify({"doc_count":total,"active_true":total,"active_false":0,"subscribed_true":subscribed,"subscribed_false":max(0,total-subscribed)})

    @app.get("/api/stats/download/")
    def ta_stats_download():
        items=_download_rows(); pending=[i for i in items if i["status"]=="pending"]
        return jsonify({"pending":len(pending),"ignore":sum(i["status"]=="ignore" for i in items),"pending_videos":sum(i["vid_type"]=="videos" for i in pending),"pending_shorts":sum(i["vid_type"]=="shorts" for i in pending),"pending_streams":sum(i["vid_type"]=="streams" for i in pending)})

    @app.get("/api/stats/watch/")
    def ta_stats_watch():
        with connect(_database_path()) as connection: rows=connection.execute("SELECT duration,watch_progress,watched FROM media").fetchall()
        total_duration=sum(int(r["duration"] or 0) for r in rows); watched=[r for r in rows if r["watched"]]; unwatched=[r for r in rows if not r["watched"]]
        def watch_item(items):
            duration=sum(int(r["duration"] or 0) for r in items); progress=sum(int(r["duration"] or 0)*float(r["watch_progress"] or 0)/100 for r in items)
            return {"duration":int(duration),"duration_str":_duration_str(duration),"progress":progress,"items":len(items)}
        return jsonify({"total":{"duration":total_duration,"duration_str":_duration_str(total_duration),"items":len(rows)},"unwatched":watch_item(unwatched),"watched":watch_item(watched)})

    @app.get("/api/stats/downloadhist/")
    def ta_stats_downloadhist():
        with connect(_database_path()) as connection: rows=connection.execute("SELECT substr(downloaded_at,1,10) date,COUNT(*) count,COALESCE(SUM(file_size),0) media_size FROM media GROUP BY substr(downloaded_at,1,10) ORDER BY date").fetchall()
        return jsonify([dict(row) for row in rows])

    @app.get("/api/stats/biggestchannels/")
    def ta_stats_biggest():
        order=request.args.get("order","doc_count"); order_sql={"doc_count":"COUNT(m.id)","duration":"SUM(m.duration)","media_size":"SUM(m.file_size)"}.get(order)
        if not order_sql: return _error("Invalid order.")
        with connect(_database_path()) as connection: rows=connection.execute(f"SELECT c.channel_id id,c.name,COUNT(m.id) doc_count,COALESCE(SUM(m.duration),0) duration,COALESCE(SUM(m.file_size),0) media_size FROM channels c LEFT JOIN media m ON m.channel_db_id=c.id GROUP BY c.id ORDER BY {order_sql} DESC LIMIT 10").fetchall()
        return jsonify([{**dict(row),"duration_str":_duration_str(row["duration"])} for row in rows])

    @app.get("/api/user/account/")
    def ta_user_account(): return jsonify({"id":1,"name":"tubearchivist","is_superuser":True,"is_staff":True,"groups":[],"user_permissions":[],"last_login":None})

    @app.route("/api/user/me/",methods=["GET","POST"])
    def ta_user_me():
        if request.method=="POST":
            data=_json_object()
            if data is None: return _error("Expected a JSON object.")
            config=_user_config(); unknown=set(data)-set(config)
            if unknown: return _error(f"Unknown fields: {', '.join(sorted(unknown))}")
            config.update(data); _set_setting("api_user_config",json.dumps(config))
            if "page_size" in data: update_settings(_database_path(),{"page_size":int(data["page_size"])})
        return jsonify(_user_config())

    @app.post("/api/user/login/")
    def ta_login():
        data=_json_object() or {}
        if not data.get("username") or not data.get("password"): return _error("Invalid credentials")
        return Response(status=204)

    @app.post("/api/user/logout/")
    def ta_logout(): return Response(status=204)

    @app.get("/api/task/by-name/")
    def ta_task_list():
        _ensure_schema()
        with connect(_database_path()) as connection: rows=connection.execute("SELECT * FROM api_tasks ORDER BY date_done DESC").fetchall()
        return jsonify([_task_dict(row) for row in rows])

    @app.route("/api/task/by-name/<task_name>/",methods=["GET","POST"])
    def ta_task_name(task_name):
        _ensure_schema()
        task_config = TASK_CONFIG.get(task_name)
        if not task_config: return _error("task name not found",404)
        if request.method=="POST":
            if not task_config["api_start"]:
                return _error("can not start task through this endpoint",404)
            result = "done"
            if task_name == "update_subscribed":
                result = {"queued": queue_subscription_scan(_database_path())}
                _app_module().QUEUE_WAKE.set()
            elif task_name == "download_pending":
                _app_module().QUEUE_WAKE.set()
            elif task_name == "run_backup":
                result = {"filename": create_snapshot(_database_path(), _snapshot_dir()).name}
            return jsonify({"message":f"{task_name} task started","task_id":_task(task_name,result)})
        with connect(_database_path()) as connection: rows=connection.execute("SELECT * FROM api_tasks WHERE name=? ORDER BY date_done DESC",(task_name,)).fetchall()
        return jsonify([_task_dict(row) for row in rows])

    @app.route("/api/task/by-id/<task_id>/",methods=["GET","POST"])
    def ta_task_id(task_id):
        _ensure_schema()
        with connect(_database_path()) as connection: row=connection.execute("SELECT * FROM api_tasks WHERE task_id=?",(task_id,)).fetchone()
        if not row: return _error("task not found",404)
        if request.method=="POST":
            data=_json_object() or {}
            if data.get("command") not in {"stop","kill"}: return _error("invalid command")
            task_config = TASK_CONFIG.get(row["name"], {})
            if not task_config.get("api_stop"):
                return _error(f"task can not be {data['command']}ped" if data["command"] == "stop" else "task can not be killed")
            with connect(_database_path()) as connection: connection.execute("UPDATE api_tasks SET status='REVOKED' WHERE task_id=?",(task_id,))
            return Response(status=204)
        return jsonify(_task_dict(row))

    @app.get("/api/task/schedule/")
    def ta_schedule_list():
        _ensure_schema()
        with connect(_database_path()) as connection: rows=connection.execute("SELECT * FROM api_schedules ORDER BY name").fetchall()
        return jsonify([{"name":row["name"],"schedule":row["schedule"],"schedule_human":row["schedule"],"last_run_at":row["last_run_at"],"config":json.loads(row["config_json"])} for row in rows])

    @app.route("/api/task/schedule/<task_name>/",methods=["GET","POST","DELETE"])
    def ta_schedule(task_name):
        _ensure_schema()
        if request.method=="POST":
            if task_name not in TASK_CONFIG: return _error("task name not found",404)
            data=_json_object() or {}; schedule=data.get("schedule"); config=data.get("config")
            if schedule is None and config is None: return _error("expected schedule or config key")
            with connect(_database_path()) as connection:
                current=connection.execute("SELECT * FROM api_schedules WHERE name=?",(task_name,)).fetchone()
                if schedule is None:
                    if current is None: return _error("schedule is required")
                    schedule=current["schedule"]
                if config is None:
                    config=json.loads(current["config_json"]) if current else {}
                connection.execute("INSERT INTO api_schedules(name,schedule,config_json) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET schedule=excluded.schedule,config_json=excluded.config_json,updated_at=CURRENT_TIMESTAMP",(task_name,str(schedule),json.dumps(config)))
        elif request.method=="DELETE":
            with connect(_database_path()) as connection:
                cursor=connection.execute("DELETE FROM api_schedules WHERE name=?",(task_name,))
            if not cursor.rowcount: return _error("schedule not found",404)
            return Response(status=204)
        with connect(_database_path()) as connection: row=connection.execute("SELECT * FROM api_schedules WHERE name=?",(task_name,)).fetchone()
        if not row: return _error("schedule not found",404)
        return jsonify({"name":row["name"],"schedule":row["schedule"],"schedule_human":row["schedule"],"last_run_at":row["last_run_at"],"config":json.loads(row["config_json"])})

    @app.route("/api/task/notification/",methods=["GET","POST","DELETE"])
    def ta_task_notification():
        _ensure_schema()
        if request.method=="POST":
            data=_json_object() or {}
            if not data.get("task_name") or not data.get("url"): return _error("task_name and url are required")
            with connect(_database_path()) as connection: connection.execute("INSERT OR IGNORE INTO api_notifications(task_name,url) VALUES(?,?)",(data["task_name"],data["url"]))
        elif request.method=="DELETE":
            data=_json_object() or {}
            with connect(_database_path()) as connection: connection.execute("DELETE FROM api_notifications WHERE task_name=? AND (? IS NULL OR url=?)",(data.get("task_name"),data.get("url"),data.get("url")))
            return Response(status=204)
        with connect(_database_path()) as connection: rows=connection.execute("SELECT * FROM api_notifications").fetchall()
        grouped={}
        for row in rows: grouped.setdefault(row["task_name"],{"title":row["task_name"],"urls":[]})["urls"].append(row["url"])
        return jsonify(grouped)

    @app.post("/api/task/notification/test/")
    def ta_notification_test():
        data=_json_object() or {}
        if not data.get("url"): return _error("url is required")
        return jsonify({"success":True,"message":"test notification accepted"})

    @app.route("/api/appsettings/membership/token/",methods=["GET","POST","DELETE"])
    def ta_membership_token():
        if request.method=="DELETE": _set_setting("membership_token",""); return Response(status=204)
        if request.method=="POST":
            data=_json_object() or {}
            if "token" not in data: return _error("token is required")
            _set_setting("membership_token",data["token"] or "")
        token = _setting("membership_token", "")
        return jsonify({"token": token or None})

    @app.get("/api/appsettings/membership/profile/")
    def ta_membership_profile(): return jsonify({"message":"membership service is not configured"}), 400

    @app.post("/api/appsettings/membership/sync/")
    def ta_membership_sync(): return Response(status=204)

    @app.get("/api/schema/")
    def ta_schema():
        paths={}
        for rule in app.url_map.iter_rules():
            if (
                not rule.rule.startswith("/api/")
                or not rule.endpoint.startswith("ta_")
                or rule.endpoint in {"ta_schema", "ta_docs"}
            ):
                continue
            openapi_path = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", rule.rule)
            path_parameters = [
                {
                    "name": name,
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
                for name in re.findall(r"{([^}]+)}", openapi_path)
            ]
            paths[openapi_path]={}
            for method in sorted(rule.methods-{"HEAD","OPTIONS"}):
                operation = {
                    "operationId": f"{rule.endpoint}_{method.lower()}",
                    "responses": {
                        "200": {
                            "description": "TubeArchivist compatible response",
                            "content": {"application/json": {"schema": {}}},
                        }
                    },
                    "security": [{"tokenAuth": []}],
                }
                if path_parameters:
                    operation["parameters"] = path_parameters
                if method in {"POST", "PUT", "PATCH", "DELETE"}:
                    operation["requestBody"] = {
                        "required": False,
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    }
                paths[openapi_path][method.lower()] = operation
        return jsonify({"openapi":"3.0.3","info":{"title":"TubeArchivist API","version":TA_VERSION},"paths":paths,"components":{"securitySchemes":{"tokenAuth":{"type":"apiKey","in":"header","name":"Authorization"}}}})

    @app.get("/api/docs/")
    def ta_docs():
        return render_template_string("""<!doctype html>
<html><head><title>TubeArchivist API</title><style>
body{font:15px system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem;background:#111827;color:#e5e7eb}a{color:#7dd3fc}input{padding:.6rem;border-radius:.35rem;border:1px solid #475569;background:#0f172a;color:#fff}.toolbar{display:flex;gap:.7rem;align-items:center;flex-wrap:wrap}.path{border:1px solid #334155;border-radius:.5rem;margin:.6rem 0;background:#1e293b}.path summary{padding:.7rem;cursor:pointer}.operation{padding:.6rem 1rem;border-top:1px solid #334155}.method{display:inline-block;min-width:4.5rem;font-weight:700;color:#86efac}.post,.put,.patch,.delete{color:#fbbf24}button{padding:.35rem .7rem}pre{white-space:pre-wrap;background:#020617;padding:.7rem;max-height:20rem;overflow:auto}
</style></head><body><h1>TubeArchivist API {{ version }}</h1>
<div class="toolbar"><label>Filter <input id="filter" placeholder="video, download, settings"></label><label>Token <input id="token" placeholder="optional API token"></label><a href="/api/schema/">OpenAPI schema</a></div>
<p id="status">Loading API documentation...</p><main id="paths"></main>
<script>
const root=document.getElementById('paths'),status=document.getElementById('status'),filter=document.getElementById('filter');let schema;
function render(){const needle=filter.value.toLowerCase();root.replaceChildren();const entries=Object.entries(schema.paths).filter(([path])=>path.toLowerCase().includes(needle));status.textContent=`${entries.length} documented paths - OpenAPI ${schema.openapi}`;for(const [path,methods] of entries){const details=document.createElement('details');details.className='path';const summary=document.createElement('summary');summary.textContent=path;details.append(summary);for(const [method,operation] of Object.entries(methods)){const section=document.createElement('section');section.className='operation';const label=document.createElement('span');label.className=`method ${method}`;label.textContent=method.toUpperCase();section.append(label,document.createTextNode(operation.operationId||''));if(method==='get'&&!path.includes('{')){const button=document.createElement('button');button.textContent='Try GET';const output=document.createElement('pre');output.hidden=true;button.onclick=async()=>{const token=document.getElementById('token').value;const response=await fetch(path,{headers:token?{Authorization:`Token ${token}`}:{}});output.hidden=false;output.textContent=`${response.status} ${response.statusText}\n${await response.text()}`};section.append(document.createTextNode(' '),button,output)}details.append(section)}root.append(details)}}
filter.addEventListener('input',render);fetch('/api/schema/').then(response=>response.json()).then(value=>{schema=value;render()}).catch(error=>{status.textContent=`Unable to load schema: ${error}`});
</script>
</body></html>""",version=TA_VERSION)

    @app.get("/youtube/<channel_id>/<path:filename>")
    @app.get("/media/<channel_id>/<path:filename>")
    def ta_media_file(channel_id,filename):
        with connect(_database_path()) as connection:
            match = connection.execute(
                "SELECT video_id FROM media WHERE channel_id=? AND file_path LIKE ? LIMIT 1",
                (channel_id, f"%/{filename}"),
            ).fetchone()
            if match is None:
                match = connection.execute(
                    "SELECT video_id FROM media WHERE channel_id=? AND file_path LIKE ? LIMIT 1",
                    (channel_id, f"%\\{filename}"),
                ).fetchone()
        row=_media_row(match["video_id"]) if match else None
        if not row: return _error("video not found",404)
        path=Path(row["file_path"]).resolve()
        try: path.relative_to(_download_dir().resolve())
        except ValueError: return _error("video not found",404)
        return send_file(path,conditional=True,max_age=3600)
