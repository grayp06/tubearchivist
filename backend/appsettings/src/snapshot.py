"""
functionality:
- handle snapshots of the index
- a snapshot is a consistent copy of the index database file
"""

import os
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from common.src.env_settings import EnvironmentSettings
from common.src.index_schema import FTS_FIELDS, all_indexes, fts_table
from common.src.index_store import IndexStore
from common.src.sqlite_conn import write_tx


class ElasticSnapshot:
    """interact with snapshots of the index"""

    POLICY = "ta_daily"
    SNAPSHOT_DIR = os.path.join(EnvironmentSettings.CACHE_DIR, "snapshot")
    EXPIRE_DAYS = 7
    MIN_COUNT = 3
    MAX_COUNT = 14
    DAILY_HOUR = 12
    STAMP_FORMAT = "%Y%m%d-%H%M%S"

    def __init__(self):
        self.all_indices = all_indexes()
        self.zone = ZoneInfo(EnvironmentSettings.TZ)

    def setup(self):
        """setup snapshots, take one now if the last one is outdated"""
        print("snapshot: run setup")
        os.makedirs(self.SNAPSHOT_DIR, exist_ok=True)
        if self._needs_snapshot():
            _ = self.take_snapshot_now()

    def run_daily(self) -> bool:
        """called regularly, take daily snapshot and rotate if needed"""
        os.makedirs(self.SNAPSHOT_DIR, exist_ok=True)
        if not self._needs_snapshot():
            return False

        self.take_snapshot_now()
        self.rotate()

        return True

    def _needs_snapshot(self) -> bool:
        """check if last snapshot is older than a day"""
        snap_dicts = self._get_all_snapshots()
        if not snap_dicts:
            print("snapshot: create initial snapshot")
            return True

        last_stamp = snap_dicts[0]["end_stamp"]
        now = int(datetime.now().timestamp())
        outdated = (now - last_stamp) / 60 / 60 > 24
        if outdated:
            print("snapshot: is outdated, create new now")

        return outdated

    def _path(self, snapshot_id: str) -> str | None:
        """get validated path of snapshot"""
        if snapshot_id != os.path.basename(snapshot_id):
            return None

        if not snapshot_id.startswith(f"{self.POLICY}_"):
            return None

        return os.path.join(self.SNAPSHOT_DIR, f"{snapshot_id}.sqlite3")

    def take_snapshot_now(self, wait=False):
        """create snapshot now"""
        # pylint: disable=unused-argument
        os.makedirs(self.SNAPSHOT_DIR, exist_ok=True)
        started = datetime.now(tz=self.zone)
        snapshot_name = f"{self.POLICY}_{started.strftime(self.STAMP_FORMAT)}"
        file_path = self._path(snapshot_name)
        if os.path.exists(file_path):
            return {"snapshot_name": snapshot_name}

        temp_path = f"{file_path}.partial"
        if os.path.exists(temp_path):
            os.remove(temp_path)

        IndexStore().snapshot_to(temp_path)
        os.rename(temp_path, file_path)
        print(f"snapshot: created {snapshot_name}")

        return {"snapshot_name": snapshot_name}

    def rotate(self) -> None:
        """delete expired snapshots"""
        snap_dicts = self._get_all_snapshots() or []
        expire = datetime.now().timestamp() - self.EXPIRE_DAYS * 24 * 60 * 60
        for idx, snapshot in enumerate(snap_dicts):
            if idx < self.MIN_COUNT:
                continue

            if idx >= self.MAX_COUNT or snapshot["end_stamp"] < expire:
                self.delete_single_snapshot(snapshot["id"])

    def get_snapshot_stats(self):
        """get snapshot info for frontend"""
        snapshot_info = self._build_policy_details()
        snapshot_info.update({"snapshots": self._get_all_snapshots() or []})

        return snapshot_info

    def get_single_snapshot(self, snapshot_id):
        """get single snapshot metadata"""
        file_path = self._path(snapshot_id)
        if not file_path or not os.path.exists(file_path):
            print(f"snapshots: not found: {snapshot_id}")
            return False

        return self._parse_single_snapshot(snapshot_id, file_path)

    def _get_all_snapshots(self):
        """get a list of all snapshots, newest first"""
        if not os.path.exists(self.SNAPSHOT_DIR):
            return False

        snap_dicts = []
        for file_name in os.listdir(self.SNAPSHOT_DIR):
            snapshot_id, ext = os.path.splitext(file_name)
            if ext != ".sqlite3" or not self._path(snapshot_id):
                continue

            file_path = os.path.join(self.SNAPSHOT_DIR, file_name)
            try:
                snap_dicts.append(
                    self._parse_single_snapshot(snapshot_id, file_path)
                )
            except ValueError:
                continue

        if not snap_dicts:
            return False

        snap_dicts.sort(key=lambda i: i["id"], reverse=True)

        return snap_dicts

    def _parse_single_snapshot(self, snapshot_id, file_path):
        """build metadata of single snapshot"""
        prefix_len = len(self.POLICY) + 1
        stamp = snapshot_id[prefix_len:]
        started = datetime.strptime(stamp, self.STAMP_FORMAT)
        started = started.replace(tzinfo=self.zone)
        end_stamp = int(os.path.getmtime(file_path))
        ended = datetime.fromtimestamp(end_stamp, tz=self.zone)

        return {
            "id": snapshot_id,
            "state": "SUCCESS",
            "es_version": f"sqlite {sqlite3.sqlite_version}",
            "start_date": started.strftime("%Y-%m-%d %H:%M"),
            "end_date": ended.strftime("%Y-%m-%d %H:%M"),
            "end_stamp": end_stamp,
            "duration_s": max(end_stamp - int(started.timestamp()), 0),
        }

    def _build_policy_details(self):
        """get additional policy details"""
        now = datetime.now(tz=self.zone)
        snap_dicts = self._get_all_snapshots()
        if snap_dicts:
            last = datetime.fromtimestamp(
                snap_dicts[0]["end_stamp"], self.zone
            )
            next_exec_date = max(last + timedelta(days=1), now)
        else:
            next_exec_date = now

        return {
            "next_exec": int(next_exec_date.timestamp()),
            "next_exec_str": next_exec_date.strftime("%Y-%m-%d %H:%M"),
            "expire_after": f"{self.EXPIRE_DAYS}d",
        }

    def restore_all(self, snapshot_name):
        """replace all documents of the index with the snapshot"""
        file_path = self._path(snapshot_name)
        if not file_path or not os.path.exists(file_path):
            print(f"snapshot: failed to restore, not found {snapshot_name}")
            return False

        store = IndexStore()
        conn = store.conn
        conn.execute("ATTACH DATABASE ? AS snapshot", (file_path,))
        try:
            with write_tx(conn):
                for index in self.all_indices:
                    self._restore_index(store, index)
        finally:
            conn.execute("DETACH DATABASE snapshot")

        store.optimize()
        print(f"snapshot: restored {snapshot_name}")

        return {"accepted": True}

    @staticmethod
    def _restore_index(store: IndexStore, index: str) -> None:
        """restore single index from attached snapshot"""
        conn = store.conn
        store.delete_index(index)
        store.create_index(index)
        in_snapshot = conn.execute(
            "SELECT 1 FROM snapshot.sqlite_master "
            + "WHERE type = 'table' AND name = ?",
            (index,),
        ).fetchone()
        if not in_snapshot:
            return

        # copy without triggers, then build the full text index in one go
        has_fts = index in FTS_FIELDS
        if has_fts:
            for suffix in ("ai", "ad", "au"):
                conn.execute(f"DROP TRIGGER IF EXISTS {index}_{suffix}")

        conn.execute(
            f"INSERT INTO main.{index} (rowid, _id, source) "
            + f"SELECT rowid, _id, source FROM snapshot.{index}"
        )
        if has_fts:
            fts = fts_table(index)
            conn.execute(f"INSERT INTO {fts} ({fts}) VALUES ('rebuild')")
            store.create_index(index)

    def delete_single_snapshot(self, snapshot_id):
        """delete single snapshot"""
        file_path = self._path(snapshot_id)
        if not file_path or not os.path.exists(file_path):
            print(f"snapshot: failed to delete, not found {snapshot_id}")
            return False

        os.remove(file_path)
        print(f"snapshot: deleting {snapshot_id}")

        return {"acknowledged": True}
