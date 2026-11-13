"""
functionality:
- single background thread inside of the web process
- starts queued tasks as short lived processes
- queues scheduled tasks when they are due
- there is no memory used for workers while no task is running
"""

import fcntl
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from common.src.env_settings import EnvironmentSettings
from task.src.task_queue import TaskQueue

WAKE = threading.Event()
APP_DIR = str(Path(__file__).resolve().parents[2])


def wake_dispatcher() -> None:
    """wake up dispatcher in this process to look for new tasks"""
    WAKE.set()


class Scheduler:
    """queue tasks based on their stored schedule"""

    def run_due(self) -> list[str]:
        """queue all tasks due now, returns task names queued"""
        # pylint: disable=import-outside-toplevel
        from django.db import close_old_connections
        from task.models import CustomPeriodicTask
        from task.src.cron import CronSchedule

        zone = ZoneInfo(EnvironmentSettings.TZ)
        now = datetime.now(tz=zone)
        queued = []
        close_old_connections()
        try:
            for schedule in CustomPeriodicTask.objects.filter(enabled=True):
                try:
                    cron = CronSchedule(
                        schedule.minute, schedule.hour, schedule.day_of_week
                    )
                except ValueError as err:
                    print(f"[schedule] invalid cron {schedule.name}: {err}")
                    continue

                last_run = schedule.last_run_at
                if last_run:
                    last_run = last_run.astimezone(zone).replace(tzinfo=None)

                if not cron.is_due(last_run, now.replace(tzinfo=None)):
                    continue

                print(f"[schedule] queue scheduled task {schedule.task}")
                TaskQueue().enqueue(schedule.task)
                schedule.last_run_at = now
                schedule.save(update_fields=["last_run_at"])
                queued.append(schedule.task)
        finally:
            close_old_connections()

        return queued


class Dispatcher(threading.Thread):
    """run tasks from the queue"""

    IDLE_INTERVAL = 5
    BUSY_INTERVAL = 1
    SCHEDULE_INTERVAL = 20
    HOUSEKEEPING_INTERVAL = 600
    KILL_TIMEOUT = 10

    def __init__(self):
        super().__init__(name="ta-dispatcher", daemon=True)
        self.concurrency = max(EnvironmentSettings.TASK_CONCURRENCY, 1)
        self.running: dict[str, subprocess.Popen] = {}
        self.stopping = threading.Event()
        self.last_schedule = 0.0
        self.last_housekeeping = time.monotonic()
        self.lock_file = None

    def acquire_lock(self) -> bool:
        """make sure there is only one dispatcher for the cache folder"""
        path = os.path.join(EnvironmentSettings.CACHE_DIR, "dispatcher.lock")
        self.lock_file = open(path, "w", encoding="utf-8")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock_file.close()
            self.lock_file = None
            return False

        return True

    def run(self) -> None:
        """main loop"""
        if not self.acquire_lock():
            print("[dispatcher] already running in another process")
            return

        print(f"[dispatcher] started, concurrency {self.concurrency}")
        while not self.stopping.is_set():
            try:
                self.tick()
            except Exception as err:  # pylint: disable=broad-exception-caught
                print(f"[dispatcher] error: {type(err).__name__}: {err}")

            is_busy = self.running or TaskQueue().has_queued()
            interval = self.BUSY_INTERVAL if is_busy else self.IDLE_INTERVAL
            WAKE.wait(timeout=interval)
            WAKE.clear()

    def tick(self) -> None:
        """single loop iteration"""
        self.reap()
        self.kill_requested()
        if time.monotonic() - self.last_schedule >= self.SCHEDULE_INTERVAL:
            self.last_schedule = time.monotonic()
            Scheduler().run_due()

        if (
            time.monotonic() - self.last_housekeeping
            >= self.HOUSEKEEPING_INTERVAL
        ):
            self.last_housekeeping = time.monotonic()
            self.housekeeping()

        self.spawn()

    @staticmethod
    def housekeeping() -> None:
        """take daily index snapshot if enabled"""
        # pylint: disable=import-outside-toplevel
        from appsettings.src.config import AppConfig
        from appsettings.src.snapshot import ElasticSnapshot

        try:
            config = AppConfig().config
        except ValueError:
            return

        if config["application"]["enable_snapshot"]:
            ElasticSnapshot().run_daily()

    def spawn(self) -> None:
        """start queued tasks while there are free slots"""
        queue = TaskQueue()
        while len(self.running) < self.concurrency:
            task = queue.claim_next()
            if not task:
                return

            task_id = task["task_id"]
            print(f"[dispatcher] start {task['name']} {task_id}")
            process = subprocess.Popen(  # pylint: disable=consider-using-with
                [sys.executable, "manage.py", "ta_task", task_id],
                cwd=APP_DIR,
                start_new_session=True,
            )
            queue.set_pid(task_id, process.pid)
            self.running[task_id] = process

    def reap(self) -> None:
        """collect finished task processes"""
        queue = TaskQueue()
        for task_id, process in list(self.running.items()):
            returncode = process.poll()
            if returncode is None:
                continue

            self.running.pop(task_id)
            if queue.get_state(task_id) == "STARTED":
                # process ended without storing a result
                message = f"Task process exited with code {returncode}"
                print(f"[dispatcher] {task_id}: {message}")
                queue.finish(task_id, "FAILURE", result=message)

    def kill_requested(self) -> None:
        """terminate tasks with pending kill command"""
        queue = TaskQueue()
        for task_id in queue.get_kill_requests():
            process = self.running.pop(task_id, None)
            if process:
                print(f"[dispatcher] kill {task_id}")
                self._terminate(process)

            queue.finish(task_id, "REVOKED", result="Task killed")

    def _terminate(self, process: subprocess.Popen) -> None:
        """terminate process group of task"""
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return

            try:
                process.wait(timeout=self.KILL_TIMEOUT)
                return
            except subprocess.TimeoutExpired:
                continue

    def shutdown(self) -> None:
        """stop loop and terminate all running tasks"""
        self.stopping.set()
        WAKE.set()
        queue = TaskQueue()
        for task_id, process in list(self.running.items()):
            self._terminate(process)
            if queue.get_state(task_id) == "STARTED":
                queue.finish(task_id, "FAILURE", result="Task interrupted")

        self.running.clear()
