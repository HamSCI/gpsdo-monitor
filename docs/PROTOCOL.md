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
| LBE-1425  | `0x2269` | 1.4 GHz| 1421 wire format + GNSS config echoes; HID Feature + CDC | opcode = report ID; status read = `0x4B` |
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

David's 1420 reverse-engineering notes also report a CDC NMEA port and
a u-blox M10 receiver on his unit. Both bvernoux's driver and ours
assume the 1420 is HID-only (no CDC), matching the model/PID table
above. This is likely a hardware/firmware revision difference between
units — unresolved; we have no local 1420 to confirm either way.

### LBE-1421 / 1423 (Report ID `0x4B`)

| Offset | Bytes | Meaning |
|-------:|------:|---------|
| 1      | 1     | status bitmap |
| 6..9   | 4     | frequency1 (Hz, u32 LE) |
| 14..17 | 4     | frequency2 (Hz, u32 LE) |
| 18     | 1     | FLL mode (0 = PLL, 1 = FLL) |
| 19     | 1     | OUT1 power (0 = normal, 1 = low) |
| 20     | 1     | OUT2 power |
| 21..22 | 2     | observed config-echo candidates (not decoded) — see note below |
| 23     | 1     | candidate antenna bias current (mA) — **decode disabled**, not verified (see note below) |
| 24..59 | 36    | **unmapped** — candidate region for host firmware string / build date; preserve as `raw_trailing_hex` for later reverse-engineering |

Status bitmap bits (from `lbe_common.h`): PLL lock, antenna OK, OUT1
enable, OUT2 enable, PPS enable.

Byte 23 is documented per `ringof/lbe-142x` (1425 docs) as a candidate
bias-current field, but the 2026-08-13 bench check could not verify it:
the bench's antenna feed is DC-blocked (splitter), so byte 23 read `0`
with the antenna connected *and* disconnected, and `ANT_OK` never
dropped — the gate ("plausible nonzero mA when connected") was not met.
The decode is **disabled** (`Health.antenna_bias_ma` stays `None` for
the 1421/1423) pending a bench check with a powered antenna feed.

The same 2026-08-13 bench run observed bytes 21..22 as static across
that check: `0x67` (u-blox M8 default GNSS mask: GPS+SBAS+Galileo+
QZSS+GLONASS) and `0x02` (dynModel Stationary), mirroring the tail
layout described in the 1425 docs. These are recorded as an
observation only — not decoded into the schema.

### LBE-1425 (Report ID `0x4B`, inherits the 1421/1423 layout)

The 1425 rides the 1421 wire format (same offsets, same 60-byte status)
and adds GNSS-receiver configurability. Byte map for the tail region,
from David Goncalves' `ringof/lbe-142x` `docs/reverse/LBE-1425-config-v1.10.md`
(MIT):

| Offset | Meaning |
|-------:|---------|
| 21     | GNSS constellation mask echo (default `0x47` = GPS+SBAS+Galileo+GLONASS) |
| 22     | u-blox dynamic platform model echo (CFG-NAV5 `dynModel`) |
| 23     | measured antenna bias current, mA |
| 24     | NMEA output enable echo |

Unlike the 1421/1423, byte 23 **is decoded** here (`Health.antenna_bias_ma`)
— the 1425 doc verifies it directly against 1425 hardware, so the bench
gate that disabled the decode on the 1421/1423 (see above) doesn't apply
to this model.

Three GNSS-receiver config opcodes exist on the wire but are
deliberately **not implemented**: `0x03` (`SET_GNSS`), `0x04`
(`SET_DYNMODEL`), `0x0F` (`SET_NMEA`). gpsdo-monitor watches the
receiver; it doesn't reconfigure it.

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

Feature reads on the Mini are 60 payload bytes, but because it has no
Report ID the `GET_FEATURE` transfer length must be requested as 61 —
the report-ID placeholder byte is still present on the wire even
though the device sends none; shorter reads stall the device (verified
live: `OSError` on every attempt, plus usbhid interface resets in
`dmesg`).

`set_frequency` programs the divider chain via opcode `0x04`
(`SET_PLL`); `fin` (the synth's input reference) is written as part of
that payload rather than being a fixed constant on the wire. Upstream's
solver hardcodes `fin = 97600` Hz and gpsdo-monitor's port does the
same — both work. A bench Mini (serial `A7D99EE165`) was found with a
factory-programmed `fin = 95000` Hz instead; since `fin` travels with
every `SET_PLL` write, a raw-divider-register restore preserves a
unit's factory `fin` exactly, where re-solving with `solve_pll()`
would substitute 97600.

## Live GPS data

- **LBE-1421 / 1423 / 1425**: NMEA sentences (RMC, GGA, GSA, GSV) over
  the CDC port (`/dev/ttyACM*`). The u-blox 1PPS is carried on the CDC
  DCD line — `pyserial.Serial.get_cd()` + `TIOCMIWAIT` gives us edge
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
| LBE-1425 | not known | n/a |
| LBE-Mini | not known | **UBX-MON-VER** → SW (30B), HW (10B), PROTVER extension |

For the 1420/1421/1423/1425 we emit `firmware = null`, `firmware_source
= "unavailable"` and leave a hex dump of the unmapped status bytes in
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
  bias current field, the 1425 tail-byte map (`LBE-1425-config-v1.10.md`),
  and the Mini divider-solver port (`mini_solve_pll`, `src/model_mini.c`).
