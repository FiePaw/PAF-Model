#!/usr/bin/env bash
#
# start.sh — start the PAF-ModelDeepSeek VPS server with a crash-restart loop.
#
# Usage:
#   PAF_TOKEN=mysecret ./start.sh            # uses defaults (port 8000)
#   ./start.sh --port 8080 --token mysecret  # pass-through args
#
set -euo pipefail

cd "$(dirname "$0")"

PORT="${PAF_PORT:-8000}"
TOKEN="${PAF_TOKEN:-change-me}"
REQUEST_TIMEOUT="${PAF_REQUEST_TIMEOUT:-330}"

export PAF_PORT="$PORT"
export PAF_TOKEN="$TOKEN"
export PAF_REQUEST_TIMEOUT="$REQUEST_TIMEOUT"

# Forward any extra CLI args (e.g. --port / --token) straight to the server.
EXTRA_ARGS=("$@")

echo "[start.sh] launching VPS server on port ${PORT} (restart loop enabled)"

# Restart loop so the server survives crashes. Ctrl+C exits cleanly.
trap 'echo "[start.sh] stopping"; exit 0' INT TERM

while true; do
    python vps_server.py --port "$PORT" --token "$TOKEN" "${EXTRA_ARGS[@]}" || true
    echo "[start.sh] server exited; restarting in 3s..."
    sleep 3
done
