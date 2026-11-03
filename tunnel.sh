#!/bin/sh
# Publishes the local site (serve.sh, localhost:8765) at https://events.3dation.com via a Cloudflare Tunnel.
# Config: ~/.cloudflared/config.yml (not in the repo; holds the tunnel id).
cd "$(dirname "$0")" && exec "$HOME/.local/bin/cloudflared" tunnel --no-autoupdate run zurich-events >> tunnel.log 2>&1
