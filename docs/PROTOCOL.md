# HID Protocol Reference

This document summarizes what `gpsdo-monitor` needs to talk to the four
Leo Bodnar variants. **The canonical reference is
[bvernoux/lbe-142x](https://github.com/bvernoux/lbe-142x)** — the
opcodes and layouts below are a condensed port of its
`src/model_*.c` and `include/lbe_common.h`.

USB vendor ID: **`0x1DD2`** (Leo Bodnar Electronics).

| Model     | PID      | Max f  | Transport convention                     | Report ID scheme |
|-----------|----------|--------|------------------------------------------|------------------|
| LBE-1420  | `0x2443` | 1.6 GHz| HID Feature Reports                      | opcode = report ID; status read = `0x4B` |
| LBE-1421  | `0x2444` | 1.4 GHz| HID Feature Reports + CDC (NMEA, 1PPS)   | opcode = report ID; status read = `0x4B` |
| LBE-1423  | `0x226F` | 1.4 GHz| Same wire format as 1421                 | opcode = report ID; status read = `0x4B` |
| LBE-Mini  | `0x2211` | 810 MHz| HID Feature Reports + HID interrupt-IN   | **no report ID**; UBX wrap for u-blox pass-through |

Payload size is **60 bytes** (`LBE_REPORT_SIZE`). For Report-ID devices
the transport prepends the report ID byte; for the Mini the wire
payload is 60 bytes with no prefix.

## Status feature-report layouts

### LBE-1420 (`get_status` returns 60 bytes from Report ID `0x4B`)

| Offset | Meaning |
|-------:|---------|
| 1      | status bitmap (`PLL_LOCK`, `ANT_OK`, `OUT1_EN`, `OUT2_EN`, `PPS_EN`) |
| 1..4   | frequency1 (u32 little-endian) on 1420 |
| 12     | antenna bias current (mA; 0 = no antenna) |
| …      | 1420 layout differs from 1421; see `src/model_1420.c` upstream |

Byte 12 is documented per `ringof/lbe-142x` `docs/reverse/LBE-1420-config-v1.08.md`;
unverified on our bench (no local 1420). David Goncalves offered bench
confirmation.

### LBE-1421 / 1423 (Report ID `0x4B`)

| Offset | Bytes | Meaning |
|-------:|------:|---------|
| 1      | 1     | status bitmap |
| 6..9   | 4     | frequency1 (Hz, u32 LE) |
| 14..17 | 4     | frequency2 (Hz, u32 LE) |
| 18     | 1     | FLL mode (0 = PLL, 1 = FLL) |
| 19     | 1     | OUT1 power (0 = normal, 1 = low) |
| 20     | 1     | OUT2 power |
| 21..22 | 2     | unmapped |
| 23     | 1     | antenna bias current (mA) |
| 24..59 | 36    | **unmapped** — candidate region for host firmware string / build date; preserve as `raw_trailing_hex` for later reverse-engineering |

Status bitmap bits (from `lbe_common.h`): PLL lock, antenna OK, OUT1
enable, OUT2 enable, PPS enable.

Byte 23 is documented per `ringof/lbe-142x` `docs/reverse/`; verification
against a bench 1421 is pending Task 11's bench check (this note will be
updated to "live-verified" once that check runs).

### LBE-Mini (no Report ID)

Status is a short feature report documented against the vendor v1.17 UI
in upstream `src/model_mini.c`. Relevant fields:

- `pll_locked` bit
- `gps_lock` bit
- `outputs_enabled`
- `signal_loss_count` (running count from firmware, resets on power-up)
- OUT1 drive strength (8/16/24/32 mA)
- OUT1 frequency (Hz, u32)

No antenna-OK indicator and no OUT2 / PPS fields (Mini has one output,
no 1PPS).

## Live GPS data

- **LBE-1421 / 1423**: NMEA sentences (RMC, GGA, GSA, GSV) over the CDC
  port (`/dev/ttyACM*`). The u-blox 1PPS is carried on the CDC DCD
  line — `pyserial.Serial.get_cd()` + `TIOCMIWAIT` gives us edge
  timestamps.
- **LBE-Mini**: UBX binary (`0xB5 0x62` sync, Fletcher-8 checksum) on
  a HID interrupt-IN endpoint, wrapped in a Leo Bodnar frame header.
  Messages of interest: `NAV-PVT` (UTC + fix), `NAV-SAT` (per-SV CNR),
  `NAV-CLOCK` (receiver clock stats), `MON-VER` (SW/HW string +
  PROTVER).

## Firmware version readback

| Device | Host firmware readback | GPS-module firmware readback |
|--------|------------------------|------------------------------|
| LBE-1420 | not known | n/a |
| LBE-1421 | not known (candidate region: bytes 21..59 of status report — uncharacterized) | n/a |
| LBE-1423 | not known | n/a |
| LBE-Mini | not known | **UBX-MON-VER** → SW (30B), HW (10B), PROTVER extension |

For the 1420/1421/1423 we emit `firmware = null`, `firmware_source =
"unavailable"` and leave a hex dump of the unmapped status bytes in
`raw_trailing_hex` so future reverse engineering has a paper trail.

For the Mini we emit the u-blox strings plus a computed
`firmware_advisory` keyed by PROTVER against
`data/firmware_advisories.toml`.

## See also

- Upstream source: https://github.com/bvernoux/lbe-142x (MIT)
- Mini reverse-engineering notes: `docs/reverse/LBE-Mini-config-v1.10.md`
  in the upstream repo (referenced from `include/lbe_common.h`).
- Additional reverse-engineering notes: https://github.com/ringof/lbe-142x
  (`docs/reverse/`) — source for the 1420 byte 12 / 1421 byte 23 antenna
  bias current field.
