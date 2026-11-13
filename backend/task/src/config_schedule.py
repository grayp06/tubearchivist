"""
Functionality:
- Handle scheduler config update
"""

from datetime import datetime

from django.utils import dateformat
from task.models import CustomPeriodicTask
from task.src.cron import CronSchedule
from task.src.task_config import TASK_CONFIG


class ScheduleBuilder:
    """build schedules for the task scheduler"""

    SCHEDULES = {
        "update_subscribed": "0 8 *",
        "download_pending": "0 16 *",
        "check_reindex": "0 12 *",
        "thumbnail_check": "0 17 *",
        "run_backup": "0 18 0",
        "version_check": "0 11 *",
    }
    MSG = "message:setting"

    def update_schedule(
        self, task_name: str, cron_schedule: str, schedule_conf: dict | None
    ) -> CustomPeriodicTask:
        """update schedule"""
        if cron_schedule == "auto":
            cron_schedule = self.SCHEDULES[task_name]

        task = self.get_set_task(task_name, cron_schedule)

        if schedule_conf:
            for key, value in schedule_conf.items():
                task = self.set_config(task_name, key, value)

        return task

    def get_set_task(self, task_name, schedule=False):
        """get task"""
        try:
            task = CustomPeriodicTask.objects.get(name=task_name)
        except CustomPeriodicTask.DoesNotExist:
            description = TASK_CONFIG[task_name].get("title")
            task = CustomPeriodicTask(
                name=task_name,
                task=task_name,
                description=description,
            )

        if schedule:
            cron = self.get_set_cron_tab(schedule)
            task.minute = cron.minute
            task.hour = cron.hour
            task.day_of_week = cron.day_of_week
            task.last_run_at = dateformat.make_aware(datetime.now())
            task.save()

        return task

    @staticmethod
    def get_set_cron_tab(schedule: str) -> CronSchedule:
        """needs to be validated before"""
        return CronSchedule.from_string(schedule)

    def set_config(
        self, task_name: str, key: str, value
    ) -> CustomPeriodicTask:
        """set task_config, validate before"""
        task = CustomPeriodicTask.objects.get(name=task_name)
        task.task_config.update({key: value})
        task.save()

        return task


class CrontabValidator:
    """validate crontab"""

    CONFIG = {
        "check_reindex": ["days"],
        "run_backup": ["rotate"],
    }

    @staticmethod
    def validate_fields(cron_fields: str) -> None:
        """expect 3 cron fields"""
        if not len(cron_fields) == 3:
            raise ValueError("expected three cron schedule fields")

    @staticmethod
    def validate_minute(minute_field: str):
        """expect minute int"""
        if not minute_field.isdigit():
            raise ValueError("Invalid value for minutes. Must be an integer.")

        minutes = int(minute_field)
        if not 0 <= minutes <= 59:
            raise ValueError("Invalid minutes. Must be between 0 and 59.")

    @staticmethod
    def validate_cron_tab(minute, hour, day_of_week):
        """check if crontab can be created"""
        try:
            CronSchedule(minute=minute, hour=hour, day_of_week=day_of_week)
        except ValueError as err:
            raise ValueError(f"invalid crontab: {err}") from err

    def validate_cron(self, cron_expression):
        """create crontab schedule"""
        if not cron_expression or cron_expression == "auto":
            return

        cron_fields = cron_expression.split()
        self.validate_fields(cron_fields)

        minute, hour, day_of_week = cron_fields
        self.validate_minute(minute)
        self.validate_cron_tab(minute, hour, day_of_week)

    def validate_config(self, task_name: str, schedule_config: dict):
        """validate config for given task"""
        if not schedule_config:
            return

        config_keys = self.CONFIG.get(task_name)
        if not config_keys:
            raise ValueError(f"task '{task_name}' doesn't take config")

        for key in schedule_config:
            if key not in config_keys:
                raise ValueError(f"invalid config key for task '{task_name}'")
