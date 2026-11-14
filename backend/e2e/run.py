"""
end to end tests against a running instance

smoke tests always run:
    TA_E2E_URL=http://localhost:8000 TA_E2E_USERNAME=... TA_E2E_PASSWORD=... \\
        python e2e/run.py

the complete pipeline runs with TA_E2E_PIPELINE=1, that needs:
- a fresh instance started with PYTHONPATH=/app/e2e/fake,
  so no request goes to YouTube
- to be run on the same machine as the instance, from the app folder
"""

# pylint: disable=missing-function-docstring

import datetime
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zoneinfo

import requests

URL = os.environ.get("TA_E2E_URL", "http://localhost:8000").rstrip("/")
USERNAME = os.environ.get("TA_E2E_USERNAME") or os.environ.get("TA_USERNAME")
PASSWORD = os.environ.get("TA_E2E_PASSWORD") or os.environ.get("TA_PASSWORD")
PIPELINE = os.environ.get("TA_E2E_PIPELINE", "").lower() in ("1", "true")
MEDIA_DIR = os.environ.get("TA_MEDIA_DIR", "/youtube")
CACHE_DIR = os.environ.get("TA_CACHE_DIR", "/cache")
SLOW_FILE = os.path.join(CACHE_DIR, "e2e_slow")
THUMB_PORT = int(os.environ.get("TA_E2E_THUMB_PORT", "8099"))

CHANNEL_A = "UCfakechannel000000000aa"
CHANNEL_B = "UCfakechannel000000000bb"
PLAYLIST = "PLfakeplaylist00000000000000000001"
TASK_DONE = ("SUCCESS", "FAILURE", "REVOKED", "FAILED")

SMOKE_PATHS = [
    "/api/ping/",
    "/api/user/account/",
    "/api/user/me/",
    "/api/video/",
    "/api/channel/",
    "/api/playlist/",
    "/api/download/",
    "/api/notification/",
    "/api/search/?query=test",
    "/api/stats/video/",
    "/api/stats/channel/",
    "/api/stats/playlist/",
    "/api/stats/download/",
    "/api/stats/watch/",
    "/api/stats/downloadhist/",
    "/api/stats/biggestchannels/?order=doc_count",
    "/api/appsettings/config/",
    "/api/appsettings/snapshot/",
    "/api/appsettings/backup/",
    "/api/appsettings/ytdlp/",
    "/api/task/schedule/",
    "/api/task/by-name/",
]


class Client:
    """api client with session login"""

    def __init__(self):
        self.session = requests.Session()
        self.failed: list[str] = []

    def login(self) -> None:
        response = self.session.post(
            f"{URL}/api/user/login/",
            json={"username": USERNAME, "password": PASSWORD},
            timeout=60,
        )
        self.check("login", response.status_code == 204, response.text[:200])

    def request(self, method: str, path: str, **kwargs):
        headers = {"X-CSRFToken": self.session.cookies.get("csrftoken", "")}
        return self.session.request(
            method, URL + path, headers=headers, timeout=300, **kwargs
        )

    def call(self, method: str, path: str, **kwargs):
        response = self.request(method, path, **kwargs)
        try:
            return response.json()
        except ValueError:
            return {"error": response.text[:200]}

    def check(self, name: str, is_ok: bool, extra="") -> None:
        print(f"{'ok  ' if is_ok else 'FAIL'} {name} {extra if extra else ''}")
        if not is_ok:
            self.failed.append(name)

    def wait(self, task_id: str, timeout=240, until=TASK_DONE) -> dict:
        """wait for task to reach one of the states"""
        started = time.time()
        task: dict = {}
        while time.time() - started < timeout:
            task = self.call("GET", f"/api/task/by-id/{task_id}/")
            if task.get("status") in until:
                return task

            time.sleep(0.3)

        self.check(f"task {task_id} finished", False, json.dumps(task)[:200])

        return task

    def run_task(self, name: str, **kwargs) -> dict:
        """start task by name and wait for the result"""
        task = self.call("POST", f"/api/task/by-name/{name}/")
        return self.wait(task["task_id"], **kwargs)

    def wait_for(self, name: str, callback, timeout=60) -> bool:
        """poll until callback returns true"""
        started = time.time()
        while time.time() - started < timeout:
            if callback():
                return True

            time.sleep(0.5)

        self.check(name, False, "timeout")

        return False

    def total(self, path: str) -> int:
        """total hits of list endpoint"""
        response = self.call("GET", path)
        if not isinstance(response, dict):
            return -1

        return response.get("paginate", {}).get("total_hits", -1)

    def pending_task(self, name: str) -> str | None:
        """get id of queued or running task by name"""
        for _ in range(50):
            tasks = self.call("GET", f"/api/task/by-name/{name}/")
            pending = [i for i in tasks if i["status"] == "PENDING"]
            if pending:
                return pending[-1]["task_id"]

            time.sleep(0.2)

        return None


