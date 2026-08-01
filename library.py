"""SQLite persistence for channels, downloaded media, and the download queue."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from yt_dlp import YoutubeDL


CHANNELS_SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extractor TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source_url TEXT,
    thumbnail_url TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(extractor, channel_id)
)
"""

MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extractor TEXT NOT NULL,
    video_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    webpage_url TEXT,
    title TEXT NOT NULL,
    channel TEXT,
    channel_id TEXT,
    channel_db_id INTEGER REFERENCES channels(id),
    duration INTEGER,
    upload_date TEXT,
    description TEXT,
    thumbnail_url TEXT,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    extension TEXT,
    metadata_json TEXT NOT NULL,
    watch_progress REAL NOT NULL DEFAULT 0,
    watched INTEGER NOT NULL DEFAULT 0,
    watched_at TEXT,
    downloaded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(extractor, video_id)
)
"""

DOWNLOADS_SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_url TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'auto',
    parent_id INTEGER REFERENCES downloads(id),
    source_position INTEGER,
    item_count INTEGER,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued', 'downloading', 'complete', 'error', 'cancelled')),
    progress REAL NOT NULL DEFAULT 0,
    percent TEXT,
    speed TEXT,
    eta TEXT,
    title TEXT,
    filename TEXT,
    file_path TEXT,
    media_id INTEGER REFERENCES media(id) ON DELETE SET NULL,
    error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

SETTINGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

IGNORED_SCHEMA = """
CREATE TABLE IF NOT EXISTS ignored_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extractor TEXT NOT NULL,
    video_id TEXT,
    source_url TEXT NOT NULL UNIQUE,
    title TEXT,
    ignored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(extractor, video_id)
)
"""

DEFAULT_SETTINGS = {
    "collection_limit": "100",
    "download_quality": "1080",
}


@contextmanager
def connect(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a configured SQLite connection."""
    connection = sqlite3.connect(database_path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def initialize_database(database_path: Path) -> None:
    """Create or migrate the database without discarding existing records."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as connection:
        connection.execute(CHANNELS_SCHEMA)
        connection.execute(MEDIA_SCHEMA)
        columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(media)")
        }
        if "channel_db_id" not in columns:
            connection.execute(
                "ALTER TABLE media ADD COLUMN channel_db_id INTEGER REFERENCES channels(id)"
            )
        if "watched" not in columns:
            connection.execute(
                "ALTER TABLE media ADD COLUMN watched INTEGER NOT NULL DEFAULT 0"
            )
        if "watched_at" not in columns:
            connection.execute("ALTER TABLE media ADD COLUMN watched_at TEXT")
        if "watch_progress" not in columns:
            connection.execute(
                "ALTER TABLE media ADD COLUMN watch_progress REAL NOT NULL DEFAULT 0"
            )
        connection.execute(
            "UPDATE media SET watch_progress=100 WHERE watched=1 AND watch_progress<95"
        )

        connection.execute(DOWNLOADS_SCHEMA)
        connection.execute(SETTINGS_SCHEMA)
        connection.execute(IGNORED_SCHEMA)
        connection.executemany(
            "INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)",
            DEFAULT_SETTINGS.items(),
        )
        download_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(downloads)")
        }
        if "kind" not in download_columns:
            connection.execute(
                "ALTER TABLE downloads ADD COLUMN kind TEXT NOT NULL DEFAULT 'video'"
            )
        if "parent_id" not in download_columns:
            connection.execute(
                "ALTER TABLE downloads ADD COLUMN parent_id INTEGER REFERENCES downloads(id)"
            )
        if "source_position" not in download_columns:
            connection.execute(
                "ALTER TABLE downloads ADD COLUMN source_position INTEGER"
            )
        if "item_count" not in download_columns:
            connection.execute("ALTER TABLE downloads ADD COLUMN item_count INTEGER")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_downloaded ON media(downloaded_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_channel ON media(channel_db_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_downloads_queue ON downloads(status, id)"
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_downloads_parent_url
            ON downloads(parent_id, source_url)
            WHERE parent_id IS NOT NULL
            """
        )

        existing = connection.execute(
            """
            SELECT DISTINCT extractor, channel_id, channel, metadata_json
            FROM media
            WHERE channel_id IS NOT NULL AND channel_id != ''
            """
        ).fetchall()
        for row in existing:
            try:
                metadata = json.loads(row["metadata_json"])
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            source_url = metadata.get("channel_url") or metadata.get("uploader_url")
            connection.execute(
                """
                INSERT INTO channels (extractor, channel_id, name, source_url)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(extractor, channel_id) DO UPDATE SET
                    name=excluded.name,
                    source_url=COALESCE(excluded.source_url, channels.source_url),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    row["extractor"],
                    row["channel_id"],
                    row["channel"] or row["channel_id"],
                    source_url,
                ),
            )
        connection.execute(
            """
            UPDATE media
            SET channel_db_id = (
                SELECT channels.id FROM channels
                WHERE channels.extractor = media.extractor
                  AND channels.channel_id = media.channel_id
            )
            WHERE channel_db_id IS NULL AND channel_id IS NOT NULL
            """
        )


def save_media(
    database_path: Path,
    source_url: str,
    info: dict,
    file_path: Path,
) -> int:
    """Insert or refresh a downloaded video and its normalized channel."""
    extractor = str(info.get("extractor_key") or info.get("extractor") or "unknown")
    video_id = str(info.get("id") or "")
    if not video_id:
        raise ValueError("yt-dlp metadata did not contain a video ID.")

    resolved_file = file_path.resolve()
    if not resolved_file.is_file():
        raise FileNotFoundError(f"Downloaded file not found: {resolved_file}")

    sanitized = YoutubeDL.sanitize_info(info)
    metadata_json = json.dumps(
        sanitized,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    channel_id = info.get("channel_id") or info.get("uploader_id")
    channel_name = info.get("channel") or info.get("uploader")
    channel_source_url = info.get("channel_url") or info.get("uploader_url")
    channel_thumbnail_url = info.get("channel_thumbnail")

    with connect(database_path) as connection:
        channel_db_id = None
        if channel_id:
            connection.execute(
                """
                INSERT INTO channels (
                    extractor, channel_id, name, source_url, thumbnail_url
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(extractor, channel_id) DO UPDATE SET
                    name=excluded.name,
                    source_url=COALESCE(excluded.source_url, channels.source_url),
                    thumbnail_url=COALESCE(
                        excluded.thumbnail_url, channels.thumbnail_url
                    ),
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    extractor,
                    str(channel_id),
                    str(channel_name or channel_id),
                    channel_source_url,
                    channel_thumbnail_url,
                ),
            )
            channel_row = connection.execute(
                "SELECT id FROM channels WHERE extractor=? AND channel_id=?",
                (extractor, str(channel_id)),
            ).fetchone()
            channel_db_id = int(channel_row["id"])

        values = {
            "extractor": extractor,
            "video_id": video_id,
            "source_url": source_url,
            "webpage_url": info.get("webpage_url") or source_url,
            "title": str(info.get("title") or video_id),
            "channel": channel_name,
            "channel_id": channel_id,
            "channel_db_id": channel_db_id,
            "duration": int(info["duration"]) if info.get("duration") else None,
            "upload_date": info.get("upload_date"),
            "description": info.get("description"),
            "thumbnail_url": info.get("thumbnail"),
            "file_path": str(resolved_file),
            "file_size": resolved_file.stat().st_size,
            "extension": resolved_file.suffix.lstrip(".") or info.get("ext"),
            "metadata_json": metadata_json,
        }
        cursor = connection.execute(
            """
            INSERT INTO media (
                extractor, video_id, source_url, webpage_url, title, channel,
                channel_id, channel_db_id, duration, upload_date, description,
                thumbnail_url, file_path, file_size, extension, metadata_json
            ) VALUES (
                :extractor, :video_id, :source_url, :webpage_url, :title,
                :channel, :channel_id, :channel_db_id, :duration, :upload_date,
                :description, :thumbnail_url, :file_path, :file_size,
                :extension, :metadata_json
            )
            ON CONFLICT(extractor, video_id) DO UPDATE SET
                source_url=excluded.source_url,
                webpage_url=excluded.webpage_url,
                title=excluded.title,
                channel=excluded.channel,
                channel_id=excluded.channel_id,
                channel_db_id=excluded.channel_db_id,
                duration=excluded.duration,
                upload_date=excluded.upload_date,
                description=excluded.description,
                thumbnail_url=excluded.thumbnail_url,
                file_path=excluded.file_path,
                file_size=excluded.file_size,
                extension=excluded.extension,
                metadata_json=excluded.metadata_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            values,
        )
        row = connection.execute(
            "SELECT id FROM media WHERE extractor=? AND video_id=?",
            (extractor, video_id),
        ).fetchone()
        return int(row["id"] if row else cursor.lastrowid)


def get_settings(database_path: Path) -> dict:
    """Return typed application settings, falling back to safe defaults."""
    with connect(database_path) as connection:
        rows = connection.execute("SELECT key, value FROM app_settings").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    return {
        "collection_limit": int(
            values.get("collection_limit", DEFAULT_SETTINGS["collection_limit"])
        ),
        "download_quality": values.get(
            "download_quality", DEFAULT_SETTINGS["download_quality"]
        ),
    }


def update_settings(database_path: Path, settings: dict) -> None:
    """Persist validated application setting values."""
    unknown = set(settings) - set(DEFAULT_SETTINGS)
    if unknown:
        raise ValueError(f"Unsupported settings: {sorted(unknown)}")
    with connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO app_settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=CURRENT_TIMESTAMP
            """,
            [(key, str(value)) for key, value in settings.items()],
        )


