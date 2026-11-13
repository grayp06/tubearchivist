"""test yt-dlp updater"""

# pylint: disable=redefined-outer-name

import os

import pytest
from common.src import ytdlp_path
from common.src.env_settings import EnvironmentSettings
from common.src.ytdlp_path import parse_version
from download.src import ytdlp_update
from download.src.ytdlp_update import YtDlpUpdate

VERSIONS = [
    ("2026.07.04", (2026, 7, 4)),
    ("2026.7.4", (2026, 7, 4)),
    ("2026.7.5.232843.dev0", (2026, 7, 5, 232843)),
    (None, ()),
]


@pytest.mark.parametrize("version, expected", VERSIONS)
def test_parse_version(version, expected):
    """parse versions to compare"""
    assert parse_version(version) == expected


def test_version_order():
    """newer versions compare higher"""
    assert parse_version("2026.10.26") > parse_version("2026.7.4")
    assert parse_version("2026.7.5.232843.dev0") > parse_version("2026.7.4")


@pytest.fixture
def updater(tmp_path, monkeypatch):
    """updater with temporary folders and no network"""
    monkeypatch.setattr(EnvironmentSettings, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(EnvironmentSettings, "YTDLP_UPDATE_CHANNEL", "release")
    monkeypatch.setenv("TA_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(ytdlp_update, "get_active_version", lambda: "2026.7.4")
    handler = YtDlpUpdate()
    installed = []
    monkeypatch.setattr(handler, "install", lambda v: installed.append(v) or v)
    handler.installed = installed

    return handler


def test_update_needed(updater, monkeypatch):
    """install if there is a newer version"""
    monkeypatch.setattr(updater, "get_latest_version", lambda: "2026.10.26")
    assert updater.run() == "Updated yt-dlp from 2026.7.4 to 2026.10.26."
    assert updater.installed == ["2026.10.26"]
    status = updater.get_status()
    assert status["updated"] is True and status["latest"] == "2026.10.26"


def test_up_to_date(updater, monkeypatch):
    """do nothing if already latest"""
    monkeypatch.setattr(updater, "get_latest_version", lambda: "2026.07.04")
    assert updater.run() == "yt-dlp 2026.7.4 is up to date."
    assert not updater.installed


def test_disabled(updater):
    """do nothing if disabled"""
    updater.channel = "off"
    assert updater.run() == "yt-dlp updates are disabled."
    assert not updater.installed


def test_remove_outdated(updater, monkeypatch):
    """drop the update once the bundled version caught up"""
    os.makedirs(updater.update_dir)
    monkeypatch.setattr(
        ytdlp_update, "get_updated_version", lambda: "2026.8.1"
    )
    monkeypatch.setattr(
        ytdlp_update, "get_bundled_version", lambda: "2026.7.4"
    )
    assert updater.remove_outdated() is None
    assert os.path.exists(updater.update_dir)
    monkeypatch.setattr(
        ytdlp_update, "get_bundled_version", lambda: "2026.9.1"
    )
    assert updater.remove_outdated() == "2026.8.1"
    assert not os.path.exists(updater.update_dir)
    assert ytdlp_path.activate_update() is False
