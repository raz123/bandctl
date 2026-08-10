# Band Controller audit

Audit captured 2026-08-06 for the v2.6 white mobile skin. This is an
investigation record only; no fixes are implied by this file.

Baseline when captured:

- Branch: `codex/white-mobile-skin`, HEAD `55b0e2d` (`v2.6`)
- Existing uncommitted work preserved: `diag/protocol.py`, `module.prop`,
  `bandctl-v2.6.1.zip`, and `test_diag_protocol.py`
- Local validation previously completed: 68 Python tests passed; the v2.6.1
  ZIP passed `unzip -t`

## Findings

### A-01 — Released v2.6 diag parser ABI mismatch (P0)

The v2.6 release calls `parse_nv_read_response(response, nv_id, slot)` and
the write equivalent, while the tagged v2.6 protocol functions accepted only
`response`. On a diag-only device, health/read/write fail with a Python
argument-count error. A compatible signature is present only in the
uncommitted v2.6.1 worktree change.

Evidence: `diag/diag_client.py:370,379`; tagged `v2.6` versus current
`diag/protocol.py:143,186`.

### A-02 — Transport errors are presented as healthy/connected (P1)

`/api/health`, `/api/signal`, and `/api/registration` return JSON errors with
HTTP 200. The frontend checks `r.ok` but not the JSON error/status payload, so
the header and modem strip can remain green while diagnostics are unavailable.

Evidence: `web/server.py:1430,1447,1600,1610`; `web/index.html:1250-1267,
1391-1430`.

### A-03 — Registration values truncate critical state on mobile (P1)

The three-column registration grid ellipsizes values such as
`POWER_OFF`, `IN_SERVICE`, `IWLAN`, and `Home`, making the actual modem state
unreadable on the phone.

Evidence: `web/index.html:825-829`; reproduced in the phone screenshot.

### A-04 — LAN mode cannot work from a remote device (P0)

The page hardcodes every API request to `http://127.0.0.1:8080`. Opening the
page from a laptop via the phone's LAN address sends API calls to the laptop's
own loopback instead of the phone.

Evidence: `web/index.html:1113`; documented remote-LAN promise in `README.md:82`.

### A-05 — Sticky header/navigation scrolls away (P1)

The mobile shell has `overflow:hidden`, so the sticky header and tab bar use
the wrong scroll container and disappear while the long Bands/Settings pages
scroll.

Evidence: `web/index.html:688,697,716-717`; reproduced with browser scroll
measurements.

### A-06 — Diagnostics chart label collides with its top tick (P2)

The chart draws `dBm` at y=9 while the top tick is drawn at y=15, causing
visible overlap.

Evidence: `web/index.html:1545,1577`; reproduced in the diagnostics screenshot.

### A-07 — Config Summary says “Applied” for merely staged changes (P1)

The Settings label is static, while band toggles update the staged counts
without updating the status label. A changed selection is therefore presented
as already applied.

Evidence: `web/index.html:1012,1226,2029-2035`; reproduced by toggling a band
and opening Settings.

### A-08 — Reset-to-defaults is clobbered by Refresh (P1)

`resetDefaults()` changes the selection but does not set `userTouched`. A
subsequent Refresh treats it as untouched and replaces it with the modem
configuration. Reproduction: reset showed 25 LTE / 17 NR; Refresh restored
13 / 10.

Evidence: `web/index.html:1245-1259,1321-1333`.

### A-09 — Imported config is clobbered by Refresh (P1)

`onImportFile()` calls `setBands()` but never sets `userTouched`, unlike the
manual band-toggle path. Refresh can therefore erase an imported selection
before it is applied.

Evidence: `web/index.html:1222-1228,1245-1259,1764-1780`.

### A-10 — Import accepts invalid/out-of-catalog values (P1)

Import silently filters malformed strings, accepts out-of-range numeric values,
allows empty LTE, and reports success. Unknown values do not render as tiles;
Save later rejects them server-side.

Evidence: `web/index.html:1772-1780`; stricter server validation is at
`web/server.py:1153-1172`.

### A-11 — Boot service has false readiness gates (P1)

`wait_server()` accepts any HTTP 200 health response, including
`{"status":"error"}`. `wait_radio()` accepts any registration response whose
`service_state` is not `POWER_OFF`; an error payload has no state (`None`) and
is treated as ready. The boot apply can therefore run before the radio is
usable.

Evidence: `service.sh:70-110`; error payload producer at
`web/server.py:1437-1452`.

### A-12 — Diag transport silently drops high-numbered bands (P1)

The diag bitmask helper only packs bands 1-64. LTE B66/B71 and NR B77/B78
become zero, while a successful NV write still returns success. A direct
round-trip check produced an empty list for each of those bands.

Evidence: `diag/protocol.py:232-245`; `diag/diag_client.py:407-420`.

### A-13 — QMI transport silently drops LTE B66/B71 (P1)

The QMI client defines an LTE extension TLV but never sends it. Its LTE mask
builder and response printer only handle bands 1-64. The UI exposes B66/B71,
and non-Rogers defaults include them, but QMI success can apply a different
set than the UI requested.

Evidence: `qmi/qmi_band.c:71-73,112-130,137-148,239-248`; UI catalog at
`web/index.html:1138-1145`.

### A-14 — QMI non-success responses can be parsed as valid reads (P1)

The server ignores the QMI process status and the client's `result: status`
field. If a failed QMI response contains mask TLVs, `_parse_qmi_get()` still
returns them as a `source: qmi` configuration. A simulated `status=1 FAILURE`
response was accepted as band data.

Evidence: `web/server.py:547-571,1276-1283`.

### A-15 — Partial diag reads are reported as authoritative empty configs (P1)

`DiagClient.get_band_config()` converts a failed or malformed NV read to an
empty list without an error marker. `read_config()` then labels that result
`source: diag` instead of falling back to the persisted config file.

Evidence: `diag/diag_client.py:388-405`; `web/server.py:1257-1274`.

### A-16 — Save can report success without saving persistence (P1)

`_save_config_file()` catches and prints disk errors. `write_config()` then
returns `{"ok":true}` if the modem write succeeded, even though boot-time
reapply will have no saved configuration. A mocked disk-full run reproduced
`Config save error: disk full` followed by `{'ok': True, 'source': 'qmi'}`.

Evidence: `web/server.py:1304-1315,1395-1400`.

### A-17 — LAN-setting failures are shown as successful changes (P1)

The backend mutates in-memory settings before persistence and returns an error
only after the save fails. The UI's `toggleLan()` and `regenerateToken()` check
HTTP status but not `d.ok`, then render the error payload and show success.

Evidence: `web/server.py:1199-1207`; `web/index.html:1972-2003`.

### A-18 — White-theme transitions reference an undefined easing token (P2)

The white skin uses `var(--ease)` repeatedly, but no `--ease` custom property is
defined. Browser computed styles collapse those transitions to the default
`all` behavior, so intended tab, tile, button, switch, and icon motion is lost.

Evidence: `web/index.html:741,745,766,785,797,878-888`; browser computed-style
check returned `transition: all`.

### A-19 — Repository screenshots/documentation still show the old dark skin (P3)

`README.md` still embeds `bands.png`, `diag.png`, and `settings.png`, which are
dark v2.x screenshots while the shipped v2.6 UI is white. This makes the
release documentation disagree with the actual module skin.

Evidence: `README.md:37`; `bands.png`, `diag.png`, `settings.png`.

### A-20 — Modem reset can report success while the radio stays off (P1)

The preferred `cmd phone radio power` path waits for `POWER_OFF`, sends the
power-on command, and immediately returns `ok: true`. It never waits for a
post-on state or checks that the modem is usable again. `_run_cmd()` also does
not check the command's exit status. A deterministic mock run returned
`{'ok': True}` after exactly one state poll (`POWER_OFF`), even when the mock
provided no evidence that the `on` command restored the radio.

Evidence: `web/server.py:459-467,1506-1524`; reset tests at
`test_server.py:580-603` cover the command sequence but not post-on recovery.

### A-21 — QMI discovery can accept a stale response as the current endpoint (P1)

`probe_raw()` uses `recv()` and accepts any datagram with a QMI response type
and minimum length. It does not validate the sender node/port, transaction
ID, or message ID. `find_nas()` probes multiple endpoints with the same
transaction and message ID, then treats a status-zero response as proof that
the endpoint currently being probed is NAS. A late response from an earlier
probe can therefore select the wrong endpoint, causing later reads/writes to
time out or target the wrong service.

Evidence: `qmi/qmi_band.c:273-314,627-652`. The stricter
`recv_response()` immediately below already demonstrates the missing checks:
`qmi/qmi_band.c:322-359`.

### A-22 — Boot-time cleanup kills unrelated `server.py` processes (P2)

`service.sh` kills every process matched by `pgrep -f "server.py"`, not just
the module's own PID or a PID recorded at launch. Another Python service,
developer process, or test command containing that argument can be terminated
on module start/restart. This is an unnecessarily broad process-wide side
effect in a privileged boot script.

Evidence: `service.sh:51-53`.

### A-23 — The dirty-selection guard never clears after Save (P1)

Once a band is toggled or a preset is loaded, `userTouched` becomes true. A
successful Save & Apply updates the UI with the confirmation read but never
sets that flag back to false. Every later Refresh from modem therefore takes
the guarded branch and keeps the stale in-memory selection instead of showing
the modem's current bands. Reset to defaults has the same lingering-dirty
state. The Refresh button is effectively disabled for the rest of the page
session unless the page is reloaded.

Evidence: `web/index.html:1222-1228,1245-1259,1283-1308,1704-1712`; `rg`
found no `userTouched = false` assignment after initialization.

### A-24 — Restart confirmation can certify the old server (P1)

The restart API returns immediately and launches `service.sh` only after a
one-second background delay. The frontend starts its health check immediately
after receiving that response; the old server is still alive and can answer
the first health request, so the UI reports “Server restarted” before any
restart occurred. If the detached service launch fails, the old process can
still make the same false-success path pass.

Evidence: `web/server.py:1215-1231`; `web/index.html:2005-2025`.

### A-25 — One polling endpoint failure stops every monitor (P1)

`pollSignal()` and `pollRegistration()` both call the global `serverDown()` on
their own request failure. `serverDown()` clears the signal, registration,
and band-camping timers together and requires a manual Retry. A transient
failure in one diagnostic endpoint therefore freezes healthy monitoring from
the other endpoints and the camping sampler.

Evidence: `web/index.html:1335-1363,1389-1419`.

### A-26 — Loopback mode exposes write APIs to arbitrary browser origins (P1)

When LAN mode is disabled, every client bypasses authentication, including
cross-origin browser requests. The server also returns wildcard CORS headers
and allows JSON POST preflights. A webpage opened in a browser on the same
device/ADB-forwarded laptop can therefore issue authenticated-by-location
requests to band write, modem reset, export, or settings APIs without a
token. A live preflight returned `204` with `Access-Control-Allow-Origin: *`.

Evidence: `web/server.py:982-995,1038-1050,1610-1616`; live `OPTIONS
/api/write?action=write` probe on 2026-08-06.

### A-27 — Reduced-motion mode removes all state-change feedback (P2)

The global reduced-motion rule forces every transition to `0.01ms` and
removes every animation. This includes the tab transition, active-band
feedback, button/switch movement, and toast motion; it provides no intentional
alternative for hierarchy or state change. The accessibility preference is
treated as a blanket visual kill switch rather than a calmer motion design.

Evidence: `web/index.html:640-645,741-766,785-797,878-889`.

### A-28 — Band-camping history grows forever and is reread in full (P2)

The daemon appends a sample every five seconds without rotation or a retention
limit. Each `/api/band-camping` request then reads the entire log into memory
before slicing the last `limit` rows; the UI polls that endpoint every five
seconds. Long-running use therefore creates unbounded disk growth and an
increasing CPU/memory cost on every refresh even though the UI only displays
five samples.

Evidence: `web/server.py:920-939,1481-1502`; frontend polling at
`web/index.html:1127-1129,1478-1492`.

### A-29 — Primary teal controls fail WCAG text contrast (P1)

The white text used by the primary buttons sits on `#079eb3`, which measures
only 3.21:1 against white. The button text is 13px, so it does not qualify as
large text and fails WCAG 1.4.3's 4.5:1 AA threshold. The same accent is also
used for small status/tag text on light surfaces, so the issue is not limited
to the Save & Apply button.

Evidence: `web/index.html:658,797-802`; measured contrast `#fff` on
`#079eb3` = `3.21:1`.

### A-30 — Band camping has no NR/5G serving-cell path (P1)

The parser only searches `CellInfoLte` / `CellIdentityLte` and only extracts
`mEarfcn`. A plausible NR-only `CellInfoNr` dump with `mNrarfcn` and B78
returns `(None, None)`, which the UI renders as “No LTE cell” / `—` even when
the phone is camped on 5G. The application exposes NR band controls but its
live camping validation cannot report NR camping.

Evidence: `web/server.py:881-917`; UI fallback at
`web/index.html:1467-1476` and NR controls at `web/index.html:959-960`.

### A-31 — Dynamic success/error feedback is invisible to assistive technology (P2)

The status panel, toast notifications, and signal canvas have no `role`,
`aria-live`, accessible name, or text alternative. Save failures, token-copy
results, modem-reset results, and changing signal information are therefore
communicated visually but are not reliably announced to a screen reader.

Evidence: `web/index.html:969,979,1078`; updates at
`web/index.html:2068-2080`.

### A-32 — Diag response timeout has no overall deadline (P1)

`read_response()` enters an unbounded loop and gives each subsequent
`_timed_read()` a fresh full timeout. A stream that keeps delivering partial
bytes before each timeout can prevent the function from ever returning, even
though the caller supplied a finite timeout. On the phone this can hold an
HTTP worker and a modem operation indefinitely.

Evidence: `diag/diag_client.py:331-354`; the loop has no monotonic deadline or
maximum buffered-frame size.

