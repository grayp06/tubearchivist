"""test sqlite backed state store"""

# pylint: disable=redefined-outer-name,unused-argument

import time

import pytest
from common.src.env_settings import EnvironmentSettings
from common.src.ta_redis import RedisArchivist, RedisQueue, TaskRedis


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """state store in temporary folder"""
    monkeypatch.setattr(EnvironmentSettings, "CACHE_DIR", str(tmp_path))


def test_messages(state_dir):
    """set, get and delete messages"""
    handler = RedisArchivist()
    handler.set_message("1:progress:abc", {"position": 5})
    handler.set_message("1:progress:def", {"position": 9})
    handler.set_message("plain", "text")
    assert handler.get_message_dict("1:progress:abc") == {"position": 5}
    assert handler.get_message_str("plain") == "text"
    assert handler.get_message_dict("missing") == {}
    assert handler.get_message_str("missing") is None
    assert len(handler.list_keys("1:progress")) == 2
    assert len(handler.list_items("1:progress:*")) == 2
    assert handler.del_message("plain") is True
    assert handler.del_message("plain") is False


def test_message_expire(state_dir):
    """expired messages are gone"""
    handler = RedisArchivist()
    handler.set_message("short", {"a": 1}, expire=1)
    assert handler.get_message_dict("short") == {"a": 1}
    time.sleep(1.1)
    assert handler.get_message_dict("short") == {}
    assert not handler.list_keys("short")


def test_queue(state_dir):
    """queue keeps order and is unique"""
    queue = RedisQueue("test:queue")
    queue.add_list(["one", "two", "three"])
    queue.add("four")
    queue.add("two")
    assert queue.length() == 4
    assert queue.get_all() == ["one", "three", "four", "two"]
    assert queue.in_queue("three") == "in_queue"
    assert queue.in_queue("nope") is False
    assert queue.get_next() == ("one", 1)
    assert queue.max_score() == 5
    queue.clear()
    assert queue.get_next() == (None, None)
    assert RedisQueue("other").max_score() is None


def test_task_results(state_dir):
    """task results and commands"""
    handler = TaskRedis()
    message = {"status": "PENDING", "name": "download_pending", "task_id": "1"}
    handler.set_key("1", message)
    assert handler.get_all() == ["1"]
    assert handler.get_single("1")["status"] == "PENDING"
    handler.set_command("1", "STOP")
    assert handler.get_single("1")["command"] == "STOP"
    with pytest.raises(ValueError):
        handler.set_command("1", "INVALID")

    with pytest.raises(KeyError):
        handler.set_command("missing", "STOP")

    handler.del_task("1")
    assert handler.get_single("1") == {}
