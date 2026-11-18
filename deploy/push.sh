#!/usr/bin/env bash
# Copy the code to the VPS and restart the web service. Run from the project root (Git Bash works):
#   bash deploy/push.sh user@host
# Never copies .env, the database, logs or the local venv.
set -euo pipefail
HOST="${1:?usage: push.sh user@host}"
APP=/opt/concert-scraper

tar czf - --exclude=.venv --exclude=data --exclude=.env --exclude=__pycache__ --exclude=.pytest_cache \
    --exclude=.claude --exclude=.git . \
  | ssh "$HOST" "sudo mkdir -p $APP && sudo tar xzf - -C $APP && sudo chown -R concert:concert $APP 2>/dev/null || true"

# pick up new requirements, then restart (skip quietly before the first setup.sh run)
ssh "$HOST" "if [ -x $APP/.venv/bin/pip ]; then sudo -u concert $APP/.venv/bin/pip install -q -r $APP/requirements.txt && sudo systemctl restart concert-watcher; fi"
echo "pushed to $HOST:$APP"
