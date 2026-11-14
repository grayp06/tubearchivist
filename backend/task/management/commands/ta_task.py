"""
Functionality:
- run a single queued task in this process
- called by the dispatcher: python manage.py ta_task <task_id>
"""

from django.core.management.base import BaseCommand, CommandError
from task.src.runner import get_task
from task.src.task_queue import TaskQueue


class Command(BaseCommand):
    """command framework"""

    help = "Run queued task by ID"

    def add_arguments(self, parser):
        parser.add_argument("task_id", type=str)

    def handle(self, *args, **options):
        """run task"""
        task_id = options["task_id"]
        queue = TaskQueue()
        task = queue.get(task_id)
        if not task:
            raise CommandError(f"task not found: {task_id}")

        definition = get_task(task["name"])
        if not definition:
            queue.finish(task_id, "FAILURE", result="Unknown task")
            raise CommandError(f"unknown task: {task['name']}")

        definition.execute(task_id, task["kwargs"], task["retries"])
