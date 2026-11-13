"""
functionality:
- persistent task queue in sqlite
- tasks get added by any process and picked up by the dispatcher
"""

import json
import time
import uuid
from datetime import datetime, timezone

from common.src.sqlite_conn import write_tx
from common.src.ta_redis import RedisBase


class TaskQueue(RedisBase):
    """interact with queued and running tasks"""

    EXPIRE: int = 60 * 60 * 24

    def enqueue(self, name: str, kwargs: dict | None = None) -> str:
        """add task to queue, returns task id"""
        task_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO task (task_id, name, kwargs, state, created) "
            + "VALUES (?, ?, ?, 'QUEUED', ?)",
            (task_id, name, json.dumps(kwargs or {}), time.time()),
        )

        return task_id

    def claim_next(self) -> dict | None:
        """mark next queued task as started and return it"""
        with write_tx(self.conn):
            row = self.conn.execute(
                "SELECT task_id, name, kwargs, retries FROM task "
                + "WHERE state = 'QUEUED' AND run_after <= ? "
                + "ORDER BY created LIMIT 1",
                (time.time(),),
            ).fetchone()
            if not row:
                return None

            self.conn.execute(
                "UPDATE task SET state = 'STARTED', pid = NULL "
                + "WHERE task_id = ?",
                (row[0],),
            )

        return {
            "task_id": row[0],
            "name": row[1],
            "kwargs": json.loads(row[2]),
            "retries": row[3],
        }

    def get(self, task_id: str) -> dict | None:
        """get task to execute"""
        row = self.conn.execute(
            "SELECT task_id, name, kwargs, retries, state FROM task "
            + "WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if not row:
            return None

        return {
            "task_id": row[0],
            "name": row[1],
            "kwargs": json.loads(row[2]),
            "retries": row[3],
            "state": row[4],
        }

    def has_queued(self) -> bool:
        """check if there are tasks waiting, also delayed ones"""
        row = self.conn.execute(
            "SELECT 1 FROM task WHERE state = 'QUEUED' LIMIT 1"
        ).fetchone()

        return bool(row)

    def set_pid(self, task_id: str, pid: int) -> None:
        """store process id of running task"""
        self.conn.execute(
            "UPDATE task SET pid = ? WHERE task_id = ?", (pid, task_id)
        )

    def get_state(self, task_id: str) -> str | None:
        """get runner state of task"""
        row = self.conn.execute(
            "SELECT state FROM task WHERE task_id = ?", (task_id,)
        ).fetchone()

        return row[0] if row else None

    def get_kill_requests(self) -> list[str]:
        """get ids of started tasks asked to be killed"""
        rows = self.conn.execute(
            "SELECT task_id FROM task "
            + "WHERE state = 'STARTED' AND command = 'KILL'"
        ).fetchall()

        return [i[0] for i in rows]

    def retry(self, task_id: str, delay: int) -> None:
        """put started task back to the queue to run again after delay"""
        message = self._build_message(task_id, "RETRY", None, None)
        self.conn.execute(
            "UPDATE task SET state = 'QUEUED', retries = retries + 1, "
            + "run_after = ?, message = ?, pid = NULL WHERE task_id = ?",
            (time.time() + delay, json.dumps(message), task_id),
        )

    def finish(
        self,
        task_id: str,
        status: str,
        result: str | None = None,
        traceback: str | None = None,
    ) -> None:
        """store final result of task"""
        message = self._build_message(task_id, status, result, traceback)
        self.conn.execute(
            "UPDATE task SET state = 'DONE', message = ?, expires = ?, "
            + "pid = NULL WHERE task_id = ?",
            (json.dumps(message), time.time() + self.EXPIRE, task_id),
        )

    def _build_message(
        self,
        task_id: str,
        status: str,
        result: str | None,
        traceback: str | None,
    ) -> dict:
        """build result message as exposed over the api"""
        task = self.get(task_id) or {}

        return {
            "status": status,
            "result": result,
            "traceback": traceback,
            "date_done": datetime.now(tz=timezone.utc).isoformat(),
            "name": task.get("name"),
            "task_id": task_id,
            "kwargs": task.get("kwargs", {}),
            "retries": task.get("retries", 0),
        }

    def recover(self) -> int:
        """fail tasks interrupted by a restart, returns count"""
        rows = self.conn.execute(
            "SELECT task_id FROM task WHERE state = 'STARTED'"
        ).fetchall()
        for row in rows:
            self.finish(row[0], "FAILURE", result="Task interrupted")

        return len(rows)