def video_id(channel_id: str, kind: str, idx: int) -> str:
    """video id as built by the yt_dlp stand-in"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fake"))
    from yt_dlp import video_ids  # pylint: disable=import-outside-toplevel

    return video_ids(channel_id, kind)[idx]


def serve_thumbnails() -> None:
    """serve the image the yt_dlp stand-in uses for all thumbnails"""
    # pylint: disable=import-outside-toplevel
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1280, 720), (40, 110, 60)).save(buffer, "JPEG")
    image = buffer.getvalue()

    class Handler(http.server.BaseHTTPRequestHandler):
        """always return the image"""

        def do_GET(self):  # pylint: disable=invalid-name
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(image)))
            self.end_headers()
            self.wfile.write(image)

        def log_message(self, *args):  # pylint: disable=arguments-differ
            return

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", THUMB_PORT), Handler
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()


def set_slow(seconds: float) -> None:
    """make every download take that long, 0 to disable"""
    if seconds:
        with open(SLOW_FILE, "w", encoding="utf-8") as f:
            f.write(str(seconds))
    elif os.path.exists(SLOW_FILE):
        os.remove(SLOW_FILE)


def smoke(client: Client) -> None:
    """every main endpoint answers"""
    response = requests.get(f"{URL}/api/health/", timeout=30)
    client.check("health", response.status_code == 200)
    response = requests.get(f"{URL}/api/video/", timeout=30)
    client.check("auth required", response.status_code in (401, 403))
    client.login()
    for path in SMOKE_PATHS:
        response = client.request("GET", path)
        is_ok = response.status_code == 200
        client.check(
            f"GET {path}", is_ok, "" if is_ok else response.text[:200]
        )

    status = client.call("GET", "/api/appsettings/ytdlp/")
    client.check("yt-dlp version reported", bool(status.get("version")))

    before = client.call("GET", "/api/appsettings/snapshot/")["snapshots"]
    created = client.call("POST", "/api/appsettings/snapshot/")
    snapshot_id = created.get("snapshot_name")
    client.check("snapshot created", bool(snapshot_id), json.dumps(created))
    snapshots = client.call("GET", "/api/appsettings/snapshot/")["snapshots"]
    client.check("snapshot listed", len(snapshots) >= max(len(before), 1))
    if snapshot_id and snapshot_id not in [i["id"] for i in before]:
        path = f"/api/appsettings/snapshot/{snapshot_id}/"
        client.check(
            "snapshot deleted", client.request("DELETE", path).ok, path
        )

    task = client.run_task("thumbnail_check")
    client.check("task runs", task.get("status") == "SUCCESS", str(task))


def subscribe(client: Client) -> None:
    """subscribe to channel and find its videos"""
    config = {
        "downloads": {"sleep_interval": 0, "comment_max": "20,all,100,all"}
    }
    client.call("POST", "/api/appsettings/config/", json=config)
    data = {"data": [{"channel_id": CHANNEL_A, "channel_subscribed": True}]}
    client.call("POST", "/api/channel/", json=data)
    path = f"/api/channel/{CHANNEL_A}/"
    client.wait_for(
        "channel subscribed",
        lambda: client.call("GET", path).get("channel_subscribed"),
    )
    channel = client.call("GET", path)
    is_ok = channel.get("channel_name") == "Fake Alpha Channel"
    client.check("channel indexed", is_ok, json.dumps(channel)[:200])

    task = client.run_task("update_subscribed")
    client.check("rescan subscriptions", task.get("status") == "SUCCESS")
    pending = client.total("/api/download/?filter=pending")
    client.check("pending videos found", pending == 18, pending)


def stop_and_kill(client: Client) -> None:
    """stop and kill a running download, queue stays usable"""
    set_slow(1)
    task_id = client.call("POST", "/api/task/by-name/download_pending/")
    task_id = task_id["task_id"]
    time.sleep(3.2)
    path = f"/api/task/by-id/{task_id}/"
    client.request("POST", path, json={"command": "stop"})
    task = client.wait(task_id)
    count = client.total("/api/video/")
    is_ok = task.get("status") == "SUCCESS" and 0 < count < 18
    client.check("download stopped", is_ok, f"{count} downloaded")

    task_id = client.call("POST", "/api/task/by-name/download_pending/")
    task_id = task_id["task_id"]
    time.sleep(2.5)
    path = f"/api/task/by-id/{task_id}/"
    client.request("POST", path, json={"command": "kill"})
    task = client.wait(task_id, timeout=30)
    client.check("download killed", task.get("status") == "REVOKED", str(task))
    time.sleep(1)
    count = client.total("/api/video/")
    time.sleep(3)
    client.check(
        "no progress after kill", client.total("/api/video/") == count
    )

    first = client.call("POST", "/api/task/by-name/download_pending/")
    time.sleep(1)
    second = client.run_task("download_pending", timeout=30)
    is_ok = second.get("status") == "SUCCESS" and second.get("result") is None
    client.check("second download refused while running", is_ok, str(second))
    set_slow(0)
    task = client.wait(first["task_id"])
    client.check("download completed", task.get("status") == "SUCCESS")


def verify_downloads(client: Client) -> dict:
    """all videos of the channel are indexed and stored"""
    videos = client.call("GET", "/api/video/")
    total = videos["paginate"]["total_hits"]
    client.check("videos indexed", total == 18, total)
    video = videos["data"][0]
    media_file = os.path.join(
        MEDIA_DIR, CHANNEL_A, f"{video['youtube_id']}.mp4"
    )
    client.check("media file stored", os.path.exists(media_file), media_file)
    is_ok = video["player"]["duration"] == 2
    is_ok = is_ok and video["streams"][0]["width"] == 320
    client.check(
        "media file probed", is_ok, json.dumps(video["streams"])[:200]
    )
    pending = client.call("GET", "/api/download/?filter=pending")
    client.check("queue is empty", pending.get("data") == [])
    leftover = os.listdir(os.path.join(CACHE_DIR, "download"))
    client.check("download cache is empty", leftover == [], leftover)
    types = {
        i: client.total(f"/api/video/?type={i}")
        for i in ("videos", "streams", "shorts")
    }
    expected = {"videos": 6, "streams": 6, "shorts": 6}
    client.check("video types", types == expected, types)
    comments = client.call("GET", f"/api/video/{video['youtube_id']}/comment/")
    client.check("comments indexed", len(comments) == 1)
    nav = client.call("GET", f"/api/channel/{CHANNEL_A}/nav/")
    is_ok = nav.get("has_videos") and nav.get("has_streams")
    is_ok = is_ok and nav.get("has_shorts") and not nav.get("has_pending")
    client.check("channel navigation", bool(is_ok), nav)
    # misspelled and incomplete on purpose
    query = "tomatos+gard"  # codespell:ignore
    search = client.call("GET", f"/api/search/?query={query}")
    results = search["results"]["video_results"]
    client.check("fuzzy full text search", len(results) > 0)

    return video


def autostart(client: Client) -> None:
    """add single video of other channel with autostart"""
    to_add = video_id(CHANNEL_B, "videos", 2)
    data = {"data": [{"youtube_id": to_add, "status": "pending"}]}
    task = client.call("POST", "/api/download/?autostart=true", json=data)
    client.wait(task["task_id"])
    time.sleep(1)
    task_id = client.pending_task("download_pending")
    if task_id:
        client.wait(task_id)

    path = f"/api/video/{to_add}/"
    client.wait_for(
        "autostart", lambda: client.call("GET", path).get("youtube_id")
    )
    video = client.call("GET", path)
    channel = video.get("channel", {})
    is_ok = channel.get("channel_id") == CHANNEL_B
    is_ok = is_ok and channel.get("channel_subscribed") is False
    client.check("autostart downloaded", is_ok, json.dumps(video)[:200])


def playlist(client: Client) -> None:
    """subscribe to playlist"""
    data = {"data": [{"playlist_id": PLAYLIST, "playlist_subscribed": True}]}
    client.call("POST", "/api/playlist/", json=data)
    path = f"/api/playlist/{PLAYLIST}/"
    client.wait_for(
        "playlist subscribed",
        lambda: client.call("GET", path).get("playlist_subscribed"),
    )
    indexed = client.call("GET", path)
    entries = indexed.get("playlist_entries", [])
    is_ok = len(entries) == 4 and all(i["downloaded"] for i in entries)
    client.check("playlist indexed", is_ok, json.dumps(indexed)[:200])
    # videos get linked to the playlist with the next rescan
    client.run_task("update_subscribed")
    videos = client.call("GET", f"/api/video/?playlist={PLAYLIST}")
    is_ids = [i["youtube_id"] for i in videos.get("data", [])]
    client.check(
        "playlist order", is_ids == [i["youtube_id"] for i in entries]
    )
    nav = client.call("GET", f"/api/video/{entries[1]['youtube_id']}/nav/")
    client.check("playlist navigation", len(nav) == 1)


def reindex(client: Client, video: dict) -> None:
    """refresh metadata"""
    data = {
        "video": [video["youtube_id"]],
        "channel": [CHANNEL_A],
        "playlist": [PLAYLIST],
    }
    task = client.call("POST", "/api/refresh/?extract_videos=false", json=data)
    task_id = task.get("task_id") or client.pending_task("check_reindex")
    if task_id:
        client.wait(task_id)

    refreshed = client.call("GET", f"/api/video/{video['youtube_id']}/")
    client.check("reindex", refreshed.get("title", "").startswith("Fake"))


def retry(client: Client) -> None:
    """failed download is retried and the error is stored"""
    failing = {
        "youtube_id": "FAILvideo01",
        "title": "will fail",
        "channel_id": CHANNEL_A,
        "channel_name": "x",
        "channel_indexed": True,
        "duration": "1",
        "published": 1,
        "status": "pending",
        "timestamp": int(time.time()),
        "vid_thumb_url": None,
        "vid_type": "videos",
        "auto_start": False,
    }
    code = (
        "import json, sys;"
        + "from common.src.index_store import IndexStore;"
        + "doc = json.loads(sys.argv[1]);"
        + "IndexStore().index_doc('ta_download', doc['youtube_id'], doc)"
    )
    subprocess.run(
        [sys.executable, "-c", code, json.dumps(failing)], check=True
    )
    task = client.call("POST", "/api/task/by-name/download_pending/")
    result = client.wait(task["task_id"], until=("RETRY", *TASK_DONE))
    client.check("retry scheduled", result.get("status") == "RETRY", result)
    result = client.wait(task["task_id"], timeout=60)
    is_ok = result.get("status") == "SUCCESS" and result.get("retries") == 1
    client.check("retry completed", is_ok, json.dumps(result)[:200])
    item = client.call("GET", "/api/download/FAILvideo01/")
    message = item.get("message") or ""
    client.check("error stored", "fake download failure" in message, message)
    client.request("DELETE", "/api/download/FAILvideo01/")


def filesystem(client: Client) -> None:
    """rescan filesystem and manual import"""
    video = client.call("GET", "/api/video/")["data"][0]
    channel_id = video["channel"]["channel_id"]
    media_file = os.path.join(
        MEDIA_DIR, channel_id, f"{video['youtube_id']}.mp4"
    )
    import_file = os.path.join(CACHE_DIR, "e2e_import.mp4")
    shutil.copy(media_file, import_file)
    os.remove(media_file)
    data = {"ignore_error": False, "prefer_local": False}
    task = client.call(
        "POST", "/api/appsettings/rescan-filesystem/", json=data
    )
    client.wait(task["task_id"])
    response = client.request("GET", f"/api/video/{video['youtube_id']}/")
    client.check("rescan removed missing video", response.status_code == 404)

    to_import = video_id(CHANNEL_B, "videos", 4)
    target = os.path.join(CACHE_DIR, "import", f"some video [{to_import}].mp4")
    shutil.move(import_file, target)
    task = client.call("POST", "/api/appsettings/manual-import/", json=data)
    client.wait(task["task_id"])
    imported = client.call("GET", f"/api/video/{to_import}/")
    media_file = os.path.join(MEDIA_DIR, CHANNEL_B, f"{to_import}.mp4")
    is_ok = imported.get("youtube_id") == to_import
    is_ok = is_ok and os.path.exists(media_file)
    client.check("manual import", is_ok, json.dumps(imported)[:200])


def maintenance(client: Client) -> None:
    """maintenance tasks, backup and snapshot restore"""
    for name in ("thumbnail_check", "resync_metadata", "run_backup"):
        task = client.run_task(name)
        client.check(f"task {name}", task.get("status") == "SUCCESS", task)

    count = client.total("/api/video/")
    created = client.call("POST", "/api/appsettings/snapshot/")
    snapshot_id = created["snapshot_name"]
    video = client.call("GET", "/api/video/")["data"][0]
    path = f"/api/video/{video['youtube_id']}/"
    client.request("DELETE", path)
    client.check("video deleted", client.total("/api/video/") == count - 1)
    snapshot_path = f"/api/appsettings/snapshot/{snapshot_id}/"
    response = client.request("POST", snapshot_path)
    client.check("snapshot restore accepted", response.ok, response.text[:200])
    client.wait_for(
        "snapshot restored", lambda: client.total("/api/video/") == count
    )
    client.check("index restored", client.total("/api/video/") == count)
    client.request("DELETE", snapshot_path)


def scheduler(client: Client) -> None:
    """schedule task for the next minute, runs exactly once"""
    zone = zoneinfo.ZoneInfo(os.environ.get("TZ", "UTC"))
    due = datetime.datetime.now(zone) + datetime.timedelta(minutes=1)
    path = "/api/task/by-name/thumbnail_check/"
    before = len(client.call("GET", path))
    schedule = {"schedule": f"{due.minute} {due.hour} *"}
    client.call("POST", "/api/task/schedule/thumbnail_check/", json=schedule)
    client.wait_for(
        "scheduled task started",
        lambda: len(client.call("GET", path)) > before,
        timeout=100,
    )
    time.sleep(25)
    count = len(client.call("GET", path))
    client.check("scheduled task ran once", count == before + 1, count)
    client.request("DELETE", "/api/task/schedule/thumbnail_check/")


def pipeline(client: Client) -> None:
    """complete pipeline, needs the yt_dlp stand-in"""
    status = client.call("GET", "/api/appsettings/ytdlp/")
    if status.get("version") != "0.0.0":
        client.check("instance uses the yt_dlp stand-in", False, status)
        return

    serve_thumbnails()
    try:
        subscribe(client)
        stop_and_kill(client)
        video = verify_downloads(client)
        autostart(client)
        playlist(client)
        reindex(client, video)
        retry(client)
        filesystem(client)
        maintenance(client)
        scheduler(client)
    finally:
        set_slow(0)


def main() -> int:
    if not USERNAME or not PASSWORD:
        print("TA_E2E_USERNAME and TA_E2E_PASSWORD are required")
        return 2

    client = Client()
    smoke(client)
    if PIPELINE and not client.failed:
        pipeline(client)

    if client.failed:
        print(f"\n{len(client.failed)} failed: {client.failed}")
        return 1

    print("\nall passed")

    return 0


if __name__ == "__main__":
    sys.exit(main())
