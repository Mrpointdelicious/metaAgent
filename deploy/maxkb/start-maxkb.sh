#!/bin/bash
# Pinned MaxKB v2.10.7-lts, external text models only. Keep PostgreSQL/Redis's
# official start-all.sh, replace the Python supervisor/local-model server.
set -Eeuo pipefail
mkdir -p /opt/maxkb/logs /opt/maxkb/local /opt/maxkb/python-packages /opt/maxkb-app/tmp
if [ ! -s /opt/maxkb/.app-secret ]; then
  umask 077
  python -c 'import secrets; print(secrets.token_urlsafe(50))' > /opt/maxkb/.app-secret
fi
export MAXKB_SECRET_KEY="$(cat /opt/maxkb/.app-secret)"
export TMPDIR=/opt/maxkb-app/tmp
export HF_HOME=/opt/maxkb-app/model/base
cd /opt/maxkb-app
python main.py upgrade_db
python main.py collect_static
cd /opt/maxkb-app/apps
export PYTHONPATH=/opt/maxkb-app/apps
export DJANGO_SETTINGS_MODULE=maxkb.settings
SERVER_NAME=web gunicorn maxkb.wsgi:application --bind 0.0.0.0:8080 \
  --worker-class gthread --threads 8 --workers 1 --timeout 120 \
  --graceful-timeout 30 --access-logfile /dev/null --error-logfile - &
maxkb_web_pid=$!
SERVER_NAME=celery C_FORCE_ROOT=1 PYTHONOPTIMIZE=1 LC_ALL=C.UTF-8 \
  celery -A ops worker -P threads -l error -c 2 -Q celery \
  --heartbeat-interval 10 -n 'celery@%h' --without-mingle &
maxkb_task_pid=$!
cleanup() {
  kill -TERM "$maxkb_web_pid" "$maxkb_task_pid" 2>/dev/null || true
  wait "$maxkb_web_pid" "$maxkb_task_pid" 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 0' TERM INT
wait -n "$maxkb_web_pid" "$maxkb_task_pid"
