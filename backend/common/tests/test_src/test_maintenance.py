"""test integrity check, filesystem detection and snapshots"""

# pylint: disable=redefined-outer-name

import os
import sqlite3

import pytest
from appsettings.src.snapshot import IndexSnapshot
from common.src.env_settings import EnvironmentSettings
from common.src.index_schema import all_indexes
from common.src.index_store import IndexStore
from common.src.integrity import IntegrityCheck
from common.src.sqlite_conn import (
    INDEX_DB,
    close_conn,
    db_path,
    filesystem_type,
    is_network_fs,
)
from common.src.state_store import StateStore

MOUNTS = """overlay / overlay rw,relatime 0 0
/dev/sda1 /cache ext4 rw,relatime 0 0
nas:/export/media /youtube nfs4 rw,relatime 0 0
//nas/share /cache/my\\040share cifs rw,relatime 0 0
sshfs#host:/ /cache/remote fuse.sshfs rw 0 0
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    """store with all indexes in temporary folder"""
    monkeypatch.setattr(EnvironmentSettings, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(
        IndexSnapshot, "SNAPSHOT_DIR", str(tmp_path / "snapshot")
    )
    index_store = IndexStore()
    for index in all_indexes():
        index_store.create_index(index)

    for idx in range(20):
        doc = {"youtube_id": f"video{idx}", "title": f"Some title {idx}"}
        index_store.index_doc("ta_video", doc["youtube_id"], doc)

    yield index_store
    close_conn(INDEX_DB)


@pytest.fixture
def mounts(tmp_path):
    """mounts file"""
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(MOUNTS, encoding="utf-8")

    return str(mounts_file)


def test_filesystem_type(mounts):
    """longest matching mount point wins"""
    assert filesystem_type("/cache", mounts) == "ext4"
    assert filesystem_type("/cache/index.sqlite3", mounts) == "ext4"
    assert filesystem_type("/cachefolder", mounts) == "overlay"
    assert filesystem_type("/youtube/channel", mounts) == "nfs4"
    assert filesystem_type("/cache/my share/file", mounts) == "cifs"
    assert filesystem_type("/cache/remote", mounts) == "fuse.sshfs"
    assert filesystem_type("/cache", "/does/not/exist") == ""


def test_is_network_fs():
    """detect unreliable filesystems"""
    for fs_type in ["nfs", "nfs4", "cifs", "smb3", "fuse.sshfs", "NFS"]:
        assert is_network_fs(fs_type)

    for fs_type in ["ext4", "xfs", "btrfs", "overlay", "zfs", "tmpfs", ""]:
        assert not is_network_fs(fs_type)


def test_integrity_ok(store):
    """healthy files pass, result is stored"""
    # pylint: disable=unused-argument
    checker = IntegrityCheck()
    assert checker.is_due()
    status = checker.run()
    assert status["ok"] is True
    assert status["errors"] == []
    assert checker.get_status()["ok"] is True
    assert not checker.is_due()
    assert not StateStore().get_message_dict(checker.MESSAGE_KEY)


def test_integrity_fts_out_of_sync(store):
    """full text index not matching the documents is detected"""
    conn = store.conn
    conn.execute("DROP TRIGGER ta_video_au")
    conn.execute(
        "UPDATE ta_video SET source = jsonb(?) WHERE _id = 'video1'",
        ('{"youtube_id": "video1", "title": "Completely different"}',),
    )
    checker = IntegrityCheck()
    status = checker.run()
    assert status["ok"] is False
    assert "ta_video_fts" in status["errors"][0]
    message = StateStore().get_message_dict(checker.MESSAGE_KEY)
    assert message["level"] == "error"

    # repaired index clears the notification
    conn.execute("INSERT INTO ta_video_fts (ta_video_fts) VALUES ('rebuild')")
    assert checker.run()["ok"] is True
    assert not StateStore().get_message_dict(checker.MESSAGE_KEY)


def test_integrity_damaged_file(store):
    """damaged file is reported"""
    # pylint: disable=unused-argument
    close_conn(INDEX_DB)
    path = db_path(INDEX_DB)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)

    size = os.path.getsize(path)
    with open(path, "r+b") as f:
        f.seek(4096)
        f.write(os.urandom(size - 4096))

    errors = IntegrityCheck().check_file(path)
    assert errors


def test_snapshot_compressed_roundtrip(store):
    """snapshot is compressed and can be restored"""
    handler = IndexSnapshot()
    snapshot_name = handler.take_snapshot_now()["snapshot_name"]
    files = os.listdir(handler.SNAPSHOT_DIR)
    assert files == [f"{snapshot_name}.sqlite3.gz"]

    snapshots = handler.get_snapshot_stats()["snapshots"]
    assert [i["id"] for i in snapshots] == [snapshot_name]
    assert snapshots[0]["size"] > 0

    store.delete_doc("ta_video", "video1")
    store.index_doc("ta_video", "new", {"youtube_id": "new", "title": "x"})
    assert handler.restore_all(snapshot_name)
    assert store.get_doc("ta_video", "video1")
    assert store.get_doc("ta_video", "new") is None
    assert store.count("ta_video") == 20
    response = store.search(
        ["ta_video"], {"query": {"match": {"title": "title"}}}
    )
    assert response["hits"]["total"]["value"] == 20
    assert os.listdir(handler.SNAPSHOT_DIR) == files

    assert handler.delete_single_snapshot(snapshot_name)
    assert os.listdir(handler.SNAPSHOT_DIR) == []


def test_snapshot_uncompressed_restore(store):
    """snapshots of previous versions are still usable"""
    handler = IndexSnapshot()
    os.makedirs(handler.SNAPSHOT_DIR)
    snapshot_name = "ta_daily_20240101-120000"
    file_path = os.path.join(handler.SNAPSHOT_DIR, f"{snapshot_name}.sqlite3")
    store.snapshot_to(file_path)
    sqlite3.connect(file_path).close()

    assert handler.get_single_snapshot(snapshot_name)["id"] == snapshot_name
    store.delete_doc("ta_video", "video1")
    assert handler.restore_all(snapshot_name)
    assert store.get_doc("ta_video", "video1")
    assert handler.delete_single_snapshot(snapshot_name)


def test_snapshot_invalid_id(store):
    """ids outside of the snapshot folder are refused"""
    # pylint: disable=unused-argument
    handler = IndexSnapshot()
    assert handler.restore_all("../index") is False
    assert handler.delete_single_snapshot("other_name") is False


@pytest.mark.parametrize(
    "body",
    [
        {"query": {"fuzzy_like_this": {"title": "x"}}},
        {"query": {"match": {"title": "x"}, "term": {"a": "b"}}},
        {"query": {"match_all": {}}, "aggs": {"a": {"geo_grid": {}}}},
        {
            "query": {"match_all": {}},
            "aggs": {
                "a": {"terms": {"field": "tags", "order": {"_other": "asc"}}}
            },
        },
        {
            "query": {"match_all": {}},
            "aggs": {
                "a": {
                    "date_histogram": {
                        "field": "published",
                        "calendar_interval": "fortnight",
                    }
                }
            },
        },
    ],
)
def test_unsupported_query_raises(store, body):
    """anything not translated fails loudly instead of returning nonsense"""
    with pytest.raises(ValueError):
        store.search(["ta_video"], body)