### A-33 — Diag fallback writes LTE and NR non-atomically (P1)

The diag path writes the LTE NV item first and the NR NV item second. If the
first echo succeeds and the second fails, `set_band_config()` returns false
but leaves the modem with a mixed old/new configuration. The server then
mirrors the requested full config to disk, so the UI reports a failed apply
while boot re-apply and the persisted intent describe a state the modem does
not currently have. A simulated client produced two writes, with LTE success
followed by NR failure, and returned `False`.

Evidence: `diag/diag_client.py:407-420`; fallback dispatch at
`web/server.py:1327-1337`.

### A-34 — Slow telephony polls can pile up concurrent dumpsys processes (P2)

The UI starts signal and registration polls every two seconds, while each
server-side `dumpsys telephony.registry` call has a five-second timeout. There
is no in-flight guard per endpoint, so a slow device can accumulate multiple
overlapping `dumpsys` subprocesses and HTTP worker threads. This increases
load precisely when diagnostics are already slow and can amplify the global
polling shutdown described in A-25.

Evidence: `web/index.html:1127-1129,1335-1348`; `web/server.py:470-472`.

### A-35 — Browser storage failure can abort page initialization (P1)

`getPresets()` reads `localStorage` before entering its `try` block, and
`savePresets()` has no exception handling at all. `init()` calls
`renderPresets()` before the first modem read, polling startup, and the
settings/defaults fetches. A WebView with blocked storage, a disabled origin,
or a full quota can therefore stop initialization before the app loads any
radio state; a quota error while saving a preset closes the modal without a
result or recovery message.

Evidence: `web/index.html:1658-1666,1694-1702,2142-2151`.

### A-36 — Save confirmation can overwrite a successful selection with stale or invalid data (P1)

After `/api/write` reports success, `saveBands()` immediately reads
`/api/read` and applies the response whenever the HTTP status is successful.
It does not check the JSON `ok` field, validate the two arrays, wait for the
modem to settle, or preserve the just-saved selection. A normal HTTP-200 JSON
error can therefore turn the grid into empty sets, while a lagging modem read
can revert the UI to the previous configuration immediately after the user
was told the apply succeeded.

Evidence: `web/index.html:1283-1308`; the server's exception path also returns
JSON errors with HTTP 200 at `web/server.py:1052-1110`.

### A-37 — Boot wait loops can block far longer than their documented limits (P2)

The service comments promise approximately 60 seconds for the HTTP wait and
180 seconds for the radio wait, but each attempt can consume a separate
10-second `urlopen` timeout. Under repeated timeouts, `wait_server()` can take
up to `30 × (10 + 2) = 360` seconds and `wait_radio()` up to
`60 × (10 + 3) = 780` seconds. A stuck endpoint can therefore hold the boot
service for six to thirteen minutes before the best-effort path gives up.

Evidence: `service.sh:70-106`.

### A-38 — Service startup failures are hidden and logged as success (P1)

The background Python server's stdout and stderr are redirected to
`/dev/null`. If the health wait fails because Python crashed, the port is
occupied, or the server never became ready, the script only records “boot-
apply skipped”; it then unconditionally appends “server started” and exits
with status 0. The module can be dead with no captured traceback and no
nonzero result for a service supervisor to act on.

Evidence: `service.sh:61-62,70-85,123-129`.

### A-39 — Access-token and preset-name inputs have no programmatic labels (P2)

The visible “Access token” label targets the token display `<span>` rather
than `lan-token-input`, and the token-entry and preset-name inputs rely only
on placeholders. Assistive technology therefore receives no reliable name
for either control, and the prompt text disappears as soon as the user
types.

Evidence: `web/index.html:1044-1055,1081-1088`.

### A-40 — Modal dialogs declare `aria-modal` without managing focus (P2)

`openModal()` only adds a CSS class and `closeModal()` only removes it. The
reset dialog never moves focus into the dialog, closing a dialog does not
restore focus to its trigger, and the Escape handler closes the overlay
without any focus restoration. `aria-modal="true"` therefore describes a
keyboard interaction the implementation does not enforce, leaving focus on
background controls for keyboard and screen-reader users.

Evidence: `web/index.html:1081-1102,1628-1634,2084-2089`.

### A-41 — LAN/token/restart actions are not serialized (P1)

The settings controls remain active while their requests are pending. Two
rapid token regenerations can mutate the shared `SETTINGS` object twice and
return responses out of order; the browser stores whichever token response
arrives last, which can leave it holding a token that is no longer the
persisted one. Repeated restart taps likewise schedule multiple detached
`service.sh` runs, each able to kill and replace the server.

Evidence: `web/index.html:1972-2025`; shared settings mutation and persistence
at `web/server.py:1183-1213` and restart scheduling at `web/server.py:1215-1230`.

### A-42 — Fallback configuration is trusted without band validation (P1)

When QMI and diag reads are unavailable, `_read_config_file()` returns any
syntactically valid JSON object after merely adding `source="config_file"`.
It does not enforce list types, the 1–79 range, deduplication, or a non-empty
LTE list. The frontend then treats values such as an empty LTE list or an
out-of-catalog band as the live modem configuration, while the separate
boot-apply path rejects the same file. UI state and boot behavior can thus
disagree after a stale, hand-edited, or damaged config file is encountered.

Evidence: `web/server.py:1260-1274,1285-1292`; validation exists only in
`web/server.py:1128-1159` and the boot-apply path.

### A-43 — Drop snapshots have no retention policy (P2)

With drop logging enabled, the watchdog creates a file for every drop episode
and additional snapshot files during long episodes. `_drop_log_files()` only
limits the filenames returned to the UI; it never deletes old files. The
config partition can therefore grow indefinitely during repeated radio
drops, even though the interface shows only the newest 20 snapshots.

Evidence: `web/server.py:347-399,405-412`.

### A-44 — Failed or partial QMI reads are accepted as authoritative (P1)

The QMI `--get` path does not carry its result status through to the server:
`send_get()` returns success after printing any response, regardless of the
QMI result TLV. The Python reader then ignores the subprocess return code,
and `_parse_qmi_get()` considers an LTE line sufficient even when both NR
lines are missing. A nonzero/partial QMI response can therefore be labelled
`source="qmi"`, suppress the diag/config fallback, and present an incomplete
band list as the live modem state. A synthetic nonzero QMI return with only
`LTE bands: 1 3` reproduced `{'lte': ['1', '3'], 'nr': [], 'source': 'qmi'}`.

Evidence: `qmi/qmi_band.c:476-506`; `web/server.py:527-571,1276-1283,
1564-1579`.

### A-45 — Band writes can execute concurrently and leave modem/config state racing (P1)

The Save button has only a visual loading class; the backend does not
serialize `write_config()` or `_apply_bands()`. The atomic file lock protects
the final JSON write, but not the QMI transaction or the QMI-to-diag fallback
chain. Two clients, or a keyboard/programmatic re-entry while a save is
pending, can therefore apply different band sets at the same time. The final
modem state and the persisted intent can be decided by different completion
orders. A two-thread probe reached two simultaneous `_apply_bands()` calls.

Evidence: `web/index.html:1283-1318`; `web/server.py:1304-1337`.

### A-46 — Airplane-mode cleanup can report success when verification is unavailable (P1)

The reset fallback treats `persist.radio.airplane_mode_on` as the sole source
of truth. `_get_prop()` returns an empty string when `getprop` is unavailable,
and `_airplane_on()` maps that empty/unsupported value to `False`. Consequently
`_disable_airplane()` returns success even if the disable command failed and
the device remains in airplane mode. A probe with a failed disable command
and an unavailable property returned `True`; the modem-reset path can then
report `ok:true` after the radio-off check while connectivity is still
disabled.

Evidence: `web/server.py:583-606,1528-1544`.

### A-47 — Modern registration labels with punctuation are dropped (P1)

The modern registration parser requires the network label inside the
technology parentheses to match `[A-Za-z]+`. Valid Android labels such as
`LTE_CA` (underscore) and `HSPA+` (plus) therefore fail to match, leaving
`network_type` as `null` even while the modem reports `IN_SERVICE`. The UI
then renders a neutral/empty network type instead of the active technology.
Synthetic fixtures for both labels reproduced `network_type: None`.

Evidence: `web/server.py:757-805`, especially the two network-type regexes at
`web/server.py:787-791`.

### A-48 — Service startup can wait forever for boot-complete (P1)

Before selecting a Python runtime or starting the server, `service.sh` loops
until `getprop sys.boot_completed` is exactly `1`. There is no timeout,
maximum retry count, or diagnostic log. If the property remains empty or
stuck at another value, the module never starts its server and provides no
useful failure signal. This is independent of the bounded waits that occur
after the server has already been launched.

Evidence: `service.sh:25-28`.

### A-49 — Disabling LAN access removes auth before the socket is rebound (P1)

`update_settings()` changes the live `SETTINGS["bind"]` value immediately,
but the `ThreadingHTTPServer` socket is created only once at process startup;
the UI separately tells the user to restart. If LAN mode is currently bound
to `0.0.0.0` and the user disables it, the old process remains reachable on
the LAN while `_auth_required()` now sees `127.0.0.1` and stops requiring the
bearer token. Until restart, remote callers can reach the write APIs without
authentication. A probe reproduced `bind=127.0.0.1` plus
`remote auth required: False` immediately after a successful disable, before
any server restart.

Evidence: `web/server.py:982-995,1183-1213,1648-1650`; the restart requirement
is exposed by `web/index.html:1972-1985`.

### A-50 — Diagnostics always report the first SIM/phone (P1)

Android's telephony registry dump can contain separate `Phone Id` blocks for
multiple active modems/SIMs, but both parsers scan the entire dump and return
the first matching record. The API and UI provide no phone or subscription
selector. With SIM 0 out of service at `-125 dBm` and SIM 1 in service at
`-85 dBm`, a synthetic Android-style dump produced SIM 0's registration,
operator, and signal values. A dual-SIM user can therefore see Offline/poor
diagnostics while the active data SIM is healthy.

Evidence: `web/server.py:722-742,757-771`; Android's per-phone registry dump
shape is defined by `TelephonyRegistry`.

### A-51 — Modem reset requests are not serialized (P1)

`ThreadingHTTPServer` can dispatch multiple `/api/modem-reset` requests at
once, but `modem_reset()` has no lock or in-flight guard. Two simultaneous
calls can interleave their power transitions, return success independently,
and let the first caller finish while the second reset is still changing the
radio. A two-thread probe forced the overlap and recorded `off, off, on, on`
with two concurrent radio-state waits; both requests returned `ok:true`.

Evidence: `web/server.py:1506-1553,1644-1650`; the frontend's loading class is
only visual and does not prevent another client or programmatic request
(`web/index.html:1632-1654`).

### A-52 — LAN bearer authentication is sent over plaintext HTTP (P1)

LAN mode protects write endpoints with a bearer token, but the server is a
plain `http.server.ThreadingHTTPServer` and the page hardcodes an `http://`
API base. There is no TLS configuration or certificate boundary. Anyone able
to observe the LAN can capture the token from an `Authorization` header and
replay it against the modem-control endpoints. A live request trace showed
the bearer header transmitted as readable HTTP bytes.

Evidence: `web/index.html:1107-1124`; `web/server.py:982-1017,1648-1651`.

### A-53 — Drop logging treats telemetry failure as recovery (P2)

When `_drop_state()` cannot run or parse `dumpsys`, it returns `None`. The
watchdog maps that to `state=None` and enters the normal/recovered branch. If
an episode was already open, one transient telemetry failure closes it and
writes a `RECOVERED` marker even though the radio may still be out of service;
the next failed-state poll starts a new episode and loses the true duration.
A controlled sequence of `OUT_OF_SERVICE` followed by `None` reproduced a
false recovery marker.

Evidence: `web/server.py:316-321,347-402`, especially the `state` decision at
`web/server.py:362-365` and the recovery branch at `web/server.py:385-399`.

### A-54 — Concurrent exports can overwrite one another (P2)

Export filenames are derived only from wall-clock seconds plus milliseconds,
with no unique suffix, exclusive-create flag, or write lock. Two threaded
export requests in the same millisecond therefore receive the same successful
path and the later write silently replaces the earlier configuration. A
two-thread probe returned `ok:true` for both payloads but left only one file,
containing the second payload.

Evidence: `web/server.py:1402-1417`; the threaded request model is created at
`web/server.py:1644-1650`.

### A-55 — The mobile shell hard-clips viewports below 320px (P2)

The white skin sets both `html` and `body` to a hard `min-width: 320px`.
At a 280px viewport the document reports a 320px layout width, so the
right-hand connection label, Settings tab, chart readout, and other controls
are outside the visible screen rather than reflowing. This breaks compact
split-screen/WebView widths and becomes easier to hit when accessibility text
scaling reduces the effective CSS viewport. A browser probe at 280px
reproduced the clipped screenshot and `body.scrollWidth=320`.

Evidence: `web/index.html:673-680`; reproduced with the local app at a
280x700 viewport.

### A-56 — Short diag writes are reported as accepted (P1)

`DiagClient.send_command()` calls `os.write()` once and returns `True` for
any non-error result; it never checks that the returned byte count equals the
full `[data type][HDLC frame]` length and never retries a short write. A
contract probe that made `os.write()` report one byte for an 18-byte frame
still returned `accepted=True`. The following read/write operation can then
wait for a response to a truncated command or operate on stale data.

Evidence: `diag/diag_client.py:167-185`; synthetic short-write probe returned
`{'accepted': True, 'requested_bytes': 18, 'kernel_reported_write': 1}`.

### A-57 — Malformed QMI TLVs can crash the privileged helper (P1)

