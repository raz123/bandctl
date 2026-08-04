#!/system/bin/sh
# Band Controller — web UI for LTE/NR band locking
# Starts Python HTTP server on localhost:8080 after boot

MODDIR=${0%/*}
WEB_DIR="$MODDIR/web"
PORT=8080

# PRIMARY: Python runtime bundled inside the module (module dir is DE-accessible
# at boot under KernelSU, so no user unlock / Termux needed for auto-start).
BUNDLED_PYTHON="$MODDIR/python/bin/python3.14"
BUNDLED_LD="$MODDIR/python/usr/lib"

# Fallbacks (bounded, cheap): Termux python, then portable pyroot runtime.
TERMUX_PYTHON="/data/data/com.termux/files/usr/bin/python3"
PYROOT_PYTHON="/data/local/tmp/pyroot/usr/bin/python3.14"
PYROOT_LD="/data/local/tmp/pyroot/usr/lib"

# Wait for boot to complete
while [ "$(getprop sys.boot_completed)" != "1" ]; do
  sleep 2
done

# Pick a Python. Bundled runtime is first — no wait needed, it is present at
# boot. Fallbacks are single bounded checks (no 60s Termux wait anymore).
PYTHON=""
if [ -f "$BUNDLED_PYTHON" ]; then
  PYTHON="$BUNDLED_PYTHON"
  export LD_LIBRARY_PATH="$BUNDLED_LD"
  echo "$(date) bandctl: using bundled python" >> /sdcard/modem_watch_sessions.log
elif [ -f "$TERMUX_PYTHON" ]; then
  PYTHON="$TERMUX_PYTHON"
  echo "$(date) bandctl: bundled python missing, using Termux python" >> /sdcard/modem_watch_sessions.log
elif [ -f "$PYROOT_PYTHON" ]; then
  PYTHON="$PYROOT_PYTHON"
  export LD_LIBRARY_PATH="$PYROOT_LD"
  echo "$(date) bandctl: bundled python missing, using pyroot fallback" >> /sdcard/modem_watch_sessions.log
fi

if [ -z "$PYTHON" ]; then
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
chmod 755 "$MODDIR/qmi/qmi_band" "$MODDIR/web/server.py" "$MODDIR/customize.sh" "$MODDIR/service.sh" "$MODDIR/python/bin/python3.14" 2>/dev/null

# Start Python HTTP server
nohup "$PYTHON" "$WEB_DIR/server.py" > /dev/null 2>&1 &

echo "$(date) bandctl: server started on port $PORT" >> /sdcard/modem_watch_sessions.log
