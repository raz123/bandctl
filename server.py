#!/usr/bin/env python3
"""Band Controller HTTP server with QMI-first transport.

No NSG dependency - band apply goes over QRTR QMI (the bundled static
qmi_band client), with /dev/diag and the config file as fallbacks.

HTTP API (all responses are JSON, served from the phone's web root):

  GET  /api/read?action=read
       -> {"ok": true, "lte": ["1", ...], "nr": ["77", ...],
           "source": "qmi" | "diag" | "config_file" | "default",
           "rev": int}
       LTE/NR band configuration. Tries QMI (QRTR) first, then diag, then
       the fallback config file, then a carrier-aware default list. A
       failed exchange (QMI nonzero exit / non-success result TLV, empty
       diag read, corrupt or invalid persisted file) is {"ok": false,
       "error": ...} — never a fake authoritative read. "rev" is the
       persisted-intent revision; POST /api/write may echo it to reject
       stale writes (see below).

  GET  /api/defaults?action=defaults
       -> {"carrier": "rogers" | "other", "mccmnc": "302720" | null,
           "operator": "ROGERS" | null, "lte": [...], "nr": [...]}
       Carrier-aware default band lists. Carrier is detected from
       `getprop gsm.operator.numeric` (Rogers and Fido share 302720);
       "rogers" returns the community-validated curated whitelist, any
       other carrier the unrestricted all-bands list. Never 500s.

  POST /api/write?action=write   body {"lte": [...], "nr": [...],
                                        "rev": int?}
       -> {"ok": bool, "source": ..., "rev": int, "error": ...?}
       Writes the band configuration to the modem via QMI (QRTR) first,
       falling back to diag, and mirrors it to the fallback config file.
       Both transports are verified by read-back, so ok:true means the
       requested set was actually applied and persisted; a failed apply
       is not mirrored (boot re-apply never retries a rejected set) and a
       persistence failure is reported. Writes are serialized; an
       optional "rev" from a prior /api/read causes a conflict error when
       another client changed the config in the meantime.

  POST /api/boot-apply?action=boot-apply  (no body)
       -> {"ok": true, "skipped": true} | {"ok": true,
           "source": "qmi" | "diag", "lte": [...], "nr": [...]}
           | {"ok": false, "error": ...}
       Re-applies the persisted config file (config/bands.json) through
       the same QMI->diag chain. Missing/unreadable config or empty
       lte+nr is a skip; the config file is never rewritten. Never 500s.

  GET  /api/signal?action=signal
       -> {"rsrp_dbm": int|null, "rsrq_db": int|null, "level": int|null,
           "tech": "LTE" | "NR" | ..., "timestamp": epoch_ms}
       Current signal strength, parsed from `dumpsys telephony.registry`
       (the mSignalStrength block; object format on modern builds, flat
       list on legacy ones). Unparseable metrics are null; the whole
       request returns {"error": str} only when dumpsys itself fails or
       no signal data is present.

  GET  /api/registration?action=registration
       -> {"service_state": "IN_SERVICE" | ..., "data_state": ...,
           "network_type": ..., "operator": ..., "roaming": bool,
           "timestamp": epoch_ms}
       Registration state, parsed from `dumpsys telephony.registry`
       (the mServiceState block; object format on modern builds, legacy
       flat format handled as best-effort). Missing fields are null.

  GET  /api/health?action=health
       -> {"status": "ok" | "degraded" | "error",
           "transport": "qmi" | "diag", "diag_device": ...,
           "lte_bands": n, "nr_bands": n, "md_session_owner": pid|null,
           "error": ...?}
       Transport liveness via a QMI band read (preferred) with a diag band
       read as fallback, plus an optional ioctl 41 (DIAG_IOCTL_QUERY_MD_PID)
       probe for the modem MD session owner on the diag path.
       Never 500s: any failure is reported as a status field.

  POST /api/modem-reset?action=modem-reset
       -> {"ok": bool, "error": ...?}
       Soft modem reset. Preferred mechanism is `cmd phone radio power`
       off/on (3s apart) when `cmd phone help` lists it; otherwise falls
       back to an airplane-mode toggle via `cmd connectivity
       airplane-mode` (enable, 3s, disable). The radio's power state is
       verified after the toggle; if neither mechanism is available or the
       radio does not power off, returns ok:false - never fakes success.

  GET  /api/band-camping?action=band-camping[&limit=N]
       -> {"ok": true, "enabled": bool, "samples": [{"timestamp": epoch_ms,
           "earfcn": int, "band": int|null}, ...], "log": str}
       Last N (default 50) serving-cell EARFCN/band samples recorded by
       the background band-camping sampler (findings 5c); "enabled" is
       the live sampler toggle (A-120/A-201). When the log has no
       samples yet, "samples" is an empty list - the sampler only writes
       a line when an LTE cell identity is present.

  POST /api/band-camping?action=band-camping  body {"enabled": bool}
       -> {"ok": true, "enabled": bool}
       Enables/disables the background serving-cell sampler (persisted to
       settings.json, picked up live — no restart needed).

  POST /api/export?action=export  body {"lte": [...], "nr": [...]}
       -> {"ok": bool, "path": str, "error": ...?}
       Writes the submitted band config to a timestamped JSON file in
       the module config dir. WebView-compatible export: the Manager
       WebView drops blob downloads, so the server delivers the file
       on-device and the UI toasts the path.

  GET  /api/settings?action=settings
       -> {"ok": true, "lan_enabled": bool, "token_required": bool}
       Current LAN/token state from config/settings.json. Exempt from
       the LAN auth gate (returns no token material) so a fresh laptop
       page can learn whether a token is required before storing one.

  POST /api/settings?action=settings  body {"lan_enabled": bool,
                                            "regenerate": bool}
       -> {"ok": true, "lan_enabled": bool, "token_required": bool,
           "token": str|null}
       Enables/disables LAN access (server bind). Enabling with no token
       set, or passing regenerate=true, creates a fresh bearer token
       (secrets.token_urlsafe(24)); the token is returned only when it
       was created in THIS call, otherwise null. Settings are persisted
       atomically to config/settings.json.

  POST /api/restart?action=restart
       -> {"ok": true}
       Schedules a detached module-service restart ~1s later (service.sh
       kills the old server and starts a fresh one) from a background
       thread so the response is delivered first.

  Anything else -> {"error": "unknown action"}
"""
import http.server
import fcntl
import glob
import hmac
import ipaddress
import itertools
import json
import os
import re
import secrets
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

# Module root: server.py lives in MODDIR/web/, so MODDIR is the module
# directory (KernelSU mounts it at /data/adb/modules/bandctl on-device;
# resolving from __file__ keeps dev/test checkouts working identically).
MODDIR = Path(__file__).resolve().parent.parent

# Add diag module to path
DIAG_DIR = MODDIR / "diag"
sys.path.insert(0, str(DIAG_DIR))

from diag_client import DiagClient, read_bands, write_bands
from protocol import band_bitmask_to_list, band_list_to_bitmask

PORT = 8080
WEB_DIR = MODDIR / "web"
DIAG_DEVICE = "/dev/diag"

# Settings file (v2.2 LAN-access feature): serialized bind + bearer token.
# Absent/invalid file -> DEFAULT_SETTINGS. The bind is the listen address
# (127.0.0.1 = local-only, 0.0.0.0 = LAN); the token gates /api/* from
# non-loopback clients while LAN is enabled.
SETTINGS_FILE = MODDIR / "config" / "settings.json"
# band_camping gates the 5s serving-cell sampler (A-120/A-201), the same
# way drop_log gates the drop watchdog. Default True preserves the shipped
# Diagnostics behavior (the UI polls and renders camping data); the toggle
# makes the always-on daemon user-controllable.
DEFAULT_SETTINGS = {"bind": "127.0.0.1", "token": None, "drop_log": False,
                    "band_camping": True}

# Serialize config/settings writes so concurrent saves cannot interleave
# (each save is temp-file + os.replace, atomic per write). RLock so a
# mutation+save critical section can hold the lock while _save_settings
# re-acquires it inside _atomic_write_json (A-116).
_CONFIG_WRITE_LOCK = threading.RLock()

# Serialize /api/modem-reset (A-51): the power off/on transitions must not
# interleave across concurrent requests.
_MODEM_RESET_LOCK = threading.Lock()

# Serialize the whole band-apply transaction (QMI SET + read-back verify,
# diag fallback, config-file mirror) so concurrent clients cannot
# interleave band writes on the modem (A-45). _CONFIG_WRITE_LOCK only
# guards the JSON replace; this guards the transaction end to end.
_BAND_WRITE_LOCK = threading.Lock()

# Revision of the persisted band intent (config/bands.json). Incremented
# under _BAND_WRITE_LOCK on every successful write; /api/read responses
# carry it and /api/write may echo it to fail when another client changed
# the config meanwhile (A-97).
_CONFIG_REV = 0

# Request bodies are clamped to 1 MiB before parsing (M6 guard).
MAX_BODY_BYTES = 1 * 1024 * 1024


def _load_settings():
    """Load config/settings.json, falling back to defaults.

    Absent file, malformed JSON, or invalid fields all yield
    {"bind": "127.0.0.1", "token": None} — never raises."""
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return settings
        if data.get("bind") in ("127.0.0.1", "0.0.0.0"):
            settings["bind"] = data["bind"]
        token = data.get("token")
        settings["token"] = token if isinstance(token, str) and token else None
        if isinstance(data.get("drop_log"), bool):
            settings["drop_log"] = data["drop_log"]
        if isinstance(data.get("band_camping"), bool):
            settings["band_camping"] = data["band_camping"]
        # A-105/A-218: never accept a LAN bind without a usable token. That
        # state remote-locks every non-loopback client (every request 401s
        # while the server is reachable on the LAN, and the recovery POST is
        # itself behind the gate). Downgrade to loopback-only so the server
        # stays locally administrable; update_settings never produces this
        # combination because the token is minted before the bind is flipped.
        if settings["bind"] == "0.0.0.0" and not settings["token"]:
            settings["bind"] = "127.0.0.1"
    except (OSError, ValueError):
        pass
    return settings


# In-memory settings snapshot, loaded once at startup. The restart endpoint
# re-runs service.sh so a bind/token change takes effect on the fresh process.
SETTINGS = _load_settings()

# The address the HTTP socket actually listens on, set in __main__ after the
# server binds. Auth decisions use THIS rather than SETTINGS["bind"] (A-049):
# update_settings flips SETTINGS["bind"] immediately while the socket stays
# put until the restart, so gating on the settings value would let a remote
# client pass unauthenticated for the whole window after "disable LAN".
# None outside __main__ (tests/imports) -> fall back to SETTINGS["bind"].
_EFFECTIVE_BIND = None

# Serialize POST /api/settings (and the drop-log toggle, which mutates the
# same SETTINGS dict) so two racing requests cannot interleave their
# read-modify-write of the shared snapshot (A-041).
_SETTINGS_UPDATE_LOCK = threading.Lock()

# One scheduled restart at a time (A-041): repeated taps must not stack
# detached service.sh runs that each kill and replace the server.
_RESTART_LOCK = threading.Lock()
_RESTART_PENDING = False


def _ensure_private_dir(path):
    """Create `path` root-private (0o700), re-asserting the mode on an
    existing directory.

    Hardens the module config tree (A-180): service.sh's bare `mkdir -p`
    inherits the boot umask (000 -> world-writable 777) and nothing else
    chmods it, which lets any unprivileged app unlink/replace files in it.
    The server runs as root and is the only legit reader of config/ and
    config/drop_log/, so 0o700 is safe and strictly tighter than the
    audit's 0755 recommendation."""
    path = Path(path)
    os.makedirs(str(path), mode=0o700, exist_ok=True)
    try:
        os.chmod(str(path), 0o700)
    except OSError:
        pass


def _atomic_write_json(path, data):
    """Persist `data` as JSON to `path` atomically (M7).

    Writes to a temp file in the same directory, fsyncs, then
    os.replace()s it over the target so readers never observe a
    partially-written file. The temp file is opened with O_NOFOLLOW|O_EXCL
    (A-180/A-211) so a symlink or pre-created hard link planted at the temp
    name cannot redirect or truncate the write; a leftover temp from a
    crashed save is cleared once and retried. The replace itself swaps the
    target name outright (a pre-existing symlink at the target is replaced,
    not followed). The directory is created root-private (A-180). Writes
    are serialized by _CONFIG_WRITE_LOCK. Raises on failure; callers turn
    exceptions into JSON error responses."""
    path = Path(path)
    _ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    flags |= os.O_EXCL
    tmp = os.path.join(str(path.parent),
                       ".{}.{}.tmp".format(path.name, os.getpid()))
    with _CONFIG_WRITE_LOCK:
        fd = None
        for attempt in (0, 1):
            try:
                fd = os.open(tmp, flags, 0o600)
                break
            except FileExistsError:
                if attempt:
                    raise
                # Stale temp left by a crashed save: clear and retry.
                os.unlink(tmp)
        if fd is None:
            raise OSError("cannot create temp file {}".format(tmp))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(path))
            # Crash durability (A-141): fsync the parent directory so the
            # rename itself survives a power loss — without it the file can
            # revert or vanish despite the API reporting a save. Filesystems
            # that do not support directory fsync raise; ignore that.
            try:
                dfd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _save_settings(settings=None):
    """Atomically persist the in-memory SETTINGS snapshot (or a supplied
    copy of it — persist-first callers pass the proposed new state)."""
    _atomic_write_json(SETTINGS_FILE,
                       settings if settings is not None else SETTINGS)


# QMI band-apply client (QRTR transport), resolved relative to the module
# dir like DIAG_DIR; the module zip ships the static binary at qmi/qmi_band.
# A-198: the old fallback silently exec'd /data/local/tmp/qmi_band (an
# adb-push scratch dir) as root. Never substitute non-module binaries —
# when the bundled artifact is missing, QMI is simply unavailable and
# _run_qmi reports it; __main__ logs the condition loudly.
QMI_BIN = Path(__file__).parent.parent / "qmi" / "qmi_band"
if not QMI_BIN.exists():
    QMI_BIN = None
# QMI subprocess timeouts. The helper re-discovers the NAS endpoint on
# every invocation: a fixed ~3s QRTR enumeration plus per-probe waits
# before the first response (A-194). These timeouts must sit above that
# fixed discovery+probe budget so a slow-but-working modem is not killed
# by the caller (A-64); the C-side worst-case sweep can still exceed even
# these, which is a discovery-cost fix inside qmi_band.c, not here.
QMI_GET_TIMEOUT = 15
QMI_SET_TIMEOUT = 20