`response_status()` checks only that a three-byte TLV header fits before
reading the result status and error fields. It does not verify that
`off + 3 + len <= n`, so a matching QMI response with a truncated result TLV
causes an out-of-bounds read. `parse_response()` has the same short-length
problem for result and mode TLVs. An AddressSanitizer harness passed an
11-byte response whose result TLV claimed four payload bytes; the helper
reported a stack-buffer-overflow at `qmi_band.c:370`.

Impact: a malformed modem/QRTR response can terminate the root-owned QMI
process during endpoint discovery or a band operation, turning a transient
transport fault into a hard apply/read failure.

Evidence: `qmi/qmi_band.c:172-193,316-375`; ASan reproduction reached
`response_status()` at `qmi/qmi_band.c:370`.

### A-58 — Carrier-detection failure silently selects unsafe unrestricted defaults (P1)

`_get_prop()` converts `getprop` failures into an empty string, and
`read_defaults()` interprets an empty/unparseable operator as carrier
`other`, whose defaults are the unrestricted all-band lists. The frontend
accepts that HTTP-200 response as a successful carrier lookup, and
`resetDefaults()` only checks that the returned list is non-empty. During a
boot, radio drop, or property-read failure on a Rogers device, Reset can
therefore stage bands that the curated Rogers list intentionally excludes,
including B7 and B66.

A backend probe with the operator property unavailable returned
`carrier=other`, 25 LTE bands, and 17 NR bands, including B7/B66/B71. The
Rogers safety rule is documented in `README.md:95`.

Evidence: `web/server.py:475-478,442-460,1233-1255`; frontend acceptance at
`web/index.html:1321-1331,2052-2065`.

### A-59 — Long drop episodes orphan their earlier snapshots (P2)

After `DROP_SNAP_GAP`, the watchdog replaces `episode_file` with a new
timestamped path and writes another `DROP DETECTED` snapshot, but it never
closes the previous file with a recovery marker. On recovery, only the most
recent path receives `RECOVERED`, so one continuous outage appears as multiple
episodes and the first snapshot has no duration or recovery state.

A controlled watchdog sequence at t=0, t=61s, and t=62s created two files;
the first contained only `DROP DETECTED`, while the second contained the
single `RECOVERED (duration 62s)` marker.

Evidence: `web/server.py:253,367-399`; controlled drop-loop probe reproduced
the two-file, one-recovery-marker result.

### A-60 — Incomplete HTTP requests can pin worker threads indefinitely (P2)

The LAN-capable server uses `ThreadingHTTPServer` without a socket/request
timeout, and `BandHandler` does not set one during connection setup. A client
that sends an incomplete request line or body can therefore keep its handler
thread blocked in the standard-library reader; each connection consumes a new
daemon worker and there is no concurrency limit. A local probe left three
`process_request_thread` workers blocked after three partial requests and
reported `server_timeout=None` after 250ms.

Impact: when LAN mode is enabled, a handful of slow or abandoned clients can
exhaust threads/file descriptors and make band control unavailable without
ever reaching the authentication gate.

Evidence: `web/server.py:942-1050,1644-1650`; partial-request probe reproduced
three live request workers with no server timeout.

### A-61 — A non-NV diagnostic notification is returned as the NV response (P1)

`_scan_stream()` correctly recognizes NV frames by payload byte `0x3D`/`0x3E`,
but it also saves the first successfully decoded payload and returns that
fallback when the current user-space block contains no NV frame. `read_response()`
accepts that fallback without checking its type, and `read_nv()` then returns
`None` from the parser without continuing to scan the stream. A controlled
probe placed a valid non-NV notification before a valid NV frame: the NV read
returned the notification payload and made only one timed read, leaving the
actual NV frame unread.

Impact: an ordinary diagnostic notification interleaved before a band response
can make a read appear to fail or attribute the wrong payload to the operation;
the next valid response is stranded in the stream for a later command.

Evidence: `diag/diag_client.py:289-354,356-370`; controlled mixed-notification
probe reproduced `returned_payload=b'\\x01notification'` and
`nv_frame_was_read=False`.

### A-62 — Modem reset clears a pre-existing airplane-mode choice (P1)

The airplane-mode fallback always enables airplane mode, waits, and then
unconditionally calls `_disable_airplane()`. It never records whether airplane
mode was already enabled before the reset. A controlled probe started with
airplane mode on, forced the fallback path, and observed `ok:true` with the
final airplane-mode state off.

Impact: resetting the modem while the user intentionally has airplane mode on
silently turns connectivity back on, changing a user-controlled device state.

Evidence: `web/server.py:1506-1553`; controlled probe recorded
`initial_airplane_on=True`, `result={'ok': True}`, and
`final_airplane_on=False`.

### A-63 — Failed drop-log persistence leaves the new setting live (P2)

`update_drop_log()` mutates `SETTINGS["drop_log"]` before calling
`_save_settings()`. If the write fails, the handler returns `ok:false` but does
not restore the old in-memory value. A controlled probe with `_save_settings()`
raising `OSError('disk full')` returned the error while leaving
`drop_log=True` in the live settings snapshot.

Impact: the watchdog can start or stop logging despite the API reporting a
failed change; the behavior then reverts on process restart because disk state
was never updated.

Evidence: `web/server.py:1466-1479`; controlled failed-save probe returned
`{'ok': False, 'error': 'disk full'}` with `live_drop_log_after_failed_save=True`.

### A-64 — QMI discovery can outlive the web server’s subprocess timeout (P2)

The web server kills `qmi_band --get` after 5 seconds and `--set` after 8
seconds. The helper’s `find_nas()` first waits up to 3 seconds for QRTR service
enumeration, then probes up to 64 discovered endpoints; if none works, it
sweeps ports 40–70. Each 600/400 ms probe is rounded by `time(NULL)` to an
approximately one-second wait, so the worst-case discovery path is roughly
`3 + 64 + 31 = 98` seconds before it can report that no NAS endpoint exists.

Impact: a modem whose NAS endpoint is not found early is terminated by the
caller before its own discovery completes, making QMI reads/writes fall back or
fail based on the caller timeout rather than the actual modem response.

Evidence: `web/server.py:228-229,527-545`; `qmi/qmi_band.c:300-305,608-673`.

### A-65 — Documented API routes fail without an undocumented `action` query (P2)

The README documents routes such as `GET /api/read`, `GET /api/defaults`, and
`GET /api/health`, but `handle_api()` dispatches only the value of the query
parameter `action`. A live probe of the documented bare URLs returned HTTP 200
with `{"error": "unknown action"}`; the same routes work only when the
frontend's extra `?action=...` suffix is present.

Impact: scripts, diagnostics, reverse proxies, and users following the API
documentation cannot call the advertised endpoints. The 200 response also
makes the contract failure look like a successful HTTP request to simple
clients.

Evidence: `README.md:44-57`; `web/server.py:1052-1077,1117-1118`; live
probes of `/api/read`, `/api/defaults`, `/api/settings`, `/api/restart`, and
`/api/health` without a query all returned `{"error":"unknown action"}`.

### A-66 — GET requests can invoke reset, restart, and boot-apply mutations (P1)

The dispatcher checks the method for settings and drop-log actions, but it
dispatches `restart`, `boot-apply`, and `modem-reset` without requiring POST.
A handler-level probe patched each method and sent a GET request; all three
methods were called and returned HTTP 200. The README promises POST for each of
these state-changing endpoints.

Impact: a link, prefetcher, retrying proxy, or local web page can trigger a
server restart, modem reset, or band re-apply simply by issuing a GET. This is
also unsafe for clients that assume GET is read-only and may repeat it.

Evidence: `README.md:48,51,55`; `web/server.py:1086-1091,1100-1101`; controlled
GET dispatch probe invoked `restart_service`, `modem_reset`, and `boot_apply`
with `called=True` for each.

### A-67 — Entering a LAN token does not retry the failed bootstrap reads (P1)

On a remote page with no stored token, `init()` starts `loadBands()`,
`fetchDefaults()`, and `fetchDropLog()` in parallel. Their protected calls
receive 401 and stop or leave fallback state. `saveLanToken()` stores the token,
hides the server-down banner, and starts the three polling timers, but it never
calls `loadBands()`, `fetchDefaults()`, or `fetchDropLog()` again.

Impact: after entering a valid token, the UI can say it is connected while the
band selection still shows the unrestricted/old fallback, the carrier summary
is stale, and drop-log state is stale until the user discovers a manual refresh
or reset action.

Evidence: `web/index.html:1902-1915,2116-2150`; source-level bootstrap trace
confirmed the post-token path resumes polling only and has no retry for the
three failed initial fetches.

### A-68 — A failed band apply is still promoted to boot-time source of truth (P2)

`write_config()` saves the normalized request to `config/bands.json` after
`_apply_bands()` returns, regardless of whether the modem write succeeded. It
returns `ok:false` to the caller, but `service.sh` later invokes `boot_apply()`
against that same file, and `boot_apply()` treats it as authoritative. A
controlled probe forced `_apply_bands()` to fail and observed the error response
alongside a newly written file containing the rejected bands.

Impact: a transient modem failure can be reported as “not applied” now, then
silently retried and applied at the next boot. The persisted desired state and
the last verified modem state are indistinguishable, which can surprise users
and reintroduce a band set that was intentionally rejected.

Evidence: `web/server.py:1304-1316,1339-1382`; `service.sh:64-119`; controlled
failed-apply probe returned `ok:false` while `bands.json` contained the
requested LTE/NR lists.

### A-69 — Manual reconnect does not refresh defaults, LAN state, or drop-log state (P2)

`retryConnect()` retries only `loadBands()`. Once that succeeds it restarts the
signal/registration/camping timers and clears the failure banner, but it never
re-runs `fetchDefaults()`, `fetchSettings()`, or `fetchDropLog()`. Those reads
are launched only once from `init()`.

Impact: after a server outage or a token bootstrap failure, clicking Retry can
make the app look connected while the carrier/default summary, LAN toggle and
token panel, and Debug drop-log toggle still show their pre-outage or fallback
state. The user must reload the page to resynchronize those panels.

Evidence: `web/index.html:1364-1386,1918-1928,1944-1953,2052-2065,2116-2150`;
source-level recovery trace shows `retryConnect()` calls `loadBands()` and
`startPolling()` only.

### A-70 — A 200/error defaults response can make Reset Defaults apply the fallback list (P1)

`fetchDefaults()` treats any HTTP-200 JSON as success. It updates the defaults
arrays only when the payload contains non-empty `lte`/`nr` arrays, stores the
error object as `carrierInfo` if that is what the server returned, and still
resolves `true`. `resetDefaults()` then sees a successful fetch and the
non-empty initial `DEFAULT_*` fallback arrays, so it calls `applyDefaults()`.

Impact: when `/api/defaults` returns an application error in an HTTP-200 body,
Reset Defaults can apply the unrestricted fallback bands instead of aborting as
its guard and user-facing message promise. On a carrier where the fallback is
unsafe, this can undo the curated carrier-specific restriction.

Evidence: `web/index.html:1321-1333,2052-2065`; deterministic control-flow trace
with `{error: ...}` and HTTP `200` leaves `defaultsLte/defaultsNr` at
`DEFAULT_*`, returns `true`, and reaches `applyDefaults()`.

### A-71 — GET `/api/write` can apply a modem band configuration (P1)

`BandHandler` routes both GET and POST `/api/*` requests into the same
dispatcher, and the `write` branch has no POST-only check. A GET request with a
JSON body therefore reaches `write_config()` and can run the QMI/diag modem
write path. A handler-level probe sent `GET /api/write?action=write` with
`{"lte":[1],"nr":[]}` and observed HTTP 200 plus a direct call to
`write_config()` with that payload.

Impact: a state-changing modem operation violates the documented POST-only API
contract and can be triggered by clients, retrying middleware, or tooling that
assumes GET is safe and repeatable. A-66 covers the other mutation routes; this
entry records the direct band-apply consequence of the same missing method
guard.

Evidence: `README.md:46`; `web/server.py:943-951,1088-1093`; controlled GET
probe invoked `write_config()` with the supplied LTE/NR lists.

### A-72 — Mixed-SIM carrier detection can select the wrong safety defaults (P1)

`carrier_for_mccmnc()` labels the device as Rogers when **any** comma-separated
SIM slot contains `302720`. `read_defaults()` then returns the Rogers curated
band whitelist without identifying which subscription is active for data or
the modem being controlled. A deterministic probe of
`carrier_for_mccmnc("302720,310260")` returned `"rogers"` in either slot
order.

Impact: on a dual-SIM/eSIM device with Rogers in one slot and another carrier
currently active, Reset Defaults can apply Rogers-specific exclusions to the
other carrier. Required bands can be removed, so the UI’s apparently safe
carrier defaults can instead cause loss of service or an unexpected band
configuration.

Evidence: `web/server.py:483-500,1233-1254`; controlled mixed-SIM probe returned
the Rogers defaults for `302720,310260` and `310260,302720`.

### A-73 — LAN auth misclassifies most IPv4 loopback addresses (P2)

`_is_loopback()` accepts only the literal addresses `127.0.0.1` and
`::ffff:127.0.0.1` (plus `::1`). It rejects valid IPv4 loopback addresses such
as `127.0.0.2` and `127.42.0.9`, even though the whole `127.0.0.0/8` range is
loopback. With LAN mode enabled, a local phone process connecting through one
of those addresses is treated as a remote client and receives a token gate,
contradicting the documented “the phone itself never needs the token” behavior.

Evidence: `web/server.py:977-995`; handler probe returned `False` for
`127.0.0.2` and `127.42.0.9` while returning `True` for `127.0.0.1`.

### A-74 — Band-camping API can never serialize its success response (P1)

`read_band_camping()` places the `pathlib.Path` object `BAND_CAMPING_LOG`
directly in the success payload. `send_json()` then calls `json.dumps()` on
that payload, which raises `TypeError` before the response is written; the
outer handler converts it into an HTTP-200 `ok:false` error. A live handler
probe returned exactly `Object of type PosixPath is not JSON serializable`.

