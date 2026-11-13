"""
functionality:
- key value store with expiry, backed by sqlite
- hold temporary queues in sqlite
- store task state and results

The class names are kept from the redis based implementation
so the rest of the application doesn't need to care.
"""

import json
import time

from common.src.sqlite_conn import STATE_DB, get_conn, write_tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    expires REAL
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS queue (
    name TEXT NOT NULL,
    item TEXT NOT NULL,
    score REAL NOT NULL,
    PRIMARY KEY (name, item)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS queue_score ON queue (name, score);
CREATE TABLE IF NOT EXISTS task (
    task_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kwargs TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    message TEXT,
    command TEXT,
    pid INTEGER,
    retries INTEGER NOT NULL DEFAULT 0,
    run_after REAL NOT NULL DEFAULT 0,
    created REAL NOT NULL,
    expires REAL
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS task_state ON task (state, created);
"""


class RedisBase:
    """connection base for the state store"""

    def __init__(self):
        self.conn = get_conn(STATE_DB, schema=SCHEMA)


class RedisArchivist(RedisBase):
    """collection of methods to interact with key value store"""

    CHANNELS: list[str] = [
        "download",
        "add",
        "rescan",
        "subchannel",
        "subplaylist",
        "playlistscan",
        "setting",
    ]

    def set_message(
        self,
        key: str,
        message: dict | str,
        expire: bool | int = False,
        save: bool = False,
    ) -> None:
        """write new message"""
        # pylint: disable=unused-argument
        to_write = (
            json.dumps(message) if isinstance(message, dict) else message
        )
        expires = None
        if expire:
            secs = 20 if isinstance(expire, bool) else expire
            expires = time.time() + secs

        self.conn.execute(
            "INSERT OR REPLACE INTO kv (key, value, expires) VALUES (?, ?, ?)",
            (key, to_write, expires),
        )

    def get_message_str(self, key: str) -> str | None:
        """get message string"""
        row = self.conn.execute(
            "SELECT value FROM kv WHERE key = ? "
            + "AND (expires IS NULL OR expires > ?)",
            (key, time.time()),
        ).fetchone()
        if not row:
            return None

        return row[0]

    def get_message_dict(self, key: str) -> dict:
        """get message dict"""
        reply = self.get_message_str(key)
        if not reply:
            return {}

        return json.loads(reply)

    def list_keys(self, query: str) -> list:
        """return all key matches"""
        self._purge()
        rows = self.conn.execute(
            "SELECT key FROM kv WHERE substr(key, 1, ?) = ?",
            (len(query), query),
        ).fetchall()

        return [i[0] for i in rows]

    def list_items(self, query: str) -> list:
        """list all matches"""
        query = query.rstrip("*")
        self._purge()
        rows = self.conn.execute(
            "SELECT value FROM kv WHERE substr(key, 1, ?) = ?",
            (len(query), query),
        ).fetchall()

        return [json.loads(i[0]) for i in rows]

    def del_message(self, key: str, save: bool = False) -> bool:
        """delete key"""
        # pylint: disable=unused-argument
        cursor = self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))

        return bool(cursor.rowcount)

    def _purge(self) -> None:
        """remove expired keys"""
        self.conn.execute(
            "DELETE FROM kv WHERE expires IS NOT NULL AND expires <= ?",
            (time.time(),),
        )


class RedisQueue(RedisBase):
    """
    dynamically interact with queues, ordered by score
    - low score number is first in queue
    - add new items with high score number

    queue names in use:
    download:channel            channels during download
    download:playlist:full      playlists during dl for full refresh
    download:playlist:quick     playlists during dl for quick refresh
    download:video              videos during downloads
    index:comment               videos needing comment indexing
    reindex:ta_video            reindex videos
    reindex:ta_channel          reindex channels
    reindex:ta_playlist         reindex playlists

    """

    def __init__(self, queue_name: str):
        super().__init__()
        self.key = queue_name

    def get_all(self) -> list[str]:
        """return all elements in list"""
        rows = self.conn.execute(
            "SELECT item FROM queue WHERE name = ? ORDER BY score, item",
            (self.key,),
        ).fetchall()

        return [i[0] for i in rows]

    def length(self) -> int:
        """return total elements in list"""
        row = self.conn.execute(
            "SELECT COUNT(*) FROM queue WHERE name = ?", (self.key,)
        ).fetchone()

        return row[0]

    def in_queue(self, element) -> str | bool:
        """check if element is in list"""
        row = self.conn.execute(
            "SELECT 1 FROM queue WHERE name = ? AND item = ?",
            (self.key, element),
        ).fetchone()
        if row:
            return "in_queue"

        return False

    def add(self, to_add: str) -> None:
        """add single item to queue"""
        if not to_add:
            return

        self.add_list([to_add])

    def add_list(self, to_add: list) -> None:
        """add list to queue"""
        if not to_add:
            return

        with write_tx(self.conn):
            next_score = self._get_next_score()
            self.conn.executemany(
                "INSERT OR REPLACE INTO queue (name, item, score) "
                + "VALUES (?, ?, ?)",
                [
                    (self.key, item, next_score + idx)
                    for idx, item in enumerate(to_add)
                ],
            )

    def max_score(self) -> int | None:
        """get max score"""
        row = self.conn.execute(
            "SELECT MAX(score) FROM queue WHERE name = ?", (self.key,)
        ).fetchone()
        if row[0] is None:
            return None

        return int(row[0])

    def _get_next_score(self) -> float:
        """get next score in queue to append"""
        last = self.max_score()
        if last is None:
            return 1.0

        return last + 1.0

    def get_next(self) -> tuple[str | None, int | None]:
        """return next element in the queue, if available"""
        with write_tx(self.conn):
            row = self.conn.execute(
                "SELECT item, score FROM queue WHERE name = ? "
                + "ORDER BY score, item LIMIT 1",
                (self.key,),
            ).fetchone()
            if not row:
                return None, None

            self.conn.execute(
                "DELETE FROM queue WHERE name = ? AND item = ?",
                (self.key, row[0]),
            )

        return row[0], int(row[1])

    def clear(self) -> None:
        """delete list"""
        self.conn.execute("DELETE FROM queue WHERE name = ?", (self.key,))


class TaskRedis(RedisBase):
    """interact with stored tasks

    state is the lifecycle of the task in the runner:
    QUEUED -> STARTED -> DONE
    message is the result document as the api exposes it,
    it only exists once the task registered itself or completed.
    """

    EXPIRE: int = 60 * 60 * 24
    COMMANDS: list[str] = ["STOP", "KILL"]

    def get_all(self) -> list:
        """return all task ids with a result message"""
        self._purge()
        rows = self.conn.execute(
            "SELECT task_id FROM task WHERE message IS NOT NULL "
            + "ORDER BY created"
        ).fetchall()

        return [i[0] for i in rows]

    def get_single(self, task_id: str) -> dict:
        """return content of single task"""
        row = self.conn.execute(
            "SELECT message, command, name, state FROM task WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return {}

        message, command, name, state = row
        if message:
            result = json.loads(message)
        else:
            result = {
                "status": state,
                "result": None,
                "traceback": None,
                "date_done": False,
                "name": name,
                "task_id": task_id,
            }

        if command:
            result["command"] = command

        return result

    def set_key(
        self, task_id: str, message: dict, expire: bool | int = False
    ) -> None:
        """set result message, initial or update"""
        to_write = {k: v for k, v in message.items() if k != "command"}
        expires = time.time() + self.EXPIRE if expire else None
        with write_tx(self.conn):
            cursor = self.conn.execute(
                "UPDATE task SET message = ?, expires = ? WHERE task_id = ?",
                (json.dumps(to_write), expires, task_id),
            )
            if cursor.rowcount:
                return

            self.conn.execute(
                "INSERT INTO task "
                + "(task_id, name, state, message, created, expires) "
                + "VALUES (?, ?, 'DONE', ?, ?, ?)",
                (
                    task_id,
                    message.get("name", ""),
                    json.dumps(to_write),
                    time.time(),
                    expires,
                ),
            )

    def set_command(self, task_id: str, command: str) -> None:
        """set task command"""
        if command not in self.COMMANDS:
            print(f"{command} not in valid commands {self.COMMANDS}")
            raise ValueError

        cursor = self.conn.execute(
            "UPDATE task SET command = ? WHERE task_id = ?",
            (command, task_id),
        )
        if not cursor.rowcount:
            print(f"{task_id} not found")
            raise KeyError

    def del_task(self, task_id: str) -> None:
        """delete task result by id"""
        self.conn.execute("DELETE FROM task WHERE task_id = ?", (task_id,))

    def del_all(self) -> None:
        """delete all task results"""
        self.conn.execute("DELETE FROM task")

    def _purge(self) -> None:
        """remove expired tasks"""
        self.conn.execute(
            "DELETE FROM task WHERE expires IS NOT NULL AND expires <= ?",
            (time.time(),),
        )
