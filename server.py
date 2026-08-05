#!/usr/bin/env python3
"""Band Controller HTTP server with QMI-first transport.

No NSG dependency - band apply goes over QRTR QMI (the bundled static
qmi_band client), with /dev/diag and the config file as fallbacks.

HTTP API (all responses are JSON, served from the phone's web root):

  GET  /api/read?action=read
       -> {"lte": ["1", ...], "nr": ["77", ...],
           "source": "qmi" | "diag" | "config_file" | "default"}
       LTE/NR band configuration. Tries QMI (QRTR) first, then diag, then
       the fallback config file, then a carrier-aware default list.

  GET  /api/defaults?action=defaults
       -> {"carrier": "rogers" | "other", "mccmnc": "302720" | null,
           "operator": "ROGERS" | null, "lte": [...], "nr": [...]}
       Carrier-aware default band lists. Carrier is detected from
       `getprop gsm.operator.numeric` (Rogers and Fido share 302720);
       "rogers" returns the community-validated curated whitelist, any
       other carrier the unrestricted all-bands list. Never 500s.

  POST /api/write?action=write   body {"lte": [...], "nr": [...]}
       -> {"ok": bool, "source": ..., "error": ...?}
       Writes the band configuration to the modem via QMI (QRTR) first,
       falling back to diag, and mirrors it to the fallback config file.

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
       -> {"ok": true, "samples": [{"timestamp": epoch_ms,
           "earfcn": int, "band": int|null}, ...], "log": path}
       Last N (default 50) serving-cell EARFCN/band samples recorded by
       the background band-camping sampler (findings 5c). When the log
       has no samples yet, "samples" is an empty list - the sampler
       only writes a line when an LTE cell identity is present.

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
import hmac
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
DEFAULT_SETTINGS = {"bind": "127.0.0.1", "token": None}

# Serialize config/settings writes so concurrent saves cannot interleave
# (each save is temp-file + os.replace, atomic per write).
_CONFIG_WRITE_LOCK = threading.Lock()

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
    except (OSError, ValueError):
        pass
    return settings


# In-memory settings snapshot, loaded once at startup. The restart endpoint
# re-runs service.sh so a bind/token change takes effect on the fresh process.
SETTINGS = _load_settings()


def _atomic_write_json(path, data):
    """Persist `data` as JSON to `path` atomically (M7).

    Writes to a temp file in the same directory, fsyncs, then
    os.replace()s it over the target so readers never observe a
    partially-written file. The temp file is opened with O_NOFOLLOW so a
    symlink planted at the temp name cannot redirect the write; the
    replace itself swaps the target name outright (a pre-existing symlink
    at the target is replaced, not followed). Writes are serialized by
    _CONFIG_WRITE_LOCK. Raises on failure; callers turn exceptions into
    JSON error responses."""
    path = Path(path)
    os.makedirs(str(path.parent), exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    tmp = os.path.join(str(path.parent),
                       ".{}.{}.tmp".format(path.name, os.getpid()))
    with _CONFIG_WRITE_LOCK:
        fd = os.open(tmp, flags, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, str(path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _save_settings():
    """Atomically persist the in-memory SETTINGS snapshot."""
    _atomic_write_json(SETTINGS_FILE, SETTINGS)


# QMI band-apply client (QRTR transport). Resolved relative to the module
# dir like DIAG_DIR; falls back to /data/local/tmp/qmi_band for dev use.
# The module zip ships the static binary at qmi/qmi_band.
QMI_BIN = Path(__file__).parent.parent / "qmi" / "qmi_band"
if not QMI_BIN.exists():
    QMI_BIN = Path("/data/local/tmp/qmi_band")
QMI_GET_TIMEOUT = 5
QMI_SET_TIMEOUT = 8

# Fallback config file for persistence
CONFIG_FILE = MODDIR / "config" / "bands.json"

# Export dir for WebView-compatible config export (the Manager WebView
# drops blob downloads, so export writes a timestamped file on-device).
EXPORT_DIR = MODDIR / "config"

# Band-camping sampler (findings 5c): append `timestamp,earfcn,band` CSV
# lines every BAND_CAMPING_INTERVAL seconds so a band force can be
# validated offline (does the modem ever camp on a banned band?).
BAND_CAMPING_LOG = MODDIR / "config" / "band_camping.log"
BAND_CAMPING_INTERVAL = 5

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

_ALL_BANDS = {
    "lte": ["1","2","3","4","5","7","8","12","13","14","17","20","25","26","28","29","30","38","40","41","42","43","48","66","71"],
    "nr": ["1","2","3","5","7","8","20","25","28","38","40","41","66","71","77","78","79"],
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
    """"rogers" when any SIM slot's operator numeric is Rogers/Fido
    (302720), else "other" (including empty/missing values).

    `gsm.operator.numeric` is comma-joined per SIM slot (e.g. "302720,"
    or "302720,302220"), so normalize by splitting on ","."""
    if not mccmnc:
        return "other"
    slots = [s.strip() for s in str(mccmnc).split(",")]
    return "rogers" if ROGERS_MCCMNC in slots else "other"


def defaults_for_carrier(carrier):
    """Default {"lte": [...], "nr": [...]} for a carrier: the curated
    Rogers whitelist, or unrestricted all-bands for anything else.
    Returns fresh copies so callers can stamp extra keys."""
    src = _ROGERS_BANDS if carrier == "rogers" else _ALL_BANDS
    return {"lte": list(src["lte"]), "nr": list(src["nr"])}


def _run_qmi(args, timeout):
    """Run the QMI band client; return (returncode, combined output).

    Returns (None, "") when the binary is missing, not executable (e.g.
    the exec bit was lost during install), or the call times out - callers
    treat that as "QMI unavailable" and fall back. Catching OSError keeps
    a PermissionError from killing the single-threaded HTTP server.
    """
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
    empty). NR bands are the union of the SA and NSA lists. Returns None
    when no band lists could be parsed (transport unavailable).
    """
    lte = sa = nsa = None
    for line in output.splitlines():
        m = re.match(r"\s*(LTE|NR5G SA|NR5G NSA) bands:\s*(.*)$", line)
        if not m:
            continue
        tag, rest = m.group(1), m.group(2).strip()
        bands = [] if rest in ("", "(none)") else rest.split()
        if tag == "LTE":
            lte = bands
        elif tag == "NR5G SA":
            sa = bands
        else:
            nsa = bands
    if lte is None:
        return None
    nr = sorted(set(sa or []) | set(nsa or []))
    return {"lte": [str(b) for b in lte], "nr": [str(b) for b in nr]}


