# Standalone schedule model, no external scheduler app needed

from django.db import migrations, models

IMPORT_LEGACY = """
    INSERT INTO task_customperiodictask (name, task, description, minute,
        hour, day_of_week, enabled, last_run_at, task_config)
    SELECT name, task, description, minute, hour, day_of_week, enabled,
        last_run_at, task_config FROM ta_legacy_schedule
"""


def import_legacy_schedules(apps, schema_editor):
    """import schedules converted from the multi container version"""
    # pylint: disable=unused-argument
    tables = schema_editor.connection.introspection.table_names()
    if "ta_legacy_schedule" not in tables:
        return

    schema_editor.execute(IMPORT_LEGACY)
    schema_editor.execute("DROP TABLE ta_legacy_schedule")


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="CustomPeriodicTask",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("name", models.CharField(max_length=200, unique=True)),
                ("task", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True, default="")),
                ("minute", models.CharField(default="*", max_length=240)),
                ("hour", models.CharField(default="*", max_length=96)),
                ("day_of_week", models.CharField(default="*", max_length=64)),
                ("enabled", models.BooleanField(default=True)),
                ("last_run_at", models.DateTimeField(blank=True, null=True)),
                ("task_config", models.JSONField(default=dict)),
            ],
        ),
        migrations.RunPython(
            import_legacy_schedules, migrations.RunPython.noop
        ),
    ]
