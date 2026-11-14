"""
functionality:
- define background tasks
- execute a task with callbacks, results and retries
- every task runs in its own short lived process
"""

import inspect
import traceback
from typing import Any, Callable

from common.src.state_store import StateStore
from task.src.task_config import TASK_CONFIG
from task.src.task_queue import TaskQueue

REGISTRY: dict[str, "TaskDefinition"] = {}


class Retry(Exception):
    """raise to run the task again later"""


class TaskRequest:
    """describe the current execution of a task"""

    def __init__(self, task_id: str, retries: int = 0):
        self.id = task_id
        self.retries = retries


class AsyncResult:
    """describe a task added to the queue"""

    status = "PENDING"

    def __init__(self, task_id: str, name: str):
        self.id = task_id
        self.name = name


class PlainTask:
    """task without messages and notifications"""

    def __init__(self, name: str, request: TaskRequest):
        self.name = name
        self.request = request

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """callback for task failure"""

    def on_success(self, retval, task_id, args, kwargs):
        """callback task completed"""

    def before_start(self, task_id, args, kwargs):
        """callback before initiating task"""

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """callback after task returns"""

    def retry(self) -> Retry:
        """build exception to raise for retrying"""
        return Retry()


class BaseTask(PlainTask):
    """base class to inherit each task from"""

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        """callback for task failure"""
        print(f"{task_id} Failed callback")
        message, key = self._build_message(level="error")
        message.update({"messages": [f"Task failed: {exc}"]})
        StateStore().set_message(key, message, expire=20)

    def on_success(self, retval, task_id, args, kwargs):
        """callback task completed"""
        print(f"{task_id} success callback")
        message, key = self._build_message()
        message.update({"messages": ["Task completed"]})
        StateStore().set_message(key, message, expire=5)

    def before_start(self, task_id, args, kwargs):
        """callback before initiating task"""
        print(f"{self.name} create callback")
        message, key = self._build_message()
        message.update({"messages": ["New task received."]})
        StateStore().set_message(key, message)

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """callback after task returns"""
        # pylint: disable=import-outside-toplevel
        from task.src.notify import Notifications

        print(f"{task_id} return callback")
        task_title = TASK_CONFIG.get(self.name).get("title")
        Notifications(self.name).send(task_id, task_title)

    def send_progress(
        self, message_lines, progress=False, title=False, level="info"
    ):
        """send progress message"""
        message, key = self._build_message(level=level)
        message.update(
            {
                "messages": message_lines,
                "progress": progress,
            }
        )
        if title:
            message["title"] = title

        StateStore().set_message(key, message)

    def _build_message(self, level="info"):
        """build message dict"""
        # pylint: disable=import-outside-toplevel
        from task.src.task_manager import TaskManager

        task_id = self.request.id
        message = TASK_CONFIG.get(self.name).copy()
        message.update({"level": level, "id": task_id})
        task_result = TaskManager().get_task(task_id)
        if task_result:
            command = task_result.get("command", None)
            message.update({"command": command})

        key = f"message:{message.get('group')}:{task_id.split('-')[0]}"
        return message, key

    def is_stopped(self):
        """check if task is stopped"""
        # pylint: disable=import-outside-toplevel
        from task.src.task_manager import TaskManager

        return TaskManager().is_stopped(self.request.id)


class TaskDefinition:
    """a registered task function"""

    def __init__(self, func: Callable, name: str, **options):
        self.func = func
        self.name = name
        self.bind: bool = options.get("bind", False)
        self.base: type[PlainTask] = options.get("base") or PlainTask
        self.max_retries: int = options.get("max_retries", 3)
        self.retry_delay: int = options.get("default_retry_delay", 180)

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

    def _bind_kwargs(self, args: tuple, kwargs: dict) -> dict:
        """validate arguments and convert to keyword arguments"""
        signature = inspect.signature(self.func)
        if self.bind:
            bound = signature.bind(None, *args, **kwargs)
        else:
            bound = signature.bind(*args, **kwargs)

        arguments = dict(bound.arguments)
        if self.bind:
            arguments.pop(next(iter(signature.parameters)))

        return arguments

    def delay(self, *args, **kwargs) -> AsyncResult:
        """add task to the queue"""
        # pylint: disable=import-outside-toplevel
        from task.src.dispatcher import wake_dispatcher

        arguments = self._bind_kwargs(args, kwargs)
        task_id = TaskQueue().enqueue(self.name, arguments)
        wake_dispatcher()

        return AsyncResult(task_id, self.name)

    def execute(self, task_id: str, kwargs: dict, retries: int = 0) -> str:
        """run the task now in this process, returns final status"""
        task = self.base(self.name, TaskRequest(task_id, retries))
        queue = TaskQueue()
        status, retval, einfo = "SUCCESS", None, None
        try:
            task.before_start(task_id, (), kwargs)
            if self.bind:
                retval = self.func(task, **kwargs)
            else:
                retval = self.func(**kwargs)

            queue.finish(task_id, status, result=_as_result(retval))
            task.on_success(retval, task_id, (), kwargs)
        except Retry:
            status = self._handle_retry(task, task_id, kwargs)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            status, retval, einfo = "FAILURE", exc, traceback.format_exc()
            print(einfo)
            queue.finish(task_id, status, result=str(exc), traceback=einfo)
            task.on_failure(exc, task_id, (), kwargs, einfo)

        try:
            task.after_return(status, retval, task_id, (), kwargs, einfo)
        except Exception:  # pylint: disable=broad-exception-caught
            print(traceback.format_exc())

        return status

    def _handle_retry(self, task: PlainTask, task_id: str, kwargs) -> str:
        """queue task again or fail if retries are used up"""
        queue = TaskQueue()
        if task.request.retries < self.max_retries:
            print(f"{task_id} retry in {self.retry_delay}s")
            queue.retry(task_id, self.retry_delay)
            return "RETRY"

        message = f"Task failed after {self.max_retries} retries"
        queue.finish(task_id, "FAILURE", result=message)
        task.on_failure(message, task_id, (), kwargs, None)

        return "FAILURE"


def _as_result(retval: Any) -> str | None:
    """convert return value to storable result"""
    if retval is None:
        return None

    return str(retval)


def shared_task(name: str, **options) -> Callable:
    """decorator to register a function as background task"""

    def decorator(func: Callable) -> TaskDefinition:
        definition = TaskDefinition(func, name, **options)
        REGISTRY[name] = definition
        return definition

    return decorator


def get_task(name: str) -> TaskDefinition | None:
    """get registered task by name"""
    # pylint: disable=import-outside-toplevel,unused-import
    import task.tasks  # noqa: F401

    return REGISTRY.get(name)
