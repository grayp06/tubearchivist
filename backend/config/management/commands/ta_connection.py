"""Verify the single on-disk database used by the application."""

import os
import sqlite3

from common.src.env_settings import EnvironmentSettings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Check the SQLite database"

    def handle(self, *args, **options):
        del args, options
        database = EnvironmentSettings.DATABASE_PATH
        self.stdout.write(f"Checking SQLite database at {database}")
        try:
            os.makedirs(os.path.dirname(database), exist_ok=True)
            with sqlite3.connect(database, timeout=30) as connection:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("CREATE TABLE IF NOT EXISTS connection_check(id INTEGER)")
                result = connection.execute("PRAGMA quick_check").fetchone()[0]
            if result != "ok":
                raise CommandError(f"SQLite quick_check failed: {result}")
        except (OSError, sqlite3.Error) as err:
            raise CommandError(f"SQLite connection failed: {err}") from err
        self.stdout.write(self.style.SUCCESS("SQLite connection verified"))
