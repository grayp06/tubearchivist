"""TubeArchivist-compatible media and thumbnail filesystem layout."""

from __future__ import annotations

import io
import re
import shutil
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from PIL import Image

from library import connect


MAX_IMAGE_BYTES = 20 * 1024 * 1024
COMPONENT_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


def storage_component(value: object, fallback: str = "unknown") -> str:
    """Keep identifiers inside one path segment without changing YouTube IDs."""
    cleaned = COMPONENT_PATTERN.sub("_", str(value or "").strip()).strip("._")
    return cleaned or fallback


def media_relative_path(channel_id: object, video_id: object) -> Path:
    """Return TubeArchivist's media path: channel/video.mp4."""
    return Path(storage_component(channel_id)) / f"{storage_component(video_id)}.mp4"


def video_thumb_relative_path(video_id: object) -> Path:
    """Return TubeArchivist's sharded video thumbnail path."""
    item_id = storage_component(video_id)
    return Path("videos") / item_id[0].lower() / f"{item_id}.jpg"


def channel_art_relative_path(channel_id: object, art_type: str = "thumb") -> Path:
    """Return the flat TubeArchivist channel-art path."""
    if art_type not in {"thumb", "banner", "tvart"}:
        raise ValueError("Unknown channel art type.")
    return Path("channels") / f"{storage_component(channel_id)}_{art_type}.jpg"


def playlist_thumb_relative_path(playlist_id: object) -> Path:
    """Return TubeArchivist's playlist thumbnail path."""
    return Path("playlists") / f"{storage_component(playlist_id)}.jpg"


def _placeholder(kind: str) -> Image.Image:
    sizes = {
        "video": (336, 189),
        "playlist": (336, 189),
        "thumb": (256, 256),
        "banner": (1280, 200),
        "tvart": (1280, 720),
    }
    return Image.new("RGB", sizes[kind], color=(31, 41, 55))


def _read_image(source: str | Path | None, kind: str) -> Image.Image:
    if isinstance(source, Path):
        with source.open("rb") as handle:
            image = Image.open(handle)
            image.load()
            return image
    if isinstance(source, str) and source:
        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            request = Request(source, headers={"User-Agent": "TubeArchivist-Lite/1"})
            with urlopen(request, timeout=8) as response:  # nosec B310: schemes checked
                payload = response.read(MAX_IMAGE_BYTES + 1)
            if len(payload) > MAX_IMAGE_BYTES:
                raise ValueError("Thumbnail exceeds the 20 MiB limit.")
            image = Image.open(io.BytesIO(payload))
            image.load()
            return image
    return _placeholder(kind)


def cache_image(
    source: str | Path | None,
    target: Path,
    kind: str,
    *,
    skip_existing: bool = True,
) -> Path:
    """Convert one image to the same RGB JPEG form used by TubeArchivist."""
    if skip_existing and target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        image = _read_image(source, kind)
    except (OSError, ValueError):
        image = _placeholder(kind)
    with image:
        if kind in {"video", "playlist"}:
            width, height = image.size
            if width and height and width / height != 16 / 9:
                new_height = width / 16 * 9
                offset = (height - new_height) / 2
                image = image.crop((0, offset, width, height - offset))
            if kind == "playlist":
                image = image.resize((336, 189))
        image.convert("RGB").save(target, format="JPEG")
    return target


def cache_video_thumbnail(cache_dir: Path, video_id: object, source) -> Path:
    return cache_image(
        source,
        cache_dir / video_thumb_relative_path(video_id),
        "video",
    )


def cache_channel_art(
    cache_dir: Path,
    channel_id: object,
    thumb=None,
    banner=None,
    tvart=None,
    *,
    skip_existing: bool = True,
) -> dict[str, Path]:
    return {
        art_type: cache_image(
            source,
            cache_dir / channel_art_relative_path(channel_id, art_type),
            art_type,
            skip_existing=skip_existing,
        )
        for art_type, source in (
            ("thumb", thumb),
            ("banner", banner),
            ("tvart", tvart),
        )
    }


def cache_playlist_thumbnail(cache_dir: Path, playlist_id: object, source) -> Path:
    if isinstance(source, str) and not source.startswith(("http://", "https://")):
        source = cache_dir / source.lstrip("/\\")
    return cache_image(
        source,
        cache_dir / playlist_thumb_relative_path(playlist_id),
        "playlist",
    )


def select_channel_art(info: dict) -> dict[str, str | None]:
    """Use TubeArchivist's thumbnail, banner, and TV-art selection rules."""
    thumbnails = list(reversed(info.get("thumbnails") or []))
    thumb = next(
        (
            item.get("url")
            for item in thumbnails
            if item.get("width")
            and item.get("height")
            and item["width"] == item["height"]
        ),
        None,
    )
    tvart = next(
        (item.get("url") for item in thumbnails if item.get("id") == "banner_uncropped"),
        None,
    )
    if not tvart:
        tvart = next(
            (
                item.get("url")
                for item in thumbnails
                if item.get("width")
                and item.get("height")
                and item["width"] != item["height"]
                and item["width"] // item["height"] < 2
            ),
            None,
        )
    banner = next(
        (
            item.get("url")
            for item in thumbnails
            if item.get("width")
            and item.get("height")
            and item["width"] // item["height"] > 5
        ),
        None,
    )
    return {"thumb": thumb, "banner": banner, "tvart": tvart}


