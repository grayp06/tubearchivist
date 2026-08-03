"""Higher-level archive features backed exclusively by SQLite and local files."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree

from library import connect


SUBSCRIPTIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('channel', 'playlist')),
    source_url TEXT NOT NULL UNIQUE,
    name TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    auto_download INTEGER NOT NULL DEFAULT 1,
    last_scanned_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

PLAYLISTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlists (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extractor TEXT NOT NULL DEFAULT 'custom',
    playlist_id TEXT NOT NULL,
    name TEXT NOT NULL,
    source_url TEXT,
    description TEXT,
    thumbnail_url TEXT,
    custom INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(extractor, playlist_id)
)
"""

PLAYLIST_MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS playlist_media (
    playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0,
    added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(playlist_id, media_id)
)
"""

MEDIA_TAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_tags (
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    tag TEXT NOT NULL COLLATE NOCASE,
    PRIMARY KEY(media_id, tag)
)
"""


def _add_column(connection: sqlite3.Connection, table: str, definition: str) -> None:
    name = definition.split()[0]
    columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
    if name not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def initialize_features(database_path: Path) -> None:
    """Create feature tables and additive columns without replacing user data."""
    with connect(database_path) as connection:
        connection.execute(SUBSCRIPTIONS_SCHEMA)
        connection.execute(PLAYLISTS_SCHEMA)
        connection.execute(PLAYLIST_MEDIA_SCHEMA)
        connection.execute(MEDIA_TAGS_SCHEMA)
        _add_column(connection, "media", "favorite INTEGER NOT NULL DEFAULT 0")
        _add_column(connection, "media", "video_type TEXT NOT NULL DEFAULT 'video'")
        _add_column(connection, "media", "subtitle_text TEXT")
        _add_column(connection, "downloads", "priority INTEGER NOT NULL DEFAULT 0")
        _add_column(connection, "downloads", "playlist_db_id INTEGER REFERENCES playlists(id)")
        _add_column(connection, "playlists", "playlist_sort_order TEXT NOT NULL DEFAULT 'top'")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_enabled ON subscriptions(enabled, id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_playlist_media_position ON playlist_media(playlist_id, position)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_media_favorite ON media(favorite, downloaded_at DESC)"
        )


def list_subscriptions(database_path: Path) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT * FROM subscriptions ORDER BY enabled DESC, name COLLATE NOCASE, id"
        ).fetchall()
    return [dict(row) for row in rows]


