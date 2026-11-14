"""
functionality:
- regular integrity check of all sqlite files
- result is stored in the state store and surfaced as notification
"""

import os
import sqlite3
import time

from common.src.env_settings import EnvironmentSettings
from common.src.index_schema import FTS_FIELDS, fts_table
from common.src.sqlite_conn import INDEX_DB, STATE_DB, db_path
from common.src.state_store import StateStore


class IntegrityCheck:
    """check database files for corruption"""

    STATUS_KEY = "integrity:status"
    MESSAGE_KEY = "message:settings:integrity"
    INTERVAL = 7 * 24 * 60 * 60
    MAX_ERRORS = 10

    def is_due(self) -> bool:
        """true if last check is older than interval"""
        status = self.get_status()
        if not status:
            return True

        return time.time() - status.get("checked", 0) >= self.INTERVAL

    def get_status(self) -> dict:
        """get result of last check"""
        return StateStore().get_message_dict(self.STATUS_KEY)

    @staticmethod
    def db_files() -> list[str]:
        """all database files to check"""
        return [
            db_path(INDEX_DB),
            db_path(STATE_DB),
            os.path.join(EnvironmentSettings.CACHE_DIR, "db.sqlite3"),
        ]

    def run(self) -> dict:
        """run check on all files, store and return result"""
        started = time.time()
        errors: list[str] = []
        for file_path in self.db_files():
            if not os.path.exists(file_path):
                continue

            name = os.path.basename(file_path)
            errors.extend(f"{name}: {i}" for i in self.check_file(file_path))

        status = {
            "ok": not errors,
            "checked": int(started),
            "duration": round(time.time() - started, 1),
            "errors": errors[: self.MAX_ERRORS],
        }
        self._store(status)

        return status

    def check_file(self, file_path: str) -> list[str]:
        """check single file, returns list of problems"""
        errors: list[str] = []
        try:
            conn = sqlite3.connect(file_path, timeout=60)
        except sqlite3.Error as err:
            return [str(err)]

        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            errors.extend(i[0] for i in rows if i[0] != "ok")
            if not errors and os.path.basename(file_path) == INDEX_DB:
                errors.extend(self._check_fts(conn))
        except sqlite3.Error as err:
            errors.append(str(err))
        finally:
            conn.close()

        return errors[: self.MAX_ERRORS]

    @staticmethod
    def _check_fts(conn: sqlite3.Connection) -> list[str]:
        """compare full text index with the documents"""
        errors = []
        for index in FTS_FIELDS:
            fts = fts_table(index)
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?", (fts,)
            ).fetchone()
            if not exists:
                continue

            try:
                conn.execute(
                    f"INSERT INTO {fts} ({fts}, rank) "
                    + "VALUES ('integrity-check', 1)"
                )
            except sqlite3.Error as err:
                errors.append(f"{fts}: {err}")

        return errors

    def _store(self, status: dict) -> None:
        """store result, add or remove notification"""
        handler = StateStore()
        handler.set_message(self.STATUS_KEY, status)
        if status["ok"]:
            handler.del_message(self.MESSAGE_KEY)
            print(f"[integrity] all ok, took {status['duration']}s")
            return

        print(f"[integrity] problems found: {status['errors']}")
        message = {
            "id": "integrity",
            "title": "Database integrity check failed",
            "group": "settings:integrity",
            "api_start": False,
            "api_stop": False,
            "level": "error",
            "messages": [
                "Restore a snapshot or backup, check your storage.",
                *status["errors"][:3],
            ],
        }
        handler.set_message(self.MESSAGE_KEY, message)
