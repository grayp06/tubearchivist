#!/bin/bash
# startup script inside the container for tubearchivist

set -e

# keep memory fragmentation of the threaded web process low
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

# stop on pending manual migration
python manage.py ta_stop_on_error

# django setup
python manage.py migrate
python manage.py collectstatic --noinput -c

# ta setup
python manage.py ta_envcheck
python manage.py ta_connection
python manage.py ta_startup

# start web server, the application runs as a single process:
# web threads plus the task dispatcher, tasks run as short lived processes
nginx &

exec python backend_start.py
