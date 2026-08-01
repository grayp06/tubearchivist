"""Start the backend with a deliberately small default process footprint."""

from os import environ

import uvicorn
from common.src.env_settings import get_positive_int_env

LOG_LEVEL = "info" if environ.get("DJANGO_DEBUG") else "error"
PORT = int(environ.get("TA_BACKEND_PORT", 8080))


if __name__ == "__main__":
    uvicorn.run(
        "config.asgi:application",
        host="0.0.0.0",
        port=PORT,
        # Each worker imports Django and the application index code. One worker
        # is plenty for a personal archive and saves substantially more memory
        # than the old fixed pool of four. Larger installs can opt back in.
        workers=get_positive_int_env("TA_WEB_WORKERS", 1),
        log_level=LOG_LEVEL,
        reload=False,
    )