# Fallback config file for persistence
CONFIG_FILE = MODDIR / "config" / "bands.json"

# Persistent data dir OUTSIDE the module dir (A-173): a KernelSU module
# update replaces MODDIR wholesale, wiping config/drop_log and any
# exports. /data/local/tmp/bandctl/ survives updates on-device; dev/test
# checkouts without that path fall back to the module config dir.
def _persistent_data_dir():
    alt = Path("/data/local/tmp/bandctl")
    try:
        if alt.parent.is_dir():
            alt.mkdir(parents=True, exist_ok=True)
            return alt
    except OSError:
        pass
    return MODDIR / "config"

PERSIST_DIR = _persistent_data_dir()

# Export dir for WebView-compatible config export (the Manager WebView
# drops blob downloads, so export writes a timestamped file on-device).
# Only the newest EXPORT_KEEP export files are retained (A-082: repeated
# exports used to accumulate in the module config dir without bound).
EXPORT_DIR = MODDIR / "config"
EXPORT_KEEP = 10

# Band-camping sampler (findings 5c): append `timestamp,earfcn,band` CSV
# lines every BAND_CAMPING_INTERVAL seconds so a band force can be
# validated offline (does the modem ever camp on a banned band?). The log
# is trimmed to BAND_CAMPING_MAX_LINES on append (A-028: the old sampler
# grew the file forever and every /api/band-camping read re-read it in
# full) and gated on the SETTINGS["band_camping"] toggle (A-120/A-201).
BAND_CAMPING_LOG = MODDIR / "config" / "band_camping.log"
BAND_CAMPING_INTERVAL = 5
BAND_CAMPING_MAX_LINES = 2000
# Wall-clock ms can step backwards (NTP/carrier time sync), which would
# make the CSV / last-N chart show time going backwards (A-154). Keep the
# written timestamp strictly monotonic by clamping to last+1.
_BAND_CAMPING_LAST_MS = [0]

# v2.5 drop logger: when enabled (Settings > Debug > Drop logging), a
# daemon watchdog stamps every radio drop (voice or data registration
# out of service, radio powered off, emergency-only, or an IWLAN/VoWiFi
# transport handover — A-085/A-108/A-176) with correlation context —
# registration, call state, wifi link/AP, data counters, and a redacted
# radio-buffer summary (A-219) — and records the recovery duration.
# Snapshots live in PERSIST_DIR/drop_log (survives module updates and
# reboot; the logger itself survives reboot because the server starts at
# boot and re-reads the persisted setting).
DROP_LOG_DIR = PERSIST_DIR / "drop_log"
DROP_POLL_INTERVAL = 5  # seconds between registration polls (A-112: 10s
                        # samples miss sub-interval blips entirely; 5s
                        # shrinks the blind window — true sub-sample
                        # outages remain invisible to any poller)
DROP_SNAP_GAP = 60  # min seconds between snapshots of one episode
DROP_RECOVERY_CONFIRM = 2  # consecutive non-drop polls before RECOVERED
                           # is stamped (A-199: a single IN_SERVICE blip
                           # between drops must not close the episode)
DROP_LOG_MAX_FILES = 40  # retention cap for episode files (A-043/A-207)
_ACTIVE_EPISODE_FILE = ".active_episode"  # A-188: persisted episode state
_EPISODE_COUNTER = itertools.count()      # per-process unique episode id

# A-196: bounded auto self-heal. After AUTO_RECOVER_GRACE of sustained
# drop, invoke the modem reset once per episode, rate-limited to
# AUTO_RECOVER_MAX_PER_HOUR (monotonic window). The reset itself must
# observe the radio return to service before reporting success (A-197).
AUTO_RECOVER_GRACE = 120
AUTO_RECOVER_MAX_PER_HOUR = 2
AUTO_RECOVER_WINDOW = 3600
_AUTO_RECOVER_TIMES = []
_AUTO_RECOVER_LOCK = threading.Lock()

# Registration states that mean "the radio is down" (A-085: data-state
# counts too; A-108: SERVICE_EMERGENCY is a real outage).
_DROP_STATES = ("POWER_OFF", "OUT_OF_SERVICE", "EMERGENCY_ONLY",
                "SERVICE_EMERGENCY")


class _DropWatch(object):
    """Drop-state tracking shared with the watchdog thread: episode
    bookkeeping (in-drop flag, start time, snapshot path) is kept in one
    place so the API can report the current state."""

    def __init__(self):
        self.in_drop = False
        self.drop_start = None          # time.monotonic() at detection (A-154)
        self.last_snap = None           # time.monotonic() of last snapshot write
        self.episode_file = None
        self.episode_id = None          # per-process unique episode id (A-128)
        self.recovery_polls = 0         # consecutive non-drop polls (A-199)
        self.auto_recover_attempted = False  # once per episode (A-196)
        self.lock = threading.Lock()


_DROP_WATCH = _DropWatch()


def _read_sys(path):
    """Read a small sysfs file, or None on failure."""
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except Exception:
        return None


def _wifi_status():
    """Short wifi link summary: cmd wifi status head + wlan0 inet line."""
    try:
        out = _run_cmd(["/system/bin/cmd", "wifi", "status"], timeout=5)
        first = [ln.strip() for ln in out.splitlines() if ln.strip()][:3]
        ip = _run_cmd(["/system/bin/ip", "-4", "addr", "show", "wlan0"],
                      timeout=5)
        wlan = [ln.strip() for ln in ip.splitlines() if "inet" in ln][:1]
        return " | ".join(first + wlan) or "unknown"
    except Exception as e:
        return "error: {}".format(e)


def _net_counters():
    """rx/tx byte counters for the cellular + wifi interfaces."""
    out = []
    for iface in ("rmnet_data0", "rmnet0", "wwan0", "wlan0"):
        rx = _read_sys("/sys/class/net/{}/statistics/rx_bytes".format(iface))
        tx = _read_sys("/sys/class/net/{}/statistics/tx_bytes".format(iface))
        if rx is not None:
            out.append("{} rx={} tx={}".format(iface, rx, tx))
    return "; ".join(out) or "none"


def _call_state():
    """mCallState from telephony.registry (0 idle, 1 ringing, 2 off-hook)."""
    try:
        text = _run_dumpsys("telephony.registry")
        for line in text.splitlines():
            if line.lstrip().startswith("mCallState="):
                return line.split("mCallState=", 1)[1].strip()
    except Exception:
        pass
    return None


def _drop_state():
    """Parsed registration dict, or None when dumpsys/parse fails."""
    try:
        return _parse_registration(_run_dumpsys("telephony.registry"))
    except Exception:
        return None


def _drop_snapshot_text(reg):
    """Correlation context for a drop: registration, call state, wifi,
    counters, and a REDACTED radio-buffer summary.

    The PHONE0 radio-buffer lines carry raw RIL traffic (IMSI, dialed
    numbers, cell IDs), so the snapshot must not persist them (A-185 /
    A-219): only the line count is recorded, never the payload."""
    lines = ["registration: {}".format(json.dumps(reg))]
    call = _call_state()
    if call is not None:
        lines.append("call_state: {}".format(call))
    lines.append("wifi: {}".format(_wifi_status()))
    lines.append("counters: {}".format(_net_counters()))
    try:
        tail = _run_cmd(["/system/bin/logcat", "-b", "radio", "-d",
                         "-v", "threadtime", "-t", "300"], timeout=5)
        ph0 = [ln for ln in tail.splitlines() if "PHONE0" in ln][-40:]
        if ph0:
            lines.append("radio tail: {} PHONE0 line(s) omitted "
                         "(privacy redacted)".format(len(ph0)))
        else:
            lines.append("radio tail: (no PHONE0 lines)")
    except Exception as e:
        lines.append("radio tail unavailable: {}".format(e))
    return "\n".join(lines) + "\n"


def _reg_drop_state(reg):
    """True when the registration indicates a drop: voice or data
    registration in a failure state (A-085 data-only outages, A-108
    SERVICE_EMERGENCY), or the network transport on IWLAN/VoWiFi — the
    field-observed precursor to the collapse (A-176), visible either as
    network_type == "IWLAN" or as a WLAN/IWLAN entry inside
    mNetworkRegistrationInfos (A-208)."""
    svc = (reg or {}).get("service_state")
    data = (reg or {}).get("data_state")
    net = (reg or {}).get("network_type")
    if svc in _DROP_STATES or data in _DROP_STATES:
        return True
    if net == "IWLAN":
        return True
    transports = (reg or {}).get("transports") or []
    if any(t.get("tech") == "IWLAN" for t in transports):
        return True
    return False


def _new_episode_path(episode_id):
    """Unique per-episode path (A-128/A-214/A-215): the wall-clock stamp
    is for readability only; pid + per-process counter guarantee no
    same-second or cross-loop collision."""
    return DROP_LOG_DIR / "drop_{}_{}_{}.txt".format(
        time.strftime("%Y%m%d_%H%M%S"), os.getpid(), episode_id)


def _write_detection(w, reg):
    with open(w.episode_file, 'w') as f:
        f.write("=== DROP DETECTED {} (episode {}) ===\n".format(
            time.strftime("%Y-%m-%d %H:%M:%S"), w.episode_id))
        f.write(_drop_snapshot_text(reg))


def _append_refresh(w, reg):
    """Long-episode refresh: append to the SAME episode file so one
    outage stays one file and the recovery stamp lands on the original
    (A-059/A-187)."""
    with open(w.episode_file, 'a') as f:
        f.write("--- snapshot refresh {} (episode {}) ---\n".format(
            time.strftime("%Y-%m-%d %H:%M:%S"), w.episode_id))
        f.write(_drop_snapshot_text(reg))


def _write_recovery(w, now):
    """Stamp RECOVERED on the episode file, then clear episode state.

    The marker is written BEFORE the state is cleared (A-115): if the
    append fails, the exception propagates, the episode stays open, and
    the next poll retries — the recovery boundary is never silently
    lost. Duration comes from monotonic time (A-154)."""
    path = w.episode_file
    if path is None:
        return
    duration = int(now - w.drop_start) if w.drop_start else 0
    with open(path, 'a') as f:
        f.write("=== RECOVERED {} (duration {}s) ===\n".format(
            time.strftime("%Y-%m-%d %H:%M:%S"), duration))
    print("Drop episode recorded: {} ({}s)".format(path, duration))
    w.in_drop = False
    w.drop_start = None
    w.last_snap = None
    w.episode_file = None
    w.episode_id = None
    w.recovery_polls = 0
    _clear_active_marker()


def _close_episode_on_disable(w):
    """A-093: when drop logging is turned off mid-episode, close the
    open episode with a duration-bearing marker instead of silently
    discarding the record. Best-effort — never raises."""
    with w.lock:
        if not w.in_drop:
            return
        path = w.episode_file
        duration = int(time.monotonic() - w.drop_start) if w.drop_start else 0
        try:
            if path:
                with open(path, 'a') as f:
                    f.write("=== EPISODE CLOSED {} (duration {}s, drop "
                            "logging disabled) ===\n".format(
                                time.strftime("%Y-%m-%d %H:%M:%S"),
                                duration))
        except Exception as e:
            print("Drop episode close-on-disable failed: {}".format(e))
        w.in_drop = False
        w.drop_start = None
        w.last_snap = None
        w.episode_file = None
        w.episode_id = None
        w.recovery_polls = 0
        _clear_active_marker()


def _write_active_marker(w):
    """A-188: persist the open episode (file + wall start) so a server
    restart can reconcile it instead of orphaning the snapshot."""
    try:
        if w.episode_file is None:
            return
        data = {"file": w.episode_file.name,
                "start": time.strftime("%Y-%m-%d %H:%M:%S"),
                "pid": os.getpid()}
        _atomic_write_json(DROP_LOG_DIR / _ACTIVE_EPISODE_FILE, data)
    except Exception:
        pass


def _clear_active_marker():
    try:
        p = DROP_LOG_DIR / _ACTIVE_EPISODE_FILE
        if p.exists():
            p.unlink()
    except OSError:
        pass


def _reconcile_open_episode():
    """A-188: a previous server process may have died mid-episode (API
    restart, boot). If an active-episode marker survives, close that
    orphan file with an explicit EPISODE INTERRUPTED line so it is not
    mistaken for an un-recovered drop, then clear the marker. Never
    raises."""
    try:
        p = DROP_LOG_DIR / _ACTIVE_EPISODE_FILE
        if not p.exists():
            return
        data = json.loads(p.read_text())
        name = data.get("file")
        if name:
            path = DROP_LOG_DIR / name
            if path.exists():
                with open(path, 'a') as f:
                    f.write("=== EPISODE INTERRUPTED (server restart) "
                            "{} ===\n".format(
                                time.strftime("%Y-%m-%d %H:%M:%S")))
        p.unlink()
    except Exception as e:
        print("Drop episode reconcile failed: {}".format(e))


def _rotate_drop_log():
    """A-043/A-207: keep only the newest DROP_LOG_MAX_FILES episode
    files. The API listing cap was display-only; the files themselves
    accumulated forever. Called after a new episode file is written."""
    try:
        if not os.path.isdir(DROP_LOG_DIR):
            return
        names = sorted(n for n in os.listdir(DROP_LOG_DIR)
                       if n.startswith("drop_") and n.endswith(".txt"))
        for old in names[:-DROP_LOG_MAX_FILES]:
            try:
                os.unlink(os.path.join(DROP_LOG_DIR, old))
            except OSError:
                pass
    except Exception:
        pass


def _maybe_auto_recover(w, now):
    """A-196: bounded auto self-heal. Once an episode has lasted past
    AUTO_RECOVER_GRACE, invoke the modem reset once per episode,
    rate-limited to AUTO_RECOVER_MAX_PER_HOUR. The reset itself waits
    for the radio to return to service (A-197) before reporting success,
    and the episode only closes on an explicit recovery poll."""
    if w.auto_recover_attempted or not w.in_drop or w.drop_start is None:
        return
    if now - w.drop_start < AUTO_RECOVER_GRACE:
        return
    with _AUTO_RECOVER_LOCK:
        recent = [t for t in _AUTO_RECOVER_TIMES
                  if now - t < AUTO_RECOVER_WINDOW]
        if len(recent) >= AUTO_RECOVER_MAX_PER_HOUR:
            return
        _AUTO_RECOVER_TIMES[:] = recent
        _AUTO_RECOVER_TIMES.append(now)
    w.auto_recover_attempted = True
    threading.Thread(target=_auto_recover_worker, daemon=True).start()


