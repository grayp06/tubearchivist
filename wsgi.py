"""Production container entry point with one SQLite queue worker."""

from app import (
    APP,
    CACHE_DIR,
    DATABASE_PATH,
    DOWNLOAD_DIR,
    recover_interrupted_downloads,
    initialize_database,
    migrate_storage_layout,
    start_download_worker,
)


DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)
initialize_database(DATABASE_PATH)
migrate_storage_layout(DATABASE_PATH, DOWNLOAD_DIR, CACHE_DIR)
recovered = recover_interrupted_downloads(DATABASE_PATH)
if recovered:
    print(f"Requeued {recovered} interrupted download(s).", flush=True)
start_download_worker()

application = APP