def _media_rows(database_path: Path, limit: int | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT m.*, c.thumbnail_url AS channel_thumbnail_url,
               c.source_url AS channel_source_url
        FROM media AS m
        LEFT JOIN channels AS c ON c.id = m.channel_db_id
        ORDER BY m.downloaded_at DESC, m.id DESC
    """
    parameters: tuple[int, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        parameters = (limit,)
    with connect(database_path) as connection:
        return connection.execute(query, parameters).fetchall()


def list_media(
    database_path: Path,
    include_metadata: bool = False,
    limit: int | None = None,
) -> list[dict]:
    """Return the newest library records first."""
    output = []
    for row in _media_rows(database_path, limit):
        item = dict(row)
        metadata_json = item.pop("metadata_json")
        if include_metadata:
            item["metadata"] = json.loads(metadata_json)
        output.append(item)
    return output


def get_media(database_path: Path, media_id: int) -> dict | None:
    """Return one library record without exposing the large metadata payload."""
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT m.*, c.thumbnail_url AS channel_thumbnail_url,
                   c.source_url AS channel_source_url
            FROM media AS m
            LEFT JOIN channels AS c ON c.id = m.channel_db_id
            WHERE m.id=?
            """,
            (media_id,),
        ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item.pop("metadata_json")
    return item


def _upsert_ignored(
    connection: sqlite3.Connection,
    source_url: str,
    extractor: str,
    video_id: str | None,
    title: str | None,
) -> None:
    """Insert or refresh one ignore identity inside an existing transaction."""
    connection.execute(
        """
        INSERT OR IGNORE INTO ignored_videos (
            source_url, extractor, video_id, title
        ) VALUES (?, ?, ?, ?)
        """,
        (source_url, extractor, video_id, title),
    )
    connection.execute(
        """
        UPDATE ignored_videos
        SET source_url=?, extractor=?, video_id=?,
            title=COALESCE(?, title), ignored_at=CURRENT_TIMESTAMP
        WHERE source_url=?
           OR (? IS NOT NULL AND extractor=? AND video_id=?)
        """,
        (
            source_url,
            extractor,
            video_id,
            title,
            source_url,
            video_id,
            extractor,
            video_id,
        ),
    )


def add_ignored(
    database_path: Path,
    source_url: str,
    extractor: str = "url",
    video_id: str | None = None,
    title: str | None = None,
) -> None:
    """Prevent a video identity from being queued again."""
    with connect(database_path) as connection:
        _upsert_ignored(connection, source_url, extractor, video_id, title)


def is_ignored(
    database_path: Path,
    source_url: str,
    extractor: str = "url",
    video_id: str | None = None,
) -> bool:
    """Return whether a URL or stable extractor identity is ignored."""
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM ignored_videos
            WHERE source_url=?
               OR (? IS NOT NULL AND extractor=? AND video_id=?)
            LIMIT 1
            """,
            (source_url, video_id, extractor, video_id),
        ).fetchone()
    return row is not None


def list_ignored(database_path: Path) -> list[dict]:
    """Return newest ignored video identities first."""
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM ignored_videos ORDER BY ignored_at DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def remove_ignored(database_path: Path, ignored_id: int) -> bool:
    """Allow a previously ignored video to be queued again."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM ignored_videos WHERE id=?", (ignored_id,)
        )
    return cursor.rowcount == 1


