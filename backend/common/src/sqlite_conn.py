"""
functionality:
- shared sqlite connection handling for the index and the state store
- one connection per thread and file, tuned for low memory usage
"""

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Callable

from common.src.env_settings import EnvironmentSettings

INDEX_DB = "index.sqlite3"
STATE_DB = "state.sqlite3"

MIN_SQLITE = (3, 45, 0)

_LOCAL = threading.local()


def db_path(name: str) -> str:
    """absolute path of db file in the cache folder"""
    return os.path.join(EnvironmentSettings.CACHE_DIR, name)


def get_conn(
    name: str,
    schema: str | None = None,
    setup: Callable[[sqlite3.Connection], None] | None = None,
) -> sqlite3.Connection:
    """
    get the connection of this thread for db file name
    schema and setup run once when the connection gets created
    """
    path = db_path(name)
    key = (os.getpid(), path)
    conns = getattr(_LOCAL, "conns", None)
    if conns is None:
        conns = {}
        _LOCAL.conns = conns

    conn = conns.get(key)
    if conn is None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        conn = sqlite3.connect(path, timeout=60, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute(f"PRAGMA cache_size=-{EnvironmentSettings.DB_CACHE_KB}")
        conn.execute("PRAGMA wal_autocheckpoint=500")
        if schema:
            conn.executescript(schema)

        if setup:
            setup(conn)

        conns[key] = conn

    return conn


def close_conn(name: str) -> None:
    """close the connection of this thread if there is one"""
    key = (os.getpid(), db_path(name))
    conns = getattr(_LOCAL, "conns", None) or {}
    conn = conns.pop(key, None)
    if conn is not None:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """immediate write transaction, reentrant"""
    if conn.in_transaction:
        yield conn
        return

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    conn.execute("COMMIT")


def check_sqlite() -> tuple[bool, str]:
    """validate the sqlite library has everything needed"""
    version = sqlite3.sqlite_version
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        expected = ".".join(str(i) for i in MIN_SQLITE)
        return False, f"SQLite {version} is too old, need {expected}"

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE t USING fts5(a, tokenize='porter')")
        conn.execute("SELECT jsonb('{}')")
    except sqlite3.Error as err:
        return False, f"SQLite {version} misses a feature: {err}"
    finally:
        conn.close()

    return True, version


NETWORK_FS = {
    "nfs",
    "nfs4",
    "cifs",
    "smb",
    "smb2",
    "smb3",
    "smbfs",
    "9p",
    "afs",
    "ceph",
    "glusterfs",
    "lustre",
    "ncpfs",
    "davfs",
    "fuse.sshfs",
    "fuse.glusterfs",
    "fuse.rclone",
    "fuse.s3fs",
    "fuse.davfs",
}


def filesystem_type(path: str, mounts_file: str = "/proc/mounts") -> str:
    """get filesystem type of the mount holding path, empty if unknown"""
    try:
        with open(mounts_file, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return ""

    real_path = os.path.realpath(path)
    best_match = ("", "")
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue

        mount_point = parts[1].replace("\\040", " ")
        prefix = mount_point.rstrip("/") + "/"
        if real_path != mount_point and not real_path.startswith(prefix):
            continue

        if len(mount_point) >= len(best_match[0]):
            best_match = (mount_point, parts[2])

    return best_match[1]


def is_network_fs(fs_type: str) -> bool:
    """true if sqlite locking is not reliable on filesystem type"""
    return fs_type.lower() in NETWORK_FS