def _cmd_available(service, subcommand):
    """True if `cmd <service> help` lists <subcommand>."""
    try:
        out = _run_cmd(["/system/bin/cmd", service, "help"], timeout=5)
        return subcommand in out
    except Exception:
        return False


def _parse_int(value):
    """Parse an int from a regex match group, or None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_signal(value):
    """True if a parsed signal metric is a real measurement (not a
    sentinel like 2147483647 or the legacy 99 unknown marker)."""
    return value is not None and value < _INVALID_SIGNAL and value != 99


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
        lte_level = _field_int(fields, r'level')

    # NR section: mNr=CellSignalStrengthNr:{ csiRsrp = .. ssRsrp = .. level = 0 }
    nr_rsrp = nr_rsrq = nr_level = None
    nm = re.search(r'mNr=CellSignalStrengthNr:\{\s*(.*?)\s*\}', sig_obj, re.S)
    if nm:
        fields = nm.group(1)
        nr_rsrp = _field_int(fields, r'ssRsrp')
        nr_rsrq = _field_int(fields, r'ssRsrq')
        nr_level = _field_int(fields, r'level')

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


def _parse_signal_strength(text):
    """Parse signal strength from `dumpsys telephony.registry` output.

    Tries the modern mSignalStrength=SignalStrength:{...} object format
    first (each line is one subscription; the first line carrying a valid
    measurement wins), then the legacy flat `SignalStrength: <ints>` list.
    Returns None when nothing parseable exists.
    """
    for line in text.splitlines():
        if 'mSignalStrength=' in line:
            parsed = _parse_signal_object(line.split('mSignalStrength=', 1)[1])
            if parsed:
                return parsed
    # Legacy flat list: `SignalStrength:` followed by ints (the object
    # format is `SignalStrength:{`, which this pattern cannot match).
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

    Uses the first top-level `mServiceState=` line (the current state;
    everything after is notify history). Handles the modern object format
    `mServiceState={mVoiceRegState=0(IN_SERVICE), ...}` and, as a
    best-effort, the legacy flat `ServiceState: <voice> <data> ...` form.
    Returns a dict with None for fields the build does not expose.
    """
    svc = None
    for line in text.splitlines():
        if line.lstrip().startswith('mServiceState='):
            svc = line.split('mServiceState=', 1)[1].strip()
            break
    if not svc:
        return None

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

        m = re.search(r'getRilDataRadioTechnology=(-?\d+)\(([A-Za-z]+)\)', svc)
        if not m:
            m = re.search(r'getRilVoiceRadioTechnology=(-?\d+)\(([A-Za-z]+)\)', svc)
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
    return reg


