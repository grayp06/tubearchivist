"""
functionality:
- locate yt-dlp updates installed at runtime
- updates live in the cache folder to survive container updates
- needs to stay free of heavy imports, gets called from settings
"""

import sys
from importlib import metadata
from os import environ, path

PACKAGE = "yt-dlp"


def get_update_dir() -> str:
    """folder holding the updated yt-dlp packages"""
    cache_dir = str(environ.get("TA_CACHE_DIR", "/cache"))
    return path.join(cache_dir, "ytdlp", "packages")


def activate_update() -> bool:
    """prefer the updated yt-dlp over the bundled one, if there is one"""
    update_dir = get_update_dir()
    if not path.isdir(path.join(update_dir, "yt_dlp")):
        return False

    if update_dir not in sys.path:
        sys.path.insert(0, update_dir)

    return True


def get_version(search_path: list[str]) -> str | None:
    """get yt-dlp version installed in search_path, without importing it"""
    for dist in metadata.distributions(path=search_path):
        if (dist.metadata["Name"] or "").lower() == PACKAGE:
            return dist.version

    return None


def get_bundled_version() -> str | None:
    """get yt-dlp version shipped with the application"""
    update_dir = get_update_dir()
    return get_version([i for i in sys.path if i != update_dir])


def get_updated_version() -> str | None:
    """get yt-dlp version installed by the updater"""
    return get_version([get_update_dir()])


def get_active_version() -> str | None:
    """get yt-dlp version in use"""
    return get_updated_version() or get_bundled_version()


def parse_version(version: str | None) -> tuple[int, ...]:
    """parse version to compare, like 2026.07.04 or 2026.7.4.232843.dev0"""
    if not version:
        return ()

    parts = []
    for part in version.split("."):
        if not part.isdigit():
            break

        parts.append(int(part))

    return tuple(parts)
