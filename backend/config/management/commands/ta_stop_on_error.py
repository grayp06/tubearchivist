"""
Functionality:
- stop on incompatible database layout
- convert the database of the multi container version in place
"""

from time import sleep

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

ERROR_MESSAGE = """
    🗙 Database is incompatible, see latest release notes for instructions:
    🗙 https://github.com/tubearchivist/tubearchivist/releases/tag/v0.5.0
"""

LEGACY_SCHEDULES = """
    SELECT p.name, p.task, p.description, c.minute, c.hour, c.day_of_week,
        p.enabled, p.last_run_at, t.task_config
    FROM task_customperiodictask t
    JOIN django_celery_beat_periodictask p ON p.id = t.periodictask_ptr_id
    JOIN django_celery_beat_crontabschedule c ON c.id = p.crontab_id
"""

LEGACY_PERMISSIONS = """
    SELECT id FROM auth_permission WHERE content_type_id IN (
        SELECT id FROM django_content_type
        WHERE app_label = 'django_celery_beat'
    )
"""
LEGACY_CLEANUP = [
    "DELETE FROM auth_group_permissions "
    + f"WHERE permission_id IN ({LEGACY_PERMISSIONS})",
    "DELETE FROM user_account_user_permissions "
    + f"WHERE permission_id IN ({LEGACY_PERMISSIONS})",
    "DELETE FROM auth_permission WHERE content_type_id IN ("
    + "SELECT id FROM django_content_type "
    + "WHERE app_label = 'django_celery_beat')",
    "DELETE FROM django_content_type WHERE app_label = 'django_celery_beat'",
]


class Command(BaseCommand):
    """command framework"""

    # pylint: disable=no-member

    def handle(self, *args, **options):
        """handle"""
        self.stdout.write("[MIGRATION] Confirming v0.5.0 table layout")
        all_tables = self.list_tables()
        for table in all_tables:
            if table == "home_account":

                self.stdout.write(self.style.ERROR(ERROR_MESSAGE))
                sleep(60)
                raise CommandError(ERROR_MESSAGE)

        if "django_celery_beat_periodictask" in all_tables:
            self.convert_legacy_schedules(all_tables)

        self.stdout.write(self.style.SUCCESS("    ✓ local DB is up-to-date."))

    def list_tables(self):
        """raw list all tables"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table';"
            )
            tables = cursor.fetchall()

        return [table[0] for table in tables]

    def convert_legacy_schedules(self, all_tables):
        """
        the multi container version stored schedules for an external
        scheduler, move them to the standalone schedule table
        """
        self.stdout.write("[MIGRATION] Convert schedules to standalone table")
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(LEGACY_SCHEDULES)
            schedules = cursor.fetchall()
            cursor.execute("DROP TABLE task_customperiodictask")
            for table in all_tables:
                if table.startswith("django_celery_beat_"):
                    cursor.execute(f'DROP TABLE "{table}"')

            cursor.execute(
                "DELETE FROM django_migrations "
                + "WHERE app IN ('task', 'django_celery_beat')"
            )
            for statement in LEGACY_CLEANUP:
                cursor.execute(statement)

            cursor.execute(
                "CREATE TABLE ta_legacy_schedule (name, task, description, "
                + "minute, hour, day_of_week, enabled, last_run_at, "
                + "task_config)"
            )
            cursor.executemany(
                "INSERT INTO ta_legacy_schedule VALUES "
                + "(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                schedules,
            )

        message = f"    ✓ converted {len(schedules)} schedules"
        self.stdout.write(self.style.SUCCESS(message))
