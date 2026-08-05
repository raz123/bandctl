#!/system/bin/sh
# Band Controller v2.1 — KernelSU install-time hook.
# Creates the config dir with a carrier-aware default bands.json, fixes
# file permissions, and prints an install banner (KernelSU shows this in
# the install log).

MODDIR=${MODDIR:-${0%/*}}
CONFIG_DIR="$MODDIR/config"
CONFIG_FILE="$CONFIG_DIR/bands.json"

# Carrier-aware default band config. Bands 7 and 66 are omitted from the
# Rogers default (SM8250 66<->7 handover crash fix, community-validated);
# the curated list applies to Rogers (MCC/MNC 302720) only. Any other
# carrier gets no seeded file: the server's carrier-aware unrestricted
# all-bands defaults apply, and bands.json is created on first Save &
# Apply via the server's mirror logic.
ROGERS_MCCMNC="302720"
ROGERS_BANDS='{
  "lte": ["1", "2", "3", "4", "5", "8", "12", "17", "20", "28", "38", "40", "41"],
  "nr": ["1", "3", "5", "8", "20", "28", "38", "41", "77", "78"]
}'

echo "=========================================="
echo "  Band Controller v2.1"
echo "  Standalone LTE/NR band control + modem"
echo "  diagnostics web UI (no NSG required)"
echo "=========================================="

# Create the config directory
mkdir -p "$CONFIG_DIR" || {
  echo "[bandctl] ERROR: could not create $CONFIG_DIR"
  exit 1
}

# Seed a Rogers default band config only if none exists (respect user
# overrides). Other carriers get no file: the server's carrier-aware
# all-bands fallback applies until the first Save & Apply mirrors one.
if [ ! -f "$CONFIG_FILE" ]; then
  # gsm.operator.numeric is comma-joined per SIM slot ("302720," or
  # "302720,302220") — match the Rogers token, not the whole string.
  MCCMNC=$(getprop gsm.operator.numeric)
  case ",${MCCMNC}," in
    *,302720,*)
      echo "$ROGERS_BANDS" > "$CONFIG_FILE"
      echo "[bandctl] Wrote Rogers default band config: $CONFIG_FILE"
      ;;
    *)
      echo "[bandctl] Non-Rogers carrier: no default seeded (server all-bands fallback applies)"
      ;;
  esac
else
  echo "[bandctl] Existing config found, keeping: $CONFIG_FILE"
fi

# Fix permissions: 755 dirs, 644 files, scripts executable
chmod 755 "$MODDIR" "$CONFIG_DIR" "$MODDIR/web" "$MODDIR/diag" "$MODDIR/qmi" 2>/dev/null
chmod 644 "$MODDIR/module.prop" "$MODDIR/web/index.html" 2>/dev/null
chmod 644 "$MODDIR/diag/__init__.py" "$MODDIR/diag/protocol.py" "$MODDIR/diag/diag_client.py" 2>/dev/null
chmod 755 "$MODDIR/customize.sh" "$MODDIR/service.sh" "$MODDIR/web/server.py" 2>/dev/null
chmod 755 "$MODDIR/qmi/qmi_band" 2>/dev/null

# Bundled Python runtime: the interpreter must be executable. No recursive
# chmod over the ~21MB stdlib — zip entries carry correct 755/644 modes and
# KernelSU preserves them; only the entry point needs re-asserting here.
chmod 755 "$MODDIR/python" "$MODDIR/python/bin" "$MODDIR/python/bin/python3.14" 2>/dev/null
chmod 755 "$MODDIR/python/usr/lib" 2>/dev/null

echo "[bandctl] Install complete."
echo "[bandctl] After boot: open http://localhost:8080"
echo "=========================================="

exit 0
