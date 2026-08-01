#!/bin/sh
# Bambu Monitor launcher for Synology DSM.
# Idempotent: safe to run at boot AND as a periodic watchdog (every 5 min) -
# it (re)starts the app only if it isn't already running. Uses a pidfile +
# kill -0 check, which is busybox/POSIX-safe on DSM (pgrep -f is not reliable).
#
# DSM setup: Control Panel -> Task Scheduler
#   1) Triggered Task -> User-defined script -> Event: Boot-up,  User: root
#   2) Scheduled Task  -> repeat every 5 minutes,                User: root
#   Both run:  sh /volume1/apps/bambu-monitor/start.sh

APP_DIR=/volume1/apps/bambu-monitor
PY="$APP_DIR/venv/bin/python3"
PIDFILE="$APP_DIR/app.pid"

# Already running? do nothing (this is what keeps the watchdog from spawning
# duplicates). Called as `restart`, wait for the old process to die first -
# otherwise `kill ...; sh start.sh` races and silently starts nothing.
if [ "$1" = "restart" ] && [ -f "$PIDFILE" ]; then
    OLD=$(cat "$PIDFILE" 2>/dev/null)
    kill "$OLD" 2>/dev/null
    i=0
    while kill -0 "$OLD" 2>/dev/null && [ $i -lt 15 ]; do
        sleep 1
        i=$((i + 1))
    done
fi

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE" 2>/dev/null)" 2>/dev/null; then
    exit 0
fi

cd "$APP_DIR" || exit 1
echo "$(date '+%Y-%m-%d %H:%M:%S') starting bambu-monitor" >> "$APP_DIR/app.log"
nohup "$PY" "$APP_DIR/app.py" >> "$APP_DIR/app.log" 2>&1 &
echo $! > "$PIDFILE"
