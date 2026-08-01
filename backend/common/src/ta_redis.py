"""Disk-backed state and queue compatibility layer.

The public class names are retained to avoid a flag-day change throughout the
application.  No Redis server is used: values and ordered queues live in the
same on-disk SQLite database as Django.
"""

import json
import sqlite3
import time
from contextlib import contextmanager

from common.src.env_settings import EnvironmentSettings


class SQLiteBase:
    """Open short-lived SQLite transactions, safe across worker processes."""

    NAME_SPACE = EnvironmentSettings.REDIS_NAME_SPACE

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(
            EnvironmentSettings.DATABASE_PATH, timeout=30, isolation_level=None
        )
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        self._create_tables(connection)
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _create_tables(connection):
        connection.execute(
            """CREATE TABLE IF NOT EXISTS common_state (
                key TEXT PRIMARY KEY, value TEXT NOT NULL, expires_at REAL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS common_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue TEXT NOT NULL, value TEXT NOT NULL,
                UNIQUE(queue, value)
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS common_queue_order ON common_queue(queue, id)"
        )


class RedisArchivist(SQLiteBase):
    """Legacy API for application state, now persisted by SQLite."""

    CHANNELS = [
        "download", "add", "rescan", "subchannel", "subplaylist",
        "playlistscan", "setting",
    ]

    def __init__(self):
        # A few old connection checks access ``conn``.  Point it at this
        # compatibility object while those call sites are phased out.
        self.conn = self

    def set_message(self, key, message, expire=False, save=False):
        del save
        value = json.dumps(message) if isinstance(message, dict) else message
        seconds = 20 if expire is True else expire
        expires_at = time.time() + seconds if seconds else None
        with self.connection() as connection:
            connection.execute(
                """INSERT INTO common_state(key, value, expires_at) VALUES(?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                   value=excluded.value, expires_at=excluded.expires_at""",
                (self.NAME_SPACE + key, value, expires_at),
            )

    def bg_save(self):
        """SQLite commits every state mutation; retained as a no-op."""

    def get_message_str(self, key):
        full_key = self.NAME_SPACE + key
        with self.connection() as connection:
            row = connection.execute(
                "SELECT value, expires_at FROM common_state WHERE key=?", (full_key,)
            ).fetchone()
            if not row:
                return None
            if row[1] is not None and row[1] <= time.time():
                connection.execute("DELETE FROM common_state WHERE key=?", (full_key,))
                return None
            return row[0]

    def get_message_dict(self, key):
        value = self.get_message_str(key)
        return json.loads(value) if value else {}

    def get_message(self, key):
        return self.get_message_dict(key) or {"status": False}

    def list_keys(self, query):
        prefix = self.NAME_SPACE + query
        with self.connection() as connection:
            connection.execute(
                "DELETE FROM common_state WHERE expires_at IS NOT NULL AND expires_at<=?",
                (time.time(),),
            )
            rows = connection.execute(
                "SELECT key FROM common_state WHERE key LIKE ? ORDER BY key",
                (prefix.replace("%", "\\%").replace("_", "\\_") + "%",),
            )
            return [row[0][len(self.NAME_SPACE) :] for row in rows]

    def list_items(self, query):
        return [self.get_message_dict(key) for key in self.list_keys(query)]

    def del_message(self, key, save=False):
        del save
        with self.connection() as connection:
            cursor = connection.execute(
                "DELETE FROM common_state WHERE key=?", (self.NAME_SPACE + key,)
            )
            return cursor.rowcount > 0


class RedisQueue(SQLiteBase):
    """FIFO queue persisted on disk and atomically consumable by workers."""

    def __init__(self, queue_name):
        self.key = self.NAME_SPACE + queue_name

    def get_all(self):
        with self.connection() as connection:
            return [row[0] for row in connection.execute(
                "SELECT value FROM common_queue WHERE queue=? ORDER BY id", (self.key,)
            )]

    def length(self):
        with self.connection() as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM common_queue WHERE queue=?", (self.key,)
            ).fetchone()[0]

    def in_queue(self, element):
        with self.connection() as connection:
            found = connection.execute(
                "SELECT 1 FROM common_queue WHERE queue=? AND value=?",
                (self.key, element),
            ).fetchone()
            return "in_queue" if found else False

    def add(self, to_add):
        if to_add:
            with self.connection() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO common_queue(queue, value) VALUES(?, ?)",
                    (self.key, to_add),
                )

    def add_list(self, to_add):
        with self.connection() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO common_queue(queue, value) VALUES(?, ?)",
                ((self.key, value) for value in to_add if value),
            )

    def max_score(self):
        with self.connection() as connection:
            row = connection.execute(
                "SELECT MAX(id) FROM common_queue WHERE queue=?", (self.key,)
            ).fetchone()
            return row[0]

    def _get_next_score(self):
        return (self.max_score() or 0) + 1

    def get_next(self):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, value FROM common_queue WHERE queue=? ORDER BY id LIMIT 1",
                (self.key,),
            ).fetchone()
            if not row:
                connection.execute("COMMIT")
                return None, None
            connection.execute("DELETE FROM common_queue WHERE id=?", (row[0],))
            connection.execute("COMMIT")
            return row[1], row[0]

    def clear(self):
        with self.connection() as connection:
            connection.execute("DELETE FROM common_queue WHERE queue=?", (self.key,))


class TaskRedis(RedisArchivist):
    """Legacy task-status API backed by the common state table."""

    BASE = "celery-task-meta-"
    EXPIRE = 60 * 60 * 24
    COMMANDS = ["STOP", "KILL"]

    def get_all(self):
        return [key.removeprefix(self.BASE) for key in self.list_keys(self.BASE)]

    def get_single(self, task_id):
        return self.get_message_dict(self.BASE + task_id)

    def set_key(self, task_id, message, expire=False):
        self.set_message(
            self.BASE + task_id, message, expire=self.EXPIRE if expire else False
        )

    def set_command(self, task_id, command):
        if command not in self.COMMANDS:
            raise ValueError(f"{command} not in valid commands {self.COMMANDS}")
        message = self.get_single(task_id)
        if not message:
            raise KeyError(f"{task_id} not found")
        message["command"] = command
        self.set_key(task_id, message)

    def del_task(self, task_id):
        self.del_message(self.BASE + task_id)

    def del_all(self):
        for task_id in self.get_all():
            self.del_task(task_id)