Impact: the Diagnostics tab’s advertised Band camping history and live
serving-cell readout are permanently blank, even when the sampler has written
valid samples. The frontend checks only the HTTP status, renders an empty list
for the error payload, and deliberately suppresses the exception, so the user
gets no indication that this feature is broken.

Evidence: `web/server.py:1481-1504,1610-1619`; `web/index.html:1478-1492`;
controlled `GET /api/band-camping?action=band-camping` probe returned HTTP 200
with `ok:false` and the PosixPath serialization error.

### A-75 — A malformed diagnostics length can grow the reader buffer without a limit (P2)

`DiagClient._scan_stream()` trusts the 32-bit `item_len` field and retains the
entire buffer when the advertised frame is incomplete. `read_response()` then
appends every subsequent device read to that buffer indefinitely; there is no
maximum stream or frame size and no discard/resynchronization path for an
impossible length. A synthetic `0x20 / num_data=1 / item_len=0xffffffff`
header was retained unchanged, confirming the growth path.

Impact: a corrupted or unexpected `/dev/diag` stream can make the privileged
server accumulate memory until the process is killed or the phone experiences
memory pressure. This is separate from the normal read timeout: each short
chunk can arrive before the timeout and keep extending the same buffer.

Evidence: `diag/diag_client.py:302-329,331-352`; controlled `_scan_stream()`
probe retained the 12-byte malformed header and returned no bounded-error
state.

### A-76 — Several mobile controls are below a usable touch target (P2)

The white mobile skin sets `.btn.small` to `min-height:36px`, `.icon-btn` to
`36px`, and the custom LAN switches to only `45px × 26px`. This applies to
token copy/share/show, token save, regenerate, restart, preset Load/Delete,
the Retry action, and both LAN/drop-log toggles. The token icon buttons are
also separated by only 5px.

Impact: on-device taps near the edges can miss or hit a neighboring action,
especially for the three adjacent token controls and the short switch. The
primary 48px actions meet the intended mobile scale, but the secondary and
destructive actions do not, making Settings harder to operate precisely one-
handed.

Evidence: `web/index.html:763,807,876-879,888,1038-1059,1065-1070,1671-1675`;
static mobile-target audit measured 36px button heights and a 26px switch
height against the 44px minimum used for the app’s full-size controls.

### A-77 — The sticky tab bar is positioned 12px inside the header (P2)

The mobile override makes the header approximately 90px tall: 22px top
padding + the 24px/1.05 title line + 7px gap + the 13px/1.2 subtitle line +
20px bottom padding. The tab bar still uses the hard-coded `--header-h:78px`
offset. Once the existing overflow/sticky issue is corrected, the tab bar will
stick at roughly 78px and overlap the lower part of the header by about 12px;
the mismatch is already present in the CSS geometry.

Impact: scrolling the long Diagnostics or Settings screens can produce a
stacked header where the tab bar covers the subtitle/connection area or leaves
an inconsistent gap, depending on font metrics and device width. The offset
must be derived from the actual white-theme header or kept in a shared layout
measurement.

Evidence: `web/index.html:648-671,696-718`; CSS-box calculation from the
mobile override gives ~89.8px for the header versus `--header-h:78px`.

### A-78 — Repeated diag timeouts leak blocked reader threads (P1)

`_timed_read()` starts a daemon thread around every blocking `os.read()` and
returns after the join timeout while that thread is still alive. The next-read
cleanup closes and reopens the file descriptor, but it never joins or otherwise
terminates the abandoned thread; `close()` also clears the only stored thread
reference. If the diag driver does not unblock a read when its descriptor is
closed, every retry leaves another blocked thread behind.

Impact: a modem or diag-driver failure that causes repeated reads to time out
can accumulate one permanent Python thread per attempt, eventually exhausting
thread/memory resources or preventing reliable diagnostics. A controlled probe
with a permanently blocking read produced three live orphan reader threads
after three timed-out reads.

Evidence: `diag/diag_client.py:149-157,188-226,228-263`; controlled
`_timed_read(0.01)` probe returned `None` three times and found
`orphan_reader_threads_before_release=3`.

### A-79 — White-theme success, warning, and error text fail small-text contrast (P2)

The white skin uses `#1b8d47` for success text, `#a96d09` for warning text,
and `#d93f37` for danger text on pale status backgrounds and white surfaces.
The measured contrast ratios are below WCAG AA for the small text actually
using them: success `4.24:1` on white and `3.80:1` on `#e9f6e8`, warning
`3.93:1` on `#fff4d6`, and danger `4.02:1` on `#fff0ee` (`4.45:1` on white).

Impact: the Registration chips, event-history tags, status banners, warning
readouts, and destructive controls can be difficult to read for users with
low vision, especially in the compact mobile UI. A-29 covers the separate
white-on-teal primary-control failure; this entry covers the remaining state
palette.

Evidence: `web/index.html:648-665,803-812,833-855`; deterministic sRGB
relative-luminance calculation against the rendered foreground/background
pairs.

### A-80 — HDLC decoder accepts an unterminated frame as valid (P2)

`hdlc_decode()` verifies the CRC but never records whether it encountered the
required trailing `0x7e` flag. If a frame ends immediately after the CRC, the
loop simply exits and the CRC check succeeds. A controlled probe showed that
`hdlc_decode(hdlc_encode(payload)[:-1])` returns the original payload instead
of rejecting the truncated frame.

Impact: a short or truncated diag item can be accepted as a complete NV
response when the bytes happen to include a valid CRC. The caller may then
act on a response that was never properly delimited, weakening the stream
parser’s protection against partial modem reads. This is distinct from the
stream buffering and timeout findings above.

Evidence: `diag/protocol.py:73-113`; controlled complete-versus-missing-final-
flag probe returned the same payload for both frames.

### A-81 — Registration history ignores data, roaming, and operator transitions (P2)

The registration chips render `service_state`, `data_state`, `network_type`,
`operator`, and `roaming`, but `checkRegChange()` stores and compares only
`service_state` and `network_type`. A data connection can therefore change
from `IN_SERVICE` to `OUT_OF_SERVICE`, or the phone can enter roaming or move
to another operator, without adding an event-history entry when the service
and network type stay the same. The chip visibly changes while the history
silently loses the transition.

Impact: Event History cannot be trusted to explain data-only outages, roaming
changes, or operator changes—the exact context needed to diagnose intermittent
connectivity. The initial and subsequent history messages also omit those
fields.

Evidence: `web/index.html:1431-1440` renders all five registration fields;
`web/index.html:1494-1507` builds `cur` with only `service` and `net` and uses
only those two keys for change detection.

### A-82 — On-device exports accumulate without retention or cleanup (P2)

The Manager-WebView export path writes every export to `EXPORT_DIR` using a
new timestamped filename, but there is no age/count limit, pruning step, or
delete/clear action. The settings UI can create these files repeatedly and
only reports the newest path. A controlled temp-directory probe made three
exports with distinct timestamps and left all three JSON files present.

Impact: repeated exports consume module/config storage indefinitely and leave
users with no supported way to remove stale copies or identify the current
one. On a device with limited writable module storage, this can eventually
turn an otherwise harmless export action into a storage-pressure failure.

Evidence: `web/server.py:1402-1415` creates a unique file under `EXPORT_DIR`
and never removes older exports; `web/index.html:1726-1748` exposes the
on-device path and has no cleanup path. Controlled probe result:
`results_ok=[True, True, True]`, `files_written=3`.

### A-83 — `viewport-fit=cover` is enabled without a top safe-area inset (P2)

The mobile document opts into edge-to-edge layout with
`viewport-fit=cover`, but the app only adds
`env(safe-area-inset-bottom)` to its bottom padding. The header starts at
`top: 0` with a fixed 22px top padding and no top inset. On a notched iPhone
or another browser that overlays content into the status-area cutout, the
title and connection indicator can be pushed under the system status bar.

Impact: the primary title, connection state, and the top of the sticky header
can be obscured or become difficult to tap on edge-to-edge mobile viewports.
The safe-area behavior is device-dependent, so it can pass laptop testing
while failing on the phone the UI targets.

Evidence: `web/index.html:5` declares `viewport-fit=cover`;
`web/index.html:674-704` provides bottom-only safe-area padding and a fixed
top-padded header with no `safe-area-inset-top` compensation.

### A-84 — API calls have no client-side deadline and can leave controls stuck (P1)

`apiFetch()` passes every request directly to `fetch()` without an
`AbortController`, timeout, or cancellation signal. The action handlers rely
on the request eventually resolving or rejecting to clear their loading state.
If the local server accepts a connection but stalls while a modem or dumpsys
operation is blocked, `Save & Apply`, Refresh, modem reset, token actions, and
other controls can remain in their in-progress state indefinitely. Polling
requests also remain pending instead of producing a recoverable error.

Impact: a transient server stall can make core tasks appear frozen until the
user reloads the entire WebView; the Retry path is not reached because a
hung `fetch()` does not enter the existing `catch`/`finally` paths.

Evidence: `web/index.html:1805-1814` contains the only API wrapper and has no
deadline/cancellation mechanism; `web/index.html:1245-1279,1283-1317,1632-1653`
clears action loading states only after the awaited request settles. This is
the client-side failure mode, distinct from the server/diag timeout findings.

### A-85 — Drop logging does not capture data-only outages (P2)

The Settings copy promises drop snapshots with registration, Wi-Fi, and data
context, but the watchdog decides whether a drop exists from
`reg.service_state` alone. `reg.data_state` is parsed and available, yet a
transition such as `service_state=IN_SERVICE` with
`data_state=OUT_OF_SERVICE` never enters the drop branch and produces no
snapshot. The data counters are collected only as context after a voice
registration drop has already been detected.

Impact: the diagnostic feature misses exactly the outages that can break
internet access while voice registration remains healthy, leaving no
server-side snapshot or recovery duration for those incidents.

Evidence: `README.md:59` and `web/index.html:1064-1072` describe drop/data
logging; `web/server.py:347-365` computes `state` from only
`service_state` and tests only `POWER_OFF`, `OUT_OF_SERVICE`, and
`EMERGENCY_ONLY`.

### A-86 — The ARIA tabset has no keyboard tab navigation (P2)

The three navigation buttons are exposed as `role="tab"` inside a
`role="tablist"`, but the keyboard handler implements no ArrowLeft,
ArrowRight, Home, or End behavior and does not manage the tab roving
`tabindex`. Keyboard users must tab through every control in the active panel
to reach another section; the standard tabset interaction of moving between
tabs with arrow keys is absent.

Impact: section navigation is slower and less predictable for keyboard and
switch-access users, and the ARIA roles promise a tab interaction model that
the implementation does not provide.

Evidence: `web/index.html:939-943` declares the ARIA tabset;
`web/index.html:1193-1201` updates selection only on hash changes and
`web/index.html:2103-2113` handles band keys, Escape, and preset Enter but no
tab navigation keys.

### A-87 — Legacy connected-data state is rendered as neutral instead of healthy (P2)

When a build exposes only the legacy `mDataConnectionState` field, the parser
maps numeric state `2` to `CONNECTED`. The Diagnostics UI, however, assigns
the healthy treatment only when `data_state === 'IN_SERVICE'`; `CONNECTED`
does not match that condition and is not in the bad-state map. The live Data
chip therefore shows a connected data session with neutral styling rather
than the green healthy state used by the equivalent modern label.

Impact: users can misread an active data connection as unknown or degraded,
and the visual state is inconsistent across Android dump formats for the same
radio condition.

Evidence: `web/server.py:825-833` maps `mDataConnectionState=2` to
`CONNECTED`; `web/index.html:1435-1436` marks data good only for
`IN_SERVICE`. Controlled fixture result:
`{'service_state': 'IN_SERVICE', 'data_state': 'CONNECTED'}`.

### A-88 — Diag fallback can leave the modem with a half-applied configuration (P1)

The diag writer updates the LTE and NR NV items as two independent writes and
returns only the combined boolean. If the first write succeeds and the second
fails, it does not roll the first write back or verify the final pair. The API
can therefore report `ok:false` while the modem is already running the new LTE
mask with the old NR mask (or the reverse).

Impact: on devices using the diag fallback, a transient second-write failure
leaves hardware in a mixed state that looks like a valid configuration on a
subsequent read. This is separate from persistence of the requested config:
the modem itself is already partially changed before the error is surfaced.

Evidence: `diag/diag_client.py:407-420` calls `write_nv()` for LTE and then NR
with no transaction, rollback, or read-back. Controlled probe with
`write_nv` returning `[True, False]` produced
`{'result': False, 'write_count': 2, 'nv_ids': [26664, 26950]}`.

### A-89 — The initial screen claims Connected and Applied before bootstrap completes (P2)

The first paint hardcodes the connection state as `Connected`, the live modem
state as `Connected`, the staged count as `0 LTE / 0 NR`, and the Settings
summary as `Applied`. `init()` then starts `loadBands()` and polling without
awaiting either request or putting the controls into an unknown/loading state.
During a slow or failed startup the user sees a healthy, applied modem before
the app has received any modem response; Save & Apply is already enabled and
can submit the empty initial selection.

Impact: startup presents false operational state and allows an avoidable
empty-band action while the authoritative configuration is still unknown.
The backend rejects an empty LTE list, but only after the user has taken the
action and received an error.

Evidence: `web/index.html:932-935,949-952,1011-1017` contains the optimistic
initial labels and counts; `web/index.html:2142-2150` calls `loadBands()` and
`startPolling()` asynchronously during the same init pass.

### A-90 — Server/API outages are recorded as radio DROP events (P2)

`serverDown()` uses the same `logDrop()` path as an observed cellular service
loss. Both signal and registration polling call `serverDown()` for any failed
HTTP request or parse, so a dead local server, expired LAN token, or transient
WebView/network error inserts a red `DROP` row such as `Server unreachable`
into Event History even though no radio registration state was observed.

Impact: Event History conflates application/transport failures with modem
service drops, which can send troubleshooting in the wrong direction and
make the drop history unreliable.

