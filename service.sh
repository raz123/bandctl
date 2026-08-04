#!/system/bin/sh
# Band Controller — web UI for LTE/NR band locking
# Starts Python HTTP server on localhost:8080 after boot

MODDIR=${0%/*}
WEB_DIR="$MODDIR/web"
PORT=8080
PYTHON="/data/data/com.termux/files/usr/bin/python3"
# Fallback: portable Python runtime (Termux .debs unpacked to /data/local/tmp)
# used when Termux is missing/unavailable on the device.
PYROOT_PYTHON="/data/local/tmp/pyroot/usr/bin/python3.14"
PYROOT_LD="/data/local/tmp/pyroot/usr/lib"

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

if [ ! -f "$PYTHON" ] && [ -f "$PYROOT_PYTHON" ]; then
  echo "$(date) bandctl: Termux python missing, using pyroot fallback" >> /sdcard/modem_watch_sessions.log
  PYTHON="$PYROOT_PYTHON"
  export LD_LIBRARY_PATH="$PYROOT_LD"
fi

if [ ! -f "$PYTHON" ]; then
  echo "$(date) bandctl: Python not found, aborting" >> /sdcard/modem_watch_sessions.log
  exit 1
fi

# Kill any existing server
kill $(pgrep -f "server.py") 2>/dev/null
sleep 1

# Ensure config dir exists (customize.sh may be skipped on manual installs)
mkdir -p "$MODDIR/config"

# Self-heal exec bits: KernelSU's extractor may not preserve Unix modes and
# the metainstall pass of customize.sh can run with a mangled MODDIR, leaving
# qmi_band without +x. MODDIR is reliable here (service.sh runs by absolute
# path at boot), so re-assert 755 before anything executes the binaries.
chmod 755 "$MODDIR/qmi/qmi_band" "$MODDIR/web/server.py" "$MODDIR/customize.sh" "$MODDIR/service.sh" 2>/dev/null

# Start Python HTTP server
nohup "$PYTHON" "$WEB_DIR/server.py" > /dev/null 2>&1 &

echo "$(date) bandctl: server started on port $PORT" >> /sdcard/modem_watch_sessions.log
