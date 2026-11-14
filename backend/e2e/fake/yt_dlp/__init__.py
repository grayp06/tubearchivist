"""
stand-in for yt_dlp used by the end to end tests
- deterministic metadata, no network access
- download creates a tiny media file with ffmpeg
- thumbnails are served by the test runner, see e2e/run.py
"""

import hashlib
import os
import re
import shutil
import subprocess
import time

from . import utils, version

__all__ = ["YoutubeDL", "utils", "version"]

CACHE_DIR = os.environ.get("TA_CACHE_DIR", "/cache")
MEDIA_FILE = os.path.join(CACHE_DIR, "e2e_media.mp4")
SLOW_FILE = os.path.join(CACHE_DIR, "e2e_slow")
THUMB_PORT = os.environ.get("TA_E2E_THUMB_PORT", "8099")
THUMB_URL = f"http://127.0.0.1:{THUMB_PORT}/thumb.jpg"
CHANNELS = {
    "UCfakechannel000000000aa": "Fake Alpha Channel",
    "UCfakechannel000000000bb": "Fake Beta Channel",
}
PLAYLIST_ID = "PLfakeplaylist00000000000000000001"
KINDS = ("videos", "streams", "shorts")
COMMENT = {
    "id": "c1",
    "text": "great tomatoes",
    "timestamp": 1700000000,
    "_time_text": "1 day ago",
    "like_count": 1,
    "is_favorited": False,
    "author": "me",
    "author_id": "UCx",
    "author_thumbnail": "t",
    "author_is_uploader": False,
    "parent": "root",
}


def video_ids(channel_id, kind="videos", count=6):
    """ids of all videos of channel tab"""
    tag = hashlib.md5((channel_id + kind).encode()).hexdigest()
    return [f"{kind[0]}{tag[:6]}{i:04d}" for i in range(count)]


def _channel_of(video_id):
    """find channel and tab of video"""
    for channel_id in CHANNELS:
        for kind in KINDS:
            if video_id in video_ids(channel_id, kind):
                return channel_id, kind

    return "UCfakechannel000000000aa", "videos"


def _video(video_id, flat=False):
    """build video metadata"""
    channel_id, kind = _channel_of(video_id)
    name = CHANNELS[channel_id]
    info = {
        "id": video_id,
        "title": f"Fake {kind} title {video_id} about gardening tomatoes",
        "channel": name,
        "channel_id": channel_id,
        "uploader": name,
        "duration": 30 if kind == "shorts" else 600,
        "thumbnails": [{"url": THUMB_URL}],
        "view_count": 1234,
        "live_status": "was_live" if kind == "streams" else "not_live",
        "timestamp": 1700000000 + int(video_id[-4:]) * 86400,
        "url": f"https://www.youtube.com/watch?v={video_id}",
    }
    if flat:
        return info

    info.update(
        {
            "description": "A fake description with compost and seeds",
            "upload_date": "20231115",
            "tags": ["garden", "tomato"],
            "categories": ["Howto"],
            "thumbnail": THUMB_URL,
            "like_count": 55,
            "average_rating": None,
            "width": 320,
            "height": 240,
            "channel_follower_count": 4242,
            "subtitles": {},
            "automatic_captions": {},
            "comment_count": 0,
        }
    )

    return info


def _limit(entries, items):
    """apply playlist_items like :5"""
    match = re.match(r"^:(\d+)", str(items or ""))
    if match:
        return entries[: int(match.group(1))]

    return entries


def _channel(channel_id, tab, items):
    """build channel metadata with entries of tab"""
    kind = tab or "videos"
    entries = []
    if kind in KINDS and not (channel_id.endswith("bb") and kind != "videos"):
        entries = [_video(i, flat=True) for i in video_ids(channel_id, kind)]

    name = CHANNELS[channel_id]

    return {
        "id": channel_id,
        "channel_id": channel_id,
        "uploader": name,
        "channel": name,
        "description": "fake channel about gardens",
        "channel_follower_count": 4242,
        "tags": ["garden"],
        "thumbnails": [
            {"url": THUMB_URL, "width": 2560, "height": 424},
            {"url": THUMB_URL, "width": 900, "height": 900},
            {"url": THUMB_URL, "id": "banner_uncropped"},
        ],
        "entries": _limit(entries, items),
    }


def _playlist(playlist_id, items):
    """build playlist metadata"""
    channel_id = "UCfakechannel000000000aa"
    entries = [_video(i, flat=True) for i in video_ids(channel_id)[:4]]
    if str(items or "").endswith("-1"):
        entries.reverse()

    return {
        "id": playlist_id,
        "title": "Fake Playlist One",
        "channel": CHANNELS[channel_id],
        "channel_id": channel_id,
        "description": "fake playlist",
        "thumbnails": [{"url": THUMB_URL}],
        "entries": entries,
    }


def _media_file():
    """create the media file once, 2 seconds 320x240 with sound"""
    if os.path.exists(MEDIA_FILE):
        return MEDIA_FILE

    temp_file = f"{MEDIA_FILE}.{os.getpid()}.mp4"
    command = ["ffmpeg", "-y", "-loglevel", "error"]
    command += ["-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=10"]
    command += ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    command += ["-c:v", "mpeg4", "-c:a", "aac", "-shortest", temp_file]
    subprocess.run(command, check=True)
    os.replace(temp_file, MEDIA_FILE)

    return MEDIA_FILE


def _slow():
    """sleep if the tests asked for slow downloads"""
    try:
        with open(SLOW_FILE, "r", encoding="utf-8") as f:
            time.sleep(float(f.read().strip()))
    except (OSError, ValueError):
        pass


class YoutubeDL:
    """stand-in for yt_dlp.YoutubeDL"""

    def __init__(self, params=None):
        self.params = params or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        """extract fake metadata"""
        # pylint: disable=unused-argument
        if "FAIL" in url:
            raise utils.DownloadError("ERROR: fake failure")

        items = self.params.get("playlist_items")
        match = re.search(r"/channel/(UC[\w-]+)(?:/(\w+))?", url)
        if match:
            channel_id = match.group(1)
            if "playlists" in url:
                entry = {"id": PLAYLIST_ID, "title": "Fake Playlist One"}
                return {"id": channel_id, "entries": [entry]}

            if channel_id not in CHANNELS:
                raise utils.DownloadError("ERROR: channel does not exist")

            return _channel(channel_id, match.group(2), items)

        match = re.search(r"list=(PL[\w-]+)", url)
        if match:
            return _playlist(match.group(1), items)

        info = _video(url.split("v=")[-1])
        if self.params.get("getcomments"):
            info["comments"] = [COMMENT]

        return info

    def download(self, urls):
        """create media file at the expected location"""
        for url in urls:
            video_id = url.split("v=")[-1]
            if "FAIL" in video_id:
                raise utils.DownloadError("ERROR: fake download failure")

            template = self.params["outtmpl"]
            if isinstance(template, dict):
                template = template["default"]

            target = template % {"id": video_id, "ext": "mp4"}
            os.makedirs(os.path.dirname(target), exist_ok=True)
            for hook in self.params.get("progress_hooks", []):
                hook(
                    {
                        "status": "downloading",
                        "_percent_str": " 50.0%",
                        "_speed_str": "1MiB/s",
                        "_eta_str": "00:01",
                        "_total_bytes_str": "1MiB",
                        "info_dict": {"id": video_id, "title": "Fake title"},
                    }
                )

            _slow()
            shutil.copy(_media_file(), target)

        return 0
