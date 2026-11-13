"""
Functionality:
- check that the storage is usable
- python management.py ta_connection
"""

import os
from time import sleep

from common.src.env_settings import EnvironmentSettings
from common.src.index_store import IndexStore
from common.src.sqlite_conn import INDEX_DB, check_sqlite, db_path
from common.src.ta_redis import RedisArchivist
from django.core.management.base import BaseCommand, CommandError

TOPIC = """

#######################
#    Storage check    #
#######################

"""


class Command(BaseCommand):
    """command framework"""

    # pylint: disable=no-member
    help = "Check storage"

    def handle(self, *args, **options):
        """run all commands"""
        self.stdout.write(TOPIC)
        self._sqlite_version_check()
        self._cache_dir_check()
        self._state_store_check()
        self._index_store_check()

    def _fail(self, message):
        """stop startup with message"""
        self.stdout.write(self.style.ERROR(message))
        sleep(60)
        raise CommandError(message)

    def _sqlite_version_check(self):
        """check sqlite library has all needed features"""
        self.stdout.write("[1] SQLite version check")
        is_ok, message = check_sqlite()
        if not is_ok:
            self._fail(f"    🗙 {message}")

        self.stdout.write(self.style.SUCCESS(f"    ✓ SQLite {message}"))

    def _cache_dir_check(self):
        """check cache dir is writable"""
        self.stdout.write("[2] check cache folder is writable")
        cache_dir = EnvironmentSettings.CACHE_DIR
        test_file = os.path.join(cache_dir, ".write_test")
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(test_file, "w", encoding="utf-8") as f:
                f.write("test")

            os.remove(test_file)
        except OSError as err:
            self._fail(f"    🗙 can not write to {cache_dir}: {err}")

        self.stdout.write(self.style.SUCCESS(f"    ✓ {cache_dir} is writable"))

    def _state_store_check(self):
        """check state store"""
        self.stdout.write("[3] check state store")
        try:
            handler = RedisArchivist()
            handler.set_message("startup_check", "ok", expire=10)
            is_ok = handler.get_message_str("startup_check") == "ok"
        except Exception as err:  # pylint: disable=broad-except
            self._fail(f"    🗙 {type(err).__name__}: {err}")

        if not is_ok:
            self._fail("    🗙 state store read failed")

        self.stdout.write(self.style.SUCCESS("    ✓ state store verified"))

    def _index_store_check(self):
        """check index store"""
        self.stdout.write("[4] check index store")
        try:
            row = IndexStore().conn.execute("PRAGMA quick_check").fetchone()
        except Exception as err:  # pylint: disable=broad-except
            self._fail(f"    🗙 {type(err).__name__}: {err}")

        if row[0] != "ok":
            self._fail(f"    🗙 {db_path(INDEX_DB)} is damaged: {row[0]}")

        self.stdout.write(self.style.SUCCESS("    ✓ index store verified"))
