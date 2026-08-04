# Band Controller

A standalone KernelSU module for LTE/NR band control and modem diagnostics on Qualcomm devices — no third-party apps required (Network Signal Guru is not needed).

## Download

**v1.5 release asset (recommended):**

- Direct download: [bandctl-v1.5.zip](https://github.com/raz123/bandctl/releases/download/v1.5/bandctl-v1.5.zip)
- SHA-256: `c941b882d4a55e5c5fc7487082d92a4203d12ca85cc1a18430ed107c41208de8`
- Release page: [Band Controller v1.5 — QA fixes](https://github.com/raz123/bandctl/releases/tag/v1.5)

**v1.4.1 release asset (still live):**

- Direct download: [bandctl-v1.4.1.zip](https://github.com/raz123/bandctl/releases/download/v1.4.1/bandctl-v1.4.1.zip)
- SHA-256: `3611794b549f79fdcb9cd3fac9e244a2429a9c231c23782b1289c1f330ed4d8b`
- Release page: [Band Controller v1.4.1 — boot auto-start fix](https://github.com/raz123/bandctl/releases/tag/v1.4.1)

**v1.4 release (still live):**

- Direct download: [bandctl-v1.4.zip](https://github.com/raz123/bandctl/releases/download/v1.4/bandctl-v1.4.zip)
- SHA-256: `8bc6921fc45a30415ba91069c663d0682abd4616f95653c8ebf023c431684e11`
- Release page: [Band Controller v1.4 — real band apply via QMI](https://github.com/raz123/bandctl/releases/tag/v1.4)

**v1.3 release (still live):**

- Direct download: [bandctl-v1.3.zip](https://github.com/raz123/bandctl/releases/download/v1.3/bandctl-v1.3.zip)
- SHA-256: `d236052c03bf657415445574c9a80d65f03369a861dce7bae0def041959a2e4a`
- Release page: [Band Controller v1.3 — UI redesign](https://github.com/raz123/bandctl/releases/tag/v1.3)

**v1.2 release (still live):**

- Direct download: [bandctl-v1.2.zip](https://github.com/raz123/bandctl/releases/download/v1.2/bandctl-v1.2.zip)
- SHA-256: `8e581b995cbdf605aad66e373323a5c0ff699ce69e111bcbfa1579e30a43ce29`
- Release page: [Band Controller v1.2 — Manager WebUI fix](https://github.com/raz123/bandctl/releases/tag/v1.2)

**v1.1 release (still live):**

- Direct download: [bandctl-v1.1.zip](https://github.com/raz123/bandctl/releases/download/v1.1/bandctl-v1.1.zip)
- SHA-256: `87269da4b1ff1a9cc5cd57f583156e269042e8234233e6becb96b69749fcc8a9`
- Release page: [Band Controller v1.1 — Manager WebUI](https://github.com/raz123/bandctl/releases/tag/v1.1)

**v1.0 release (still live):**

- Direct download: [bandctl-v1.0.zip](https://github.com/raz123/bandctl/releases/download/v1.0/bandctl-v1.0.zip)
- SHA-256: `6ce0d9dc5cbe8084f133aef547e29b90851edb0c43a04b35f43f3a7164d7862b`

## Requirements

- **Root**: KernelSU (or KernelSU-compatible) root.
- **Device**: A Qualcomm modem device (tested on Poco F3 / alioth).
- **Termux python3**: installed at `/data/data/com.termux/files/usr/bin/python3` — this is the web server engine. Install with:
  ```
  pkg install python
  ```
  On boot, the module waits up to 60 seconds for python3 to become available.
  If Termux is missing, the service falls back to the portable Python runtime at `/data/local/tmp/pyroot` (v1.4.1+).

## Install

1. Download [bandctl-v1.4.1.zip](https://github.com/raz123/bandctl/releases/download/v1.4.1/bandctl-v1.4.1.zip) to your device.
2. Open the **KernelSU app** → **Modules** → **Install from storage** and pick the zip.
   - Alternatively, from a root shell: `ksud module install bandctl-v1.4.1.zip`
3. Reboot (or let the module start the service).
4. Open the web UI:
   - **KernelSU Manager WebUI** — open the module in KernelSU/ReSukiSU Manager and tap the launch button (or open `ksu://webui/bandctl` directly). The UI is served from inside the Manager and the API calls reach the module's python server at 127.0.0.1:8080 (loopback cleartext is allowed by the Manager). Works out of the box — no extra setup.
   - **Browser** — open **http://localhost:8080** from the device browser.
   - From a computer: `adb forward tcp:8080 tcp:8080`, then open http://localhost:8080.

### What's new in v1.5

- **QA fixes: live band counts, working export, retry, sticky tabs.** Band count chips and the Settings config summary now update immediately when you toggle bands; Export config (.json) works inside the Manager WebView (the server writes the file on-device and the UI shows its path); the Retry button retries up to 3 times before giving up; and the tab bar stays pinned below the header while scrolling.

### What's new in v1.4.1

- **Boot auto-start fix for devices without Termux python.** If Termux python3 is missing at boot, the service now falls back to the portable Python runtime at `/data/local/tmp/pyroot` (`python3.14` with its bundled `lib/` via `LD_LIBRARY_PATH`), so the web server comes up on its own after every reboot. Proven on this device: after a reboot with no manual server start, the server is running with the pyroot fallback logged in `/sdcard/modem_watch_sessions.log` and `/api/read` reports the live QMI band state.

### What's new in v1.4

- **Real band apply via QRTR QMI.** Save & Apply now talks to the modem over Qualcomm's QRTR transport with a bundled static `qmi_band` client — no more NACKed diag writes. Proven on this device: forcing bands moved camping from band 4 (EARFCN 2050) to band 12 (EARFCN 5060) in ~40s and back. `/api/read` reports the live modem state with `source: "qmi"`; diag remains the fallback transport where it works, and the config file still persists intent.

### What's new in v1.2

- **Manager WebUI now works.** v1.1's WebView pages resolved the API base relative to the Manager's `https://mui.kernelsu.org` origin, so API calls were intercepted by the WebViewAssetLoader and 404'd. The API base is now unconditional (`http://127.0.0.1:8080`), which is correct whether the page is served by the Manager or by the python server itself. Desktop use at http://localhost:8080 is unchanged.

## Features

- **7 API endpoints**:
  - `GET  /api/read` — current LTE/NR band configuration (from QMI, diag, config file, or default)
  - `POST /api/write` — write LTE/NR band configuration to the modem (QMI first, diag fallback)
  - `GET  /api/signal` — current signal strength (RSRP/RSRQ/level) from `dumpsys telephony.registry`
  - `GET  /api/registration` — service/data state, network type, operator, roaming
  - `GET  /api/health` — transport status (QMI or diag, band counts, diag session owner)
  - `POST /api/modem-reset` — soft modem reset (`cmd phone radio power` off/on, with fallback)
  - `GET  /api/band-camping` — live band-camping log: last N serving-cell EARFCN/band samples
- **Live band-camping log** — the module records serving-cell EARFCN/band samples so you can see whether a forced band actually stuck.
- **RSRP graph UI** — the web UI plots signal strength over time.
- **Config persistence** — band settings are mirrored to `/data/adb/modules/bandctl/config/bands.json` and survive reboots.
- **KernelSU Manager WebUI** — after install, open the module in KernelSU/ReSukiSU Manager and tap the launch button (or open `ksu://webui/bandctl`); the UI also still works at http://localhost:8080.

## How it works

- **Direct QMI band apply over QRTR** via the bundled static `qmi_band` client (source in `qmi/`). It discovers the live NAS endpoint at runtime (service/node/port are probed, not hardcoded), reads the current band configuration, and applies LTE/NR band preference changes — the modem camps per the applied list.
- **`/dev/diag` fallback** via the bundled pure-Python protocol stack (the diag kernel driver, feature masks, and NV/band-mask commands) for devices where diag works.
- **Telephony-framework monitoring** via `dumpsys telephony.registry` — signal strength, registration state, and band camping are reported regardless of transport.
- **Graceful fallback chain** — reads try QMI, then diag, then the config file; writes try QMI, then diag, and always mirror intent to `config/bands.json`.

## Status and honest limitations

**QMI band apply works on this device (Poco F3 / alioth).** Forcing bands via the web UI moved camping off band 4 (EARFCN 2050) onto band 12 (EARFCN 5060) in ~40s, and restoring the Rogers config moved it back toward band 4 with registration IN_SERVICE. `/api/read` reports the live QMI band state with `source: "qmi"`.

Where the QRTR QMI service is unavailable, the module falls back to `/dev/diag` — on kernels where the diag feature-mask handshake never completes, band **writes** may be NACKed. In every case band intent is mirrored to the config file and monitoring keeps working.

## Band configuration

Default config (`config/bands.json`):

```json
{
  "lte": ["1", "2", "3", "4", "5", "8", "12", "17", "20", "28", "38", "40", "41"],
  "nr": ["1", "3", "5", "8", "20", "28", "38", "41", "77", "78"]
}
```

Bands **6, 7, and 66 are intentionally absent/disabled** — this is a community-validated fix for Canadian carriers: enabling them causes Telus/Koodo/Rogers to drop network reports. Edit `/data/adb/modules/bandctl/config/bands.json` (or use the web UI) to customize.

## Development / testing

- Tested on: **Poco F3 (alioth)**, **ArrowOS 13.1**, kernel **4.19.325-cip130**.
- Protocol tests: **27/27 passing**.
- The repo contains the complete module source: `customize.sh` (installer), `service.sh` (boot service), `web/` (pure-stdlib Python HTTP server + UI), `webroot/` (KernelSU Manager WebUI), `diag/` (pure-Python diag protocol stack), `qmi/` (the QRTR QMI band client — `qmi_band.c` + Makefile, built static for aarch64 with musl; only the binary ships in the module zip), and `config/bands.json` (default band config).

## License

[MIT](LICENSE) © 2026.
