#!/system/bin/sh
# Band Controller — web UI for LTE/NR band locking
# Starts Python HTTP server on localhost:8080 after boot

MODDIR=${0%/*}
WEB_DIR="$MODDIR/web"
PORT=8080
PYTHON="/data/data/com.termux/files/usr/bin/python3"

# Wait for boot to complete
while [ "$(getprop sys.boot_completed)" != "1" ]; do
  sleep 2
done

# Wait for Termux Python
i=0
while [ ! -f "$PYTHON" ] && [ "$i" -lt 30 ]; do
  sleep 2
  i=$((i+1))
done

if [ ! -f "$PYTHON" ]; then
  echo "$(date) bandctl: Python not found, aborting" >> /sdcard/modem_watch_sessions.log
  exit 1
fi

# Kill any existing server
kill $(pgrep -f "server.py") 2>/dev/null
sleep 1

# Ensure config dir exists (customize.sh may be skipped on manual installs)
mkdir -p "$MODDIR/config"

# Start Python HTTP server
nohup "$PYTHON" "$WEB_DIR/server.py" > /dev/null 2>&1 &

echo "$(date) bandctl: server started on port $PORT" >> /sdcard/modem_watch_sessions.log
