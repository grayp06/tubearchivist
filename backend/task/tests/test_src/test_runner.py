"""test cron schedule, task queue and runner"""

# pylint: disable=redefined-outer-name,unused-argument

from datetime import datetime

import pytest
from common.src.env_settings import EnvironmentSettings
from task.src.cron import CronSchedule
from task.src.runner import PlainTask, Retry, TaskDefinition
from task.src.task_manager import TaskManager
from task.src.task_queue import TaskQueue


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """state store in temporary folder"""
    monkeypatch.setattr(EnvironmentSettings, "CACHE_DIR", str(tmp_path))


CRON_DAYS = [
    ("0 8 *", [0, 1, 2, 3, 4, 5, 6]),
    ("0 8 1-5", [1, 2, 3, 4, 5]),
    ("0 8 0,7", [0]),
    ("0 8 sat-sun", [0, 6]),
    ("0 8 */2", [0, 2, 4, 6]),
]


@pytest.mark.parametrize("expression, days", CRON_DAYS)
def test_cron_days(expression, days):
    """parse day of week"""
    assert sorted(CronSchedule.from_string(expression).days) == days


def test_cron_due():
    """a schedule is due once after the scheduled time passed"""
    cron = CronSchedule.from_string("30 8 *")
    last_run = datetime(2026, 1, 1, 8, 30)
    assert not cron.is_due(last_run, datetime(2026, 1, 2, 8, 29))
    assert cron.is_due(last_run, datetime(2026, 1, 2, 8, 30))
    assert cron.is_due(last_run, datetime(2026, 1, 5, 12, 0))
    assert not cron.is_due(
        datetime(2026, 1, 2, 8, 30), datetime(2026, 1, 2, 9)
    )
    assert not cron.is_due(None, datetime(2026, 1, 2, 9, 0))
    assert cron.is_due(None, datetime(2026, 1, 2, 8, 30))
    assert cron.human_readable == "At 08:30 AM, every day"
    weekly = CronSchedule.from_string("0 18 0")
    assert weekly.previous_run(datetime(2026, 1, 7, 12)) == datetime(
        2026, 1, 4, 18
    )


def test_queue_order(state_dir):
    """tasks get claimed in order, once"""
    queue = TaskQueue()
    first = queue.enqueue("one", {"key": "value"})
    second = queue.enqueue("two")
    claimed = queue.claim_next()
    assert claimed["task_id"] == first
    assert claimed["kwargs"] == {"key": "value"}
    assert queue.claim_next()["task_id"] == second
    assert queue.claim_next() is None
    assert queue.get_state(first) == "STARTED"
    # queued and started tasks have no result to list yet
    assert not TaskManager().get_all_results()
    assert queue.recover() == 2
    assert TaskManager().get_task(first)["status"] == "FAILURE"


def test_execute_success(state_dir):
    """run task and store result"""

    def add(self, first, second=2):
        return f"{self.name}: {first + second}"

    definition = TaskDefinition(add, "add", bind=True, base=PlainTask)
    task_id = TaskQueue().enqueue("add", {"first": 1})
    assert definition.execute(task_id, {"first": 1}) == "SUCCESS"
    result = TaskManager().get_task(task_id)
    assert result["status"] == "SUCCESS"
    assert result["result"] == "add: 3"


def test_execute_failure(state_dir):
    """failing task stores error"""

    def fail():
        raise ValueError("did not work")

    definition = TaskDefinition(fail, "fail")
    task_id = TaskQueue().enqueue("fail")
    assert definition.execute(task_id, {}) == "FAILURE"
    result = TaskManager().get_task(task_id)
    assert result["result"] == "did not work"
    assert "ValueError" in result["traceback"]


def test_execute_retry(state_dir):
    """task asking for a retry gets queued again until retries are used"""

    def retry():
        raise Retry()

    definition = TaskDefinition(retry, "retry", max_retries=1)
    definition.retry_delay = 0
    queue = TaskQueue()
    task_id = queue.enqueue("retry")
    queue.claim_next()
    assert definition.execute(task_id, {}, retries=0) == "RETRY"
    claimed = queue.claim_next()
    assert claimed["task_id"] == task_id and claimed["retries"] == 1
    assert definition.execute(task_id, {}, retries=1) == "FAILURE"
    assert queue.claim_next() is None


def test_bind_arguments():
    """positional arguments get validated and stored by name"""

    def func(self, url, expected_type=False):
        return self, url, expected_type

    definition = TaskDefinition(func, "func", bind=True)
    # pylint: disable=protected-access
    arguments = definition._bind_kwargs(("abc",), {"expected_type": "channel"})
    assert arguments == {"url": "abc", "expected_type": "channel"}
    with pytest.raises(TypeError):
        definition._bind_kwargs((), {"unknown": 1})