def _auto_recover_worker():
    try:
        result = _modem_reset()
        ok = bool(result.get("ok")) if isinstance(result, dict) else False
        print("Auto modem recovery: {}".format("ok" if ok else result))
    except Exception as e:
        print("Auto modem recovery failed: {}".format(e))


def _drop_log_poll(w=None):
    """One watchdog poll iteration. Returns the seconds to sleep before
    the next poll.

    Separated from the daemon loop so tests can drive the watchdog
    deterministically. Never raises — failures fall through to the
    loop's guard."""
    w = w if w is not None else _DROP_WATCH
    if not SETTINGS.get("drop_log"):
        # A-093: disabling mid-episode must not discard the record.
        _close_episode_on_disable(w)
        return 5
    reg = _drop_state()
    if reg is None or reg.get("service_state") is None:
        # Telemetry failure / unparseable dump: the radio state is
        # UNKNOWN, not recovered (A-053/A-181/A-144). Skip the poll
        # without touching episode state.
        return DROP_POLL_INTERVAL
    now = time.monotonic()
    if _reg_drop_state(reg):
        with w.lock:
            w.recovery_polls = 0
            if not w.in_drop:
                w.in_drop = True
                w.drop_start = now
                w.last_snap = now
                w.auto_recover_attempted = False
                os.makedirs(DROP_LOG_DIR, exist_ok=True)
                w.episode_id = next(_EPISODE_COUNTER)
                w.episode_file = _new_episode_path(w.episode_id)
                _write_detection(w, reg)
                _write_active_marker(w)
                _rotate_drop_log()
            elif now - w.last_snap >= DROP_SNAP_GAP:
                # Long episode: refresh the snapshot in the SAME file so
                # the tail stays relevant and one outage stays one file
                # (A-059/A-187).
                _append_refresh(w, reg)
                w.last_snap = now
        _maybe_auto_recover(w, now)
        return DROP_POLL_INTERVAL
    # Explicit non-drop state: recovery, with hysteresis (A-199) — a
    # single IN_SERVICE blip between drops must not close the episode.
    with w.lock:
        if w.in_drop:
            w.recovery_polls += 1
            if w.recovery_polls >= DROP_RECOVERY_CONFIRM:
                _write_recovery(w, now)
        else:
            w.recovery_polls = 0
    return DROP_POLL_INTERVAL


def _drop_log_loop():
    """Watchdog (daemon): while drop_log is enabled, watch the radio
    registration. On a drop, write a context snapshot to the drop_log
    dir and append the recovery duration to the same episode file.
    Live-toggles with the SETTINGS flag (no restart needed). Never
    raises."""
    _reconcile_open_episode()
    while True:
        try:
            time.sleep(_drop_log_poll())
        except Exception as e:
            print("Drop logger failed: {}".format(e))
            time.sleep(DROP_POLL_INTERVAL)


def _drop_log_files():
    """Episode filenames in the drop_log dir, newest first (UI cap 20;
    the on-disk cap is _rotate_drop_log, A-043/A-207)."""
    try:
        if not os.path.isdir(DROP_LOG_DIR):
            return []
        names = [n for n in os.listdir(DROP_LOG_DIR)
                 if n.startswith("drop_") and n.endswith(".txt")]
        return sorted(names, reverse=True)[:20]
    except Exception:
        return []

# Plain ioctl number (include/linux/diagchar.h) - NOT _IO-encoded, same
# convention as DIAG_IOCTL_SWITCH_LOGGING in diag_client.py.
DIAG_IOCTL_QUERY_MD_PID = 41

# Sentinel value used by CellSignalStrength dumps for "no measurement"
# (Integer.MAX_VALUE); legacy flat dumps use 99 for unknown ASU values.
_INVALID_SIGNAL = 2147483647

# Legacy ServiceState numeric registration states -> labels.
_REG_STATE_LABELS = {
    "0": "IN_SERVICE",
    "1": "OUT_OF_SERVICE",
    "2": "EMERGENCY_ONLY",
    "3": "POWER_OFF",
}

# TelephonyManager.NETWORK_TYPE_* ints -> names (legacy flat dumps).
_NETWORK_TYPE_NAMES = {
    "0": "UNKNOWN", "1": "GPRS", "2": "EDGE", "3": "UMTS", "4": "CDMA",
    "5": "EVDO_0", "6": "EVDO_A", "7": "1xRTT", "8": "HSDPA", "9": "HSUPA",
    "10": "HSPA", "11": "IDEN", "12": "EVDO_B", "13": "LTE", "14": "EHRPD",
    "15": "HSPAP", "16": "GSM", "17": "TD_SCDMA", "18": "IWLAN",
    "19": "LTE_CA", "20": "NR",
}

# AOSP-style LTE RSRP thresholds for the legacy-format level fallback.
_LTE_RSRP_THRESHOLDS = (-140, -128, -118, -108)

# Carrier-aware default bands (bandctl product plan, Approach 1): Rogers
# (MCC/MNC 302720; Fido shares it) gets the community-validated curated
# whitelist - bands 7 and 66 omitted per the SM8250 66<->7 handover crash
# fix. Every other carrier gets the unrestricted all-bands defaults.
ROGERS_MCCMNC = "302720"

_ROGERS_BANDS = {
    "lte": ["1", "2", "3", "4", "5", "8", "12", "17", "20", "28", "38", "40", "41"],
    "nr": ["1", "3", "5", "8", "20", "28", "38", "41", "77", "78"],
}

# Band catalogs the UI renders (web/index.html LTE_BANDS / NR_BANDS) — the
# read/write contract: a band may only be applied in its own RAT namespace
# (A-131) and out-of-catalog bands are filtered from QMI reads (A-145), so
# the displayed set can always be re-applied.
_LTE_CATALOG = frozenset([1, 2, 3, 4, 5, 7, 8, 12, 13, 14, 17, 20, 25, 26,
                          28, 29, 30, 38, 40, 41, 42, 43, 48, 66, 71])
_NR_CATALOG = frozenset([1, 2, 3, 5, 7, 8, 20, 25, 28, 38, 40, 41, 66, 71,
                         77, 78, 79])

# Non-Rogers default: unrestricted, EXCEPT bands 7 and 66 — the README's
# community-validated SM8250 crash pair (66<->7 handover crash) must not be
# selected by any default path (A-186); they stay selectable only via
# explicit user action.
_ALL_BANDS = {
    "lte": ["1","2","3","4","5","8","12","13","14","17","20","25","26","28","29","30","38","40","41","42","43","48","71"],
    "nr": ["1","2","3","5","8","20","25","28","38","40","41","71","77","78","79"],
}


def _run_cmd(args, timeout=5):
    """Run a command and return combined stdout+stderr.

    Raises only when the binary is missing or the call times out. The
    return code is deliberately NOT checked: `cmd` on Android returns 0
    even for unknown subcommands, so callers verify effect separately.
    """
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    return (proc.stdout or "") + (proc.stderr or "")


def _run_dumpsys(service="telephony.registry"):
    """Run `dumpsys <service>` with a short timeout, returning stdout."""
    return _run_cmd(["/system/bin/dumpsys", service], timeout=5)


def _get_prop(name):
    """Read an Android system property via `getprop`; "" on failure."""
    try:
        return _run_cmd(["/system/bin/getprop", name], timeout=5).strip()
    except Exception:
        return ""


def carrier_for_mccmnc(mccmnc):
    """"rogers" when the active SIM slot's operator numeric is Rogers/Fido
    (302720), else "other" (including empty/missing values).

    `gsm.operator.numeric` is comma-joined per SIM slot (e.g. "302720,"
    or "302720,302220"), so normalize by splitting on ",". With more than
    one distinct slot present we cannot tell which subscription is active,
    so Rogers-specific band exclusions are NOT applied — "other" yields
    the unrestricted all-bands defaults, which cannot remove bands the
    active carrier needs (A-072: mixed-SIM detection used to apply the
    Rogers whitelist to whichever carrier was actually active)."""
    if not mccmnc:
        return "other"
    slots = [s.strip() for s in str(mccmnc).split(",") if s.strip()]
    if len(set(slots)) > 1:
        return "other"
    return "rogers" if slots and slots[0] == ROGERS_MCCMNC else "other"


def defaults_for_carrier(carrier):
    """Default {"lte": [...], "nr": [...]} for a carrier: the curated
    Rogers whitelist, or unrestricted all-bands for anything else.
    Returns fresh copies so callers can stamp extra keys."""
    src = _ROGERS_BANDS if carrier == "rogers" else _ALL_BANDS
    return {"lte": list(src["lte"]), "nr": list(src["nr"])}


def seed_config_if_absent():
    """Persist the carrier-aware default to config/bands.json when absent.

    customize.sh's install-time seed is lost in KernelSU's metainstall
    staging (its writes go to an ephemeral dir, not the final module), so
    fresh installs land with no persisted preference and boot-apply would
    skip forever. Seeding here — the runtime MODDIR/config is
    authoritative — restores the documented boot re-apply. Rogers (302720)
    gets the curated whitelist; any other carrier gets no file (the
    server's carrier-aware all-bands fallback applies until the first
    Save & Apply mirrors one). If the operator prop is empty (mid-radio-
    drop), nothing is seeded — never raises."""
    try:
        if os.path.exists(CONFIG_FILE):
            # A-134: an interrupted install-time seed leaves a truncated
            # bands.json. Mere existence must not block recovery — a file
            # that is unreadable or not valid config is re-seeded.
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                if (isinstance(data, dict)
                        and isinstance(data.get("lte"), list)
                        and isinstance(data.get("nr"), list)):
                    return  # existing file is valid; keep user config
            except (OSError, ValueError):
                pass
            print(f"Config file missing/invalid, re-seeding: {CONFIG_FILE}")
        mccmnc = (_get_prop("gsm.operator.numeric") or "").strip(" ,")
        if carrier_for_mccmnc(mccmnc) != "rogers":
            return
        _atomic_write_json(CONFIG_FILE, defaults_for_carrier("rogers"))
        print(f"Seeded Rogers default band config: {CONFIG_FILE}")
    except Exception as e:
        print(f"Config seed skipped: {e}")

def _run_qmi(args, timeout):
    """Run the QMI band client; return (returncode, combined output).

    Returns (None, "") when the binary is missing (including the A-198
    None sentinel for a module without the bundled artifact), not
    executable (e.g. the exec bit was lost during install), or the call
    times out - callers treat that as "QMI unavailable" and fall back.
    Catching OSError keeps a PermissionError from killing the
    single-threaded HTTP server.
    """
    if QMI_BIN is None:
        return None, ""
    try:
        proc = subprocess.run([str(QMI_BIN)] + args, capture_output=True,
                              text=True, timeout=timeout)
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except OSError:
        # FileNotFoundError (missing binary) and PermissionError (exec bit
        # lost during install) both land here.
        return None, ""
    except subprocess.TimeoutExpired:
        return None, ""


def _parse_qmi_get(output):
    """Parse `qmi_band --get` output into {"lte": [...], "nr": [...]}.

    The client prints one `LTE bands:` / `NR5G SA bands:` / `NR5G NSA
    bands:` line per mask (band numbers space-separated, "(none)" when
    empty). Base and extension TLVs print under the SAME labels, so each
    category accumulates the UNION of its lines (A-122) — the total
    preference is the union of the base and extension masks. Bands
    outside the app's LTE/NR catalogs are filtered out (A-145) so the
    reported set can always be displayed and re-applied. Returns None
    when no LTE band line could be parsed (transport unavailable or
    malformed output).
    """
    lte = set()
    sa = set()
    nsa = set()
    lte_seen = False
    for line in output.splitlines():
        m = re.match(r"\s*(LTE|NR5G SA|NR5G NSA) bands:\s*(.*)$", line)
        if not m:
            continue
        tag, rest = m.group(1), m.group(2).strip()
        bands = [] if rest in ("", "(none)") else rest.split()
        if tag == "LTE":
            lte_seen = True
        for b in bands:
            if not b.isdigit():
                continue
            band = int(b)
            if tag == "LTE":
                if band in _LTE_CATALOG:
                    lte.add(band)
            elif tag == "NR5G SA":
                if band in _NR_CATALOG:
                    sa.add(band)
            else:
                if band in _NR_CATALOG:
                    nsa.add(band)
    if not lte_seen:
        return None
    # A-221: sort the NR union numerically, not lexicographically, so
    # '1','10','2' orders the same as the numeric LTE list.
    try:
        nr = sorted(set(sa or []) | set(nsa or []), key=int)
    except (TypeError, ValueError):
        nr = sorted(set(sa or []) | set(nsa or []))
    return {"lte": [str(b) for b in sorted(lte)], "nr": [str(b) for b in nr]}


def _cmd_available(service, subcommand):
    """True if `cmd <service> help` lists <subcommand>."""
    try:
        out = _run_cmd(["/system/bin/cmd", service, "help"], timeout=5)
        return subcommand in out
    except Exception:
        return False


def _airplane_on():
    """Tri-state airplane-mode property: True/False when getprop answers
    authoritatively, None when it cannot be verified (getprop failure,
    empty or unexpected value).

    Callers must never treat None as "off" — certifying cleanup on an
    unverifiable property is the A-46 bug (a failed disable command plus
    an unavailable property used to report success)."""
    try:
        val = str(_get_prop("persist.radio.airplane_mode_on")).strip()
    except Exception:
        return None
    if val == "1":
        return True
    if val == "0":
        return False
    return None


def _disable_airplane(attempts=3):
    """Disable airplane mode and verify it took; retries.

    Returns True only when the property is verifiably off (A-46): if the
    property cannot be read, cleanup cannot be certified and this returns
    False so callers report an honest failure."""
    for _ in range(attempts):
        try:
            _run_cmd(["/system/bin/cmd", "connectivity", "airplane-mode", "disable"], timeout=10)
        except Exception:
            pass
        time.sleep(1)
        if _airplane_on() is False:
            return True
    return False