Evidence: `web/index.html:1356-1363` unconditionally calls
`logDrop('Server unreachable — ...')`; `web/index.html:1410-1418` routes
poll failures into `serverDown()`, while `web/index.html:1509-1518` renders
every `logDrop()` entry with the `DROP` tag.

### A-91 — Save confirmation can erase edits made while Apply is in flight (P1)

`saveBands()` snapshots the current LTE/NR sets and starts an asynchronous
write, but the band tiles remain interactive while that request is pending.
If the user changes a tile during a slow QMI/diag apply, the later
confirmation read unconditionally calls `applyConfig()` and replaces the
newer in-memory selection with the older configuration that was submitted.
There is no revision check, conflict message, or save-state lock on the band
grid.

Impact: a user can make a second, visible edit and then lose it silently when
the first apply completes. The screen offers no indication that the staged
selection was overwritten.

Evidence: `web/index.html:1222-1228` toggles bands without checking a save in
progress; `web/index.html:1283-1295` captures the request snapshot before
awaiting the write; `web/index.html:1301-1307` applies the confirmation result
unconditionally.

### A-92 — An in-flight poll can resurrect Connected after the app declares the server down (P2)

When one signal or registration request fails, `serverDown()` marks the app
offline and clears the interval timers, but it cannot cancel requests that
were already started. Any such request that resolves afterward still calls
`setConnectionState(true)`; neither polling handler checks
`serverDownReported` before publishing its success.

Impact: the header can switch back to green `Connected` while the red server
banner remains visible and polling is actually stopped. The user receives a
contradictory recovery signal and may retry or change bands under a false
healthy status.

Evidence: `web/index.html:1356-1362` stops timers and marks offline;
`web/index.html:1389-1418` publishes successful signal/registration results
after awaits without a generation/cancellation check; `web/index.html:1805-1813`
uses raw `fetch()` with no cancellation signal.

### A-93 — Disabling drop logging discards an active episode without recovery data (P2)

The drop logger is advertised as a live toggle. If the user disables it while
the radio is in an active drop, the next watchdog iteration immediately clears
`in_drop`, `drop_start`, and `episode_file`, then sleeps. That path bypasses the
normal recovery branch, so the existing snapshot is left without a
`RECOVERED` marker or duration. Re-enabling logging cannot reconstruct the
discarded episode.

Impact: turning off the diagnostic feature at the wrong moment silently loses
the duration and recovery boundary for an outage that was already captured.
The file remains on disk, which makes it look retained even though its record
is incomplete.

Evidence: `web/server.py:347-361` clears the active episode when
`SETTINGS["drop_log"]` becomes false; the only recovery write is the separate
branch at `web/server.py:384-399`. The setting is explicitly live-toggled by
`web/server.py:1470-1480` and `web/index.html:1954-1970`.

### A-94 — Event History is session-only despite being presented as history (P2)

`history-list` starts empty on every page load. `lastReg` is initialized to
`null`, and `logEntry()` only inserts DOM nodes; there is no API read, local
storage record, or server-side history source to restore those entries. The
200-entry limit only bounds the current DOM session.

Impact: leaving or reloading the mobile WebView erases the initial registration,
radio drops, and server-unreachable events that the user would need to explain
an intermittent outage. This is especially misleading because the separate
drop-log snapshots are persisted across reboots while the visible Event
History is not.

Evidence: `web/index.html:1003-1005` has an empty history container;
`web/index.html:1132,1171` initializes only an in-memory limit and `lastReg`;
`web/index.html:1494-1528` detects and renders events but contains no restore
or persistence path.

### A-95 — Bottom-aligned modals ignore the mobile bottom safe area (P2)

The document opts into edge-to-edge layout with `viewport-fit=cover`, and the
white skin bottom-aligns both in-page dialogs. Their overlay uses `inset: 0`
and a fixed `padding: 16px`; it never adds `env(safe-area-inset-bottom)`.
Unlike the body and toast, the modal action row therefore sits at a fixed
distance from the physical viewport edge.

Impact: on gesture-navigation phones or devices with a bottom cutout, the
Preset Save and Reset Modem buttons can sit inside the system gesture area,
making the primary action hard to tap or partially obscured. Laptop and
non-edge-to-edge previews will not reveal this failure.

Evidence: `web/index.html:5` enables `viewport-fit=cover`;
`web/index.html:896-899` adds the safe-area inset only to the toast and
defines `.modal-overlay` with fixed 16px padding; the modal buttons are at
`web/index.html:1086-1088,1097-1099`.

### A-96 — The service diagnostic log has no retention limit (P3)

`service.sh` appends to `config/bandctl.log` for every boot/service restart,
runtime-selection branch, boot-apply result, and startup skip. There is no
truncate, rotation, maximum size, or cleanup policy. The UI's Restart server
action invokes the same service script, so repeated testing can grow the file
without any user-visible bound.

Impact: on-device module/config storage can be consumed indefinitely by a
diagnostic log that users have no supported way to inspect or clear. This is
separate from the already tracked unbounded band-camping, drop-snapshot, and
export files.

Evidence: `service.sh:10` defines the persistent log and
`service.sh:36-48,119-127` appends to it; there is no retention operation in
the script.

### A-97 — Independent tabs or LAN clients can silently overwrite band changes (P1)

The UI posts the complete staged LTE/NR set on every Save & Apply, but neither
the read response nor the write request carries a revision, generation, or
compare-and-swap token. The server serializes file writes only; it does not
check whether the submitted configuration was read before another client
changed it.

Impact: two open Manager tabs, or the phone UI and a laptop using LAN mode,
can both show successful saves while the last response silently wins. A user
can therefore apply a carefully selected configuration in one client and have
it replaced by a stale selection from another client without a conflict or
warning. This is distinct from A-91's same-tab edit loss during one request.

Evidence: `web/index.html:1245-1317` snapshots and submits only the local band
sets; `web/server.py:1304-1316` validates and applies every request without a
configuration revision. An isolated two-handler probe returned `ok:true` for
both writes and left the second client's LTE/NR lists in `bands.json`.

### A-98 — Modem-reset verification rejects the supported legacy numeric state format (P2)

The general registration parser accepts both `mVoiceRegState=3(POWER_OFF)`
and the bare legacy form `mVoiceRegState=3`, mapping both to `POWER_OFF`.
Modem reset uses a different parser, `_radio_reg_state()`, whose regex only
matches the parenthesized form. On a legacy dump, the reset wait therefore
returns `None` for every attempt even when the radio is actually powered off.

Impact: the reset command can perform the transition and then report failure
(`radio power off did not take effect` or an airplane-toggle failure) on a
build the rest of the app claims to support. This creates a false failure and
can trigger unnecessary retries or manual intervention.

Evidence: `_parse_registration()` handles the bare numeric form at
`web/server.py:745-754,806-811`, while `_radio_reg_state()` requires
parentheses at `web/server.py:837-844`; reset verification calls it through
`web/server.py:1506-1553`. Synthetic input
`mServiceState={mVoiceRegState=3, mDataRegState=3}` parsed as
`service_state=POWER_OFF` but produced `_radio_reg_state()=None`.

### A-99 — NR diagnostics ignore valid CSI-RSRP/RSRQ measurements (P2)

The modern NR signal parser reads only `ssRsrp`, `ssRsrq`, and `level` from
`mNr`. It never examines the also-exposed `csiRsrp` or `csiRsrq` fields. When
the SS measurements are unavailable but CSI measurements are valid, the NR
candidate is discarded; with no valid LTE candidate the endpoint returns a
no-signal error, and with one it can display LTE while the primary technology
is NR.

Impact: the Diagnostics screen can show blank or wrong signal data during a
real 5G session, making band experiments and coverage troubleshooting
unreliable precisely when NR metrics are needed.

Evidence: `web/server.py:670-701` extracts only `ssRsrp`/`ssRsrq`, and
`web/server.py:617-620,722-742` discards the candidate when those values are
sentinels. A synthetic current-format dump with
`csiRsrp=-95`, `csiRsrq=-10`, and sentinel SS fields returned
`_parse_signal_strength(...) is None`.

### A-100 — A failed `/api/read` payload is treated as a successful modem load (P1)

`handle_api()` converts backend exceptions into HTTP 200 JSON errors, but
`loadBands()` checks only `r.ok` and then passes `d.lte` and `d.nr` directly
to `applyConfig()`. An error payload such as
`{"ok":false,"error":"diag unavailable"}` therefore takes the success
branch: both missing arrays become empty sets, the UI marks the connection
green, starts polling, and says `Loaded from modem` instead of preserving the
last known selection or showing the read failure. This is separate from A-36,
which covers the confirmation read after a successful Save.

Impact: a transient read/transport failure can erase the visible band
selection and make the user believe an empty configuration came from the
modem. Refresh and the initial bootstrap are both affected.

Evidence: `web/server.py:1052-1120` emits application errors with HTTP 200;
`web/index.html:1245-1267` checks only the HTTP response and never validates
`d.ok` or the two arrays. An isolated handler probe returned
`http_status=200`, `payload={'ok': False, 'error': 'diag unavailable'}`; the
frontend path would derive `lte=[]`, `nr=[]`, `connection=Connected`, and
`status=Loaded from modem`.

### A-101 — Saving a LAN token leaves the paste-token form visibly active (P2)

When no token is stored, `renderSettings()` makes `lan-token-entry` visible.
`saveLanToken()` stores the entered value, clears the input, and calls
`renderTokenDisplay()`, but neither function hides `lan-token-entry`. The
masked token/actions and the now-empty paste form therefore remain visible at
the same time. Because `showReAuth()` checks whether that entry is already
visible, a subsequent 401 also suppresses its `Token required` toast.

Impact: the Settings screen presents an authenticated-looking token row and
an empty credential form simultaneously, and an invalid token can fail
without a clear re-auth prompt. This is a UI state-transition defect, not an
authentication bypass.

Evidence: `web/index.html:1819-1841,1847-1858,1902-1915`; the static flow
probe found `saveLanToken_hides_entry=False` and
`renderTokenDisplay_hides_entry=False`.

### A-102 — A valid radio outage still paints the app green as “Connected” (P1)

`setConnectionState(true)` is called whenever the signal or registration
endpoint returns a successful HTTP response. It is not derived from the
registration state. A valid response with `service_state=OUT_OF_SERVICE`
therefore sets both the header and the Bands-tab `Modem` strip to green
`Connected`, while `updateChips()` simultaneously marks the Service chip red.

Impact: the primary connection indicator contradicts the actual radio state
during coverage loss, emergency-only mode, or powered-off transitions. Users
can read the green modem status as healthy even though the diagnostics below
say the phone is out of service. This is distinct from A-02's transport-error
case: the API response here is valid and correctly reports the outage.

Evidence: `web/index.html:949-952,1181-1188,1409-1417` updates the green
connection state from HTTP success; `web/index.html:1431-1436` marks
`OUT_OF_SERVICE` as bad. The isolated source probe found both
`radio_success_sets_connection_green=True` and
`radio_chip_marks_out_of_service_bad=True`.

### A-103 — A settings-read error silently hides the LAN recovery surface (P2)

`handle_api()` converts unexpected backend exceptions into HTTP 200 JSON with
`ok:false`, but `fetchSettings()` checks only the HTTP status and passes any
payload to `renderSettings()`. The renderer coerces a missing
`lan_enabled` field to `false`, unchecks the LAN switch, hides the entire
token section, and returns. A transient settings-read failure can therefore
make an enabled LAN server look disabled and remove the visible token/re-auth
controls needed to recover it.

Impact: the Settings screen can contradict the server state after a bootstrap
or refresh failure, and a user may not know where to enter the token or why
remote calls are failing. This is separate from A-17, which covers POST
settings failures being shown as successful changes.

Evidence: `web/server.py:1052-1120` emits application errors with HTTP 200;
`web/index.html:1819-1834,1917-1928` has no `d.ok` guard before assigning
`lanEnabled`. A controlled source probe found
`fetch_settings_checks_payload_ok=False`,
`render_settings_error_lan_enabled=True`, and
`render_settings_error_hides_section=True`.

### A-104 — A valid fallback read is reported as “Loaded from modem” (P2)

The read chain intentionally returns `source: "qmi"`, `"diag"`,
`"config_file"`, or `"default"`. `loadBands()` treats all four as the same
success path and hardcodes `Loaded from modem (...)` into the status message.
When QMI and diag are unavailable, the visible confirmation therefore says
the modem was read even though the arrays came from persisted intent or
carrier defaults.

Impact: users troubleshooting an unavailable transport can mistake a
fallback snapshot for verified modem state and assume a band selection is
already active. That weakens the safety boundary around the fallback behavior
covered by A-42 without changing the backend's source metadata.

Evidence: `web/server.py:1257-1302` defines the fallback sources;
`web/index.html:1245-1267` uses the same message for every source. A
controlled source-flow probe produced `Loaded from modem (config_file)` and
`Loaded from modem (default)` for the two fallback cases.

### A-105 — A persisted LAN bind without a token creates a remote lockout (P1)

`_load_settings()` accepts `bind: "0.0.0.0"` even when the token is missing
or invalid. For a non-loopback client, `_auth_required()` then gates every
request except `GET /api/settings`, while `_check_auth()` rejects every
request because `SETTINGS["token"]` is empty. The only recovery operation,
`POST /api/settings` to disable LAN or regenerate a token, is itself behind
that gate.

Impact: a legacy settings file, manual edit, or token-corruption recovery can
leave the server reachable enough to reveal `token_required` but impossible
to administer remotely. Recovery requires the phone itself, ADB, or direct
editing of the module settings file.

Evidence: `web/server.py:156-175` loads the invalid combination;
`web/server.py:985-1017` exempts only the read-only settings GET and rejects
the POST. A controlled probe loaded `{'bind': '0.0.0.0', 'token': None,
'drop_log': False}`, then returned
`remote_post_settings_gated=True` and
`remote_post_settings_without_token={'ok': False, 'error': 'unauthorized'}`.

