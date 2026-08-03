#!/system/bin/sh
# Band Controller v1.0 — KernelSU install-time hook.
# Creates the config dir with a default bands.json, fixes file permissions,
# and prints an install banner (KernelSU shows this in the install log).

MODDIR=${MODDIR:-${0%/*}}
CONFIG_DIR="$MODDIR/config"
CONFIG_FILE="$CONFIG_DIR/bands.json"

# Default Rogers band config. Community-validated for Canadian carriers:
# LTE bands 6, 7 and 66 (and NR 66) are intentionally DISABLED — they are
# simply omitted from the enabled lists below.
DEFAULT_BANDS='{
  "lte": ["1", "2", "3", "4", "5", "8", "12", "17", "20", "28", "38", "40", "41"],
  "nr": ["1", "3", "5", "8", "20", "28", "38", "41", "77", "78"]
}'

echo "=========================================="
echo "  Band Controller v1.0"
echo "  Standalone LTE/NR band control + modem"
echo "  diagnostics web UI (no NSG required)"
echo "=========================================="

# Create the config directory
mkdir -p "$CONFIG_DIR" || {
  echo "[bandctl] ERROR: could not create $CONFIG_DIR"
  exit 1
}

# Seed default band config only if none exists (respect user overrides)
if [ ! -f "$CONFIG_FILE" ]; then
  echo "$DEFAULT_BANDS" > "$CONFIG_FILE"
  echo "[bandctl] Wrote default band config: $CONFIG_FILE"
else
  echo "[bandctl] Existing config found, keeping: $CONFIG_FILE"
fi

# Fix permissions: 755 dirs, 644 files, scripts executable
chmod 755 "$MODDIR" "$CONFIG_DIR" "$MODDIR/web" "$MODDIR/diag" 2>/dev/null
chmod 644 "$MODDIR/module.prop" "$MODDIR/web/index.html" 2>/dev/null
chmod 644 "$MODDIR/diag/__init__.py" "$MODDIR/diag/protocol.py" "$MODDIR/diag/diag_client.py" 2>/dev/null
chmod 755 "$MODDIR/customize.sh" "$MODDIR/service.sh" "$MODDIR/web/server.py" 2>/dev/null

echo "[bandctl] Install complete."
echo "[bandctl] After boot: open http://localhost:8080"
echo "=========================================="

exit 0
