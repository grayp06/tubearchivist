"""Production container entry point with one SQLite queue worker."""

from app import (
    APP,
    DATABASE_PATH,
    DOWNLOAD_DIR,
    recover_interrupted_downloads,
    initialize_database,
    start_download_worker,
)


DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
initialize_database(DATABASE_PATH)
recovered = recover_interrupted_downloads(DATABASE_PATH)
if recovered:
    print(f"Requeued {recovered} interrupted download(s).", flush=True)
start_download_worker()

application = APP
