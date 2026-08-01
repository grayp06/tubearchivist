#!/bin/bash
# startup script inside the container for tubearchivist

set -e

if [[ -n "$DJANGO_DEBUG" ]]; then
  LOGLEVEL="DEBUG"
else
  LOGLEVEL="INFO"
fi

TA_TASK_WORKERS="${TA_TASK_WORKERS:-1}"
if ! [[ "$TA_TASK_WORKERS" =~ ^[1-9][0-9]*$ ]]; then
    echo "TA_TASK_WORKERS must be a positive integer" >&2
    exit 1
fi

# update yt-dlp if needed
if [[ "${TA_AUTO_UPDATE_YTDLP,,}" =~ ^(release|nightly)$ ]]; then
    echo "Updating yt-dlp..."
    preflag=$([[ "${TA_AUTO_UPDATE_YTDLP,,}" == "nightly" ]] && echo "--pre" || echo "")
    python -m pip install --target=/root/.local/bin --upgrade $preflag "yt-dlp[default]" || {
        echo "yt-dlp update failed"
    }
fi

# stop on pending manual migration
python manage.py ta_stop_on_error

# django setup
python manage.py migrate
python manage.py collectstatic --noinput -c

# ta setup
python manage.py ta_envcheck
python manage.py ta_connection
python manage.py ta_startup

# start all tasks
nginx &
celery -A task.celery worker \
    --loglevel=$LOGLEVEL \
    --concurrency "$TA_TASK_WORKERS" \
    --max-tasks-per-child 5 \
    --max-memory-per-child 150000 &

./beat_auto_spawn.sh &

python backend_start.py