def delete_media_and_ignore(database_path: Path, media_id: int) -> dict | None:
    """Forget downloaded media while permanently retaining its ignore identity."""
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT id, extractor, video_id, source_url, webpage_url, title, file_path
            FROM media WHERE id=?
            """,
            (media_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        _upsert_ignored(
            connection,
            str(item["webpage_url"] or item["source_url"]),
            str(item["extractor"]),
            str(item["video_id"]),
            str(item["title"]),
        )
        connection.execute("UPDATE downloads SET media_id=NULL WHERE media_id=?", (media_id,))
        connection.execute("DELETE FROM media WHERE id=?", (media_id,))
    return item


def set_media_watched(database_path: Path, media_id: int, watched: bool) -> bool:
    """Persist a video's watched state."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE media
            SET watched=?, watch_progress=?,
                watched_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (int(watched), 100 if watched else 0, int(watched), media_id),
        )
        return cursor.rowcount == 1


def update_media_progress(
    database_path: Path,
    media_id: int,
    percentage: float,
) -> dict | None:
    """Normalize and persist playback progress, marking 95 percent as watched."""
    if percentage < 5:
        normalized = 0.0
    elif percentage >= 95:
        normalized = 100.0
    else:
        normalized = round(min(percentage, 100), 2)

    with connect(database_path) as connection:
        connection.execute(
            """
            UPDATE media
            SET watch_progress=?,
                watched=CASE WHEN ? >= 95 THEN 1 ELSE watched END,
                watched_at=CASE
                    WHEN ? >= 95 THEN COALESCE(watched_at, CURRENT_TIMESTAMP)
                    ELSE watched_at
                END,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (normalized, normalized, normalized, media_id),
        )
        row = connection.execute(
            "SELECT id, watch_progress, watched, watched_at FROM media WHERE id=?",
            (media_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_channels(database_path: Path) -> list[dict]:
    """Return channels with persistent video and watched counts."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT c.*, COUNT(m.id) AS video_count,
                   COALESCE(SUM(m.watched), 0) AS watched_count,
                   MAX(m.downloaded_at) AS latest_download
            FROM channels AS c
            LEFT JOIN media AS m ON m.channel_db_id = c.id
            GROUP BY c.id
            ORDER BY latest_download DESC, c.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_channel(database_path: Path, channel_id: int) -> dict | None:
    """Return one channel with its aggregate library counts."""
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT c.*, COUNT(m.id) AS video_count,
                   COALESCE(SUM(m.watched), 0) AS watched_count,
                   MAX(m.downloaded_at) AS latest_download
            FROM channels AS c
            LEFT JOIN media AS m ON m.channel_db_id = c.id
            WHERE c.id=?
            GROUP BY c.id
            """,
            (channel_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_channel_media(database_path: Path, channel_id: int) -> list[dict]:
    """Return newest videos belonging to one normalized channel."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT m.*, c.thumbnail_url AS channel_thumbnail_url,
                   c.source_url AS channel_source_url
            FROM media AS m
            JOIN channels AS c ON c.id = m.channel_db_id
            WHERE c.id=?
            ORDER BY m.downloaded_at DESC, m.id DESC
            """,
            (channel_id,),
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item.pop("metadata_json")
        output.append(item)
    return output


def list_channels_missing_thumbnail(
    database_path: Path,
    limit: int = 5,
) -> list[dict]:
    """Return a bounded batch of channels that need artwork."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM channels
            WHERE thumbnail_url IS NULL AND source_url IS NOT NULL
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(row) for row in rows]


