"""task model"""

from django.db import models
from task.src.cron import CronSchedule


class CustomPeriodicTask(models.Model):
    """scheduled task with custom metadata"""

    name = models.CharField(max_length=200, unique=True)
    task = models.CharField(max_length=200)
    description = models.TextField(blank=True, default="")
    minute = models.CharField(max_length=240, default="*")
    hour = models.CharField(max_length=96, default="*")
    day_of_week = models.CharField(max_length=64, default="*")
    enabled = models.BooleanField(default=True)
    last_run_at = models.DateTimeField(null=True, blank=True)
    task_config = models.JSONField(default=dict)

    def __str__(self):
        return f"{self.name}: {self.schedule_parsed}"

    @property
    def schedule_parsed(self):
        """parse schedule"""
        return f"{self.minute} {self.hour} {self.day_of_week}"

    @property
    def schedule_human(self):
        """describe schedule in words"""
        try:
            cron = CronSchedule(self.minute, self.hour, self.day_of_week)
        except ValueError:
            return self.schedule_parsed

        return cron.human_readable
