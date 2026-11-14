# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**gpsdo-monitor** is a health monitor, mDNS advertiser, and
configurator for [Leo Bodnar](http://www.leobodnar.com/) GPS-disciplined
clock sources (LBE-1420 / LBE-1421 / LBE-1423 / LBE-Mini), and for the
MVR GPSDO B (serial-only; see "MVR GPSDO B" below).

Its primary consumer is [`hf-timestd`](https://github.com/HamSCI/hf-timestd)'s
authority manager: gpsdo-monitor publishes an **actively probed A-level**
signal that closes the "GPSDO is *probably* still disciplining the
RX888 ADC" gap. The daemon also stands alone — it emits a simple file
plus mDNS contract that any consumer can read.

Part of the HamSCI sigmond suite — see `/opt/git/sigmond/sigmond/CLAUDE.md`
(orchestrator) and `/opt/git/sigmond/CLAUDE.md` (umbrella) for
cross-repo context.

## Authors

- Michael Hauan (AC0G, GitHub: mijahauan)
- Repo: https://github.com/HamSCI/gpsdo-monitor
- Driver bytes ported from [`bvernoux/lbe-142x`](https://github.com/bvernoux/lbe-142x)
  (LBE-1420 / Mini code paths — see README for live-hardware coverage).

## Commands

```bash
# Development — uv canonical
uv sync --extra dev
uv run pytest tests/

# Production install (uses sigmond's shared _ensure_uv helper)
sudo ./install.sh

# CLI
gpsdo-monitor detect           # enumerate Leo Bodnar HIDs and MVRs (listens up to 65 s per RP2040 tty)
gpsdo-monitor status           # one-shot JSON dump per device
gpsdo-monitor serve            # long-lived probe daemon (systemd)
gpsdo-monitor tui              # Textual live-view TUI ([tui] extra)
gpsdo-monitor config <...>     # manual debugging only; prefer `smd gpsdo config`
gpsdo-monitor mvr probe        # MVR: read the menu header (service must be stopped)
gpsdo-monitor mvr passthrough on|off [--persist]   # MVR: position needs passthrough on
```

The canonical operator surface for configuration is **`smd gpsdo
config`** (in the sigmond orchestrator). The `gpsdo-monitor config`
subcommand is a placeholder for manual debugging.

## What it does (and doesn't)

The lifecycle is **Detect → Probe → Publish → Advertise → Advise**:

1. **Detect** attached Leo Bodnar USB HIDs (VID `0x1DD2`, four known
   PIDs). Refuses to guess when multiple devices are attached without
   a declared serial.
2. **Probe** health at 10 s cadence — PLL lock, GPS fix (from NMEA on
   1421/1423 or NAV-PVT on Mini), antenna status, satellites used,
   fix age, output frequencies, 1 PPS presence, Mini signal-loss
   count.
3. **Publish** per-device state atomically to `/run/gpsdo/<serial>.json`
   (schema v1) plus an aggregate `/run/gpsdo/index.json` for TUI
   consumption.
4. **Advertise** over mDNS as `_gpsdo._tcp` so remote consumers
   (splitter-fed topologies) can read health without SSH.
5. **Advise** — emits `a_level_hint` (`A1` / `A0`) with a
   human-readable reason; on the Mini, parses UBX-MON-VER to report
   u-blox firmware and flag outdated PROTVER.

It is **not**:

- A metrology reference. PPS stability numbers are OS-millisecond
  bound (via `TIOCMIWAIT` on the CDC DCD line) and only indicate
  liveness + gross stability. Every `pps_study` block carries that
  warning verbatim in its `note` field.
- A system-clock disciplinarian. Chrony does that.
- A vendor reverse-engineering project. It stays within the
  feature-report layouts documented by lbe-142x; no undocumented
  HID opcodes.

## Project structure

```
src/gpsdo_monitor/
  cli.py            # argparse — detect / status / serve / tui / config
  service.py        # long-lived daemon loop (the `serve` command)
  discovery.py      # USB HID enumeration of Leo Bodnar VID 0x1DD2
  health.py         # health-state assembly per probe cycle
  publish.py        # atomic write to /run/gpsdo/<serial>.json + index.json
  advisories.py     # a_level_hint reasoning + UBX-MON-VER firmware advisory
  schema.py         # schema v1 dataclasses
  hid_xport.py      # HID feature-report transport
  nmea.py           # NMEA fix parser (1421 / 1423 CDC stream; MVR passthrough)
  mvr.py            # MVR GPSDO B: stream parser, passive reader, discovery,
                    #   report builder, and the console used by `mvr` commands
  ubx.py            # UBX NAV-PVT + MON-VER parser (Mini)
  pps.py            # DCD-edge capture via TIOCMIWAIT
  config.py         # device-configuration writes (frequencies, PPS, PLL/FLL)
  tui.py            # Textual live-view UI
  models/
    base.py         # device-model ABC
    registry.py     # PID → model dispatch
    lbe_1420.py     # status parser per upstream lbe-142x layout
    lbe_1421.py     # 1420 + NMEA CDC stream
    lbe_mini.py     # u-blox / UBX parser + signal-loss tracking
deploy/             # systemd unit, tmpfiles, udev rules
scripts/            # auxiliary scripts
tests/              # byte-level model tests + integration
deploy.toml         # sigmond client manifest
```

## Hardware support matrix

| Feature            | LBE-1420 | LBE-1421 | LBE-1423 | LBE-Mini |
|--------------------|:--------:|:--------:|:--------:|:--------:|
| HID status parse   | ✓        | ✓        | ✓        | ✓        |
| NMEA fix / sats    | —        | ✓ CDC    | ✓ CDC    | ✓ NAV-PVT |
| 1 PPS edge capture | —        | ✓ DCD    | ✓ DCD    | —        |
| Firmware advisory  | —        | —        | —        | ✓ MON-VER |
| Live-hardware coverage | ported (not validated) | ✓ | ported (not validated) | ported (not validated) |

The LBE-1421 is the actively-validated path. The other three drivers
are byte-level-tested against captures from `bvernoux/lbe-142x` but
have not been exercised against live hardware here.

## MVR GPSDO B

A Seeed XIAO RP2040 on USB CDC, no HID, so it bypasses `models/` and
`hid_xport.py`. `mvr.MvrCandidate` has the same shape as `HidCandidate`,
so `discovery.match()` and the service handle both families with one set
of rules; `service.MvrWorker` stands beside `DeviceWorker`.

- 2e8a:000a belongs to every RP2040 CDC board. Discovery listens, read-only,
  for up to 65 s and calls a tty an MVR only once it prints a status line,
  a lock event, the banner, the HALTED line or a `$PMVR,MSG,` sentence. Any
  other RP2040 gets
  its termios back and stays closed while it stays plugged in.
- ⛔ The MVR opens its menu on the first printable byte it receives. Default
  termios echoes the device's own output back to it, which opened N8UR's
  menu on 2026-11-14. The monitor path opens the port `O_RDONLY`, takes an
  exclusive `flock`, and sets raw termios (all echo flags, CR/NL translation
  and IXON/IXOFF off) before the first read. Tests pin each of these.
- Only `gpsdo-monitor mvr probe|passthrough` writes, and only the bytes in
  `mvr.ALLOWED_WRITES`. They refuse while the service runs or holds the lock.
- Position exists only in NMEA passthrough (off by default). Without it the
  report carries no position and an advisory names the command.

## File / mDNS contract for consumers

- **Per-device state:** `/run/gpsdo/<serial>.json` (schema v1, written
  atomically; see `schema.py`).
- **Aggregate:** `/run/gpsdo/index.json`.
- **mDNS service:** `_gpsdo._tcp`, payload pointers to the same JSON
  shape so remote consumers can read without SSH.

`hf-timestd`'s authority manager subscribes to this contract. New
consumers should read schema.py rather than re-deriving the field
layout.

## Production paths

- Runtime state: `/run/gpsdo/<serial>.json`, `/run/gpsdo/index.json`
- Venv: `/opt/gpsdo-monitor/venv`
- Source: `/opt/git/sigmond/gpsdo-monitor` (editable install)
- Systemd unit: `gpsdo-monitor.service` (via sigmond catalog + `smd
  install gpsdo-monitor`)
- udev rules: shipped under `deploy/` so the HID is readable without
  root once installed.

## Dependencies

- `hidapi>=0.14` — HID feature-report I/O.
- `zeroconf>=0.131` — mDNS service registration.
- `pyserial>=3.5` — CDC stream for NMEA (1421/1423) and DCD-line PPS.
- Optional `[tui]` extra: `textual` (for the live-view TUI).

This is a Python-only daemon: stdlib-first style, with the three
hardware-facing libraries above as the only required deps.