def _radio_reg_state():
    """Current mVoiceRegState label (e.g. IN_SERVICE, POWER_OFF) or None."""
    try:
        text = _run_dumpsys("telephony.registry")
        m = re.search(r'mVoiceRegState=-?\d+\(([A-Z_]+)\)', text)
        return m.group(1) if m else None
    except Exception:
        return None


def _wait_for_radio_state(expected, attempts=4, interval=1):
    """Poll until the radio registration state equals `expected`."""
    for _ in range(attempts):
        if _radio_reg_state() == expected:
            return True
        time.sleep(interval)
    return False


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
            res = fcntl.ioctl(fd, DIAG_IOCTL_QUERY_MD_PID, buf)
            if len(res) == 16:
                buf = res
            _, _, pid, _ = struct.unpack('<IIiI', bytes(buf))
            return pid if pid > 0 else None
        finally:
            os.close(fd)
    except Exception:
        return None


def _parse_band_camping(text):
    """Parse serving EARFCN and band from `dumpsys telephony.registry`.

    Prefers the LTE identity from an mCellInfo entry marked mRegistered=YES
    (the cell actually camped on — e.g. Rogers EARFCN 2050 reports
    mBands=[4] there, while the registered-identity block can carry a
    misleading mBands list). Falls back to the registered-identity block
    (mCellIdentity=CellIdentityLte). band is the first entry of the
    mBands list. Returns (None, None) when no LTE cell identity with an
    EARFCN is present (radio off, or camped on another RAT).
    """
    def _earfcn_band(identity):
        em = re.search(r'mEarfcn=(\d+)', identity)
        if not em:
            return None
        earfcn = _parse_int(em.group(1))
        band = None
        bm = re.search(r'mBands=\[?([0-9,\s]*)\]?', identity)
        if bm:
            bands = [int(x) for x in bm.group(1).split(',') if x.strip()]
            if bands:
                band = bands[0]
        return earfcn, band

    # 1) The camped-on cell: CellInfoLte entries marked mRegistered=YES.
    for m in re.finditer(
            r'CellInfoLte:\{mRegistered=YES[^}]*?CellIdentityLte:\{([^}]*)\}',
            text):
        hit = _earfcn_band(m.group(1))
        if hit:
            return hit
    # 2) Registered-identity block (mCellIdentity=CellIdentityLte).
    for m in re.finditer(r'mCellIdentity=CellIdentityLte:\s*\{([^}]*)\}', text):
        hit = _earfcn_band(m.group(1))
        if hit:
            return hit
    return None, None


