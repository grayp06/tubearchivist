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


def test_swap_keeps_previous(updater):
    """replaced version is kept for a rollback"""
    for folder, marker in ((updater.update_dir, "old"), ("new", "new")):
        target = folder if folder != "new" else f"{updater.update_dir}.new"
        os.makedirs(os.path.join(target, "yt_dlp"))
        with open(os.path.join(target, "marker"), "w", encoding="utf-8") as f:
            f.write(marker)

    updater._swap(f"{updater.update_dir}.new")  # pylint: disable=W0212
    with open(
        os.path.join(updater.update_dir, "marker"), encoding="utf-8"
    ) as f:
        assert f.read() == "new"
    with open(
        os.path.join(updater.previous_dir, "marker"), encoding="utf-8"
    ) as f:
        assert f.read() == "old"


def test_rollback(updater, monkeypatch):
    """go back to the previous version"""
    monkeypatch.setattr(ytdlp_update, "get_updated_version", lambda: "old")
    os.makedirs(updater.update_dir)
    os.makedirs(updater.previous_dir)
    with open(os.path.join(updater.previous_dir, "marker"), "w") as f:
        f.write("old")

    assert updater.rollback() == "old"
    assert os.path.exists(os.path.join(updater.update_dir, "marker"))
    assert not os.path.exists(updater.previous_dir)


def test_rollback_without_previous(updater):
    """fall back to the bundled version if there is no previous one"""
    os.makedirs(updater.update_dir)
    assert updater.rollback() is None
    assert not os.path.exists(updater.update_dir)


def test_canary_regression(updater, monkeypatch):
    """a version failing where the current one works is rejected"""
    calls = []

    def fake_run(command, env=None, timeout=0):
        calls.append(env.get("PYTHONPATH") if env else None)
        if env and env.get("PYTHONPATH") == "new":
            raise ValueError("broken")

        return ""

    monkeypatch.setattr(updater, "_run", fake_run)
    with pytest.raises(ValueError, match="canary"):
        updater._canary("new")  # pylint: disable=W0212

    # current version fails too, so not a regression
    monkeypatch.setattr(
        updater, "_run", lambda *a, **k: (_ for _ in ()).throw(ValueError("x"))
    )
    updater._canary("new")  # pylint: disable=W0212
