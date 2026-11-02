#!/bin/sh
# Daily scan: fetch all sources, update events.db, re-render site/index.html
cd "$(dirname "$0")" && exec .venv/bin/python agent.py >> cron.log 2>&1
