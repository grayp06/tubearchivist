"""
start backend python application, read env var
single process: threaded web server plus the task dispatcher thread
"""

import os
import signal
import sys
from os import environ

from waitress import serve

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

PORT = int(environ.get("TA_BACKEND_PORT") or 8080)
THREADS = int(environ.get("TA_WEB_THREADS") or 4)


def main():
    """start dispatcher and serve application"""
    # pylint: disable=import-outside-toplevel
    from config.wsgi import application
    from task.src.dispatcher import Dispatcher

    dispatcher = Dispatcher()
    dispatcher.start()

    def shutdown(signum, frame):
        """stop running tasks before exit"""
        # pylint: disable=unused-argument
        print("shutdown: stopping running tasks")
        dispatcher.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    serve(
        application,
        host="0.0.0.0",
        port=PORT,
        threads=THREADS,
        connection_limit=100,
        channel_timeout=120,
        ident="tubearchivist",
    )


if __name__ == "__main__":
    main()
