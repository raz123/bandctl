# Band Controller

A standalone KernelSU module for LTE/NR band control and modem diagnostics on Qualcomm devices — no third-party apps required (Network Signal Guru is not needed).

## Download

**v1.0 release asset:**

- Direct download: [bandctl-v1.0.zip](https://github.com/raz123/bandctl/releases/download/v1.0/bandctl-v1.0.zip)
- SHA-256: `6ce0d9dc5cbe8084f133aef547e29b90851edb0c43a04b35f43f3a7164d7862b`
- Release page: [Band Controller v1.0 — NSG-independent](https://github.com/raz123/bandctl/releases/tag/v1.0)

## Requirements

- **Root**: KernelSU (or KernelSU-compatible) root.
- **Device**: A Qualcomm modem device (tested on Poco F3 / alioth).
- **Termux python3**: installed at `/data/data/com.termux/files/usr/bin/python3` — this is the web server engine. Install with:
  ```
  pkg install python
  ```
  On boot, the module waits up to 60 seconds for python3 to become available.

## Install

1. Download [bandctl-v1.0.zip](https://github.com/raz123/bandctl/releases/download/v1.0/bandctl-v1.0.zip) to your device.
2. Open the **KernelSU app** → **Modules** → **Install from storage** and pick the zip.
   - Alternatively, from a root shell: `ksud module install bandctl-v1.0.zip`
3. Reboot (or let the module start the service).
4. Open the web UI at **http://localhost:8080** from the device browser.
   - From a computer: `adb forward tcp:8080 tcp:8080`, then open http://localhost:8080.

## Features

- **7 API endpoints**:
  - `GET  /api/read` — current LTE/NR band configuration (from diag, config file, or default)
  - `POST /api/write` — write LTE/NR band configuration to the modem
  - `GET  /api/signal` — current signal strength (RSRP/RSRQ/level) from `dumpsys telephony.registry`
  - `GET  /api/registration` — service/data state, network type, operator, roaming
  - `GET  /api/health` — transport status (diag device, band counts, diag session owner)
  - `POST /api/modem-reset` — soft modem reset (`cmd phone radio power` off/on, with fallback)
  - `GET  /api/band-camping` — live band-camping log: last N serving-cell EARFCN/band samples
- **Live band-camping log** — the module records serving-cell EARFCN/band samples so you can see whether a forced band actually stuck.
- **RSRP graph UI** — the web UI plots signal strength over time.
- **Config persistence** — band settings are mirrored to `/data/adb/modules/bandctl/config/bands.json` and survive reboots.

## How it works

- **Direct /dev/diag access** via the bundled pure-Python protocol stack (kernel-verified: the diag kernel driver, feature masks, and NV/band-mask commands are exercised without any third-party binaries or libraries).
- **Telephony-framework monitoring** via `dumpsys telephony.registry` — signal strength, registration state, and band camping are reported even when the diag transport is unavailable.
- **Graceful degraded mode** — if diag is down, reads fall back to the config file and monitoring keeps working.

## Status and honest limitations

On some kernels/boots, the modem never completes the diag feature-mask handshake, so band **writes** may be NACKed (kernel `0x13`) until modem bring-up completes. The UI reports transport status clearly, band intent is still saved to the config file, and monitoring keeps working. **Band locking is best-effort on the device's current kernel/modem combo** — if the write is NACKed, the modem ignores it; a modem reset or reboot may help complete bring-up.

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
- The repo contains the complete module source: `customize.sh` (installer), `service.sh` (boot service), `web/` (pure-stdlib Python HTTP server + UI), `diag/` (pure-Python diag protocol stack), and `config/bands.json` (default band config).

## License

[MIT](LICENSE) © 2026.