def update_channel_thumbnail(
    database_path: Path,
    channel_id: int,
    thumbnail_url: str,
) -> None:
    """Cache channel artwork discovered by yt-dlp."""
    with connect(database_path) as connection:
        connection.execute(
            """
            UPDATE channels
            SET thumbnail_url=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (thumbnail_url, channel_id),
        )


def enqueue_download(
    database_path: Path,
    source_url: str,
    *,
    kind: str = "auto",
    parent_id: int | None = None,
    source_position: int | None = None,
    title: str | None = None,
) -> dict:
    """Append a download request to the durable FIFO queue."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO downloads (
                source_url, kind, parent_id, source_position, title
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (source_url, kind, parent_id, source_position, title),
        )
        row = connection.execute(
            "SELECT * FROM downloads WHERE id=?",
            (cursor.lastrowid,),
        ).fetchone()
    return dict(row)


def enqueue_collection_items(
    database_path: Path,
    parent_id: int,
    items: list[dict],
) -> int:
    """Append a batch of unique, not-yet-archived collection videos."""
    inserted = 0
    with connect(database_path) as connection:
        for item in items:
            source_url = str(item["source_url"])
            extractor = str(item.get("extractor") or "url")
            video_id = item.get("video_id")
            ignored = connection.execute(
                """
                SELECT 1 FROM ignored_videos
                WHERE source_url=?
                   OR (? IS NOT NULL AND extractor=? AND video_id=?)
                LIMIT 1
                """,
                (source_url, video_id, extractor, video_id),
            ).fetchone()
            if ignored is not None:
                continue
            archived = connection.execute(
                """
                SELECT 1 FROM media
                WHERE source_url=? OR webpage_url=?
                LIMIT 1
                """,
                (source_url, source_url),
            ).fetchone()
            if archived is not None:
                continue
            already_pending = connection.execute(
                """
                SELECT 1 FROM downloads
                WHERE source_url=? AND status IN ('queued', 'downloading')
                LIMIT 1
                """,
                (source_url,),
            ).fetchone()
            if already_pending is not None:
                continue
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO downloads (
                    source_url, kind, parent_id, source_position, title
                ) VALUES (?, 'video', ?, ?, ?)
                """,
                (
                    source_url,
                    parent_id,
                    item.get("source_position"),
                    item.get("title"),
                ),
            )
            inserted += cursor.rowcount
    return inserted


def count_collection_items(database_path: Path, parent_id: int) -> int:
    """Return the durable number of child videos grouped under a collection."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS item_count FROM downloads WHERE parent_id=?",
            (parent_id,),
        ).fetchone()
    return int(row["item_count"])