def add_subscription(
    database_path: Path,
    source_url: str,
    kind: str,
    name: str | None = None,
) -> dict:
    if kind not in {"channel", "playlist"}:
        raise ValueError("Subscription must be a channel or playlist.")
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO subscriptions (source_url, kind, name)
            VALUES (?, ?, ?)
            ON CONFLICT(source_url) DO UPDATE SET
                kind=excluded.kind, name=COALESCE(excluded.name, subscriptions.name),
                enabled=1, updated_at=CURRENT_TIMESTAMP
            """,
            (source_url, kind, name),
        )
        row = connection.execute(
            "SELECT * FROM subscriptions WHERE source_url=?", (source_url,)
        ).fetchone()
    return dict(row)


def update_subscription(
    database_path: Path,
    subscription_id: int,
    *,
    enabled: bool | None = None,
    auto_download: bool | None = None,
) -> bool:
    changes = []
    values: list[object] = []
    if enabled is not None:
        changes.append("enabled=?")
        values.append(int(enabled))
    if auto_download is not None:
        changes.append("auto_download=?")
        values.append(int(auto_download))
    if not changes:
        return False
    values.append(subscription_id)
    with connect(database_path) as connection:
        cursor = connection.execute(
            f"UPDATE subscriptions SET {', '.join(changes)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            values,
        )
    return cursor.rowcount == 1


def delete_subscription(database_path: Path, subscription_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute("DELETE FROM subscriptions WHERE id=?", (subscription_id,))
    return cursor.rowcount == 1


def queue_subscription_scan(database_path: Path) -> int:
    """Queue enabled subscriptions once; the existing worker performs extraction."""
    inserted = 0
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        subscriptions = connection.execute(
            "SELECT * FROM subscriptions WHERE enabled=1 ORDER BY id"
        ).fetchall()
        for item in subscriptions:
            pending = connection.execute(
                """
                SELECT 1 FROM downloads
                WHERE source_url=? AND status IN ('queued', 'downloading') LIMIT 1
                """,
                (item["source_url"],),
            ).fetchone()
            if pending is not None:
                continue
            connection.execute(
                """
                INSERT INTO downloads (
                    source_url, kind, title, subscription_id, available_at
                ) VALUES (
                    ?, ?, ?, ?,
                    (SELECT CASE
                        WHEN youtube_available_at > CURRENT_TIMESTAMP
                        THEN youtube_available_at END
                     FROM queue_state WHERE id=1)
                )
                """,
                (item["source_url"], item["kind"], item["name"], item["id"]),
            )
            connection.execute(
                "UPDATE subscriptions SET last_scanned_at=CURRENT_TIMESTAMP WHERE id=?",
                (item["id"],),
            )
            inserted += 1
    return inserted


def save_external_playlist(database_path: Path, source_url: str, info: dict) -> int:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "unknown")
    playlist_id = str(info.get("id") or info.get("playlist_id") or source_url)
    name = str(info.get("title") or info.get("playlist_title") or playlist_id)
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO playlists (
                extractor, playlist_id, name, source_url, description, thumbnail_url
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(extractor, playlist_id) DO UPDATE SET
                name=excluded.name, source_url=excluded.source_url,
                description=COALESCE(excluded.description, playlists.description),
                thumbnail_url=COALESCE(excluded.thumbnail_url, playlists.thumbnail_url),
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                extractor,
                playlist_id,
                name,
                source_url,
                info.get("description"),
                info.get("thumbnail"),
            ),
        )
        row = connection.execute(
            "SELECT id FROM playlists WHERE extractor=? AND playlist_id=?",
            (extractor, playlist_id),
        ).fetchone()
    return int(row["id"])


def set_download_playlist(database_path: Path, download_id: int, playlist_id: int) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "UPDATE downloads SET playlist_db_id=? WHERE id=?", (playlist_id, download_id)
        )


def attach_download_to_playlist(database_path: Path, download_id: int, media_id: int) -> None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT COALESCE(d.playlist_db_id, p.playlist_db_id) AS playlist_id,
                   d.source_position
            FROM downloads AS d
            LEFT JOIN downloads AS p ON p.id=d.parent_id
            WHERE d.id=?
            """,
            (download_id,),
        ).fetchone()
        if row is None or row["playlist_id"] is None:
            return
        connection.execute(
            """
            INSERT INTO playlist_media (playlist_id, media_id, position)
            VALUES (?, ?, ?)
            ON CONFLICT(playlist_id, media_id) DO UPDATE SET position=excluded.position
            """,
            (row["playlist_id"], media_id, row["source_position"] or 0),
        )


