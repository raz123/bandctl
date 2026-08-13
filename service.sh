#!/system/bin/sh
# Band Controller — web UI for LTE/NR band locking
# Starts Python HTTP server on localhost:8080 after boot
# Re-applies the persisted band preference at boot: waits for the server +
# radio, then POSTs /api/boot-apply (best-effort — the UI can apply later)

MODDIR=${0%/*}

# MODDIR self-heal (A-171): invoked as `sh service.sh` from inside the
# module dir, ${0%/*} yields "service.sh" and every state write below
# (config/, bandctl.log, chmod paths, nohup server path) would resolve
# under a stray CWD-relative directory. Resolve the real module root (the
# dir containing module.prop), like customize.sh does.
if [ ! -f "$MODDIR/module.prop" ]; then
  CAND="$PWD"
  while [ -n "$CAND" ] && [ "$CAND" != "/" ] && [ ! -f "$CAND/module.prop" ]; do
    CAND=${CAND%/*}
  done
  if [ -f "$CAND/module.prop" ]; then
    MODDIR="$CAND"
  else
    echo "bandctl: module root not found (MODDIR=$MODDIR); aborting" >&2
    exit 1
  fi
fi

WEB_DIR="$MODDIR/web"
PORT=8080
LOG="$MODDIR/config/bandctl.log"
SERVER_LOG="$MODDIR/config/server.log"

# Ensure config dir exists (customize.sh may be skipped on manual installs)
mkdir -p "$MODDIR/config"

# Diagnostic-log retention (A-96): rotate each log once at 256 KB, keep a
# single .old backup. The UI restart action re-runs this script, so the
# logs stay bounded under repeated testing.
for L in "$LOG" "$SERVER_LOG"; do
  if [ -f "$L" ]; then
    SIZE=$(wc -c < "$L" 2>/dev/null || echo 0)
    if [ "$SIZE" -gt 262144 ]; then
      mv -f "$L" "$L.old" 2>/dev/null
    fi
  fi
done

# Serialize concurrent service runs (A-183 boot/restart race): the boot
# script holds this lock for up to a few minutes (server + radio waits),
# so a manual restart in that window must not kill the fresh server or
# double-apply the band config. A stale lock from a crashed run is
# reclaimed once its recorded PID is no longer alive.
LOCKDIR="$MODDIR/config/.service.lock"
if ! mkdir "$LOCKDIR" 2>/dev/null; then
  OLDPID=$(cat "$LOCKDIR/pid" 2>/dev/null)
  if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
    echo "$(date) bandctl: another service instance running (pid $OLDPID); skipping" >> "$LOG"
    exit 0
  fi
  rm -rf "$LOCKDIR"
  mkdir "$LOCKDIR" 2>/dev/null || {
    echo "$(date) bandctl: could not acquire service lock" >> "$LOG"
    exit 1
  }
fi
echo $$ > "$LOCKDIR/pid"
trap 'rm -rf "$LOCKDIR" 2>/dev/null' EXIT INT TERM

# PRIMARY: Python runtime bundled inside the module (module dir is DE-accessible
# at boot under KernelSU, so no user unlock / Termux needed for auto-start).
BUNDLED_PYTHON="$MODDIR/python/bin/python3.14"
BUNDLED_LD="$MODDIR/python/usr/lib"

# Fallbacks (bounded, cheap): Termux python, then portable pyroot runtime.
TERMUX_PYTHON="/data/data/com.termux/files/usr/bin/python3"
PYROOT_PYTHON="/data/local/tmp/pyroot/usr/bin/python3.14"
PYROOT_LD="/data/local/tmp/pyroot/usr/lib"

# Wait for boot to complete, bounded (A-48): up to ~300s (150 x 2s), then
# abort loudly instead of hanging forever with no failure signal.
i=0
while [ "$(getprop sys.boot_completed)" != "1" ]; do
  i=$((i + 1))
  if [ "$i" -ge 150 ]; then
    echo "$(date) bandctl: sys.boot_completed never set after 300s; aborting" >> "$LOG"
    exit 1
  fi
  sleep 2
done

# Pick a Python. Bundled runtime is first — no wait needed, it is present at
# boot. Fallbacks are single bounded checks (no 60s Termux wait anymore).
# -x, not -f (A-179): a present-but-inexecutable launcher must not be
# "selected" and then fail silently in nohup.
PYTHON=""
if [ -x "$BUNDLED_PYTHON" ]; then
  PYTHON="$BUNDLED_PYTHON"
  export LD_LIBRARY_PATH="$BUNDLED_LD"
  echo "$(date) bandctl: using bundled python" >> "$LOG"
elif [ -x "$TERMUX_PYTHON" ]; then
  PYTHON="$TERMUX_PYTHON"
  echo "$(date) bandctl: bundled python missing, using Termux python" >> "$LOG"
elif [ -x "$PYROOT_PYTHON" ]; then
  PYTHON="$PYROOT_PYTHON"
  export LD_LIBRARY_PATH="$PYROOT_LD"
  echo "$(date) bandctl: bundled python missing, using pyroot fallback" >> "$LOG"
fi

if [ -z "$PYTHON" ]; then
  echo "$(date) bandctl: Python not found, aborting" >> "$LOG"
  exit 1
fi

# Kill any existing server — scoped to THIS module's server only
# (A-022/A-183): a bare `pgrep -f "server.py"` regex-matches the full
# command line, so it SIGTERMs unrelated processes whose cmdline merely
# contains that string (webserver.py, another module's python server, a
# dev process). Matching the absolute module server path keeps the kill
# inside the module.
for pid in $(pgrep -f "$WEB_DIR/server.py" 2>/dev/null); do
  kill "$pid" 2>/dev/null
done
sleep 1

# Self-heal exec bits: KernelSU's extractor may not preserve Unix modes and
# the metainstall pass of customize.sh can run with a mangled MODDIR, leaving
# qmi_band without +x. MODDIR is reliable here (service.sh runs by absolute
# path at boot), so re-assert 755 before anything executes the binaries.
chmod 755 "$MODDIR/qmi/qmi_band" "$MODDIR/web/server.py" "$MODDIR/customize.sh" "$MODDIR/service.sh" "$MODDIR/python/bin/python3.14" 2>/dev/null

# Start Python HTTP server. Output is captured to server.log (A-038): a
# crash, bind failure (port occupied), or startup traceback is preserved
# instead of being discarded to /dev/null. server.log is rotation-capped
# above like bandctl.log.
nohup "$PYTHON" "$WEB_DIR/server.py" >> "$SERVER_LOG" 2>&1 &

# Boot-time band re-apply: wait for the server + radio, then apply the
# persisted band preference so it survives reboots. Best-effort — any failure
# is logged and skipped; the UI can still apply the config later. The log
# lives in the module dir (DE-accessible at boot) rather than /sdcard, which
# is not reliably writable before the user unlocks.

# Bounded wait for the HTTP server to answer /api/health (~60s max:
# 8 attempts x (5s urlopen timeout + 2s sleep) = 56s worst case).
# A-011: a 200 with {"status":"error"} is NOT ready — the health JSON must
# report status "ok" or "degraded" (a live transport). A-037: the per-attempt
# timeout is bounded so a stuck endpoint cannot stretch the wait past the
# documented limit.
wait_server() {
  i=0
  while [ "$i" -lt 8 ]; do
    if "$PYTHON" -c 'import sys, json, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
        status = json.load(r).get("status")
        sys.exit(0 if r.status == 200 and status in ("ok", "degraded") else 1)
except Exception:
    sys.exit(1)' "http://127.0.0.1:$PORT/api/health?action=health"; then
      return 0
    fi
    i=$((i + 1))
    sleep 2
  done
  return 1
}

# Bounded wait for the radio to leave POWER_OFF (~180s max: 22 attempts x
# (5s timeout + 3s sleep) = 176s worst case). IN_SERVICE and OUT_OF_SERVICE
# both count as ready — the preference is applied even when out of coverage.
# A-011: an error payload (e.g. {"error": ...}) has no service_state (None)
# and must NOT count as ready; only a real state other than POWER_OFF does.
wait_radio() {
  i=0
  while [ "$i" -lt 22 ]; do
    if "$PYTHON" -c 'import sys, json, urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=5) as r:
        state = json.load(r).get("service_state")
        sys.exit(0 if r.status == 200 and state is not None and state != "POWER_OFF" else 1)
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
  echo "$(date) bandctl: server ready on port $PORT" >> "$LOG"
  if wait_radio; then
    # A-175: the boot apply is synchronous server-side and can legitimately
    # outlast a short client timeout (QMI discovery alone can take ~98s), so
    # the POST gets enough time to cover the apply bound. A timeout or HTTP
    # failure is logged with its rc — never as a blank "boot-apply -> " line.
    resp=$("$PYTHON" -c 'import sys, urllib.request
try:
    req = urllib.request.Request(sys.argv[1], method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        sys.stdout.write(r.read().decode("utf-8", "replace"))
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)' "http://127.0.0.1:$PORT/api/boot-apply?action=boot-apply" 2>/dev/null)
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "$(date) bandctl: boot-apply -> $resp" >> "$LOG"
    else
      echo "$(date) bandctl: boot-apply POST failed (rc=$rc, resp=$resp)" >> "$LOG"
    fi
  else
    echo "$(date) bandctl: boot-apply skipped (radio not ready)" >> "$LOG"
  fi
else
  # A-038: startup failure must be loud — the server died, never became
  # ready, or the port is occupied. The traceback is captured in server.log;
  # exit nonzero so a service supervisor can act. Previously this path was
  # logged as "boot-apply skipped" followed by an unconditional "server
  # started" and exit 0.
  echo "$(date) bandctl: server failed to become ready (see $SERVER_LOG); giving up" >> "$LOG"
  exit 1
fi

# Boot apply is best-effort: never fail the boot-time script over it.
exit 0
