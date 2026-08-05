#!/system/bin/sh
# Band Controller — web UI for LTE/NR band locking
# Starts Python HTTP server on localhost:8080 after boot
# Re-applies the persisted band preference at boot: waits for the server +
# radio, then POSTs /api/boot-apply (best-effort — the UI can apply later)

MODDIR=${0%/*}
WEB_DIR="$MODDIR/web"
PORT=8080
LOG="$MODDIR/config/bandctl.log"

# Ensure config dir exists (customize.sh may be skipped on manual installs)
mkdir -p "$MODDIR/config"

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
  echo "$(date) bandctl: using bundled python" >> "$LOG"
elif [ -f "$TERMUX_PYTHON" ]; then
  PYTHON="$TERMUX_PYTHON"
  echo "$(date) bandctl: bundled python missing, using Termux python" >> "$LOG"
elif [ -f "$PYROOT_PYTHON" ]; then
  PYTHON="$PYROOT_PYTHON"
  export LD_LIBRARY_PATH="$PYROOT_LD"
  echo "$(date) bandctl: bundled python missing, using pyroot fallback" >> "$LOG"
fi

if [ -z "$PYTHON" ]; then
  echo "$(date) bandctl: Python not found, aborting" >> "$LOG"
  exit 1
fi

# Kill any existing server
kill $(pgrep -f "server.py") 2>/dev/null
sleep 1

# Self-heal exec bits: KernelSU's extractor may not preserve Unix modes and
# the metainstall pass of customize.sh can run with a mangled MODDIR, leaving
# qmi_band without +x. MODDIR is reliable here (service.sh runs by absolute
# path at boot), so re-assert 755 before anything executes the binaries.
chmod 755 "$MODDIR/qmi/qmi_band" "$MODDIR/web/server.py" "$MODDIR/customize.sh" "$MODDIR/service.sh" "$MODDIR/python/bin/python3.14" 2>/dev/null

# Start Python HTTP server
nohup "$PYTHON" "$WEB_DIR/server.py" > /dev/null 2>&1 &

# Boot-time band re-apply: wait for the server + radio, then apply the
# persisted band preference so it survives reboots. Best-effort — any failure
# is logged and skipped; the UI can still apply the config later. The log
# lives in the module dir (DE-accessible at boot) rather than /sdcard, which
# is not reliably writable before the user unlocks.

# Bounded wait for the HTTP server to answer /api/health (~60s max).
wait_server() {
  i=0
  while [ "$i" -lt 30 ]; do
    if "$PYTHON" -c 'import sys, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)' "http://127.0.0.1:$PORT/api/health?action=health"; then
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  return 1
}

# Bounded wait for the radio to leave POWER_OFF (~180s max, 3s between polls).
# IN_SERVICE and OUT_OF_SERVICE both count as ready — the preference is
# applied even when out of coverage.
wait_radio() {
  i=0
  while [ "$i" -lt 60 ]; do
    if "$PYTHON" -c 'import sys, json, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
        state = json.load(r).get("service_state")
        sys.exit(0 if r.status == 200 and state != "POWER_OFF" else 1)
except Exception:
    sys.exit(1)' "http://127.0.0.1:$PORT/api/registration?action=registration"; then
      return 0
    fi
    i=$((i + 1))
    sleep 3
  done
  return 1
}

if wait_server; then
  if wait_radio; then
    resp=$("$PYTHON" -c 'import sys, urllib.request
try:
    req = urllib.request.Request(sys.argv[1], method="POST")
    with urllib.request.urlopen(req, timeout=5) as r:
        sys.stdout.write(r.read().decode("utf-8", "replace"))
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)' "http://127.0.0.1:$PORT/api/boot-apply?action=boot-apply")
    echo "$(date) bandctl: boot-apply -> $resp" >> "$LOG"
  else
    echo "$(date) bandctl: boot-apply skipped (radio not ready after 180s)" >> "$LOG"
  fi
else
  echo "$(date) bandctl: boot-apply skipped (server not ready after 60s)" >> "$LOG"
fi

echo "$(date) bandctl: server started on port $PORT" >> "$LOG"
# Boot apply is best-effort: never fail the boot-time script over it.
exit 0
