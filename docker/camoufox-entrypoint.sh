#!/usr/bin/env bash
set -euo pipefail

# Start Xvfb on :99 if not already running
if ! pgrep -x Xvfb > /dev/null; then
    Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp &
    XVFB_PID=$!
    # Give it a moment to come up
    for i in $(seq 1 20); do
        if xdpyinfo -display :99 >/dev/null 2>&1; then
            break
        fi
        sleep 0.1
    done
fi

export DISPLAY=:99
exec "$@"
