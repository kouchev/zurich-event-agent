#!/bin/sh
# Serve the site (and the "my picks" calendar API, see server.py) on localhost only.
# Run with BIND=0.0.0.0 to expose it on the network.
cd "$(dirname "$0")" && exec .venv/bin/python server.py >> server.log 2>&1
