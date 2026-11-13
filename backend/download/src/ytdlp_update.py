"""
functionality:
- check for new yt-dlp versions and install them
- runs on a schedule, no restart needed: tasks start as new processes
"""

import os
import shutil
import subprocess
import sys
from datetime import datetime

import requests
from common.src.env_settings import EnvironmentSettings
from common.src.ta_redis import RedisArchivist
from common.src.ytdlp_path import (
    PACKAGE,
    get_active_version,
    get_bundled_version,
    get_update_dir,
    get_updated_version,
    parse_version,
)


class YtDlpUpdate:
    """keep yt-dlp up to date"""

    PYPI_URL = f"https://pypi.org/pypi/{PACKAGE}/json"
    STATUS_KEY = "ytdlp:update"
    CHANNELS = ["release", "nightly"]
    TIMEOUT = 600

    def __init__(self, task=None):
        self.task = task
        self.channel = EnvironmentSettings.YTDLP_UPDATE_CHANNEL
        self.update_dir = get_update_dir()

    @property
    def is_enabled(self) -> bool:
        """check if updates are enabled"""
        return self.channel in self.CHANNELS

    def run(self) -> str:
        """check for new version and install if needed, returns message"""
        if not self.is_enabled:
            return "yt-dlp updates are disabled."

        current = get_active_version()
        self._notify(f"Installed yt-dlp version: {current}")
        latest = self.get_latest_version()
        if parse_version(latest) <= parse_version(current):
            message = f"yt-dlp {current} is up to date."
            self._store_status(current, latest, updated=False)
            print(f"[yt-dlp] {message}")
            return message

        self._notify(f"Updating yt-dlp from {current} to {latest}")
        installed = self.install(latest)
        self._store_status(installed, latest, updated=True)
        message = f"Updated yt-dlp from {current} to {installed}."
        print(f"[yt-dlp] {message}")

        return message

    def get_latest_version(self) -> str:
        """get latest version published for the configured channel"""
        response = requests.get(self.PYPI_URL, timeout=30)
        response.raise_for_status()
        data = response.json()
        if self.channel == "nightly":
            versions = [
                version
                for version, files in data["releases"].items()
                if any(not i.get("yanked") for i in files)
            ]
            return max(versions, key=parse_version)

        return data["info"]["version"]

    def install(self, version: str) -> str:
        """install version next to the bundled one, returns new version"""
        staging = f"{self.update_dir}.new"
        retired = f"{self.update_dir}.old"
        for folder in (staging, retired):
            shutil.rmtree(folder, ignore_errors=True)

        os.makedirs(os.path.dirname(self.update_dir), exist_ok=True)
        command = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--no-cache-dir",
            "--target",
            staging,
            f"{PACKAGE}[default]=={version}",
        ]
        if self.channel == "nightly":
            command.append("--pre")

        try:
            self._run(command)
            installed = self._validate(staging)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        # swap folders, running tasks keep using what they loaded
        if os.path.exists(self.update_dir):
            os.rename(self.update_dir, retired)

        os.rename(staging, self.update_dir)
        shutil.rmtree(retired, ignore_errors=True)

        return installed

    def _run(self, command: list[str], env: dict | None = None) -> str:
        """run command, raise with output on error"""
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=self.TIMEOUT,
            env=env,
            check=False,
        )
        if result.returncode != 0:
            output = (result.stderr or result.stdout).strip()[-500:]
            raise ValueError(f"yt-dlp update failed: {output}")

        return result.stdout.strip()

    def _validate(self, folder: str) -> str:
        """make sure the new version can be imported, returns version"""
        env = os.environ.copy()
        env["PYTHONPATH"] = folder
        code = "from yt_dlp.version import __version__; print(__version__)"

        return self._run([sys.executable, "-c", code], env=env)

    def remove_outdated(self) -> str | None:
        """
        remove updated version if the bundled one caught up,
        returns removed version
        """
        updated = get_updated_version()
        if not updated and not os.path.exists(self.update_dir):
            return None

        is_newer = parse_version(updated) > parse_version(
            get_bundled_version()
        )
        if is_newer and self.is_enabled:
            return None

        shutil.rmtree(self.update_dir, ignore_errors=True)

        return updated or "unknown"

    def _store_status(self, version, latest, updated: bool) -> None:
        """store last check for reference"""
        message = {
            "version": version,
            "latest": latest,
            "channel": self.channel,
            "updated": updated,
            "checked": int(datetime.now().timestamp()),
        }
        RedisArchivist().set_message(self.STATUS_KEY, message)

    def get_status(self) -> dict:
        """get current version and last check"""
        status = RedisArchivist().get_message_dict(self.STATUS_KEY)
        status.update(
            {
                "version": get_active_version(),
                "bundled": get_bundled_version(),
                "channel": self.channel,
                "enabled": self.is_enabled,
            }
        )

        return status

    def _notify(self, message: str) -> None:
        """send progress"""
        if self.task:
            self.task.send_progress([message])