def create_playlist(database_path: Path, name: str, description: str = "") -> dict:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("Playlist name is required.")
    playlist_id = f"custom-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')}"
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO playlists (extractor, playlist_id, name, description, custom)
            VALUES ('custom', ?, ?, ?, 1)
            """,
            (playlist_id, cleaned, description.strip()),
        )
        row = connection.execute("SELECT * FROM playlists WHERE id=?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def list_playlists(database_path: Path) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT p.*, COUNT(pm.media_id) AS video_count,
                   COALESCE(SUM(m.duration), 0) AS total_duration,
                   COALESCE(p.thumbnail_url, MAX(m.thumbnail_url)) AS display_thumbnail
            FROM playlists AS p
            LEFT JOIN playlist_media AS pm ON pm.playlist_id=p.id
            LEFT JOIN media AS m ON m.id=pm.media_id
            GROUP BY p.id ORDER BY p.updated_at DESC, p.name COLLATE NOCASE
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_playlist(database_path: Path, playlist_id: int) -> dict | None:
    with connect(database_path) as connection:
        row = connection.execute(
            """
            SELECT p.*, COUNT(pm.media_id) AS video_count
            FROM playlists AS p
            LEFT JOIN playlist_media AS pm ON pm.playlist_id=p.id
            WHERE p.id=? GROUP BY p.id
            """,
            (playlist_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def list_playlist_media(database_path: Path, playlist_id: int) -> list[dict]:
    with connect(database_path) as connection:
        rows = connection.execute(
            """
            SELECT m.*, c.thumbnail_url AS channel_thumbnail_url,
                   c.source_url AS channel_source_url, pm.position
            FROM playlist_media AS pm
            JOIN media AS m ON m.id=pm.media_id
            LEFT JOIN channels AS c ON c.id=m.channel_db_id
            WHERE pm.playlist_id=? ORDER BY pm.position, pm.added_at, m.id
            """,
            (playlist_id,),
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        item.pop("metadata_json", None)
        output.append(item)
    return output


def add_playlist_media(database_path: Path, playlist_id: int, media_id: int) -> bool:
    with connect(database_path) as connection:
        exists = connection.execute("SELECT 1 FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        if exists is None:
            return False
        position = connection.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 AS position FROM playlist_media WHERE playlist_id=?",
            (playlist_id,),
        ).fetchone()["position"]
        cursor = connection.execute(
            "INSERT OR IGNORE INTO playlist_media (playlist_id, media_id, position) VALUES (?, ?, ?)",
            (playlist_id, media_id, position),
        )
    return cursor.rowcount == 1


def remove_playlist_media(database_path: Path, playlist_id: int, media_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM playlist_media WHERE playlist_id=? AND media_id=?",
            (playlist_id, media_id),
        )
    return cursor.rowcount == 1


def delete_playlist(database_path: Path, playlist_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute("DELETE FROM playlists WHERE id=?", (playlist_id,))
    return cursor.rowcount == 1


SORT_FIELDS = {
    "downloaded": "m.downloaded_at",
    "published": "m.upload_date",
    "title": "m.title COLLATE NOCASE",
    "duration": "m.duration",
    "size": "m.file_size",
}


def search_media(
    database_path: Path,
    *,
    query: str = "",
    channel_id: int | None = None,
    watched: str = "all",
    favorite: bool = False,
    video_type: str = "all",
    sort: str = "downloaded",
    order: str = "desc",
    page: int = 1,
    page_size: int = 24,
) -> dict:
    conditions = ["1=1"]
    parameters: list[object] = []
    if query.strip():
        token = f"%{query.strip()}%"
        conditions.append(
            "(m.title LIKE ? OR m.description LIKE ? OR m.channel LIKE ? OR m.video_id LIKE ? OR m.subtitle_text LIKE ? OR EXISTS (SELECT 1 FROM media_tags mt WHERE mt.media_id=m.id AND mt.tag LIKE ?))"
        )
        parameters.extend([token] * 6)
    if channel_id is not None:
        conditions.append("m.channel_db_id=?")
        parameters.append(channel_id)
    if watched in {"watched", "unwatched"}:
        conditions.append("m.watched=?")
        parameters.append(int(watched == "watched"))
    if favorite:
        conditions.append("m.favorite=1")
    if video_type in {"video", "short", "stream"}:
        conditions.append("m.video_type=?")
        parameters.append(video_type)
    where = " AND ".join(conditions)
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    sort_sql = SORT_FIELDS.get(sort, SORT_FIELDS["downloaded"])
    order_sql = "ASC" if order == "asc" else "DESC"
    with connect(database_path) as connection:
        total = int(
            connection.execute(f"SELECT COUNT(*) FROM media m WHERE {where}", parameters).fetchone()[0]
        )
        rows = connection.execute(
            f"""
            SELECT m.*, c.thumbnail_url AS channel_thumbnail_url,
                   c.source_url AS channel_source_url,
                   GROUP_CONCAT(mt.tag, ', ') AS tags
            FROM media AS m
            LEFT JOIN channels AS c ON c.id=m.channel_db_id
            LEFT JOIN media_tags AS mt ON mt.media_id=m.id
            WHERE {where}
            GROUP BY m.id ORDER BY {sort_sql} {order_sql}, m.id {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*parameters, page_size, (page - 1) * page_size],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item.pop("metadata_json", None)
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
    }


def set_media_favorite(database_path: Path, media_id: int, favorite: bool) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "UPDATE media SET favorite=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (int(favorite), media_id),
        )
    return cursor.rowcount == 1