### A-106 — Using Import or Reset permanently removes the button icon (P2)

Both the Import and Reset Modem buttons contain inline SVG icons. Their action
handlers save `btn.textContent`, replace the entire button contents with a
loading string, and restore the saved text by assigning `textContent` again.
That restoration replaces the original `<svg>` and `<span>` children with a
plain text node, so the icon is lost after either operation completes or
fails.

Impact: the white mobile skin progressively degrades after normal use: the
Import and Reset controls no longer match the rest of the icon-led action
language, and the reset affordance loses its visual cue on subsequent use.

Evidence: `web/index.html:1001,1030` defines the icon-bearing buttons;
`web/index.html:1634-1653,1766-1788` uses `btn.textContent` for both loading
and restoration. The isolated DOM-flow probe found `has_svg=True`,
`sets_text_content_during_action=True`, and `restores_via_text_content=True`
for both buttons.

### A-107 — The signal graph is unavailable to non-visual users (P2)

Diagnostics renders the entire signal history as a bare `<canvas>`. The
canvas has no accessible name, role, description, data table, or textual
history alternative. The nearby readouts expose only the latest RSRP/RSRQ
values, not the time-series trend that the graph is meant to communicate.

Impact: screen-reader users cannot inspect signal stability, drops, or
recovery over time, even though that trend is a core Diagnostics feature.
The current-value readouts do not provide an equivalent for the missing
history.

Evidence: `web/index.html:979` contains only `<canvas id="signal-canvas">`;
`web/index.html:1530-1625` draws the graph exclusively through the Canvas 2D
API. A static accessibility probe found no `aria-label`, `aria-labelledby`,
role, or text alternative associated with the canvas.

### A-108 — Drop logging misses `SERVICE_EMERGENCY` radio outages (P2)

The registration parser preserves the state label emitted by the modem,
including `SERVICE_EMERGENCY`. The UI explicitly classifies that state as
bad, but the server watchdog starts a drop episode only for `POWER_OFF`,
`OUT_OF_SERVICE`, and `EMERGENCY_ONLY`. A `SERVICE_EMERGENCY` transition is
therefore visible as a failure in Diagnostics while producing no server-side
snapshot or recovery duration.

Impact: a radio outage in emergency-service mode can be omitted from the
debug evidence the feature promises, making intermittent coverage failures
harder to correlate with call, Wi-Fi, counter, and radio-log context. This is
separate from A-85's data-only outage path.

Evidence: `web/server.py:745-754` parses the label; `web/server.py:347-365`
omits `SERVICE_EMERGENCY`; `web/index.html:1430-1436` marks it bad in the
UI. A synthetic dump parsed as `SERVICE_EMERGENCY`, with
`service_emergency_backend_drop=False` and
`service_emergency_ui_bad=True`.

### A-109 — Carrier-aware defaults can arrive after fallback and never replace it (P1)

Initialization starts `loadBands()` and `fetchDefaults()` in parallel. If the
band read fails first, `loadBands()` applies the client-side unrestricted
`DEFAULT_*` lists and stops polling. A later successful carrier-defaults
response only updates `defaultsLte`, `defaultsNr`, and the carrier label; it
does not reapply those lists to the active band selection. A Rogers device can
therefore remain staged on the fallback list, including the bands the curated
Rogers safety rule intentionally excludes, until the user manually resets or
reloads.

Impact: a transient read failure during bootstrap can leave a safety-sensitive
carrier on the wrong band set even though the authoritative carrier lookup
eventually succeeded. The screen presents the carrier information without
bringing the selection into agreement with it.

Evidence: `web/index.html:1269-1276` applies fallback defaults on a failed
read; `web/index.html:2052-2065` updates carrier defaults without applying
them; and `web/index.html:2142-2149` launches both requests concurrently. A
focused async-flow probe found the sequence `read failure → applyDefaults() →
carrier defaults resolve` leaves `lteEnabled/nrEnabled` unchanged.

### A-110 — The Retry control can launch concurrent recovery loops (P2)

The server-down Retry control is a plain button, while the loading CSS guard
only applies to `.btn.loading` or a natively disabled button. `retryConnect()`
adds the `loading` class but never disables the button and has no in-flight
guard. Repeated taps therefore start independent `loadBands()` calls, each
with its own three-attempt retry chain.

Impact: a user trying to recover a flaky server can create overlapping modem
reads and competing success/failure UI updates. The first successful chain
can resume polling while later chains are still mutating the same connection
banner and status area.

Evidence: `web/index.html:924` defines `#retry-btn` without the `.btn` class;
`web/index.html:800` scopes pointer blocking to `.btn`; and
`web/index.html:1364-1386` adds only `loading` while starting an unguarded
async attempt. A static control-flow probe found no native `disabled` write
or retry-in-flight guard.

### A-111 — The signal graph draws a continuous line across polling outages (P2)

`pollSignal()` appends a history point only after a successful response and
does not append a gap marker on failure. `serverDown()` stops the timers but
does not clear or mark `signalHistory`. `drawGraph()` resets its pen only for
an explicit `rsrp == null` point, then uses `lineTo()` for every later valid
point. A measurement received after an outage is therefore connected directly
to the last pre-outage measurement, regardless of the elapsed gap; the graph
also keeps its oldest retained point when calculating the time range.

Impact: Diagnostics can show a fabricated signal trajectory through a period
where no measurements existed, and stale pre-outage history can be presented
as current continuity after reconnection. This is separate from A-107's
accessibility gap: it affects the data representation for every user.

Evidence: `web/index.html:1356-1362` stops polling without touching the
history; `web/index.html:1389-1406` has no failure/gap sample; and
`web/index.html:1553-1555,1599-1611` retains the old range and connects all
valid points without elapsed-time gap handling.

### A-112 — Drop logging is blind to outages shorter than its 10-second sample interval (P2)

The drop watchdog polls registration at `DROP_POLL_INTERVAL = 10` seconds.
There is no event subscription or transition source between samples. A radio
drop that begins after one sample and recovers before the next never enters
`w.in_drop`, so it produces neither a snapshot nor a recovery-duration record.

Impact: brief intermittent failures are invisible to the feature advertised
as drop logging, even though the UI's faster registration poll may have shown
the state to the user. This leaves the most transient failures without the
correlation context the logger is meant to capture.

Evidence: `web/server.py:252` sets the 10-second interval;
`web/server.py:362-383,384-399` checks and records state only once per loop.
A timing probe with a drop entirely between two watchdog samples produces no
episode transition.

### A-113 — Reset Defaults can overwrite an edit made while its lookup is pending (P2)

`resetDefaults()` awaits `fetchDefaults()` before applying the result, but the
Reset button is not put into a loading/disabled state and the band grid stays
interactive. If the user changes a tile while the carrier lookup is in
flight, `toggleBand()` updates the selection and marks it touched; when the
lookup resolves, `resetDefaults()` unconditionally calls `applyDefaults()` and
silently replaces that newer edit.

Impact: a slow or temporarily busy phone can discard a user's most recent band
choice without warning. The action provides no pending state or conflict
message to explain why the visible selection changed.

Evidence: `web/index.html:1222-1228` keeps tile editing available;
`web/index.html:1321-1332` applies defaults after an awaited request with no
revision check; and the Reset control at `web/index.html:965` has no loading
guard. A focused control-flow probe confirmed an edit can set
`userTouched=true` during the await, but `applyDefaults()` still runs.

### A-114 — Telemetry polling never pauses when the mobile page is hidden (P2)

`startPolling()` installs signal and registration timers for the lifetime of
the page, plus a band-camping timer, and the only stop path is
`serverDown()`. Switching to Settings, backgrounding the WebView, or leaving
the page does not pause or cancel them; the code has no visibility/page-life
cycle handler. Diagnostics therefore continues issuing `/api/signal`,
`/api/registration`, and `/api/band-camping` work when none of that data is
visible.

Impact: a phone can keep spawning telephony dumps and polling the modem while
the app is backgrounded, increasing battery, CPU, and radio-service load. It
also compounds A-34's concurrent slow-poll problem during the exact period
when the user cannot see the resulting data.

Evidence: `web/index.html:1335-1354` creates and clears timers without a page
visibility condition; `web/index.html:1127-1129` sets their intervals; and
`web/index.html:2139-2140` registers only hash/resize lifecycle listeners. A
static lifecycle probe found no `visibilitychange`, `pagehide`, `blur`, or
`beforeunload` handler.

### A-115 — A drop episode is cleared before its recovery marker is written (P2)

On recovery, the drop watchdog copies the episode path and then clears
`w.in_drop`, `w.drop_start`, and `w.episode_file` before opening the file to
append the `RECOVERED` marker. If that append fails because the file was
removed, storage is full, or the directory is unavailable, the outer handler
only prints an error. The cleared episode is not restored and is never retried.

Impact: a disk/write fault at the recovery boundary leaves a partial snapshot
that looks like a captured drop but has no duration or recovery event. The
logger silently loses the exact evidence it was supposed to preserve.

Evidence: `web/server.py:384-399` clears all episode state before
`with open(path, 'a')`; `web/server.py:400-402` catches the failure without
reinstating that state. A focused ordering probe confirmed the clear happens
before the append and before the catchable outer error path.

### A-116 — Concurrent drop-log toggles can report a state that was not persisted (P2)

The drop-log checkbox remains interactive while its POST is in flight, and
`toggleDropLog()` has no in-flight or revision guard. On the server,
`update_drop_log()` mutates the shared `SETTINGS` mapping before calling
`_save_settings()`. The write lock starts inside `_atomic_write_json()`; it
does not protect the mutation or snapshot the mapping before another request
can change it.

Impact: rapid ON/OFF taps or two clients can produce a successful
"Drop logging enabled" response while the persisted settings file ends up
disabled (or the inverse). The diagnostic watchdog can therefore collect a
different amount of outage evidence than the UI confirmation implies.

Evidence: `web/index.html:1954-1970` has no disabled/in-flight guard;
`web/server.py:1466-1479` mutates the shared setting; and
`web/server.py:183-219` serializes the mutable mapping only during the file
write. A deterministic two-thread probe held the first JSON write, changed
the setting from `true` to `false` in the second request, and observed
responses `[false, true]` with the final persisted value `false`.

### A-117 — A failed Refresh discards the last known configuration (P2)

When `loadBands()` fails and there is no current `userTouched` edit, its
failure branch calls `applyDefaults()`. That replaces the last successfully
loaded modem selection with the current client default lists, even though no
new modem configuration was read. Depending on timing, those defaults may
still be the unrestricted fallback while `/api/defaults` is loading.

Impact: a transient server, transport, or modem-read failure can silently
change the visible band selection and make the next Save & Apply push a
different configuration than the last known good one. The status message
reports the read failure but does not explain that the selection was replaced.

Evidence: `web/index.html:1245-1276` calls `applyDefaults()` from the
non-touched refresh catch path; `web/index.html:1239-1242` applies
`defaultsLte/defaultsNr` rather than the last successful selection. A focused
control-flow probe confirmed the failure path reaches the default application
when `userTouched` is false.

### A-118 — Restart confirmation treats any HTTP-200 health response as proof of restart (P2)

`restartServer()` ignores the JSON body from `POST /api/restart` and only
rejects a non-2xx transport response. Its follow-up `waitForHealth()` likewise
returns true for any HTTP-OK `/api/health` response without parsing the
`status` field. The server deliberately uses HTTP 200 for application-level
errors and reports modem health as `status: "ok"`, `"degraded"`, or
`"error"`.

Impact: a failed or unscheduled service restart, or a still-unhealthy modem,
can produce the success toast `Server restarted`. Users can then assume a
LAN/token or service change took effect when the old process or an error state
is still active.

Evidence: `web/index.html:2006-2025` checks only `r.ok` and never reads either
JSON body; `web/server.py:1215-1231` schedules the restart while always
returning `{\"ok\": true}` before the detached launch is attempted;
`web/server.py:1549-1608` exposes health failure through the JSON status; and
`web/server.py:1610-1619` sends application errors with HTTP 200. The static
probe confirmed both client paths omit body validation.

### A-119 — Deleting `bands.json` cannot disable Rogers boot re-apply (P2)

The documented boot behavior says an absent `config/bands.json` is a no-op,
but `boot_apply()` first calls `seed_config_if_absent()`. When the device
property identifies Rogers (`302720`), that helper recreates the curated
configuration before the missing-file check, and `boot_apply()` immediately
applies it.

Impact: an operator trying to remove persistent band forcing by deleting the
config file can have the whitelist silently recreated and applied at the next
boot or boot-apply request. This makes the documented no-op escape hatch
unreliable and can reintroduce a band restriction the operator intentionally
removed.

Evidence: `web/server.py:503-525` seeds a missing file for Rogers;
`web/server.py:1339-1365` invokes that helper before the documented skip
branch; and `README.md:101-103` describes an absent config as no-op. A
corrected isolated probe with a missing temp config and mocked Rogers
properties recreated the file and returned `ok: true` with `source: qmi`
instead of `skipped: true`.

### A-120 — The band-camping sampler runs without a UI client (P2)

The server starts `_band_camping_loop` unconditionally in `__main__`. The
loop invokes `dumpsys telephony.registry` every five seconds and attempts to
append a sample regardless of whether a WebView is open, the Diagnostics tab
is visible, or any client has requested camping data. There is no client-count,
visibility, or user-controlled sampling gate.

Impact: an idle installation continues spawning telephony subprocesses and
writing camping data on the phone. This consumes battery/CPU and telephony
service capacity even when the app is closed, and it compounds the hidden-page
polling cost in A-114.

Evidence: `web/server.py:920-939` contains the unconditional five-second
loop; `web/server.py:1628-1631` starts it before the HTTP server; and the
startup path has no client or visibility condition. A static startup probe
confirmed the sampler is launched independently of `_drop_log_loop` and any
request lifecycle.