def archive_download(
    source_path: Path,
    media_dir: Path,
    channel_id: object,
    video_id: object,
) -> Path:
    """Move a completed download and its sidecars into TubeArchivist layout."""
    source_path = source_path.resolve()
    target = (media_dir / media_relative_path(channel_id, video_id)).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    old_stem = source_path.stem
    if source_path != target:
        if target.exists():
            source_path.unlink(missing_ok=True)
        else:
            shutil.move(str(source_path), str(target))
    for sidecar in list(source_path.parent.iterdir()):
        if not sidecar.is_file() or sidecar == target:
            continue
        if not sidecar.name.startswith(f"{old_stem}."):
            continue
        suffix = sidecar.name[len(old_stem) :]
        sidecar_target = target.parent / f"{target.stem}{suffix}"
        if sidecar_target.exists():
            sidecar.unlink(missing_ok=True)
        else:
            shutil.move(str(sidecar), str(sidecar_target))
    return target


def migrate_storage_layout(database_path: Path, media_dir: Path, cache_dir: Path) -> dict:
    """Idempotently migrate legacy flat files to TubeArchivist's layout."""
    media_dir.mkdir(parents=True, exist_ok=True)
    for folder in ("download", "videos", "channels", "playlists"):
        (cache_dir / folder).mkdir(parents=True, exist_ok=True)
    moved = 0
    missing = 0
    with connect(database_path) as connection:
        rows = connection.execute(
            "SELECT id, video_id, channel_id, file_path, metadata_json FROM media"
        ).fetchall()
    for row in rows:
        stored = Path(row["file_path"])
        source = stored if stored.is_file() else media_dir / stored.name
        target = media_dir / media_relative_path(row["channel_id"], row["video_id"])
        if source.is_file():
            target = archive_download(source, media_dir, row["channel_id"], row["video_id"])
            moved += int(source.resolve() != target.resolve())
            with connect(database_path) as connection:
                connection.execute(
                    "UPDATE media SET file_path=?, extension='mp4', updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(target.resolve()), row["id"]),
                )
        elif not target.is_file():
            missing += 1
    with connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO app_settings(key,value) VALUES('storage_layout_version','1')
            ON CONFLICT(key) DO UPDATE SET value='1', updated_at=CURRENT_TIMESTAMP
            """
        )
    return {"moved": moved, "missing": missing}


def cache_known_artwork(database_path: Path, cache_dir: Path) -> dict:
    """Cache known remote artwork into TubeArchivist's cache paths."""
    counts = {"videos": 0, "channels": 0, "playlists": 0}
    with connect(database_path) as connection:
        videos = connection.execute(
            "SELECT video_id, thumbnail_url FROM media"
        ).fetchall()
        channels = connection.execute(
            "SELECT channel_id, thumbnail_url, banner_url, tvart_url FROM channels"
        ).fetchall()
        playlists = connection.execute(
            "SELECT playlist_id, thumbnail_url FROM playlists"
        ).fetchall()
    for row in videos:
        target = cache_dir / video_thumb_relative_path(row["video_id"])
        if not target.is_file():
            cache_video_thumbnail(cache_dir, row["video_id"], row["thumbnail_url"])
            counts["videos"] += 1
    for row in channels:
        expected = [
            cache_dir / channel_art_relative_path(row["channel_id"], art_type)
            for art_type in ("thumb", "banner", "tvart")
        ]
        if not all(path.is_file() for path in expected):
            cache_channel_art(
                cache_dir,
                row["channel_id"],
                row["thumbnail_url"],
                row["banner_url"],
                row["tvart_url"],
            )
            counts["channels"] += 1
    for row in playlists:
        target = cache_dir / playlist_thumb_relative_path(row["playlist_id"])
        if not target.is_file():
            cache_playlist_thumbnail(cache_dir, row["playlist_id"], row["thumbnail_url"])
            counts["playlists"] += 1
    return counts


def sync_playlist_thumbnail(
    database_path: Path, cache_dir: Path, playlist_database_id: int
) -> Path | None:
    """Rebuild custom playlist art from its first cached video thumbnail."""
    with connect(database_path) as connection:
        playlist = connection.execute(
            "SELECT playlist_id FROM playlists WHERE id=?", (playlist_database_id,)
        ).fetchone()
        video = connection.execute(
            """
            SELECT m.video_id FROM playlist_media pm
            JOIN media m ON m.id=pm.media_id
            WHERE pm.playlist_id=? ORDER BY pm.position, pm.added_at LIMIT 1
            """,
            (playlist_database_id,),
        ).fetchone()
    if playlist is None:
        return None
    source = (
        cache_dir / video_thumb_relative_path(video["video_id"])
        if video is not None
        else None
    )
    target = cache_dir / playlist_thumb_relative_path(playlist["playlist_id"])
    return cache_image(source, target, "playlist", skip_existing=False)


def delete_video_thumbnail(cache_dir: Path, video_id: object) -> None:
    (cache_dir / video_thumb_relative_path(video_id)).unlink(missing_ok=True)


def delete_channel_art(cache_dir: Path, channel_id: object) -> None:
    for art_type in ("thumb", "banner", "tvart"):
        (cache_dir / channel_art_relative_path(channel_id, art_type)).unlink(
            missing_ok=True
        )


def delete_playlist_thumbnail(cache_dir: Path, playlist_id: object) -> None:
    (cache_dir / playlist_thumb_relative_path(playlist_id)).unlink(missing_ok=True)