def claim_next_download(database_path: Path) -> dict | None:
    """Atomically claim the oldest row only when no other row is active."""
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT 1 FROM downloads WHERE status='downloading' LIMIT 1"
        ).fetchone()
        if active is not None:
            return None
        row = connection.execute(
            "SELECT * FROM downloads WHERE status='queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        connection.execute(
            """
            UPDATE downloads
            SET status='downloading', started_at=CURRENT_TIMESTAMP,
                updated_at=CURRENT_TIMESTAMP, error=NULL
            WHERE id=? AND status='queued'
            """,
            (row["id"],),
        )
        claimed = connection.execute(
            "SELECT * FROM downloads WHERE id=?",
            (row["id"],),
        ).fetchone()
    return dict(claimed)


def get_download(database_path: Path, download_id: int) -> dict | None:
    """Return one durable queue row."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT * FROM downloads WHERE id=?", (download_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def cancel_download(database_path: Path, download_id: int) -> bool:
    """Cancel an individual queued or active video row."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE downloads
            SET status='cancelled', error='Cancelled by user.',
                finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND kind='video'
              AND status IN ('queued', 'downloading')
            """,
            (download_id,),
        )
    return cursor.rowcount == 1


def is_download_cancelled(database_path: Path, download_id: int) -> bool:
    """Return whether a worker's current row has been cancelled."""
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT status FROM downloads WHERE id=?", (download_id,)
        ).fetchone()
    return row is not None and row["status"] == "cancelled"


DOWNLOAD_UPDATE_FIELDS = {
    "status",
    "progress",
    "percent",
    "speed",
    "eta",
    "title",
    "filename",
    "file_path",
    "media_id",
    "kind",
    "parent_id",
    "source_position",
    "item_count",
    "error",
    "finished_at",
}


def update_download(database_path: Path, download_id: int, **changes: object) -> None:
    """Update selected queue fields without constructing arbitrary SQL columns."""
    invalid = set(changes) - DOWNLOAD_UPDATE_FIELDS
    if invalid:
        raise ValueError(f"Unsupported download fields: {sorted(invalid)}")
    if not changes:
        return
    assignments = [f"{field}=?" for field in changes]
    assignments.append("updated_at=CURRENT_TIMESTAMP")
    values = [*changes.values(), download_id]
    with connect(database_path) as connection:
        connection.execute(
            f"UPDATE downloads SET {', '.join(assignments)} WHERE id=?",
            values,
        )


def list_downloads(database_path: Path, limit: int = 100) -> list[dict]:
    """Return active work first, followed by queued and recent history."""
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT d.*, parent.title AS parent_title
            FROM downloads AS d
            LEFT JOIN downloads AS parent ON parent.id = d.parent_id
            ORDER BY CASE d.status
                WHEN 'downloading' THEN 0
                WHEN 'queued' THEN 1
                ELSE 2
            END,
            CASE WHEN d.status IN ('queued', 'downloading') THEN d.id END ASC,
            CASE WHEN d.status IN ('complete', 'error', 'cancelled') THEN d.id END DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    output = [dict(row) for row in rows]
    position = 0
    for item in output:
        if item["status"] == "queued":
            position += 1
            item["queue_position"] = position
        else:
            item["queue_position"] = None
    return output


def recover_interrupted_downloads(database_path: Path) -> int:
    """Return downloads left active by a process exit to the front of the queue."""
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE downloads
            SET status='queued', progress=0, percent=NULL, speed=NULL, eta=NULL,
                started_at=NULL, error='Requeued after application restart.',
                updated_at=CURRENT_TIMESTAMP
            WHERE status='downloading'
            """
        )
        return cursor.rowcount
