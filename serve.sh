#!/bin/sh
# Serve the events page on localhost only. Run with BIND=0.0.0.0 to expose it on the network.
BIND=${BIND:-127.0.0.1}; PORT=${PORT:-8765}
cd "$(dirname "$0")/site" && exec python3 -m http.server "$PORT" --bind "$BIND" >> ../server.log 2>&1