### A-121 — Selecting a band destroys keyboard focus (P2)

The band tiles are keyboard-operable `role="button"` elements, but every
selection calls `renderBands()`, which replaces both grid elements' entire
`innerHTML`. The delegated key handler does not capture the selected tile or
restore focus after the replacement. The browser consequently moves focus to
`<body>` after a keyboard selection.

Impact: keyboard, switch-access, and desktop users cannot select several
bands in sequence without tabbing through the page again after each choice.
The loss of focus is silent and makes the control feel finished or
unresponsive even though the band state changed.

Evidence: `web/index.html:1214-1227` rebuilds the grids during
`toggleBand()`; `web/index.html:2099-2104` invokes it from the keyboard
handler with no focus restoration. A live browser probe focused B2, pressed
Space, and observed `before: DIV[data-band="2"]` followed by
`after: BODY` while the active-band count changed.

### A-122 — QMI base and extension mask lines overwrite each other (P1)

The QMI helper prints both the base and extension TLVs using the same labels:
`LTE bands:`, `NR5G SA bands:`, and `NR5G NSA bands:`. The Python parser
assigns `lte`, `sa`, and `nsa` each time it sees one of those labels instead
of unioning the masks. When a complete modem response contains a base mask
and its extension mask, only the final line for each category survives.

Impact: `/api/read` can report a truncated configuration while claiming
`source: qmi`, suppressing the diag/config fallback. A user can then see
missing low bands, or Save & Apply the truncated state, even though the modem
returned all mask TLVs successfully.

Evidence: `qmi/qmi_band.c:200-210` maps base and extension TLVs to identical
output labels; `web/server.py:555-571` overwrites each accumulator. A parser
probe with base lines `LTE bands: 1 3 12` and `NR5G SA bands: 1 77`, followed
by extension lines `LTE bands: 66` and `NR5G SA bands: 78`, returned only
`lte: ['66']` and `nr: ['78', '79']` instead of the union.

### A-123 — Out-of-order registration polls can fabricate radio drops (P2)

`startPolling()` launches registration requests immediately and every two
seconds, while the threaded server can take different amounts of time to
complete each `dumpsys telephony.registry`. `pollRegistration()` has no
request sequence, sample timestamp, or monotonic response guard. It feeds
responses to `checkRegChange()` in completion order, so an older response can
overwrite a newer state and be interpreted as a fresh transition.

Impact: a real `IN_SERVICE` response followed by a delayed stale
`OUT_OF_SERVICE` response creates a false `DROP` entry and leaves the
registration chips showing the old outage until a later poll arrives. Event
History and drop evidence are therefore unreliable during slow or uneven
telephony reads.

Evidence: `web/index.html:1335-1348,1409-1419` has no in-flight sequence or
timestamp check; `web/index.html:1489-1508` compares only the last completed
response; and `web/server.py:470-472,1650` permits slow reads to complete on
independent HTTP workers. A controlled browser probe resolved a newer
`IN_SERVICE` response before an older `OUT_OF_SERVICE` response and observed
`Initial state: IN_SERVICE` followed by a fabricated `DROP` with final
`lastReg.service = OUT_OF_SERVICE`.

### A-124 — Out-of-order signal polls overwrite newer diagnostics (P2)

`startPolling()` launches an immediate signal request and a two-second
interval, but `pollSignal()` has no request sequence, sample timestamp, or
monotonic response guard. Each response appends to the graph in completion
order, assigns the sample `Date.now()` rather than the server timestamp, and
overwrites the four signal readouts. A slower older response can therefore be
accepted as the latest sample.

Impact: uneven telephony reads can move the RSRP/RSRQ/technology/level display
backward and put stale signal data at the graph tail. A user can diagnose the
radio from an older sample even though a newer sample already succeeded.

Evidence: `web/index.html:1335-1348,1389-1406` contains no in-flight ordering
guard and uses completion time for `signalHistory`. A controlled browser probe
resolved a newer response (`-90 dBm`, timestamp `200`) before an older response
(`-110 dBm`, timestamp `100`); the resulting history was `[-90, -110]` and the
visible RSRP was `-110`.

### A-125 — A delayed bootstrap settings response can roll back a LAN change (P2)

Initialization starts `fetchSettings()` in parallel with the rest of the app,
and the settings GET has no revision, cancellation, or relationship to later
user actions. If the user enables LAN while that GET is still pending,
`toggleLan()` renders the successful POST response first, but the older GET
then calls `renderSettings()` with its stale `lan_enabled` value and replaces
the toggle and token-section state.

Impact: the server can be enabled while the Settings screen says it is
disabled and hides the token controls. The user may conclude that LAN access
failed or may miss the restart/token step even though the change was persisted.

Evidence: `web/index.html:1918-1936,1972-1990,2142-2150` has no bootstrap
request guard. A controlled browser probe resolved the POST as enabled, then
resolved the delayed GET as disabled; after the successful action the section
was visible, but the delayed response left `lanEnabled=false`, the switch
unchecked, and the token section hidden.

### A-126 — A timed-out diag read discards the next NV command during cleanup (P1)

`read_nv()` sends its command before entering `read_response()`. When the
previous read left a blocked reader behind, the first `_timed_read()` inside
`read_response()` marks the current descriptor dirty; the next read then closes
and reopens that descriptor in `_ensure_clean_reader()`. The command that was
just written to the old session is therefore retired before its response can
be observed.

Impact: after a diag timeout, the next LTE/NR read or write can fail even when
the modem is ready again. `get_band_config()` performs the two NV reads in
sequence, so one timeout can also make the following NV request disappear and
return an authoritative-looking empty configuration (A-15), while a write
retry can lose the command before the caller receives a useful response.

Evidence: `diag/diag_client.py:356-370,188-263` orders `send_command()` before
the reader cleanup. A pipe-backed lifecycle probe called `read_nv()` with a
short timeout, recorded the command on session 0, and observed the client
reopen session 1 during the same call; the result was `None` and the command's
file description had been retired.

### A-127 — The HDLC decoder accepts trailing bytes after a valid frame (P2)

`hdlc_decode()` stops at the first `0x7e` terminator and validates the bytes
before it, but never verifies that the terminator is the final byte of the
frame. A valid frame followed by arbitrary bytes is therefore accepted as if
it were canonical.

Impact: a corrupted or accidentally concatenated diag item can be treated as a
valid NV response while its trailing bytes are silently discarded. The stream
reader advances over the whole item, so a following frame in the same item can
also be lost without a framing error.

Evidence: `diag/protocol.py:73-112` breaks on the first flag without checking
the remaining input. A focused protocol probe showed
`hdlc_decode(hdlc_encode(payload) + b"\\x00\\xff") == payload`.

### A-128 — Drop episodes starting in one second can overwrite each other's evidence (P2)

The drop watchdog names each episode file only with
`drop_YYYYMMDD_HHMMSS.txt`. If one drop recovers and another begins within the
same wall-clock second, the second episode points at the existing file and the
`if not w.episode_file.exists()` guard skips its initial snapshot. Its recovery
line is then appended to the previous episode's file.

Impact: a short repeated outage can be missing from the persisted drop log or
look like part of an earlier episode. The Debug feature can therefore claim to
capture every drop while losing the correlation snapshot needed to diagnose
the recurrence.

Evidence: `web/server.py:322-355` creates the path at one-second precision and
writes the detection header only when the path does not already exist. A
controlled watchdog probe ran an `OUT_OF_SERVICE → IN_SERVICE →
OUT_OF_SERVICE` sequence with the same timestamp; the directory contained one
file with one `DROP DETECTED` header and one recovery header instead of two
detection records.

### A-129 — An abandoned diag reader can steal bytes from the replacement session (P1)

`_timed_read()` starts a daemon reader whose closure evaluates `self.fd` inside
the thread. When a timeout leaves that thread blocked, the next read closes and
reopens `self.fd`; an old thread that has not yet evaluated the attribute can
then call `os.read()` on the replacement descriptor. The supposed session
isolation in `_ensure_clean_reader()` is therefore not reliable.

Impact: after a timeout, the abandoned reader can consume the next NV response
from the fresh session while the active reader sees EOF or times out. Retry
behavior becomes nondeterministic and an available modem can be reported as
another failed or empty diagnostic read, beyond the permanent thread leak
(A-78) and the discarded-command ordering (A-126).

Evidence: `diag/diag_client.py:188-263` passes mutable `self.fd` into the
reader at execution time and replaces it during cleanup. A controlled
pipe-backed probe blocked the old reader before fd lookup, forced cleanup to
open session 1, then delivered a payload on session 1; the trace showed
`read_calls=[('new', 3), ('old', 3)]` and `second_result=None`, proving the old
reader used the new descriptor and consumed its response.

### A-130 — Revealed access tokens remain unmasked after LAN-panel transitions (P2)

`lanTokenShown` is a page-global reveal flag. `renderSettings()` hides the LAN
panel and returns when LAN is disabled, but never resets that flag;
`regenerateToken()` also replaces the stored token and rerenders without
clearing it. After a user taps Show, disables and re-enables LAN (or generates
a replacement), the next render displays the complete token without another
explicit Show action.

Impact: a secret intentionally revealed once can reappear in full after a
settings transition, increasing accidental disclosure in screenshots, screen
sharing, or shoulder-surfing and contradicting the masked default.

Evidence: `web/index.html:1816-1845,1989-2000` has no
`lanTokenShown = false` in the hide or regenerate paths. A focused source-flow
probe confirmed that the disable branch returns before any reset and that the
subsequent render selects the full-token branch while `lanTokenShown` remains
true.

### A-131 — Backend validation permits bands in the wrong RAT namespace (P1)

`_validate_bands()` accepts any numeric band from 1 through 79 in either list.
It does not enforce the app's separate LTE and NR catalogs, so a request can
place an NR-only band in `lte` or an LTE-only band in `nr`. Both the live write
path and `boot_apply()` consume the normalized lists without another
RAT-specific check.

Impact: an imported file, API client, or hand-edited boot config can ask the
modem to apply a band in the wrong preference mask. The modem may ignore or
reject that entry, or persist a configuration that does not correspond to the
selection the UI presents, making band-lock results misleading and allowing
the same invalid request to be retried at every boot.

Evidence: the UI catalogs at `web/index.html:1138-1151` exclude LTE 77 and NR
4, but `web/server.py:1122-1173` accepts
`{'lte': [77], 'nr': [4]}` unchanged. A direct `_validate_bands()` probe
returned that payload with `error=None`; there is no per-RAT validation before
`write_config()` or `boot_apply()` uses it.

### A-132 — An older band-camping poll can overwrite a newer result (P2)

`startPolling()` schedules `updateCamping()` every five seconds and also
starts an immediate request, but `updateCamping()` has no in-flight guard,
request sequence, or timestamp comparison. It renders every response in
completion order. If a slower older request finishes after a newer one, it
replaces both the recent camping rows and the live Camped chip with stale
EARFCN/band data.

Impact: the Diagnostics screen can claim that the modem is still camped on a
previous band after it has moved, or show a previous cell as the current one.
That undermines the feature used to verify whether a band force actually
stuck, especially when the telephony dump is occasionally slow.

Evidence: `web/index.html:1346-1349,1478-1492` has no response-order state. A
probe executing the current `updateCamping()` function resolved a newer
response (`B12 · 5060`) first and an older response (`B4 · 2050`) second; the
final rendered row and chip were `B4 · 2050`, proving the stale overwrite.

### A-133 — Invalid signal levels and sentinels pass through to the UI (P2)

The modern signal parser validates LTE/NR RSRP and RSRQ only. It extracts
`level` and returns it without enforcing the Android signal-level domain or
normalizing an unavailable value. A malformed or sentinel level can therefore
be presented as a live level even when the accompanying signal metrics are
valid.

Impact: the Diagnostics card can display impossible values such as `-1`, `7`,
`99`, or `2147483647` as the signal level. That makes the readout misleading
and can cause downstream UI or accessibility consumers to treat an unavailable
measurement as a real quality score.

Evidence: `web/server.py:649-701` copies `lte_level`/`nr_level` directly into
the result and `_valid_signal()` is consulted only for RSRP/RSRQ candidate
selection. A current-format LTE fixture with `rsrp=-95`, `rsrq=-10`, and
`level=2147483647` returned
`{'rsrp_dbm': -95, 'rsrq_db': -10, 'level': 2147483647, 'tech': 'LTE'}`.

### A-134 — An interrupted install-time seed can permanently block boot apply (P2)

The Rogers install hook writes `config/bands.json` directly through shell
redirection. If the installer or device is interrupted after the file is
created but before the complete JSON is written, the runtime sees an existing
file and skips `seed_config_if_absent()`. `boot_apply()` then returns
`invalid config` on every startup until the file is manually repaired or
removed.

Impact: a fresh install can lose its documented carrier-aware boot re-apply
because of a partial seed, with no automatic recovery. The failure is especially
confusing because the config file exists, but it is not a valid persisted band
preference.

Evidence: `customize.sh:59-71` uses `echo "$ROGERS_BANDS" > "$CONFIG_FILE"`
with no temporary file and rename; `web/server.py:503-525` skips seeding on
mere existence, and `web/server.py:1339-1365` reports malformed JSON as an
apply failure. A focused probe placed a truncated `bands.json` at the config
path and observed `seed_skipped_because_exists=True` followed by
`{'ok': False, 'error': 'invalid config'}`.

### A-135 — Interrupted on-device exports leave corrupt files in the export directory (P2)

`export_config()` opens its timestamped destination with `w` and serializes
directly into it. The write is not atomic and there is no cleanup of a partial
destination when serialization or storage fails. A process death, power loss,
or disk-full error can therefore leave a file that looks like an export but
cannot be imported or parsed later.

Impact: a failed export can strand a corrupt artifact next to successful
exports. Users may select that file during a later import and receive a parse
failure, while the original export is no longer recoverable from the app.