def _parse_int(value):
    """Parse an int from a regex match group, or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_signal(value, lo=-160, hi=-1):
    """True if a parsed signal metric is a real measurement.

    Rejects None, the Integer.MAX_VALUE sentinel, the legacy 99 unknown
    marker, and values outside the physical dBm domain — RSRP/RSRQ are
    always negative, so 0, 50, etc. are corrupt/vendor junk (A-140:
    non-sentinel values used to pass straight through to the UI)."""
    if value is None or value >= _INVALID_SIGNAL or value == 99:
        return False
    return lo <= value <= hi


def _signal_level(level):
    """Normalize an Android signal level (domain 0-4) to int or None.

    A-133: sentinels (2147483647, 99) and out-of-domain junk were passed
    through as a live level whenever the accompanying metrics were valid."""
    if isinstance(level, int) and 0 <= level <= 4:
        return level
    return None


def _field_int(section, name):
    """Parse `<name> = <int>` from a section string (spaces around '='
    tolerated - the mLte fields use `rsrp=-111`, mNr uses `ssRsrp = X`)."""
    m = re.search(name + r'\s*=\s*(-?\d+)', section)
    return _parse_int(m.group(1)) if m else None


def _lte_level_from_rsrp(rsrp):
    """AOSP-style LTE level 0-4 derived from RSRP.

    Fallback only: modern dumpsys output carries the framework-computed
    `level=` directly, which the server prefers.
    """
    if rsrp is None:
        return None
    if rsrp < _LTE_RSRP_THRESHOLDS[0]:
        return 0
    if rsrp < _LTE_RSRP_THRESHOLDS[1]:
        return 1
    if rsrp < _LTE_RSRP_THRESHOLDS[2]:
        return 2
    if rsrp < _LTE_RSRP_THRESHOLDS[3]:
        return 3
    return 4


def _parse_signal_object(sig_obj):
    """Parse one `SignalStrength:{...}` object dump.

    Extracts RSRP/RSRQ/level for the LTE and NR sections and picks the
    tech matching `primary=` (falling back to whichever has a valid
    measurement). Returns a dict or None when nothing is parseable.
    """
    tech = None
    pm = re.search(r'primary=CellSignalStrength(\w+)', sig_obj)
    if pm:
        tech = pm.group(1).upper()

    # LTE section: mLte=CellSignalStrengthLte: rssi=-77 rsrp=-111 rsrq=-13 ... level=2
    lte_rsrp = lte_rsrq = lte_level = None
    lm = re.search(r'mLte=CellSignalStrengthLte:([^,}]*)', sig_obj)
    if lm:
        fields = lm.group(1)
        lte_rsrp = _field_int(fields, r'rsrp')
        lte_rsrq = _field_int(fields, r'rsrq')
        lte_level = _signal_level(_field_int(fields, r'level'))

    # NR section: mNr=CellSignalStrengthNr:{ csiRsrp = .. ssRsrp = .. level = 0 }
    nr_rsrp = nr_rsrq = nr_level = None
    nm = re.search(r'mNr=CellSignalStrengthNr:\{\s*(.*?)\s*\}', sig_obj, re.S)
    if nm:
        fields = nm.group(1)
        nr_rsrp = _field_int(fields, r'ssRsrp')
        nr_rsrq = _field_int(fields, r'ssRsrq')
        # A-99: when the SS measurements are sentinels/unavailable but the
        # also-exposed CSI measurements are valid, use those — a real 5G
        # session must not be reported as no-signal.
        if not _valid_signal(nr_rsrp):
            nr_rsrp = _field_int(fields, r'csiRsrp')
        if not _valid_signal(nr_rsrq):
            nr_rsrq = _field_int(fields, r'csiRsrq')
        nr_level = _signal_level(_field_int(fields, r'level'))

    candidates = []
    if _valid_signal(lte_rsrp) or _valid_signal(lte_rsrq):
        candidates.append(("LTE",
                           lte_rsrp if _valid_signal(lte_rsrp) else None,
                           lte_rsrq if _valid_signal(lte_rsrq) else None,
                           lte_level))
    if _valid_signal(nr_rsrp) or _valid_signal(nr_rsrq):
        candidates.append(("NR",
                           nr_rsrp if _valid_signal(nr_rsrp) else None,
                           nr_rsrq if _valid_signal(nr_rsrq) else None,
                           nr_level))
    if not candidates:
        return None

    # Prefer the primary tech's section; otherwise use the first valid one.
    if tech in ("LTE", "NR"):
        for cand in candidates:
            if cand[0] == tech:
                return {"rsrp_dbm": cand[1], "rsrq_db": cand[2],
                        "level": cand[3], "tech": cand[0]}
    cand = candidates[0]
    return {"rsrp_dbm": cand[1], "rsrq_db": cand[2],
            "level": cand[3], "tech": cand[0]}


def _parse_signal_legacy(values):
    """Parse the legacy flat `SignalStrength: 99 0 99 ...` list.

    AOSP CellSignalStrength.toString() layout: 0 gsm, 1 gsmBer, 2 cdmaDbm,
    3 cdmaEcio, 4 evdoDbm, 5 evdoEcio, 6 evdoSnr, 7 lteRssi, 8 lteRsrp,
    9 lteRsrq, 10 lteRssnr, 11 lteCqi, 12 lteTa. Returns None when the list
    has no valid LTE measurements.
    """
    if len(values) < 9:
        return None
    rsrp = values[8] if _valid_signal(values[8]) else None
    rsrq = values[9] if len(values) > 9 and _valid_signal(values[9]) else None
    if rsrp is None and rsrq is None:
        return None
    return {"rsrp_dbm": rsrp, "rsrq_db": rsrq,
            "level": _lte_level_from_rsrp(rsrp), "tech": "LTE"}


def _signal_rsrp(parsed):
    """Sort key for candidate selection: RSRP, or -1000 when absent."""
    rsrp = parsed.get("rsrp_dbm")
    return rsrp if rsrp is not None else -1000


def _parse_signal_strength(text):
    """Parse signal strength from `dumpsys telephony.registry` output.

    Tries the modern mSignalStrength=SignalStrength:{...} object format
    first, then the legacy flat `SignalStrength: <ints>` list. On
    multi-phone dumps (dual-SIM) the signal of the IN_SERVICE subscription
    is preferred over the first record — SIM 0 out of service must not
    shadow SIM 1's healthy measurement (A-50). Falls back to the strongest
    valid measurement when no in-service phone is identifiable. Returns
    None when nothing parseable exists.
    """
    if 'Phone Id=' in text:
        phones = {}
        svc_by_phone = {}
        current = None
        for line in text.splitlines():
            m = re.match(r'\s*Phone Id=(\d+)', line)
            if m:
                current = int(m.group(1))
                phones.setdefault(current, [])
                continue
            if current is None:
                continue
            if line.lstrip().startswith('mServiceState='):
                svc_by_phone[current] = line.split('mServiceState=', 1)[1].strip()
            if 'mSignalStrength=' in line:
                phones[current].append(line.split('mSignalStrength=', 1)[1])
        # 1) The in-service phone's signal.
        for pid, svc in svc_by_phone.items():
            reg = _parse_service_state(svc, text)
            if reg and reg["service_state"] == "IN_SERVICE":
                for raw in phones.get(pid, []):
                    parsed = _parse_signal_object(raw)
                    if parsed:
                        return parsed
        # 2) Strongest valid measurement across phones.
        best = None
        for raw_list in phones.values():
            for raw in raw_list:
                parsed = _parse_signal_object(raw)
                if parsed and (best is None
                               or _signal_rsrp(parsed) > _signal_rsrp(best)):
                    best = parsed
        if best is not None:
            return best
    # Single-phone / ungrouped dump: first valid object, then legacy flat
    # list (the object format is `SignalStrength:{`, which the flat-list
    # pattern cannot match).
    for line in text.splitlines():
        if 'mSignalStrength=' in line:
            parsed = _parse_signal_object(line.split('mSignalStrength=', 1)[1])
            if parsed:
                return parsed
    m = re.search(r'SignalStrength:\s+(-?\d+(?: -?\d+)*)', text)
    if m:
        parsed = _parse_signal_legacy([int(x) for x in m.group(1).split()])
        if parsed:
            return parsed
    return None


def _reg_label(svc, field):
    """Parse `mVoiceRegState=0(IN_SERVICE)` (object form) or the bare
    numeric form, returning the state label or None."""
    m = re.search(field + r'=(-?\d+)\(([A-Z_]+)\)', svc)
    if m:
        return m.group(2)
    m = re.search(field + r'=(-?\d+)', svc)
    if m:
        return _REG_STATE_LABELS.get(m.group(1))
    return None


def _parse_registration(text):
    """Parse registration state from `dumpsys telephony.registry` output.

    Uses the top-level `mServiceState=` lines. On multi-phone dumps
    (separate `Phone Id` blocks, dual-SIM) the IN_SERVICE subscription is
    preferred over an out-of-service first record — SIM 0 must not shadow
    the healthy active SIM (A-50); single-phone dumps keep first-record
    semantics. Handles the modern object format and the legacy flat form.
    Returns a dict with None for fields the build does not expose.
    """
    blocks = []
    for line in text.splitlines():
        if line.lstrip().startswith('mServiceState='):
            blocks.append(line.split('mServiceState=', 1)[1].strip())
    if not blocks:
        return None
    parsed = []
    for svc in blocks:
        reg = _parse_service_state(svc, text)
        if reg is not None:
            parsed.append(reg)
    if not parsed:
        return None
    if 'Phone Id=' in text:
        for reg in parsed:
            if reg["service_state"] == "IN_SERVICE":
                return reg
    return parsed[0]


def _parse_service_state(svc, text):
    """Parse ONE `mServiceState=` block into a reg dict.

    Handles the modern object format
    `mServiceState={mVoiceRegState=0(IN_SERVICE), ...}` and, as a
    best-effort, the legacy flat `ServiceState: <voice> <data> ...` form
    (with its top-level companion lines). Returns a dict with None for
    fields the build does not expose.
    """
    reg = {
        "service_state": None,
        "data_state": None,
        "network_type": None,
        "operator": None,
        "roaming": None,
    }

    if svc.startswith('{'):
        # Modern object format.
        reg["service_state"] = _reg_label(svc, 'mVoiceRegState')
        reg["data_state"] = _reg_label(svc, 'mDataRegState')

        # A-47: network labels may carry punctuation (LTE_CA, HSPA+) that
        # the old [A-Za-z]+ pattern dropped, leaving network_type null.
        m = re.search(r'getRilDataRadioTechnology=(-?\d+)\(([A-Za-z0-9_+]+)\)', svc)
        if not m:
            m = re.search(r'getRilVoiceRadioTechnology=(-?\d+)\(([A-Za-z0-9_+]+)\)', svc)
        if m:
            reg["network_type"] = m.group(2)

        m = re.search(r'mOperatorAlphaLong=([^,}]*?)(?=,|\})', svc)
        if not m or m.group(1).strip() in ('', 'null'):
            m = re.search(r'mOperatorAlphaShort=([^,}]*?)(?=,|\})', svc)
        if m and m.group(1).strip() not in ('', 'null'):
            reg["operator"] = m.group(1).strip()

        m = re.search(r'mIsDataRoamingFromRegistration=(true|false)', svc)
        if m:
            reg["roaming"] = m.group(1) == 'true'
        else:
            m = re.search(r'roamingType=(ROAMING|NOT_ROAMING)', svc)
            if m:
                reg["roaming"] = m.group(1) == 'ROAMING'
    else:
        # Legacy flat format: ServiceState: <voice> <data> <roaming> home ...
        m = re.match(r'ServiceState:\s*(\d+)\s+(\d+)', svc)
        if m:
            reg["service_state"] = _REG_STATE_LABELS.get(m.group(1))
            reg["data_state"] = _REG_STATE_LABELS.get(m.group(2))
        # Some legacy builds expose these as their own top-level lines.
        m = re.search(r'^mRoaming=([01])', text, re.M)
        if m:
            reg["roaming"] = m.group(1) == '1'
        if reg["network_type"] is None:
            m = re.search(r'^mNetworkType=(-?\d+)', text, re.M)
            if m:
                reg["network_type"] = _NETWORK_TYPE_NAMES.get(m.group(1))
        if reg["operator"] is None:
            m = re.search(r'^mOperatorAlphaLong=([^\r\n,}]*)', text, re.M)
            if m and m.group(1).strip() not in ('', 'null'):
                reg["operator"] = m.group(1).strip()

    # data_state fallback: mDataConnectionState (0-3) when the registration
    # state itself was not present.
    if reg["data_state"] is None:
        m = re.search(r'^mDataConnectionState=(-?\d+)', text, re.M)
        if m:
            reg["data_state"] = {
                "0": "DISCONNECTED", "1": "CONNECTING",
                "2": "CONNECTED", "3": "SUSPENDED",
            }.get(m.group(1))

    # A-144: a snapshot that parsed nothing is NOT valid telemetry — the
    # modem state is unavailable, so report a failure instead of a
    # deceptively all-null success (read_registration errors, and the
    # drop watchdog treats None as "unknown", never as "recovered").
    if all(reg[k] is None for k in (
            "service_state", "data_state", "network_type",
            "operator", "roaming")):
        return None

    # A-208: capture the IWLAN/WLAN transport context. The real registry
    # carries mNetworkRegistrationInfos entries (nested braces) inside
    # mServiceState; the WLAN/IWLAN entry that precedes every observed
    # drop must survive into the structured snapshot even when
    # getRil*RadioTechnology reads 0(Unknown) at the collapse.
    transports = _parse_registration_infos(svc)
    if transports is not None:
        reg["transports"] = transports
        if reg["network_type"] in (None, "Unknown") and any(
                t.get("tech") == "IWLAN" for t in transports):
            reg["network_type"] = "IWLAN"
    return reg


def _parse_registration_infos(svc):
    """Extract mNetworkRegistrationInfos transport entries (A-208).

    The infos value is a bracket-nested list whose entries themselves
    contain nested braces and empty [] fields, so it is scanned
    brace/bracket-aware to the matching close bracket. Returns a list of
    {"transport": ..., "tech": ...} dicts, or None when the field is
    absent from the service-state block.
    """
    m = re.search(r'mNetworkRegistrationInfos=\[', svc)
    if not m:
        return None
    depth = 1
    i = m.end()
    j = i
    while j < len(svc) and depth:
        if svc[j] == '[':
            depth += 1
        elif svc[j] == ']':
            depth -= 1
        j += 1
    block = svc[i:j - 1] if depth == 0 else svc[i:]
    transports = [mm.group(1) for mm in re.finditer(
        r'transportType=(\w+)', block)]
    techs = [mm.group(1) for mm in re.finditer(
        r'accessNetworkTechnology=(\w+)', block)]
    if not transports and not techs:
        return []
    return [{"transport": t, "tech": c}
            for t, c in zip(transports, techs)]


def _radio_reg_state():
    """Current mVoiceRegState label (e.g. IN_SERVICE, POWER_OFF) or None.

    Accepts both the object form `mVoiceRegState=3(POWER_OFF)` and the
    bare legacy numeric form `mVoiceRegState=3` — modem-reset verification
    must work on legacy dumps the rest of the app supports (A-98)."""
    try:
        text = _run_dumpsys("telephony.registry")
        m = re.search(r'mVoiceRegState=-?\d+\(([A-Z_]+)\)', text)
        if m:
            return m.group(1)
        m = re.search(r'mVoiceRegState=(-?\d+)', text)
        return _REG_STATE_LABELS.get(m.group(1)) if m else None
    except Exception:
        return None


def _wait_for_radio_state(expected, attempts=4, interval=1):
    """Poll until the radio registration state equals `expected`."""
    for _ in range(attempts):
        if _radio_reg_state() == expected:
            return True
        time.sleep(interval)
    return False


def _wait_for_radio_recovery(attempts=30, interval=2):
    """A-197: after a reset re-enables the radio, wait (bounded) for it
    to return to IN_SERVICE — re-camping takes 30s+ after a power cycle,
    and a reset that powered the radio off and on has NOT recovered until
    the radio camps again. Returns True when IN_SERVICE is observed,
    False otherwise (callers report the observed post-reset state)."""
    for _ in range(attempts):
        if _radio_reg_state() == "IN_SERVICE":
            return True
        time.sleep(interval)
    return _radio_reg_state() == "IN_SERVICE"


def _wait_radio_on(attempts=10, interval=1):
    """Poll until the radio reports a definite non-POWER_OFF state.

    An unparseable registration read (None) does NOT count as recovered —
    certifying "radio is back" without proof is the A-20/A-148 bug."""
    for _ in range(attempts):
        state = _radio_reg_state()
        if state is not None and state != "POWER_OFF":
            return True
        time.sleep(interval)
    return False


def _modem_reset():
    """Soft modem reset (module-level so the drop watchdog's auto-
    recovery A-196 can invoke the same path as the API endpoint).

    A-51: a concurrent reset is refused, not raced — the lock is held
    for the whole procedure, so the API endpoint and the watchdog's
    auto-recovery path serialize on it.

    Preferred: `cmd phone radio power` off/on (3s apart), used only
    when `cmd phone help` actually lists the subcommand. Fallback:
    airplane-mode toggle (`cmd connectivity airplane-mode` enable, 3s,
    disable). Success requires the radio to reach POWER_OFF and then to
    RETURN to IN_SERVICE within a bounded wait (A-197); otherwise an
    honest ok:false with the observed post-reset state is returned —
    a reset that never recovered is not reported as success.
    """
    if not _MODEM_RESET_LOCK.acquire(blocking=False):
        return {"ok": False, "error": "already in progress"}
    try:
        return _modem_reset_locked()
    finally:
        _MODEM_RESET_LOCK.release()


def _modem_reset_locked():
    """Actual reset procedure — caller holds _MODEM_RESET_LOCK (A-51)."""
    # Preferred mechanism: cmd phone radio power (only if listed in help)
    if _cmd_available("phone", "radio power"):
        try:
            _run_cmd(["/system/bin/cmd", "phone", "radio", "power", "off"], timeout=10)
            time.sleep(3)
            if _wait_for_radio_state("POWER_OFF"):
                _run_cmd(["/system/bin/cmd", "phone", "radio", "power", "on"], timeout=10)
                if not _wait_radio_on():
                    return {"ok": False,
                            "error": "radio did not power back on after reset"}
                if not _wait_for_radio_recovery():
                    state = _radio_reg_state()
                    return {"ok": False,
                            "error": "radio did not return to service (state: {})".format(
                                state or "unknown")}
                return {"ok": True}
            # A-137: the listed command ran but the radio never powered
            # off — leave the radio on and try the airplane-mode fallback
            # instead of failing outright.
            _run_cmd(["/system/bin/cmd", "phone", "radio", "power", "on"], timeout=10)
            print("radio power off did not take effect; trying airplane-mode fallback")
        except Exception as e:
            print(f"Modem reset via radio power failed: {e}")

    # Fallback: airplane-mode toggle. Airplane mode is ALWAYS turned
    # back off with verification — a reset that leaves airplane mode on
    # would silently kill all connectivity (v2.4 hardening). The
    # enable/disable commands are separate, so even a mid-toggle
    # exception still runs the cleanup before reporting.
    if _cmd_available("connectivity", "airplane-mode"):
        was_airplane_on = _airplane_on()
        try:
            _run_cmd(["/system/bin/cmd", "connectivity", "airplane-mode", "enable"], timeout=10)
            time.sleep(3)
            powered_off = _wait_for_radio_state("POWER_OFF")
            if was_airplane_on:
                # A-62: the user had airplane mode on before the reset;
                # restore that choice, never silently turn connectivity
                # back on.
                if _airplane_on() is True:
                    return {"ok": True}
                _run_cmd(["/system/bin/cmd", "connectivity", "airplane-mode", "enable"], timeout=10)
                time.sleep(2)
                if _airplane_on() is True:
                    return {"ok": True}
                return {"ok": False,
                        "error": "airplane mode was not restored after modem reset"}
            # Guaranteed cleanup: disable + verify, retried (A-46).
            if not _disable_airplane():
                return {"ok": False,
                        "error": "airplane mode left on after modem reset (disable failed)"}
            if powered_off:
                # A-148: the radio must verifiably leave POWER_OFF.
                if not _wait_radio_on():
                    return {"ok": False,
                            "error": "radio did not power back on after modem reset"}
                # A-197: and return to IN_SERVICE (re-camping).
                if not _wait_for_radio_recovery():
                    state = _radio_reg_state()
                    return {"ok": False,
                            "error": "radio did not return to service (state: {})".format(
                                state or "unknown")}
                return {"ok": True}
            return {"ok": False, "error": "airplane-mode toggle did not power off the radio"}
        except Exception as e:
            # Mid-toggle failure: try to leave the radio on before
            # reporting, so a stuck airplane mode is never silent.
            if _airplane_on() and not _disable_airplane():
                return {"ok": False,
                        "error": f"airplane mode left on after modem reset: {e}"}
            return {"ok": False, "error": f"modem reset failed: {e}"}

    return {"ok": False, "error": "modem reset unavailable on this build"}


def _query_md_pid(device):
    """Probe DIAG_IOCTL_QUERY_MD_PID (41) for the modem MD session owner.

    diag_query_pid_t is 16 bytes: uint32 peripheral_mask; uint32 pd_mask;
    int32 pid; uint32 device_mask. The masks mirror diag_client's
    SWITCH_LOGGING params (APSS|MPSS, local proc). fcntl.ioctl is used
    because Termux Android Python builds do not expose os.ioctl. Returns
    the owner pid, or None when the kernel does not support the ioctl or
    no session is owned.
    """
    try:
        fd = os.open(device, os.O_RDWR)
        try:
            buf = bytearray(16)
            # fcntl.ioctl on a mutable buffer returns the kernel status
            # int and fills the buffer IN PLACE — the return value is not
            # the payload (A-143: the old len(res) path raised TypeError
            # and health always reported md_session_owner null).
            fcntl.ioctl(fd, DIAG_IOCTL_QUERY_MD_PID, buf)
            _, _, pid, _ = struct.unpack('<IIiI', bytes(buf))
            return pid if pid > 0 else None
        finally:
            os.close(fd)
    except Exception:
        return None


# NR-ARFCN (DL) -> band fallback for CellIdentityNr blocks that omit
# mBands. DL ranges from 3GPP TS 38.104 Table 5.4.2.3-1, restricted to the
# app's NR catalog. Overlapping ranges are inherently ambiguous (n2/n25,
# n1/n66, n7/n41, n77/n78) — first match wins; mBands from the identity
# block is authoritative when present.
_NR_ARFCN_BANDS = (
    (422000, 434000, 1),
    (386000, 398000, 2),
    (361000, 376000, 3),
    (173800, 178800, 5),
    (524000, 538000, 7),
    (185000, 192000, 8),
    (158200, 164200, 20),
    (386000, 399000, 25),
    (151600, 160600, 28),
    (514000, 524000, 38),
    (460000, 480000, 40),
    (499200, 538000, 41),
    (422000, 440000, 66),
    (123400, 130400, 71),
    (620000, 680000, 77),
    (620000, 653333, 78),
    (693334, 733333, 79),
)


def _nr_arfcn_band(nrarfcn):
    """Best-effort NR-ARFCN -> band (first matching DL range)."""
    for lo, hi, band in _NR_ARFCN_BANDS:
        if lo <= nrarfcn <= hi:
            return band
    return None


def _parse_band_camping(text):
    """Parse serving EARFCN/NRARFCN and band from `dumpsys telephony.registry`.

    Prefers the camped-on identity from an mCellInfo entry marked
    mRegistered=YES (LTE or NR — the cell actually camped on; Rogers
    EARFCN 2050 reports mBands=[4] there while the registered-identity
    block can carry a misleading mBands list). Falls back to the
    registered-identity blocks (mCellIdentity=CellIdentityLte /
    CellIdentityNr). band is the first entry of the identity's mBands
    list; NR identities without mBands use the _NR_ARFCN_BANDS fallback.
    Returns (freq, band, rat) with rat "LTE"|"NR", or (None, None, None)
    when no cell identity is present (radio off, or camped on a RAT the
    dumpsys identity dump does not expose).
    """
    def _mbands_first(identity):
        bm = re.search(r'mBands=\[?([0-9,\s]*)\]?', identity)
        if bm:
            # A-139: only real band identities (1..79, the app's band
            # contract) count; junk like 0 or 999 is not a camped band.
            bands = [int(x) for x in bm.group(1).split(',') if x.strip()]
            bands = [b for b in bands if 1 <= b <= 79]
            if bands:
                return bands[0]
        return None

    def _lte_identity(identity):
        em = re.search(r'mEarfcn=(\d+)', identity)
        if not em:
            return None
        earfcn = _parse_int(em.group(1))
        # A-136: the unknown/sentinel EARFCN (Integer.MAX_VALUE) and any
        # value outside the LTE EARFCN domain (0..65535) are NOT a camped
        # cell — do not render a placeholder identity as a serving cell.
        if earfcn is None or not (0 <= earfcn <= 65535):
            return None
        return (earfcn, _mbands_first(identity), "LTE")

    def _nr_identity(identity):
        em = re.search(r'mNrarfcn=(\d+)', identity)
        if not em:
            return None
        nrarfcn = _parse_int(em.group(1))
        band = _mbands_first(identity)
        if band is None:
            band = _nr_arfcn_band(nrarfcn)
        return (nrarfcn, band, "NR")

    # 1) The camped-on cell: CellInfo{Lte,Nr} entries marked mRegistered=YES.
    for m in re.finditer(
            r'CellInfo(Lte|Nr):\{mRegistered=YES[^}]*?CellIdentity\1:\{([^}]*)\}',
            text):
        hit = _lte_identity(m.group(2)) if m.group(1) == "Lte" \
            else _nr_identity(m.group(2))
        if hit:
            return hit
    # 2) Registered-identity blocks (mCellIdentity=CellIdentity{Lte,Nr}).
    for m in re.finditer(
            r'mCellIdentity=CellIdentity(Lte|Nr):\s*\{([^}]*)\}', text):
        hit = _lte_identity(m.group(2)) if m.group(1) == "Lte" \
            else _nr_identity(m.group(2))
        if hit:
            return hit
    return None, None, None


def _trim_band_camping_log(path, max_lines=BAND_CAMPING_MAX_LINES):
    """Cap the camping log at max_lines lines (rewrite only when large).

    A-28: the old sampler appended forever with no rotation, so disk
    growth was unbounded and every /api/band-camping read re-read the
    whole file. The trim is a cheap size check on the common path and a
    bounded rewrite only when the log has grown past the cap."""
    try:
        if os.path.getsize(path) < 64 * 1024:
            return
        with open(path, 'r') as f:
            lines = f.readlines()
        if len(lines) <= max_lines:
            return
        with open(path, 'w') as f:
            f.writelines(lines[-max_lines:])
    except OSError:
        pass


def _read_tail(path, limit):
    """Return the last `limit` lines of `path` without a full-file read.

    A-28: the log is polled every 5s; reading the whole (potentially
    large) file per poll is the exact cost the finding calls out. Reads a
    bounded tail chunk; when the chunk starts mid-line its first line is
    a fragment and is dropped. Never raises."""
    try:
        with open(path, 'rb') as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return []
            want = min(size, 4096 + limit * 256)
            f.seek(size - want)
            data = f.read().decode('utf-8', 'replace')
        lines = data.splitlines()
        if want < size:
            lines = lines[1:]
        return lines[-limit:]
    except OSError:
        return []


def _band_camping_sample():
    """One sampling pass: dump telephony, append a CSV line when an
    EARFCN is present, trim the log. Returns True when a line was
    appended. Never raises."""
    try:
        os.makedirs(os.path.dirname(BAND_CAMPING_LOG), exist_ok=True)
        freq, band, rat = _parse_band_camping(
            _run_dumpsys("telephony.registry"))
        if freq is None:
            return False
        line = "{},{},{},{}\n".format(
            int(time.time() * 1000), freq,
            band if band is not None else "",
            rat if rat is not None else "")
        with open(BAND_CAMPING_LOG, 'a') as f:
            f.write(line)
        _trim_band_camping_log(BAND_CAMPING_LOG)
        return True
    except Exception as e:
        print("Band camping sample failed: {}".format(e))
        return False


def _band_camping_loop():
    """Background sampler (daemon): while the band_camping setting is
    enabled, dump telephony.registry every BAND_CAMPING_INTERVAL seconds,
    extract the serving freq/band, and append a `timestamp,freq,band,rat`
    CSV line to BAND_CAMPING_LOG (rat LTE|NR since A-030). Gated on the
    settings toggle (A-120/A-201: the old sampler ran unconditionally at
    boot, 24/7, with no way to disable it) and live-toggled like the drop
    logger. Failures are logged and skipped, never fatal."""
    while True:
        try:
            if SETTINGS.get("band_camping"):
                _band_camping_sample()
        except Exception as e:
            print("Band camping loop failed: {}".format(e))
        time.sleep(BAND_CAMPING_INTERVAL)


# Per-endpoint in-flight guards for the 2s signal/registration polls
# (A-34): a slow dumpsys must not stack overlapping subprocesses and
# worker threads. A concurrent poll reuses the last successful result
# instead of running a second dumpsys.
_SIGNAL_READ = {"lock": threading.Lock(), "last": None}
_REG_READ = {"lock": threading.Lock(), "last": None}


def _poll_once(cache, fn):
    """Run `fn` under `cache`'s in-flight guard (A-34).

    When another poll is already in flight, reuse its last successful
    result instead of stacking a second dumpsys subprocess; the first
    concurrent caller (nothing cached yet) waits for the in-flight read.
    Only successful results are cached, so a transient dumpsys failure is
    retried on the next poll rather than served forever."""
    lock = cache["lock"]
    if not lock.acquire(blocking=False):
        cached = cache["last"]
        if cached is not None:
            return cached
        lock.acquire()
    try:
        result = fn()
        if "error" not in result:
            cache["last"] = result
        return result
    finally:
        lock.release()


class BandHandler(http.server.BaseHTTPRequestHandler):
    # A-60: bound the per-connection socket read so a client that sends an
    # incomplete request line/body cannot pin a worker thread forever
    # (BaseHTTPRequestHandler applies `timeout` to the connection socket
    # in setup()).
    timeout = 15
    # A-169: HTTP/1.1 keep-alive — the UI's 2s/5s polling reuses one
    # connection instead of churning a TCP handshake + TIME_WAIT per
    # request, and pipelined requests are read instead of abandoned.
    protocol_version = "HTTP/1.1"

    def version_string(self):
        # A-168: do not fingerprint the bundled CPython version in every
        # response's Server header.
        return "Bandctl/2.6"

    def do_GET(self):
        if self.path.startswith('/api/'):
            self.handle_api()
        else:
            self._serve_static()

    def do_POST(self):
        if self.path.startswith('/api/'):
            self.handle_api()
        else:
            self.send_error(404)

    def _serve_static(self):
        """Serve only web/index.html for / and /index.html (SRV-07).

        Everything else 404s — no directory listing, no server.py/
        __pycache__ exposure. Query strings are stripped before the path
        match."""
        path = self.path.split('?', 1)[0]
        if path not in ("/", "/index.html"):
            self.send_error(404)
            return
        try:
            with open(WEB_DIR / "index.html", 'rb') as f:
                body = f.read()
        except OSError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _is_loopback(self):
        """True when the client connected via a loopback address.

        Uses ipaddress so the WHOLE 127.0.0.0/8 range (127.0.0.2, ...),
        ::1, and IPv4-mapped forms count — not just the literal 127.0.0.1
        (A-073)."""
        host = self.client_address[0]
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    # State-changing actions that an unprivileged local app must not reach
    # without the bearer token when one is configured (A-178/A-189). All
    # other actions are read-only diagnostics.
    _WRITE_ACTIONS = frozenset(
        ["write", "modem-reset", "boot-apply", "restart", "export"])

    def _auth_required(self, action):
        """LAN auth gate (A-026/A-049/A-158/A-178).

        Remote (non-loopback) clients are gated whenever the server is
        actually reachable on a non-loopback interface (the EFFECTIVE bind,
        A-049 — not the pending SETTINGS value, which update_settings flips
        before the socket is rebound). The only remote exemption is the
        read-only GET /api/settings bootstrap, keyed on the PATH so the
        documented bare route is exempt too (A-158).

        Loopback clients are exempt only for read-only diagnostics; the
        state-changing actions in _WRITE_ACTIONS (and the drop-log toggle)
        require the bearer token when one is configured, so an unprivileged
        phone app or web page cannot drive the modem, restart the server,
        export files, or mint/steal LAN credentials (A-178/A-189). With no
        token configured (LAN never enabled), loopback state changes pass —
        the documented "the phone itself never needs the token" bootstrap.
        POST /api/settings is handled here as the exposure control and its
        regenerate path is separately gated on the current token."""
        effective_bind = _EFFECTIVE_BIND or SETTINGS.get("bind", "127.0.0.1")
        settings_get = (self.command == "GET"
                        and self.path.split('?', 1)[0] == '/api/settings')
        if not self._is_loopback():
            if effective_bind != "0.0.0.0":
                return False  # server not listening on this interface
            if settings_get:
                return False
            return True
        # Loopback client.
        if settings_get:
            return False
        if action in self._WRITE_ACTIONS:
            return SETTINGS.get("token") is not None
        if action == "drop-log" and self.command == "POST":
            return SETTINGS.get("token") is not None
        return False

    def _host_allowed(self):
        """Validate the Host header (A-178 DNS-rebinding defense).

        Allows the loopback names/addresses and the LOCAL address of this
        connection (the phone's LAN IP the client actually used). A missing
        Host (HTTP/1.0) is accepted. Anything else — a rebinding domain —
        is rejected, so a remote page cannot point its own name at
        127.0.0.1 and drive the API."""
        host = (self.headers.get('Host') or '').strip() if self.headers else ''
        if not host:
            return True
        hostname = host
        if hostname.startswith('['):  # [::1]:8080
            hostname = hostname.split(']', 1)[0][1:]
        elif hostname.count(':') == 1:
            hostname = hostname.rsplit(':', 1)[0]
        if hostname in ("localhost", "127.0.0.1", "::1"):
            return True
        try:
            local = self.connection.getsockname()[0]
        except Exception:
            local = None
        if local and hostname == local:
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False

    def _check_auth(self):
        """Validate the bearer token against SETTINGS["token"].

        Returns None when allowed, else the 401 error dict. Missing or
        wrong token both fail; the compare is constant-time
        (hmac.compare_digest)."""
        token = None
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Bearer '):
            token = auth[len('Bearer '):]
        # Bytes compare: compare_digest raises TypeError on non-ASCII str,
        # which must not turn into a 500 (handled -> plain 401 instead).
        try:
            match = (bool(SETTINGS["token"]) and bool(token)
                     and hmac.compare_digest(token.encode('utf-8'),
                                             SETTINGS["token"].encode('utf-8')))
        except Exception:
            match = False
        if not match:
            return {"ok": False, "error": "unauthorized"}
        return None

    def _read_json_body(self):
        """Read and parse the request body (M6 guards).

        Content-Length is clamped to MAX_BODY_BYTES and the JSON parse is
        guarded — malformed input raises ValueError, which handle_api's
        guard turns into {"ok": false, "error": ...} (never a 500)."""
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            raise ValueError("invalid Content-Length")
        if length < 0 or length > MAX_BODY_BYTES:
            raise ValueError("request body exceeds {} bytes".format(
                MAX_BODY_BYTES))
        body = self.rfile.read(length).decode()
        try:
            return json.loads(body)
        except (ValueError, TypeError) as e:
            raise ValueError("invalid JSON body: {}".format(e))

    @staticmethod
    def _cors_origin(origin):
        """Reflect a cross-origin request's Origin only for the trusted
        WebView schemes (KernelSU WebUI, Android asset WebViews). Never
        wildcard: an arbitrary web page must not be able to READ API
        responses (A-026). LAN-mode laptop clients are same-origin (the
        page is served by this server) and need no CORS at all."""
        if not origin:
            return None
        origin = origin.strip()
        if origin.startswith("ksu://") or origin.startswith("appassets://"):
            return origin
        return None

    def do_OPTIONS(self):
        # CORS preflight for cross-origin POSTs from the KernelSU WebUI
        # (ksu://webui/bandctl/) and Android asset WebViews (appassets://).
        # Preflights carry no Authorization header, so the gate is
        # origin-based: only trusted origins receive allow headers (plus
        # the Private-Network-Access acknowledgement); everyone else gets a
        # bare 204 and the browser blocks the request (A-026).
        self.send_response(204)
        origin = self.headers.get('Origin') if self.headers else None
        allowed = self._cors_origin(origin)
        if allowed:
            self.send_header('Access-Control-Allow-Origin', allowed)
            self.send_header('Access-Control-Allow-Methods',
                             'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers',
                             'Content-Type, Authorization')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def handle_api(self):
        """Dispatch /api/* requests (M6-guarded, never 500s).

        Order: reject non-/api/ paths, apply the LAN auth gate (except
        GET /api/settings), then per-action dispatch. The whole body is
        wrapped in try/except so any exception becomes
        {"ok": false, "error": str(e)} instead of a 500."""
        try:
            if not self.path.startswith('/api/'):
                self.send_json({"error": "unknown action"}, status=400)
                return

            # A-169: HTTP/1.1 chunked bodies are not decoded — reject them
            # explicitly instead of silently dropping the body and
            # answering "invalid JSON body". The body is not consumed, so
            # keep-alive must be disabled (see _body_may_be_unread).
            if self.headers.get('Transfer-Encoding'):
                if self._body_may_be_unread():
                    self.close_connection = True
                self.send_json(
                    {"ok": False,
                     "error": "chunked transfer encoding not supported"},
                    status=501)
                return

            # A-178: DNS-rebinding defense — the Host must be a loopback
            # name or this connection's own local address.
            if not self._host_allowed():
                self.send_json({"ok": False, "error": "invalid host"},
                               status=403)
                return

            query = self.path.split('?', 1)[1] if '?' in self.path else ''
            action = ''
            for part in query.split('&'):
                if part.startswith('action='):
                    action = part.split('=', 1)[1]
                    break
            if not action:
                # A-65: the documented bare routes (GET /api/read,
                # /api/defaults, /api/health, ...) carry no ?action= —
                # derive the action from the path so the README contract
                # works for scripts/proxies/users, not just the UI.
                seg = self.path.split('?', 1)[0].rstrip('/')
                if seg.startswith('/api/'):
                    action = seg[len('/api/'):]

            if self._auth_required(action):
                auth_error = self._check_auth()
                if auth_error is not None:
                    # The body (if any) is not consumed on the 401 path —
                    # close the connection so leftover bytes are not
                    # misparsed as a pipelined request under keep-alive.
                    if self._body_may_be_unread():
                        self.close_connection = True
                    self.send_json(auth_error, status=401)
                    return

            # A-66/A-71: state-changing endpoints are POST-only. GET must
            # never trigger a restart, modem reset, boot apply, band write,
            # or export — a link, prefetcher, retrying proxy, or <img> tag
            # could fire it.
            if self.command != 'POST' and action in (
                    'write', 'restart', 'boot-apply', 'export',
                    'modem-reset'):
                self.send_json({"ok": False, "error": "method not allowed"},
                               status=405)
                return

            if action == 'read':
                self.send_json(self.read_config())
            elif action == 'defaults':
                self.send_json(self.read_defaults())
            elif action == 'settings':
                if self.command == 'POST':
                    data = self._read_json_body()
                    # A-178/A-220: token rotation is gated on the CURRENT
                    # token — an unauthenticated caller cannot mint-and-
                    # steal a fresh credential. (First-ever enable with no
                    # token configured is the legitimate bootstrap and stays
                    # allowed.)
                    if (isinstance(data, dict)
                            and data.get("regenerate") is True):
                        auth_error = self._check_auth()
                        if auth_error is not None:
                            self.send_json(auth_error, status=401)
                            return
                    res = self.update_settings(data)
                    status = 200
                    if res.get("ok") is False:
                        # A-017: a failed persistence is distinguishable by
                        # HTTP status so status-checking clients see the
                        # failure; client validation errors are 400.
                        status = 500 if res.get("error", "").startswith(
                            "settings save failed") else 400
                    self.send_json(res, status=status)
                else:
                    self.send_json(self.read_settings())
            elif action == 'restart':
                self.send_json(self.restart_service())
            elif action == 'write':
                self.send_json(self.write_config(self._read_json_body()))
            elif action == 'boot-apply':
                self.send_json(self.boot_apply())
            elif action == 'export':
                self.send_json(self.export_config(self._read_json_body()))
            elif action == 'signal':
                result = self.read_signal()
                # A-02: transport errors must not look healthy — the
                # frontend gates on r.ok, so surface them as 503.
                self.send_json(result,
                               status=503 if "error" in result else 200)
            elif action == 'registration':
                result = self.read_registration()
                self.send_json(result,
                               status=503 if "error" in result else 200)
            elif action == 'health':
                result = self.modem_health()
                self.send_json(
                    result,
                    status=503 if result.get("status") == "error" else 200)
            elif action == 'modem-reset':
                self.send_json(self.modem_reset())
            elif action == 'drop-log':
                if self.command == 'POST':
                    res = self.update_drop_log(self._read_json_body())
                    status = 500 if (res.get("ok") is False
                                     and res.get("error", "").startswith(
                                         "settings save failed")) else 200
                    self.send_json(res, status=status)
                else:
                    self.send_json(self.read_drop_log())
            elif action == 'band-camping':
                limit = 50
                if '?' in self.path:
                    for part in self.path.split('?', 1)[1].split('&'):
                        if part.startswith('limit='):
                            try:
                                limit = max(1, int(part.split('=', 1)[1]))
                            except (TypeError, ValueError):
                                pass
                if self.command == 'POST':
                    self.send_json(
                        self.update_band_camping(self._read_json_body()))
                else:
                    result = self.read_band_camping(limit)
                    # A-142: a read failure must not erase the UI's last
                    # known cell — surface it as a non-200 error instead
                    # of an ok:false 200 the frontend treats as empty.
                    self.send_json(
                        result,
                        status=503 if "error" in result else 200)
            else:
                # Unknown action: the body (if any) is never consumed.
                if self._body_may_be_unread():
                    self.close_connection = True
                self.send_json({"error": "unknown action"}, status=400)
        except Exception as e:
            # Guard path: a body read may have failed/been skipped (e.g.
            # oversized Content-Length), so keep-alive cannot be trusted.
            if self._body_may_be_unread():
                self.close_connection = True
            self.send_json({"ok": False, "error": str(e)})

    def _body_may_be_unread(self):
        """True when the request may carry a body this handler did not
        consume (chunked, non-numeric/oversized Content-Length).

        Under HTTP/1.1 keep-alive the stdlib would otherwise re-read the
        leftover body bytes as a pipelined request and answer a spurious
        400 — error paths set close_connection in that case."""
        if self.headers.get('Transfer-Encoding'):
            return True
        try:
            length = int(self.headers.get('Content-Length', 0))
        except (TypeError, ValueError):
            return True
        return length > 0

    def _normalize_bands(self, bands):
        """Coerce a raw band list to deduped ints 1..79, order preserved.

        Accepts ints and plain numeric strings; rejects bools, non-integer
        floats, and anything else. Floats like Infinity (json.loads accepts
        them) fail via OverflowError/ValueError handling instead of
        crashing. Returns None on the first invalid entry."""
        out = []
        seen = set()
        for b in bands:
            if isinstance(b, bool):
                return None
            try:
                if isinstance(b, str):
                    b = int(b.strip())
                elif isinstance(b, float):
                    if b != b or not b.is_integer():  # NaN / non-integral
                        return None
                    b = int(b)
                elif not isinstance(b, int):
                    return None
            except (TypeError, ValueError, OverflowError):
                return None
            if not (1 <= b <= 79):
                return None
            if b not in seen:
                seen.add(b)
                out.append(b)
        return out

    def _validate_bands(self, data):
        """Validate lte/nr band lists (M8), returning (bands, error).

        Each band must be an int 1..79 (numeric strings accepted), lists
        are deduped preserving order, and LTE must be non-empty.
        Additionally each LTE band must be in the LTE catalog and each NR
        band in the NR catalog (A-131): the same catalogs the UI renders,
        so the modem is never asked to apply a band in the wrong RAT
        preference namespace. Returns ({"lte": [...], "nr": [...]}, None)
        on success or (None, msg) on violation — never raises."""
        if not isinstance(data, dict):
            return None, "request body must be a JSON object"
        lte_raw = data.get('lte', [])
        nr_raw = data.get('nr', [])
        if not isinstance(lte_raw, list) or not isinstance(nr_raw, list):
            return None, "lte and nr must be lists"
        lte = self._normalize_bands(lte_raw)
        if lte is None:
            return None, "invalid lte band list: each band must be an integer 1-79"
        nr = self._normalize_bands(nr_raw)
        if nr is None:
            return None, "invalid nr band list: each band must be an integer 1-79"
        if not lte:
            return None, "lte must contain at least one band"
        bad_lte = [b for b in lte if b not in _LTE_CATALOG]
        if bad_lte:
            return None, "invalid lte band list: {} not in the LTE band catalog".format(
                ",".join(str(b) for b in bad_lte))
        bad_nr = [b for b in nr if b not in _NR_CATALOG]
        if bad_nr:
            return None, "invalid nr band list: {} not in the NR band catalog".format(
                ",".join(str(b) for b in bad_nr))
        return {"lte": lte, "nr": nr}, None

    def read_settings(self):
        """GET /api/settings — current LAN/token state (no token value)."""
        return {
            "ok": True,
            "lan_enabled": SETTINGS["bind"] == "0.0.0.0",
            "token_required": SETTINGS["token"] is not None,
        }

    def update_settings(self, data):
        """POST /api/settings — set lan_enabled, optionally regenerate the
        bearer token.

        lan_enabled must be a bool. The token is created when enabling
        with no token set or when regenerate=true (regeneration requires
        the current token — enforced by the dispatcher, A-178/A-220), and
        is returned ONLY when it was created/regenerated in this call
        (otherwise null). The new state is computed on a COPY and persisted
        BEFORE the live SETTINGS dict is mutated, so a failed save leaves
        the running configuration — and the token every LAN client holds —
        untouched (A-017/A-153). Calls are serialized so concurrent
        requests cannot interleave the read-modify-write (A-041)."""
        if not isinstance(data, dict):
            return {"ok": False, "error": "request body must be a JSON object"}
        lan_enabled = data.get("lan_enabled")
        if not isinstance(lan_enabled, bool):
            return {"ok": False, "error": "lan_enabled must be a bool"}
        regenerate = data.get("regenerate") is True

        with _SETTINGS_UPDATE_LOCK:
            new_settings = dict(SETTINGS)
            created = False
            if (lan_enabled and new_settings["token"] is None) or regenerate:
                new_settings["token"] = secrets.token_urlsafe(24)
                created = True
            new_settings["bind"] = "0.0.0.0" if lan_enabled else "127.0.0.1"
            try:
                _save_settings(new_settings)
            except Exception as e:
                # Persist first: SETTINGS is untouched on failure, so a
                # failed regenerate does not revoke the token in use.
                return {"ok": False,
                        "error": "settings save failed: {}".format(e)}
            SETTINGS["token"] = new_settings["token"]
            SETTINGS["bind"] = new_settings["bind"]
        return {
            "ok": True,
            "lan_enabled": lan_enabled,
            "token_required": new_settings["token"] is not None,
            "token": new_settings["token"] if created else None,
        }

    def restart_service(self):
        """POST /api/restart — schedule a detached module-service restart.

        service.sh kills the old server and starts a fresh one; it is
        launched ~1s later from a background thread so the {"ok": true}
        response is delivered first. Returns immediately. Serialized
        (A-041): only one restart may be pending at a time, so repeated
        taps cannot stack detached service.sh runs that each kill and
        replace the server. The response carries the CURRENT process pid
        so a client can prove the restart by polling /api/health until it
        reports a DIFFERENT pid (A-24/A-118) — an HTTP-OK health answer
        from the old, still-running process must not certify a restart.
        """
        global _RESTART_PENDING
        with _RESTART_LOCK:
            if _RESTART_PENDING:
                return {"ok": False, "error": "restart already scheduled"}
            _RESTART_PENDING = True
        def _do_restart():
            global _RESTART_PENDING
            try:
                time.sleep(1)
                subprocess.Popen(["sh", str(MODDIR / "service.sh")],
                                 start_new_session=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as e:
                print("Service restart failed: {}".format(e))
            finally:
                with _RESTART_LOCK:
                    _RESTART_PENDING = False
        threading.Thread(target=_do_restart, daemon=True).start()
        return {"ok": True, "pid": os.getpid()}

    def read_defaults(self):
        """Carrier-aware default band lists (GET /api/defaults).

        Carrier is detected from `getprop gsm.operator.numeric` (Rogers
        and Fido share 302720); the operator name comes from
        `getprop gsm.operator.alpha`. When the operator property cannot be
        read the response still carries (crash-pair-free) defaults, but
        carrier_detected=false lets clients warn instead of trusting an
        unverified carrier — detection failure must not silently select
        unrestricted defaults (A-58). Never raises."""
        detected = True
        try:
            mccmnc = (_get_prop("gsm.operator.numeric") or "").strip(" ,") or None
            carrier = carrier_for_mccmnc(mccmnc)
            operator = (_get_prop("gsm.operator.alpha") or "").strip(" ,") or None
            detected = mccmnc is not None
        except Exception as e:
            print(f"Carrier detection failed: {e}")
            mccmnc, carrier, operator = None, "other", None
            detected = False
        defaults = defaults_for_carrier(carrier)
        return {
            "carrier": carrier,
            "carrier_detected": detected,
            "mccmnc": mccmnc or None,
            "operator": operator,
            "lte": defaults["lte"],
            "nr": defaults["nr"],
        }

    def read_config(self):
        """Read band configuration: QMI (QRTR) first, then diag, then the
        fallback config file.

        A transport is only authoritative when the exchange verifiably
        succeeded: QMI must exit 0 and carry a success result TLV
        (A-14/A-44), a diag read that returns no bands is treated as a
        failed read rather than an authoritative empty config (A-15), and
        a corrupt or invalid persisted file is reported, not silently
        replaced by carrier defaults (A-149)."""
        qmi_cfg = self._read_qmi_config()
        if qmi_cfg is not None:
            return qmi_cfg
        try:
            bands = read_bands(DIAG_DEVICE)
            lte = [str(b) for b in bands.get('lte_bands', [])
                   if b in _LTE_CATALOG]
            nr = [str(b) for b in bands.get('nr_bands', [])
                  if b in _NR_CATALOG]
            if not lte and not nr:
                # A failed/partial NV read is indistinguishable from
                # "nothing configured" — do not present it as verified
                # diag state (A-15); fall through to the persisted file.
                raise ValueError("diag read returned no bands (read failed?)")
            return self._read_result(lte, nr, "diag")
        except Exception as e:
            print(f"Diag read failed: {e}, trying config file")
            return self._read_config_file()

    def _read_qmi_config(self):
        """Read bands via the QMI client.

        Returns a full read-result dict, or None when QMI is unavailable,
        the helper exited nonzero, or the response carried no success
        result TLV (A-14/A-44) — a failed/partial QMI exchange must never
        be labelled `source: qmi` and suppress the diag/config fallback.
        """
        rc, out = _run_qmi(["--get"], QMI_GET_TIMEOUT)
        if rc != 0 or not out:
            return None
        if re.search(r"result: status=0", out) is None:
            return None
        parsed = _parse_qmi_get(out)
        if parsed is None:
            return None
        return self._read_result(parsed["lte"], parsed["nr"], "qmi")

    def _read_result(self, lte, nr, source):
        """Shape a successful read response.

        Includes ok:true (so clients can uniformly distinguish success
        from the HTTP-200 error dicts) and the current persisted-intent
        revision rev (so a client can detect that another client changed
        the config before its write — A-97)."""
        return {"ok": True,
                "lte": [str(b) for b in lte],
                "nr": [str(b) for b in nr],
                "source": source,
                "rev": _CONFIG_REV}

    def _read_config_file(self):
        """Read from the fallback config file, validated.

        The persisted file goes through the same per-RAT validation as
        /api/write (A-42): list types, ranges, dedup, non-empty LTE, and
        catalog membership. Malformed JSON or invalid bands are reported
        as {"ok": false, ...} instead of silently substituting carrier
        defaults (A-149). A missing file falls back to carrier-aware
        defaults (Rogers: curated whitelist; other: crash-pair-free
        all-bands)."""
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
            except Exception as e:
                return {"ok": False,
                        "error": "config file corrupt: {}".format(e)}
            bands, err = self._validate_bands(data)
            if bands is None:
                return {"ok": False,
                        "error": "invalid config file: {}".format(err)}
            return self._read_result(bands["lte"], bands["nr"], "config_file")
        defaults = defaults_for_carrier(
            carrier_for_mccmnc(_get_prop("gsm.operator.numeric")))
        return {"ok": True,
                "lte": [str(b) for b in defaults["lte"]],
                "nr": [str(b) for b in defaults["nr"]],
                "source": "default",
                "rev": _CONFIG_REV}

    def write_config(self, data):
        """Write band configuration to the modem: QMI (QRTR) first, then
        diag.

        The config file is mirrored only after a VERIFIED apply: a failed
        apply is never promoted to boot-time source of truth (A-68), and
        a persistence failure is reported instead of claiming a save that
        did not happen (A-16). Writes are serialized end to end (A-45);
        an optional rev from a prior /api/read rejects stale writes from
        another client (A-97)."""
        bands, err = self._validate_bands(data)
        if bands is None:
            return {"ok": False, "error": err}
        rev = data.get("rev")
        if rev is not None and not isinstance(rev, int):
            try:
                rev = int(rev)  # tolerate string-digit revs from clients
            except (TypeError, ValueError):
                rev = None  # garbage rev is treated as absent (allow)
        global _CONFIG_REV
        with _BAND_WRITE_LOCK:
            if rev is not None and rev != _CONFIG_REV:
                return {"ok": False,
                        "error": "config changed by another client; refresh and retry"}
            ok, source, error = self._apply_bands(bands["lte"], bands["nr"])
            if not ok:
                # Keep the persisted file describing the last VERIFIED
                # state — boot re-apply must not retry a rejected set.
                return {"ok": False, "error": error}
            try:
                self._save_config_file(bands)
            except Exception as e:
                return {"ok": False, "error": "config save failed: {}".format(e)}
            _CONFIG_REV += 1
            return {"ok": True, "source": source, "rev": _CONFIG_REV}

    def _apply_bands(self, lte_bands, nr_bands):
        """Apply bands to the modem: QMI (QRTR) first, then diag.

        Returns (ok, source, error) with error None on success. Both
        transports are verified by read-back, so success is only certified
        when the requested set was actually applied: QMI requires exit 0 +
        a success result TLV + a read-back showing every requested band
        (A-13/A-138), diag requires write_bands() True + a read-back
        showing the bands (A-33). Never raises: QMI failure falls through
        to diag, and any diag exception is reported as
        (False, "diag", str(e)).
        """
        # QMI write first (real band apply via QRTR)
        if self._write_qmi_config(lte_bands, nr_bands):
            return True, "qmi", None

        # Fallback: diag write, verified by read-back so a half-applied
        # config (LTE written, NR failed) is never reported as success.
        try:
            if not write_bands(lte_bands, nr_bands, DIAG_DEVICE):
                return False, "diag", "diag write failed"
            bands = read_bands(DIAG_DEVICE)
            applied_lte = set(bands.get('lte_bands', []))
            applied_nr = set(bands.get('nr_bands', []))
            if set(lte_bands) <= applied_lte and set(nr_bands) <= applied_nr:
                return True, "diag", None
            return False, "diag", \
                "diag write verification failed (some bands not applied)"
        except Exception as e:
            return False, "diag", str(e)

    def boot_apply(self):
        """Re-apply the persisted band config (config/bands.json) at boot.

        Reads the config file written by /api/write and runs it through
        the same QMI->diag apply chain. The config file is the source of
        truth here - boot apply NEVER rewrites or re-seeds it: a missing
        config is always a skip, so deleting bands.json reliably disables
        boot re-apply, even on Rogers devices (A-216/A-119: the endpoint
        is read-only w.r.t. the config — v2.3+ seeded from inside this
        endpoint, silently breaking the documented "config-file absent =
        no-op" contract, and the file is never re-created at startup
        either). Malformed JSON, invalid bands (per-RAT catalogs, LTE
        non-empty), or an apply failure return ok:false with a short
        error. Never raises - any exception becomes
        {"ok": false, "error": ...}. Serialized with /api/write so
        concurrent applies cannot interleave (A-45).
        """
        with _BAND_WRITE_LOCK:
            try:
                if not os.path.exists(CONFIG_FILE):
                    return {"ok": True, "skipped": True}
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
            except (OSError, IOError):
                # Missing/unreadable config file -> nothing to re-apply.
                return {"ok": True, "skipped": True}
            except Exception:
                # File exists but is not valid JSON -> report it.
                return {"ok": False, "error": "invalid config"}

            bands, err = self._validate_bands(data)
            if bands is None:
                return {"ok": False, "error": err}
            lte_bands = bands["lte"]
            nr_bands = bands["nr"]

            try:
                ok, source, error = self._apply_bands(lte_bands, nr_bands)
            except Exception as e:
                return {"ok": False, "error": str(e)}
            if not ok:
                return {"ok": False, "error": error or "band apply failed"}
            # Bands as reported to the modem (ints normalized to strings,
            # same shape as the rest of the API).
            return {
                "ok": True,
                "source": source,
                "lte": [str(b) for b in lte_bands],
                "nr": [str(b) for b in nr_bands],
            }

    def _verify_qmi_applied(self, requested_lte, requested_nr, attempts=2):
        """Verify a QMI --set took effect by reading the preference back.

        Returns True when a successful read-back shows every requested
        band present (subset check). The helper's LTE mask path cannot
        represent bands >64 and would silently drop them on SET, so a
        "successful" exchange must be verified (A-13); one retry absorbs
        a briefly-async preference store."""
        for _ in range(attempts):
            rc, out = _run_qmi(["--get"], QMI_GET_TIMEOUT)
            if rc == 0 and out and re.search(r"result: status=0", out):
                parsed = _parse_qmi_get(out)
                if parsed is not None:
                    applied_lte = set(int(b) for b in parsed["lte"])
                    applied_nr = set(int(b) for b in parsed["nr"])
                    if set(requested_lte) <= applied_lte \
                            and set(requested_nr) <= applied_nr:
                        return True
            time.sleep(1)
        return False

    def _write_qmi_config(self, lte_bands, nr_bands):
        """Apply bands via the QMI client.

        True only when the helper exited 0 AND its output carried a
        success result TLV (A-138: printing `result: status=0` then
        exiting nonzero must not certify an apply) AND a follow-up
        read-back shows every requested band applied (A-13)."""
        lte_csv = ",".join(str(b) for b in lte_bands)
        nr_csv = ",".join(str(b) for b in nr_bands)
        rc, out = _run_qmi(["--set", lte_csv, nr_csv], QMI_SET_TIMEOUT)
        if rc != 0 or not out:
            return False
        if re.search(r"result: status=0", out) is None:
            return False
        return self._verify_qmi_applied(lte_bands, nr_bands)

    def _save_config_file(self, data):
        """Save config to file as fallback (atomic temp+replace, M7).

        Raises on failure so write_config can report the persistence
        failure instead of returning ok:true for a save that did not
        happen (A-16)."""
        _atomic_write_json(CONFIG_FILE, data)

    def export_config(self, data):
        """Write a submitted band config to a timestamped JSON file in the
        config dir. WebView-compatible export: the Manager WebView drops
        blob downloads, so the server delivers the file on-device and the
        UI toasts the path.

        The body is validated exactly like /api/write — non-dict bodies and
        out-of-range bands are rejected instead of persisted verbatim
        (A-155). The write is symlink-proof and collision-free (A-211/A-054):
        content goes to a random O_EXCL|O_NOFOLLOW temp file (0o600) that is
        os.replace()d over the timestamped name, so a planted symlink in the
        config dir can neither be followed nor clobber another export. An
        interrupted export never strands a corrupt file (A-135), and only the
        newest EXPORT_KEEP exports are retained (A-082: exports used to
        accumulate without bound).
        Returns {"ok": true, "path": ...} or {"ok": false, "error": ...} —
        never raises."""
        bands, err = self._validate_bands(data)
        if bands is None:
            return {"ok": False, "error": err}
        tmp = None
        try:
            _ensure_private_dir(EXPORT_DIR)
            stamp = time.strftime('%Y%m%d-%H%M%S') + '.{:03d}'.format(
                int(time.time() * 1000) % 1000)
            # A-054/A-211: a random suffix keeps concurrent exports from
            # overwriting each other AND makes the final name unpredictable
            # (an attacker cannot pre-plant symlinks across the name space).
            path = os.path.join(
                EXPORT_DIR, "bandctl-export-{}-{}.json".format(
                    stamp, secrets.token_hex(3)))
            tmp = os.path.join(EXPORT_DIR, ".export-{}-{}.tmp".format(
                os.getpid(), secrets.token_hex(4)))
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(tmp, flags, 0o600)
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(bands, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            os.replace(tmp, path)
            self._prune_exports()
            return {"ok": True, "path": path}
        except Exception as e:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return {"ok": False, "error": str(e)}

    def _prune_exports(self, keep=EXPORT_KEEP):
        """Keep only the newest `keep` bandctl-export-*.json files.

        A-82: repeated exports otherwise consume module config storage
        forever, with no way to identify or remove stale copies."""
        try:
            exports = sorted(glob.glob(
                os.path.join(EXPORT_DIR, "bandctl-export-*.json")))
            for old in exports[:-keep]:
                os.unlink(old)
        except OSError:
            pass

    def read_signal(self):
        """Read current signal strength from `dumpsys telephony.registry`.

        Parses the mSignalStrength block (object format on modern builds,
        flat list on legacy ones). Missing/unparseable metrics become null;
        the response includes a timestamp. Returns {"error": ...} only when
        dumpsys fails or no signal data exists.
        """
        def _do():
            try:
                text = _run_dumpsys("telephony.registry")
            except Exception as e:
                return {"error": f"dumpsys failed: {e}"}
            parsed = _parse_signal_strength(text)
            if parsed is None:
                return {"error": "no signal strength data in dumpsys telephony.registry"}
            parsed["timestamp"] = int(time.time() * 1000)
            return parsed
        # A-34: serialize the poll — a concurrent 2s poll reuses the last
        # successful result instead of stacking a second dumpsys.
        return _poll_once(_SIGNAL_READ, _do)

    def read_registration(self):
        """Read registration state from `dumpsys telephony.registry`.

        Parses the first top-level mServiceState block. Missing fields are
        null; the response includes a timestamp. Returns {"error": ...}
        only when dumpsys fails or no service state exists.
        """
        def _do():
            try:
                text = _run_dumpsys("telephony.registry")
            except Exception as e:
                return {"error": f"dumpsys failed: {e}"}
            parsed = _parse_registration(text)
            if parsed is None:
                return {"error": "no service state data in dumpsys telephony.registry"}
            parsed["timestamp"] = int(time.time() * 1000)
            return parsed
        # A-34: serialize the poll — a concurrent 2s poll reuses the last
        # successful result instead of stacking a second dumpsys.
        return _poll_once(_REG_READ, _do)

    def read_drop_log(self):
        """GET /api/drop-log — drop-logger state + snapshot files.

        Returns the enabled flag, the snapshot dir, and the latest
        snapshot filenames. Never raises."""
        return {
            "ok": True,
            "enabled": bool(SETTINGS.get("drop_log")),
            "dir": str(DROP_LOG_DIR),
            "files": _drop_log_files(),
        }

    def update_drop_log(self, data):
        """POST /api/drop-log — enable/disable the drop logger (v2.5).

        The setting is persisted to settings.json and picked up live by
        the watchdog thread (no restart needed). Persist-first, like
        update_settings: a failed save leaves the live snapshot unchanged
        (A-153 family), so the in-memory value never disagrees with disk
        (A-63), and concurrent toggles are serialized (A-041)."""
        if not isinstance(data, dict) or not isinstance(
                data.get("enabled"), bool):
            return {"ok": False, "error": "enabled must be a bool"}
        with _SETTINGS_UPDATE_LOCK:
            new_settings = dict(SETTINGS)
            new_settings["drop_log"] = data["enabled"]
            try:
                _save_settings(new_settings)
            except Exception as e:
                return {"ok": False,
                        "error": "settings save failed: {}".format(e)}
            SETTINGS["drop_log"] = data["enabled"]
            return {"ok": True, "enabled": data["enabled"],
                    "dir": str(DROP_LOG_DIR)}

    def read_band_camping(self, limit=50):
        """Return the last `limit` lines of the band camping log as JSON.

        Each CSV line is `timestamp,freq,band,rat` (epoch ms ints; band
        empty when the mBands list was absent; rat "LTE"|"NR" since the
        A-030 fix, absent on older lines). Reads only the log tail
        (A-28: full re-reads per 5s poll cost grew with the log), reports
        the sampler toggle state (A-120/A-201), and returns the log path
        as a string (A-74/A-200: a pathlib.Path is not JSON-serializable
        and crashed the endpoint on every call). Never raises - an absent
        or unreadable log yields an empty sample list with ok:true.
        """
        try:
            enabled = bool(SETTINGS.get("band_camping"))
            if not os.path.exists(BAND_CAMPING_LOG):
                return {"ok": True, "enabled": enabled, "samples": [],
                        "log": str(BAND_CAMPING_LOG)}
            samples = []
            for ln in _read_tail(BAND_CAMPING_LOG, limit):
                parts = ln.split(',')
                sample = {"timestamp": _parse_int(parts[0]) if parts else None}
                if len(parts) > 1:
                    sample["earfcn"] = _parse_int(parts[1])
                if len(parts) > 2:
                    sample["band"] = _parse_int(parts[2]) if parts[2] else None
                if len(parts) > 3:
                    sample["rat"] = parts[3] or None
                samples.append(sample)
            return {"ok": True, "enabled": enabled, "samples": samples,
                    "log": str(BAND_CAMPING_LOG)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def update_band_camping(self, data):
        """POST /api/band-camping — enable/disable the serving-cell sampler.

        A-120/A-201: the sampler used to run unconditionally at boot with
        no way to disable it. The setting is persisted to settings.json
        and picked up live by the daemon thread (no restart needed)."""
        try:
            if not isinstance(data, dict) or not isinstance(data.get("enabled"), bool):
                return {"ok": False, "error": "enabled must be a bool"}
            SETTINGS["band_camping"] = data["enabled"]
            _save_settings()
            return {"ok": True, "enabled": data["enabled"]}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def modem_reset(self):
        """Soft modem reset — delegates to the module-level _modem_reset
        so the drop watchdog's auto-recovery (A-196) uses the same path.

        Preferred: `cmd phone radio power` off/on (3s apart), used only
        when `cmd phone help` actually lists the subcommand; if it runs
        but never powers the radio off, the airplane-mode fallback is
        tried instead of failing outright (A-137). Fallback:
        airplane-mode toggle (`cmd connectivity airplane-mode` enable, 3s,
        disable). Success is reported only when the radio verifiably
        recovers: the preferred path verifies the radio left POWER_OFF
        after power-on (A-20/A-148) and then returns to IN_SERVICE
        (A-197), the airplane path restores a pre-existing airplane-mode
        choice (A-62), and cleanup whose verification is unavailable
        (getprop failure) is never certified (A-46)."""
        return _modem_reset()

    def modem_health(self):
        """Get modem health status. Never raises - always returns a
        JSON-ready dict.

        status is "ok" when a transport reads bands, "degraded" when the
        transport answers but no bands are configured, and "error" when
        nothing is readable (message included). transport is "qmi" when
        the QRTR client verifiably works (exit 0 + success result TLV —
        A-14), "diag" otherwise. pid is the current server process, so a
        client can prove a restart by observing a pid change (A-118).
        """
        rc, out = _run_qmi(["--get"], QMI_GET_TIMEOUT)
        parsed = None
        if rc == 0 and out and re.search(r"result: status=0", out):
            parsed = _parse_qmi_get(out)
        if parsed is not None:
            lte_bands = parsed["lte"]
            nr_bands = parsed["nr"]
            status = "ok" if (lte_bands or nr_bands) else "degraded"
            result = {
                "status": status,
                "transport": "qmi",
                "lte_bands": len(lte_bands),
                "nr_bands": len(nr_bands),
                "md_session_owner": None,
                "pid": os.getpid(),
            }
            if status == "degraded":
                result["error"] = "QMI responded but no bands are configured"
            return result
        try:
            bands = read_bands(DIAG_DEVICE)
            lte_bands = bands.get('lte_bands', []) or []
            nr_bands = bands.get('nr_bands', []) or []
            if lte_bands or nr_bands:
                status = "ok"
            else:
                status = "degraded"
            result = {
                "status": status,
                "transport": "diag",
                "diag_device": DIAG_DEVICE,
                "lte_bands": len(lte_bands),
                "nr_bands": len(nr_bands),
                "md_session_owner": _query_md_pid(DIAG_DEVICE),
                "pid": os.getpid(),
            }
            if status == "degraded":
                result["error"] = "diag responded but no bands are configured"
            return result
        except Exception as e:
            return {
                "status": "error",
                "transport": "diag",
                "diag_device": DIAG_DEVICE,
                "lte_bands": 0,
                "nr_bands": 0,
                "md_session_owner": None,
                "pid": os.getpid(),
                "error": str(e),
            }

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        origin = self.headers.get('Origin') if self.headers else None
        allowed = self._cors_origin(origin)
        if allowed:
            self.send_header('Access-Control-Allow-Origin', allowed)
            self.send_header('Access-Control-Allow-Methods',
                             'GET, POST, OPTIONS')
            self.send_header('Access-Control-Allow-Headers',
                             'Content-Type, Authorization')
            self.send_header('Access-Control-Allow-Private-Network', 'true')
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)


if __name__ == '__main__':
    # Check for --no-diag flag (fallback mode)
    if '--no-diag' in sys.argv:
        DIAG_DEVICE = None
        print("Running in fallback mode (no diag)")

    # Band-camping sampler (findings 5c): log the serving EARFCN/band so a
    # band force can be validated offline. Daemon thread - dies with the
    # server; never blocks shutdown.
    threading.Thread(target=_band_camping_loop, daemon=True).start()

    # NOTE (A-119): config/bands.json is deliberately NOT seeded at startup.
    # A missing file must be a permanent no-op for boot re-apply — the
    # documented escape hatch for disabling persistent band forcing — so
    # deleting the file must never be undone by a later server start.

    # v2.5 drop logger (daemon): stamps radio drops with correlation
    # context while Settings > Debug > Drop logging is enabled. The
    # setting is live — the thread sleeps cheaply when disabled.
    threading.Thread(target=_drop_log_loop, daemon=True).start()

    # A-180: harden the module config tree even when service.sh created it
    # with the boot umask (world-writable). The server runs as root and is
    # the only legitimate reader of these dirs.
    _ensure_private_dir(MODDIR / "config")
    _ensure_private_dir(DROP_LOG_DIR)

    # A-198: refuse to substitute third-party binaries — surface a missing
    # bundled QMI client loudly instead of silently exec'ing adb scratch.
    if QMI_BIN is None:
        print("WARNING: bundled qmi/qmi_band missing — QMI band apply "
              "disabled (no fallback binary is used)")

    # Threaded: the 2s signal/registration polling (each dumpsys call takes
    # seconds on this device) must not starve other requests — a single-
    # threaded server stalls /api/defaults, /api/read, and button actions
    # behind an endless polling queue.
    # Bind comes from settings.json (default 127.0.0.1; 0.0.0.0 = LAN).
    bind = SETTINGS.get("bind", "127.0.0.1")
    server = http.server.ThreadingHTTPServer((bind, PORT), BandHandler)
    # A-049: the auth gate keys on the ACTUAL listening address, not the
    # settings value — a pending settings change must not drop auth before
    # the socket is actually rebound (which happens on restart).
    _EFFECTIVE_BIND = bind
    print(f"Band Controller server running on http://{bind}:{PORT}")
    print(f"QMI binary: {QMI_BIN}")
    print(f"Diag device: {DIAG_DEVICE or 'disabled'}")
    server.serve_forever()