def set_media_tags(database_path: Path, media_id: int, tags: str) -> bool:
    normalized = sorted(
        {tag.strip().lower() for tag in tags.split(",") if tag.strip()},
        key=str.casefold,
    )[:30]
    with connect(database_path) as connection:
        exists = connection.execute("SELECT 1 FROM media WHERE id=?", (media_id,)).fetchone()
        if exists is None:
            return False
        connection.execute("DELETE FROM media_tags WHERE media_id=?", (media_id,))
        connection.executemany(
            "INSERT INTO media_tags (media_id, tag) VALUES (?, ?)",
            [(media_id, tag[:64]) for tag in normalized],
        )
    return True


def prioritize_download(database_path: Path, download_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE downloads SET priority=priority+1, updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status='queued' AND kind='video'
            """,
            (download_id,),
        )
    return cursor.rowcount == 1


def retry_download(database_path: Path, download_id: int) -> bool:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE downloads
            SET status='queued', progress=0, percent=NULL, speed=NULL, eta=NULL,
                error=NULL, started_at=NULL, finished_at=NULL,
                available_at=(SELECT CASE
                    WHEN youtube_available_at > CURRENT_TIMESTAMP
                    THEN youtube_available_at END
                    FROM queue_state WHERE id=1),
                updated_at=CURRENT_TIMESTAMP
            WHERE id=? AND status IN ('error', 'cancelled')
            """,
            (download_id,),
        )
    return cursor.rowcount == 1


def clear_download_history(database_path: Path) -> int:
    with connect(database_path) as connection:
        cursor = connection.execute(
            "DELETE FROM downloads WHERE status IN ('complete', 'error', 'cancelled')"
        )
    return cursor.rowcount


def cancel_all_queued(database_path: Path) -> int:
    with connect(database_path) as connection:
        cursor = connection.execute(
            """
            UPDATE downloads
            SET status='cancelled', error='Cancelled by user.',
                finished_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
            WHERE status='queued' AND kind='video'
            """
        )
    return cursor.rowcount


def move_playlist_media(
    database_path: Path,
    playlist_id: int,
    media_id: int,
    direction: str,
) -> bool:
    if direction not in {"up", "down"}:
        return False
    comparator = "<" if direction == "up" else ">"
    ordering = "DESC" if direction == "up" else "ASC"
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            "SELECT position FROM playlist_media WHERE playlist_id=? AND media_id=?",
            (playlist_id, media_id),
        ).fetchone()
        if current is None:
            return False
        neighbor = connection.execute(
            f"""
            SELECT media_id, position FROM playlist_media
            WHERE playlist_id=? AND position {comparator} ?
            ORDER BY position {ordering} LIMIT 1
            """,
            (playlist_id, current["position"]),
        ).fetchone()
        if neighbor is None:
            return True
        temporary = -int(media_id)
        connection.execute(
            "UPDATE playlist_media SET position=? WHERE playlist_id=? AND media_id=?",
            (temporary, playlist_id, media_id),
        )
        connection.execute(
            "UPDATE playlist_media SET position=? WHERE playlist_id=? AND media_id=?",
            (current["position"], playlist_id, neighbor["media_id"]),
        )
        connection.execute(
            "UPDATE playlist_media SET position=? WHERE playlist_id=? AND media_id=?",
            (neighbor["position"], playlist_id, media_id),
        )
    return True


def archive_stats(database_path: Path) -> dict:
    with connect(database_path) as connection:
        media = connection.execute(
            """
            SELECT COUNT(*) AS videos, COALESCE(SUM(file_size), 0) AS bytes,
                   COALESCE(SUM(duration), 0) AS duration,
                   COALESCE(SUM(watched), 0) AS watched,
                   COALESCE(SUM(favorite), 0) AS favorites
            FROM media
            """
        ).fetchone()
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("channels", "playlists", "subscriptions", "ignored_videos")
        }
        queue = {
            row["status"]: row["count"]
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM downloads GROUP BY status"
            )
        }
        biggest = connection.execute(
            """
            SELECT c.id, c.name, COUNT(m.id) AS videos, COALESCE(SUM(m.file_size), 0) AS bytes
            FROM channels c JOIN media m ON m.channel_db_id=c.id
            GROUP BY c.id ORDER BY bytes DESC LIMIT 10
            """
        ).fetchall()
    return {**dict(media), **counts, "queue": queue, "biggest_channels": [dict(row) for row in biggest]}


VIDEO_SUFFIXES = {".mp4", ".mkv", ".webm", ".mov", ".m4v"}
VIDEO_ID_PATTERN = re.compile(r"\[([^\[\]]+)\](?:\.[^.]+)$")