def _band_camping_loop():
    """Background sampler (daemon): every BAND_CAMPING_INTERVAL seconds,
    dump telephony.registry, extract the serving EARFCN/band, and append
    a `timestamp,earfcn,band` CSV line to BAND_CAMPING_LOG. Lines are
    written only when an EARFCN is present; failures are logged and
    skipped, never fatal."""
    while True:
        try:
            os.makedirs(os.path.dirname(BAND_CAMPING_LOG), exist_ok=True)
            earfcn, band = _parse_band_camping(
                _run_dumpsys("telephony.registry"))
            if earfcn is not None:
                line = "{},{},{}\n".format(
                    int(time.time() * 1000), earfcn,
                    band if band is not None else "")
                with open(BAND_CAMPING_LOG, 'a') as f:
                    f.write(line)
        except Exception as e:
            print("Band camping sample failed: {}".format(e))
        time.sleep(BAND_CAMPING_INTERVAL)


class BandHandler(http.server.BaseHTTPRequestHandler):
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
        """True when the client connected via a loopback address."""
        host = self.client_address[0]
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def _auth_required(self, action):
        """LAN auth gate (batch contract + bootstrap amendment).

        A bearer token is required iff LAN is enabled AND the client is
        not loopback — except GET /api/settings, which returns no token
        material and must stay readable so a fresh laptop page can learn
        that a token is required before it can store one."""
        if SETTINGS["bind"] != "0.0.0.0":
            return False
        if self._is_loopback():
            return False
        if action == "settings" and self.command == "GET":
            return False
        return True

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

    def do_OPTIONS(self):
        # CORS preflight for cross-origin POSTs from the KernelSU WebUI
        # origin (ksu://webui/bandctl/ or appassets://...) and LAN-mode
        # browser clients. Authorization must be allowed so a laptop page
        # can send the bearer token on POST /api/settings. Preflights
        # carry no Authorization header, so OPTIONS stays ungated.
        self.send_response(204)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, Authorization')
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
                self.send_json({"error": "unknown action"})
                return

            query = self.path.split('?', 1)[1] if '?' in self.path else ''
            action = ''
            for part in query.split('&'):
                if part.startswith('action='):
                    action = part.split('=', 1)[1]
                    break

            if self._auth_required(action):
                auth_error = self._check_auth()
                if auth_error is not None:
                    self.send_json(auth_error, status=401)
                    return

            if action == 'read':
                self.send_json(self.read_config())
            elif action == 'defaults':
                self.send_json(self.read_defaults())
            elif action == 'settings':
                if self.command == 'POST':
                    self.send_json(self.update_settings(self._read_json_body()))
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
                self.send_json(self.read_signal())
            elif action == 'registration':
                self.send_json(self.read_registration())
            elif action == 'health':
                self.send_json(self.modem_health())
            elif action == 'modem-reset':
                self.send_json(self.modem_reset())
            elif action == 'band-camping':
                limit = 50
                if '?' in self.path:
                    for part in self.path.split('?', 1)[1].split('&'):
                        if part.startswith('limit='):
                            try:
                                limit = max(1, int(part.split('=', 1)[1]))
                            except (TypeError, ValueError):
                                pass
                self.send_json(self.read_band_camping(limit))
            else:
                self.send_json({"error": "unknown action"})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)})

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
        are deduped preserving order, and LTE must be non-empty. Returns
        ({"lte": [...], "nr": [...]}, None) on success or (None, msg) on
        violation — never raises."""
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
        with no token set or when regenerate=true, and is returned ONLY
        when it was created/regenerated in this call (otherwise null).
        Settings are persisted atomically; a save failure is reported as
        ok:false."""
        if not isinstance(data, dict):
            return {"ok": False, "error": "request body must be a JSON object"}
        lan_enabled = data.get("lan_enabled")
        if not isinstance(lan_enabled, bool):
            return {"ok": False, "error": "lan_enabled must be a bool"}
        regenerate = data.get("regenerate") is True

        created = False
        if (lan_enabled and SETTINGS["token"] is None) or regenerate:
            SETTINGS["token"] = secrets.token_urlsafe(24)
            created = True
        SETTINGS["bind"] = "0.0.0.0" if lan_enabled else "127.0.0.1"
        try:
            _save_settings()
        except Exception as e:
            return {"ok": False, "error": "settings save failed: {}".format(e)}
        return {
            "ok": True,
            "lan_enabled": lan_enabled,
            "token_required": SETTINGS["token"] is not None,
            "token": SETTINGS["token"] if created else None,
        }

    def restart_service(self):
        """POST /api/restart — schedule a detached module-service restart.

        service.sh kills the old server and starts a fresh one; it is
        launched ~1s later from a background thread so the {"ok": true}
        response is delivered first. Returns immediately."""
        def _do_restart():
            time.sleep(1)
            try:
                subprocess.Popen(["sh", str(MODDIR / "service.sh")],
                                 start_new_session=True,
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except Exception as e:
                print("Service restart failed: {}".format(e))
        threading.Thread(target=_do_restart, daemon=True).start()
        return {"ok": True}

    def read_defaults(self):
        """Carrier-aware default band lists (GET /api/defaults).

        Carrier is detected from `getprop gsm.operator.numeric` (Rogers
        and Fido share 302720); the operator name comes from
        `getprop gsm.operator.alpha`. Never raises: on any failure the
        response degrades to carrier "other" with unrestricted defaults.
        """
        try:
            mccmnc = (_get_prop("gsm.operator.numeric") or "").strip(" ,") or None
            carrier = carrier_for_mccmnc(mccmnc)
            operator = (_get_prop("gsm.operator.alpha") or "").strip(" ,") or None
        except Exception as e:
            print(f"Carrier detection failed: {e}")
            mccmnc, carrier, operator = None, "other", None
        defaults = defaults_for_carrier(carrier)
        return {
            "carrier": carrier,
            "mccmnc": mccmnc or None,
            "operator": operator,
            "lte": defaults["lte"],
            "nr": defaults["nr"],
        }

    def read_config(self):
        """Read band configuration: QMI (QRTR) first, then diag, then the
        fallback config file."""
        qmi_cfg = self._read_qmi_config()
        if qmi_cfg is not None:
            return qmi_cfg
        try:
            # Try direct diag read next
            bands = read_bands(DIAG_DEVICE)
            return {
                "lte": [str(b) for b in bands['lte_bands']],
                "nr": [str(b) for b in bands['nr_bands']],
                "source": "diag"
            }
        except Exception as e:
            # Fallback to config file
            print(f"Diag read failed: {e}, trying config file")
            return self._read_config_file()

    def _read_qmi_config(self):
        """Read bands via the QMI client; None when QMI is unavailable."""
        _rc, out = _run_qmi(["--get"], QMI_GET_TIMEOUT)
        parsed = _parse_qmi_get(out)
        if parsed is None:
            return None
        parsed["source"] = "qmi"
        return parsed

    def _read_config_file(self):
        """Read from fallback config file."""
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r') as f:
                    data = json.load(f)
                    data['source'] = 'config_file'
                    return data
        except Exception as e:
            print(f"Config file read error: {e}")

        # Default: carrier-aware. Rogers devices (MCC/MNC 302720) fall
        # back to the curated whitelist; all other carriers get the
        # unrestricted all-bands defaults.
        defaults = defaults_for_carrier(
            carrier_for_mccmnc(_get_prop("gsm.operator.numeric")))
        defaults["source"] = "default"
        return defaults

    def write_config(self, data):
        """Write band configuration to the modem: QMI (QRTR) first, then
        diag. The config file is always mirrored so intent persists."""
        bands, err = self._validate_bands(data)
        if bands is None:
            return {"ok": False, "error": err}
        ok, source, error = self._apply_bands(bands["lte"], bands["nr"])
        # Mirror intent regardless of outcome (matches historical
        # behavior: the file is saved on success AND on failure).
        self._save_config_file(bands)
        if ok:
            return {"ok": True, "source": source}
        return {"ok": False, "error": error}

    def _apply_bands(self, lte_bands, nr_bands):
        """Apply bands to the modem: QMI (QRTR) first, then diag.

        Returns (ok, source, error) with error None on success. QMI
        success means the client printed `result: status=0`; diag success
        means write_bands() returned True. Never raises: QMI failure
        falls through to diag, and any diag exception is reported as
        (False, "diag", str(e)).
        """
        # QMI write first (real band apply via QRTR)
        if self._write_qmi_config(lte_bands, nr_bands):
            return True, "qmi", None

        # Fallback: diag write
        try:
            if write_bands(lte_bands, nr_bands, DIAG_DEVICE):
                return True, "diag", None
            return False, "diag", "diag write failed"
        except Exception as e:
            return False, "diag", str(e)

    def boot_apply(self):
        """Re-apply the persisted band config (config/bands.json) at boot.

        Reads the config file written by /api/write and runs it through
        the same QMI->diag apply chain. The config file is the source of
        truth here - it is NEVER rewritten. A missing or unreadable config
        is a skip ({"ok": true, "skipped": true}); malformed JSON,
        invalid bands (each must be an int 1..79, LTE non-empty), or an
        apply failure return ok:false with a short error. Never raises -
        any exception becomes {"ok": false, "error": ...}.
        """
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

    def _write_qmi_config(self, lte_bands, nr_bands):
        """Apply bands via the QMI client. True on QMI status 0 - the client
        prints `result: status=0`; the exit code alone is not sufficient
        (it returns 0 as long as the QMI exchange completed)."""
        lte_csv = ",".join(str(b) for b in lte_bands)
        nr_csv = ",".join(str(b) for b in nr_bands)
        rc, out = _run_qmi(["--set", lte_csv, nr_csv], QMI_SET_TIMEOUT)
        if rc is None or not out:
            return False
        return re.search(r"result: status=0", out) is not None

    def _save_config_file(self, data):
        """Save config to file as fallback (atomic temp+replace, M7)."""
        try:
            _atomic_write_json(CONFIG_FILE, data)
        except Exception as e:
            print(f"Config save error: {e}")

    def export_config(self, data):
        """Write a submitted band config to a timestamped JSON file in the
        config dir. WebView-compatible export: the Manager WebView drops
        blob downloads, so the server delivers the file on-device and the
        UI toasts the path. Returns {"ok": true, "path": ...} or
        {"ok": false, "error": ...} - never raises."""
        try:
            os.makedirs(EXPORT_DIR, exist_ok=True)
            stamp = time.strftime('%Y%m%d-%H%M%S') + '.{:03d}'.format(
                int(time.time() * 1000) % 1000)
            path = os.path.join(EXPORT_DIR, "bandctl-export-{}.json".format(stamp))
            with open(path, 'w') as f:
                json.dump(data, f, indent=2)
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def read_signal(self):
        """Read current signal strength from `dumpsys telephony.registry`.

        Parses the mSignalStrength block (object format on modern builds,
        flat list on legacy ones). Missing/unparseable metrics become null;
        the response includes a timestamp. Returns {"error": ...} only when
        dumpsys fails or no signal data exists.
        """
        try:
            text = _run_dumpsys("telephony.registry")
        except Exception as e:
            return {"error": f"dumpsys failed: {e}"}
        parsed = _parse_signal_strength(text)
        if parsed is None:
            return {"error": "no signal strength data in dumpsys telephony.registry"}
        parsed["timestamp"] = int(time.time() * 1000)
        return parsed

    def read_registration(self):
        """Read registration state from `dumpsys telephony.registry`.

        Parses the first top-level mServiceState block. Missing fields are
        null; the response includes a timestamp. Returns {"error": ...}
        only when dumpsys fails or no service state exists.
        """
        try:
            text = _run_dumpsys("telephony.registry")
        except Exception as e:
            return {"error": f"dumpsys failed: {e}"}
        parsed = _parse_registration(text)
        if parsed is None:
            return {"error": "no service state data in dumpsys telephony.registry"}
        parsed["timestamp"] = int(time.time() * 1000)
        return parsed

    def read_band_camping(self, limit=50):
        """Return the last `limit` lines of the band camping log as JSON.

        Each CSV line is `timestamp,earfcn,band` (epoch ms ints; band
        empty when the mBands list was absent). Never raises - an absent
        or unreadable log yields an empty sample list with ok:true.
        """
        try:
            if not os.path.exists(BAND_CAMPING_LOG):
                return {"ok": True, "samples": [], "log": BAND_CAMPING_LOG}
            with open(BAND_CAMPING_LOG, 'r') as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            samples = []
            for ln in lines[-limit:]:
                parts = ln.split(',')
                sample = {"timestamp": _parse_int(parts[0]) if parts else None}
                if len(parts) > 1:
                    sample["earfcn"] = _parse_int(parts[1])
                if len(parts) > 2:
                    sample["band"] = _parse_int(parts[2]) if parts[2] else None
                samples.append(sample)
            return {"ok": True, "samples": samples, "log": BAND_CAMPING_LOG}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def modem_reset(self):
        """Soft modem reset.

        Preferred: `cmd phone radio power` off/on (3s apart), used only
        when `cmd phone help` actually lists the subcommand. Fallback:
        airplane-mode toggle (`cmd connectivity airplane-mode` enable, 3s,
        disable). The radio is verified to reach POWER_OFF before success
        is reported; otherwise an honest ok:false is returned.
        """
        # Preferred mechanism: cmd phone radio power (only if listed in help)
        if _cmd_available("phone", "radio power"):
            try:
                _run_cmd(["/system/bin/cmd", "phone", "radio", "power", "off"], timeout=10)
                time.sleep(3)
                if _wait_for_radio_state("POWER_OFF"):
                    _run_cmd(["/system/bin/cmd", "phone", "radio", "power", "on"], timeout=10)
                    return {"ok": True}
                _run_cmd(["/system/bin/cmd", "phone", "radio", "power", "on"], timeout=10)
                return {"ok": False, "error": "radio power off did not take effect"}
            except Exception as e:
                print(f"Modem reset via radio power failed: {e}")

        # Fallback: airplane-mode toggle
        if _cmd_available("connectivity", "airplane-mode"):
            try:
                _run_cmd(["/system/bin/cmd", "connectivity", "airplane-mode", "enable"], timeout=10)
                time.sleep(3)
                if _wait_for_radio_state("POWER_OFF"):
                    _run_cmd(["/system/bin/cmd", "connectivity", "airplane-mode", "disable"], timeout=10)
                    return {"ok": True}
                # Cleanup: make sure airplane mode is not left on.
                _run_cmd(["/system/bin/cmd", "connectivity", "airplane-mode", "disable"], timeout=10)
                return {"ok": False, "error": "airplane-mode toggle did not power off the radio"}
            except Exception as e:
                print(f"Modem reset via airplane mode failed: {e}")
                return {"ok": False, "error": f"modem reset failed: {e}"}

        return {"ok": False, "error": "modem reset unavailable on this build"}

    def modem_health(self):
        """Get modem health status. Never raises - always returns a
        JSON-ready dict.

        status is "ok" when a transport reads bands, "degraded" when the
        transport answers but no bands are configured, and "error" when
        nothing is readable (message included). transport is "qmi" when
        the QRTR client works, "diag" otherwise.
        """
        rc, out = _run_qmi(["--get"], QMI_GET_TIMEOUT)
        parsed = _parse_qmi_get(out) if rc is not None else None
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
                "error": str(e),
            }

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
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

    # Threaded: the 2s signal/registration polling (each dumpsys call takes
    # seconds on this device) must not starve other requests — a single-
    # threaded server stalls /api/defaults, /api/read, and button actions
    # behind an endless polling queue.
    # Bind comes from settings.json (default 127.0.0.1; 0.0.0.0 = LAN).
    bind = SETTINGS.get("bind", "127.0.0.1")
    server = http.server.ThreadingHTTPServer((bind, PORT), BandHandler)
    print(f"Band Controller server running on http://{bind}:{PORT}")
    print(f"QMI binary: {QMI_BIN}")
    print(f"Diag device: {DIAG_DEVICE or 'disabled'}")
    server.serve_forever()