Evidence: `web/server.py:1402-1417` writes directly to the final path. A
controlled `json.dump()` interruption returned
`{'ok': False, 'error': 'simulated interruption'}` while leaving
`bandctl-export-....json` containing only `{"lte":`, proving that the error
path does not remove the partial export.

### A-136 — An unknown EARFCN sentinel is rendered as a live serving cell (P2)

The band-camping parser accepts every non-negative `mEarfcn` value without
checking for Android's unknown/sentinel value or the valid LTE EARFCN range.
The frontend treats any non-null EARFCN as a serving cell and marks the
Camped chip selected, so an unavailable cell identity is presented as real
camping data.

Impact: Diagnostics can show `EARFCN 2147483647` (and a band) as the current
serving cell during a transition or incomplete telephony dump. That can make
a user conclude that a band lock succeeded or failed based on a placeholder
identity rather than a cell the modem actually camped on.

Evidence: `web/server.py:881-917` parses `mEarfcn` with only `\d+` and never
rejects the sentinel; `web/index.html:1484-1489` treats every non-null value
as live. A synthetic registered LTE identity with
`mEarfcn=2147483647,mBands=[4]` returned `(2147483647, 4)` from
`_parse_band_camping()`.

### A-137 — A listed-but-ineffective reset command prevents the fallback path (P1)

`modem_reset()` selects the preferred `cmd phone radio power` mechanism when
the help text lists it. If the command runs but the radio never reaches
`POWER_OFF`, the handler powers the radio back on and returns
`ok:false` immediately. It does not try the documented airplane-mode fallback,
even when that capability is available.

Impact: devices that advertise the preferred subcommand but do not implement
it correctly cannot use the reset feature, although the alternate mechanism
could still work. The user receives a hard failure instead of the available
fallback being attempted.

Evidence: `web/server.py:1515-1524` returns after the failed preferred
verification, while the fallback begins only at `web/server.py:1528`. A
controlled probe made both capabilities available, forced the preferred
verification to remain false, and observed
`{'ok': False, 'error': 'radio power off did not take effect'}` with no
`connectivity airplane-mode` command attempted.

### A-138 — A failed QMI helper can still certify a successful band apply (P1)

`_run_qmi()` returns the subprocess exit code, but `_write_qmi_config()` only
checks that the code is not `None` and that the output contains
`result: status=0`. A helper that prints a success result and then exits
nonzero is therefore accepted as a QMI apply; `_apply_bands()` skips the diag
fallback and `write_config()` returns `ok:true`.

Impact: a crashed or partially failed privileged QMI client can leave the UI
claiming that the requested bands were applied, while the fallback transport
was deliberately not tried and the persisted intent may be reapplied later.

Evidence: `web/server.py:527-543,1318-1337,1384-1393`; a controlled handler
probe with `_run_qmi()` returning `(7, 'result: status=0 ...')` returned
`{'ok': True, 'source': 'qmi'}`, recorded zero diag-fallback calls, and still
mirrored the request.

### A-139 — Camped-cell parsing renders invalid band identities (P2)

`_parse_band_camping()` converts every digit sequence in `mBands=[...]` into
the reported band without checking it against the app's band contract or
even rejecting zero. The diagnostics list renders that value verbatim, so a
malformed/vendor dump can show `B0`, `B999`, or another non-band identity as
the serving cell.

Impact: the most prominent live-camping indicator can assert a false band and
mislead validation of a band lock. This is separate from A-136, which covers
the unknown EARFCN sentinel; here the EARFCN is valid and the band field is
the invalid value.

Evidence: `web/server.py:881-917`; `web/index.html:1467-1476,1484-1489`.
A direct parser probe returned `(2050, 0)` for a registered LTE identity with
`mEarfcn=2050, mBands=[0]` and `(2050, 999)` for `mBands=[999]`.

### A-140 — Non-sentinel signal metrics are not range-validated (P2)

`_valid_signal()` rejects only `None`, the Integer.MAX_VALUE sentinel, and the
legacy `99` marker. It accepts arbitrary positive or otherwise impossible
RSRP/RSRQ values, and `_parse_signal_object()` passes them straight into the
API and graph/readouts.

Impact: malformed or vendor-specific telemetry can be displayed as a real
measurement and distort the auto-scaled signal graph, rather than being
shown as unavailable. A direct fixture with `rsrp=0`, `rsrq=50`, and
`level=9` produced those values unchanged.

Evidence: `web/server.py:617-620,649-701`; the focused signal parser probe
returned `{'rsrp_dbm': 0, 'rsrq_db': 50, 'level': 9, 'tech': 'LTE'}`.

### A-141 — Atomic JSON replacement is not crash-durable (P2)

`_atomic_write_json()` fsyncs the temporary file and then calls `os.replace()`
but never opens and fsyncs the parent directory. The rename is atomic for
concurrent readers, but after a sudden power loss the directory entry update
is not guaranteed to survive; `bands.json` or `settings.json` can revert to
the prior version or disappear despite the API having reported a successful
save.

Impact: band intent, LAN authentication settings, and boot-time re-apply state
can be lost across a crash/reboot without a write-time error. This is a
durability gap distinct from A-16's ordinary disk-error path and A-135's
interrupted export contents.

Evidence: `web/server.py:183-214`; an instrumented write recorded one file
`fsync` followed by `os.replace` and no directory `fsync`.

### A-142 — Camping API errors erase the last known cell without warning (P2)

`read_band_camping()` returns `{"ok": false, ...}` for a read failure, but
the HTTP dispatcher still sends that application error with status 200. The
frontend's `updateCamping()` checks only the HTTP status, treats the missing
`samples` array as an empty list, clears the camping history, and sets the
Camped chip to `—`.

Impact: a transient log read/serialization failure can make a previously
verified serving cell disappear while the rest of Diagnostics remains
connected. The user gets no error or preserved last-known state, so a band
lock can look as though it stopped camping. This is separate from A-02's
signal/registration/health error-state handling and A-74's server-side
success-serialization defect.

Evidence: `web/server.py:1481-1504,1610-1619`; `web/index.html:1478-1492`.
A focused source probe found `camping_checks_d_ok=False`, while the same
flow contains `renderCamping(d && d.samples ? d.samples : [])` and clears
the chip when no samples are present.

### A-143 — Health never reports the diagnostic-session owner (P2)

`_query_md_pid()` passes a mutable `bytearray` to `fcntl.ioctl()`. Python's
ioctl API returns an integer status for that form and mutates the buffer; the
function then calls `len(res)`, raises `TypeError`, catches it, and returns
`None`. The PID unpacking path is consequently unreachable even when the
kernel fills the buffer with a valid owner PID.

Impact: `/api/health` cannot identify the process holding the exclusive diag
session, removing the most useful recovery clue when NSG or another diag
client blocks band control. The health response silently reports
`md_session_owner: null` instead of the actual owner.

Evidence: `web/server.py:856-878,1580-1595`; a controlled ioctl mock mutated
the 16-byte buffer with PID `4242` and returned status `0`, yet
`_query_md_pid('/dev/diag')` returned `None`. A local mutable-buffer ioctl
probe likewise returned `int`, not a bytes buffer.

### A-144 — An unparseable registration snapshot is reported as valid telemetry (P2)

`_parse_registration()` returns an all-`None` registration dictionary as soon
as it sees an `mServiceState=` line, even when the object contains no
recognized state, operator, technology, or roaming fields. `read_registration()`
only checks whether the parser returned `None`, so that empty dictionary is
sent as a normal HTTP-200 response with a timestamp. The frontend checks only
the HTTP status, paints the app Connected, and renders neutral/unknown chips.

Impact: a truncated, unsupported, or malformed telephony dump can be mistaken
for a live but unknown radio state instead of a telemetry failure. Registration
monitoring and the visible connection indicator can stay green while the
actual modem state is unavailable. This is separate from A-02's explicit
`{"error": ...}` payload path and A-53's exception/`None` recovery path: this
case is a deceptively successful all-null response.

Evidence: `web/server.py:757-834,1437-1452`; `web/index.html:1409-1418,1431-1440`.
A focused probe with `mServiceState={}` returned
`{'service_state': None, 'data_state': None, 'network_type': None, 'operator': None, 'roaming': None, 'timestamp': ...}`
instead of an error.

### A-145 — QMI can report NR bands the mobile catalog cannot display or reapply (P2)

The QMI helper prints every set bit in its eight-word NR mask, allowing band
numbers through 512, and `_parse_qmi_get()` accepts those values unchanged.
The mobile UI catalog stops at NR B79, while the backend write validator also
rejects values above 79. A live QMI read containing NR B257 is therefore
returned as `source: qmi`; `setBands()` retains B257 and the summary counts it,
but `renderGrid()` has no tile for it. A subsequent Save & Apply cannot submit
the displayed live set because the backend rejects that hidden band.

Impact: the screen can claim a QMI configuration is loaded while silently
omitting part of it from the selectable grid, and the user can be trapped
between an unexplained count mismatch and a rejected save. This is distinct
from A-131's wrong LTE/NR namespace validation and A-122's base/extension
overwrite: the value is a valid QMI-reported NR band, but outside this UI/API
catalog contract.

Evidence: `qmi/qmi_band.c:153-168`; `web/server.py:547-571,1276-1283`;
`web/index.html:1146-1151,1230-1238`; the focused parser probe returned
`{'lte': ['1', '3', '12'], 'nr': ['257', '77']}` for an NR mask containing
B257, while the UI catalog contained no `257` entry.

### A-146 — A failed token-storage write is reported as a saved credential (P1)

`setStoredToken()` catches every `localStorage.setItem()` failure and returns
without reporting whether the credential was persisted. `saveLanToken()` then
clears the input, renders the supplied token, resumes polling, and shows
`Token saved` unconditionally. On a remote LAN page, however, `apiFetch()` reads
the token only from `localStorage`; after a quota, blocked-storage, or disabled
storage failure it sends no `Authorization` header, so the next protected
request still receives 401. The visible masked token and success toast therefore
describe a credential the app cannot actually use or retain.

Impact: a user can be told that LAN authentication is configured while every
subsequent API call remains unauthenticated; reloading loses the apparent
credential completely. This is distinct from A-35's initialization/preset
storage failure and A-67's valid-token bootstrap retry gap: here the entered
token is silently discarded before the authenticated request path can use it.

Evidence: `web/index.html:1795-1811,1902-1915`; a focused source probe with
`localStorage.setItem()` raising `QuotaExceededError` produced
`storedToken=null`, an empty input, a masked displayed token, and the
non-error toast `Token saved`.

### A-147 — Malformed persisted presets can crash the Load action (P2)

`getPresets()` validates only that the top-level JSON value is an array.
`renderPresets()` displays each entry without validating that `lte` and `nr`
are arrays, and `loadPreset()` passes those fields directly to `setBands()`.
`setBands()` then calls `.map()` on any truthy value. A stale, manually edited,
or otherwise malformed local record such as
`{"name":"Broken","lte":1,"nr":[]}` can therefore render a Load button
whose click handler throws an uncaught `TypeError` instead of showing an error
or recovering the preset.

Impact: a damaged preset cannot be loaded, produces no user-facing failure
message, and leaves the user with a misleading preset row; the only recovery
is to delete the record and recreate it. This is separate from A-10's import
validation and A-35's storage-access failure because the storage is readable
and the bad schema is accepted as persisted app state.

Evidence: `web/index.html:1230-1238,1658-1685,1704-1712`; an exact extracted
handler probe with the fixture above returned
`TypeError: (lte || []).map is not a function` from `loadPreset(0)`.

### A-148 — Airplane-mode reset reports success without restoring the radio (P1)

The airplane-mode fallback verifies only that the radio reached `POWER_OFF`
before cleanup. After `_disable_airplane()` reports that the airplane-mode
property is off, `modem_reset()` immediately returns `ok:true`; it never polls
for a usable post-cleanup registration state or verifies that the radio-power
transition completed. A controlled fallback probe returned `ok:true` with
airplane mode off while the simulated radio remained `POWER_OFF`, and the
call sequence contained no post-disable state check.

Impact: the UI can report “Modem reset sent” while the modem is still powered
off and connectivity has not returned. This is separate from A-20's preferred
radio-power path, A-46's unverifiable airplane cleanup, and A-62's intentional
airplane-mode state being cleared: this is the normal fallback success path
with a false recovery boundary.

Evidence: `web/server.py:596-606,1506-1544`; the focused probe returned
`{'ok': True, 'final_radio': 'POWER_OFF', 'final_airplane': False}` after the
fallback's single `POWER_OFF` check.

### A-149 — Corrupt persisted bands silently become carrier defaults on read (P1)

When QMI and diag are unavailable, `_read_config_file()` catches malformed
JSON and falls through to `defaults_for_carrier()` without returning an error
or marking the persisted preference as corrupt. A non-Rogers device therefore
receives the unrestricted 25-LTE/17-NR default lists from `/api/read`, while
the same existing file makes `boot_apply()` return `invalid config`. The UI
can display and later save the fallback as if it were the intended selection,
while boot behavior remains blocked by the original corruption.

Impact: an interrupted or damaged band preference can silently replace the
user's last-known selection with an unsafe all-band set and hide the repair
needed for boot re-apply. This is distinct from A-42's syntactically valid
but unvalidated JSON, A-104's source label, and A-134's install-time seed
failure: this is the malformed-read fallback that masks the corruption.

Evidence: `web/server.py:1257-1302,1339-1365`; a focused probe with
`bands.json={truncated` returned `source=default`, `25` LTE bands, and `17`
NR bands from `/api/read`, while `/api/boot-apply` returned
`{'ok': False, 'error': 'invalid config'}`.

## Not yet fixed

All findings above remain open unless explicitly marked otherwise by a later
fix commit. Keep this file as the audit index when implementing fixes; address
one behavior at a time and add a regression test or reproducible UI check for
each item.