def rescan_filesystem(database_path: Path, download_dir: Path) -> dict:
    """Index recognizable orphaned files without network requests or full-file reads."""
    from library import save_media

    added = 0
    missing = 0
    with connect(database_path) as connection:
        known = {str(Path(row["file_path"]).resolve()): row["id"] for row in connection.execute("SELECT id, file_path FROM media")}
        known_video_ids = {
            str(row["video_id"])
            for row in connection.execute("SELECT video_id FROM media")
        }
    disk_files = {
        str(path.resolve()): path
        for path in download_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and ".temp." not in path.name
    }
    with connect(database_path) as connection:
        for path_string in set(known) - set(disk_files):
            connection.execute("DELETE FROM media WHERE id=?", (known[path_string],))
            missing += 1
    for path_string, path in disk_files.items():
        if path_string in known:
            continue
        match = VIDEO_ID_PATTERN.search(path.name)
        if match:
            video_id = match.group(1)
        elif (
            path.parent.resolve() != download_dir.resolve()
            and path.suffix.lower() == ".mp4"
            and re.fullmatch(r"[A-Za-z0-9_-]+", path.stem)
        ):
            video_id = path.stem
        else:
            continue
        if video_id in known_video_ids:
            continue
        info_path = next(
            (
                candidate
                for candidate in path.parent.glob("*.info.json")
                if candidate.name == f"{video_id}.info.json"
                or f"[{video_id}]" in candidate.name
            ),
            None,
        )
        info: dict = {}
        if info_path is not None:
            try:
                info = json.loads(info_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                info = {}
        info.setdefault("id", video_id)
        info.setdefault("title", path.stem.rsplit(f" [{video_id}]", 1)[0])
        info.setdefault("extractor", "Filesystem")
        info.setdefault("webpage_url", f"https://www.youtube.com/watch?v={video_id}")
        if path.parent.resolve() != download_dir.resolve():
            info.setdefault("channel_id", path.parent.name)
            info.setdefault("channel", path.parent.name)
        save_media(database_path, str(info["webpage_url"]), info, path)
        added += 1
        known_video_ids.add(video_id)
    return {"added": added, "removed_missing": missing, "found": len(disk_files)}


def create_snapshot(database_path: Path, snapshot_dir: Path) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / f"library-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')}.db"
    source = sqlite3.connect(database_path)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def list_snapshots(snapshot_dir: Path) -> list[dict]:
    if not snapshot_dir.exists():
        return []
    return [
        {"name": path.name, "size": path.stat().st_size, "created_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
        for path in sorted(snapshot_dir.glob("library-*.db"), reverse=True)
        if path.is_file()
    ]


def export_archive(database_path: Path) -> dict:
    with connect(database_path) as connection:
        def rows(table: str) -> list[dict]:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]

        return {
            "version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "channels": rows("channels"),
            "media": rows("media"),
            "playlists": rows("playlists"),
            "playlist_media": rows("playlist_media"),
            "subscriptions": rows("subscriptions"),
            "ignored_videos": rows("ignored_videos"),
            "settings": rows("app_settings"),
        }


def subscriptions_opml(database_path: Path) -> bytes:
    root = ElementTree.Element("opml", version="2.0")
    ElementTree.SubElement(root, "head")
    body = ElementTree.SubElement(root, "body")
    for item in list_subscriptions(database_path):
        ElementTree.SubElement(
            body,
            "outline",
            text=item["name"] or item["source_url"],
            title=item["name"] or item["source_url"],
            type=item["kind"],
            xmlUrl=item["source_url"],
        )
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def parse_subscriptions_opml(content: bytes) -> list[dict]:
    """Parse a bounded OPML document into channel/playlist subscription rows."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError as exc:
        raise ValueError("Invalid OPML document.") from exc
    output = []
    for outline in root.findall(".//outline"):
        source_url = outline.attrib.get("xmlUrl") or outline.attrib.get("url")
        if not source_url:
            continue
        kind = outline.attrib.get("type", "channel")
        if kind not in {"channel", "playlist"}:
            kind = "channel"
        output.append(
            {
                "source_url": source_url,
                "kind": kind,
                "name": outline.attrib.get("title") or outline.attrib.get("text"),
            }
        )
    return output[:10_000]
