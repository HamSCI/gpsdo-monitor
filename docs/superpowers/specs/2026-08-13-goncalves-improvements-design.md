# Goncalves improvements — design

**Date:** 2026-08-13
**Origin:** Letter from David Goncalves (GitHub `ringof`), maintainer of the
`ringof/lbe-142x` fork of `bvernoux/lbe-142x` (the repo PROTOCOL.md cites as
canonical). All four suggestions were verified against this codebase before
this design was written.

**Local hardware for validation:** one LBE-Mini, one LBE-1421. No 1420,
1423, or 1425 on hand; David has offered bench testing on his 1420 and 1425.

**Approach:** one branch, four TDD slices in testability order
(NAV-CLOCK → Mini solver → bias current → 1425), each independently
committable. Live-hardware steps are explicit operator checkpoints, not
pytest. A reply letter to David goes out after the design is approved,
describing what we are building and what we ask him to bench-verify.

## Slice 1 — NAV-CLOCK telemetry (Mini)

The Mini's stream bootstrap already enables NAV-CLOCK (`0x01 0x22`) on the
HID interrupt-IN endpoint (`lbe_mini.py` `_enable_stream`), but `ubx.py`
has no parser — the frames are requested and dropped.

- `ubx.py`: add `ID_NAV_CLOCK = 0x22`, a `NavClock` dataclass, and
  `parse_nav_clock()` for the standard 20-byte u-blox M8 payload:
  `iTOW` (u4, ms), `clkB` (i4, ns), `clkD` (i4, ns/s), `tAcc` (u4, ns),
  `fAcc` (u4, ps/s). Length-checked; return `None` on short payloads,
  matching `parse_nav_pvt`'s style.
- `lbe_mini.py`: `_sample_nav()` retains the newest NavClock seen in its
  sampling window and returns it alongside the existing fields.
- `schema.py`: new optional top-level `nav_clock` section in
  `DeviceReport` (sibling of `pps_study`, not inside `Health`):
  `clk_bias_ns: int`, `clk_drift_ns_s: int`, `t_acc_ns: int`,
  `f_acc_ps_s: int`, `sampled_utc: str`, and a fixed
  `note: "u-blox receiver self-report; not an independent measurement"`
  mirroring the `PpsStudy.note` honesty pattern. `None` on devices with
  no UBX stream (1420/1421/1423; 1425 monitoring is NMEA-only for now).
- `tui.py`: one display line when `nav_clock` is present.
- **Not an A-level input this round.** Publish and display only.
- Tests: `test_ubx.py` parser round-trips (crafted payloads, short
  payload, sign handling on clkB/clkD); `test_schema.py` additive-field
  serialization; `test_mini.py` retention of newest frame.
- Live checkpoint (operator): run against the local Mini, confirm
  plausible clkB/clkD values and freshness.

## Slice 2 — Mini `set_frequency` (divider solver port)

`LbeMini.set_frequency` currently raises `NotImplementedError` pending the
upstream `mini_solve_pll` port (`models/lbe_mini.py`).

- New pure module `gpsdo_monitor/mini_pll.py`:
  `solve_pll(f_out_hz: int) -> PllSolution | None`, a direct port of
  `mini_solve_pll` from `ringof/lbe-142x` `src/model_mini.c` (MIT):
  - `f_in = 97600` Hz; reduce `f_out/f_in` by GCD to `p/q`.
  - Two passes over `k = 1..4096` (pass 0 requires VCO
    `f_osc = f_in·k·p` in [5.0 GHz, 6.5 GHz]; pass 1 accepts any),
    factoring `M = k·p` as `N2_HS·N2_LS` and `D = k·q` as `N1_HS·NC1_LS`
    with `N2_HS, N1_HS ∈ [4, 11]`, `N2_LS ∈ [2, 2^20]` even,
    `NC1_LS = 1` or even in `[2, 2^20]`, `N3 = 1`.
  - `PllSolution` is a frozen dataclass carrying all six values.
- `LbeMini.set_frequency`: solve, then pack the 19-byte opcode `0x04`
  (SET_PLL) payload. Exact byte packing is lifted verbatim from the full
  `model_mini.c` during implementation (the offsets were not captured in
  the design-phase source review — the implementation plan must fetch
  the raw file, not a summary). `persist` semantics follow upstream
  behaviour; if upstream has no separate persist step, the kwarg is
  accepted and documented as always-persistent.
- Tests (`test_mini_pll.py`): exact rational round-trip
  `f_out·N3·N1_HS·NC1_LS == f_in·N2_HS·N2_LS` for a table (10 MHz,
  810 MHz, 97.6 kHz, awkward primes, 1 Hz), divider-range compliance,
  VCO-band preference when a band-limited solution exists, `None` for
  unsolvable targets, and agreement with a fixture of expected divider
  sets captured from the C tool for a handful of frequencies.
- Live checkpoint (operator, local Mini): read current frequency →
  set test frequency → read back and verify → restore original.
  README's "142x family only" note and the stub's docstring are updated.

## Slice 3 — Antenna bias current

Today only the `ANT_OK` status bit is read (1420/1421); `health.classify`
downgrades to A0 on `antenna_ok is False`. The bit cannot distinguish
"no antenna" (open) from "short". David decodes bias current directly:
byte 12 on the 1420, byte 23 on the 1421/1425 — inside the region
PROTOCOL.md marks unmapped and preserves as `raw_trailing_hex`.

- `schema.py`: `Health.antenna_bias_ma: int | None = None`.
- Decode locations:
  - **1421 (byte 23):** gated on a live verification pass (below).
  - **1425 (byte 23):** documented as mA in
    `ringof/lbe-142x` `docs/reverse/LBE-1425-config-v1.10.md`; decoded
    in the new 1425 driver (Slice 4).
  - **1420 (byte 12):** implemented per David's 1420 RE doc
    (`LBE-1420-config-v1.08.md`, to be confirmed during implementation),
    marked **unverified on our bench** in PROTOCOL.md; David is asked to
    bench-verify in the reply letter.
- **Verification gate (1421, before the decode ships enabled):**
  operator probe against the local 1421 — read status with antenna
  connected, then disconnected (and, if safe cabling allows, a shorted
  load is NOT attempted locally; short behaviour is asked of David).
  Byte 23 must drop to ~0 on open and read a plausible mA value when
  connected. If it does not behave as documented, the 1421 decode ships
  disabled (`antenna_bias_ma = None`) and the findings go into
  PROTOCOL.md and the letter.
- A-level: **logic unchanged.** `antenna_ok is False` remains the only
  antenna A0 trigger. When bias data is present it sharpens the reason
  string only: `antenna_fault (bias=0mA, open?)` when bias ≈ 0,
  `antenna_fault (bias=<n>mA)` otherwise. `classify()` gains no new
  failure predicate.
- Tests: synthesized 60-byte status frames exercising decode offsets;
  `test_health.py` reason-string sharpening cases (bias present/absent,
  zero/nonzero) with unchanged A-level outcomes.

## Slice 4 — LBE-1425 driver (monitoring-only, experimental)

No driver exists; `registry.py` raises `ValueError` for its PID.

- New `models/lbe_1425.py`: `Lbe1425(Lbe1421)`, PID `0x2269`, same
  60-byte report-ID `0x4B` wire format as the 1421, plus read-only
  decode of the documented tail bytes:
  - byte 21 → `gnss_mask` (constellation bitmask echo)
  - byte 22 → `dyn_model` (u-blox dynamic platform model echo)
  - byte 23 → `antenna_bias_ma` (documented mA)
  - byte 24 → `nmea_enabled` (output-enable echo)

  `gnss_mask` / `dyn_model` / `nmea_enabled` surface in a new optional
  top-level `receiver_config` section of `DeviceReport` (a small
  dataclass with those three fields, all `| None`), present only on the
  1425. Additive within schema v1. `antenna_bias_ma` goes in `Health`
  like the other models.
- NMEA fix / sats and 1PPS-via-DCD reuse the inherited 1421 CDC path.
- **No config opcodes** (`SET_GNSS 0x03`, `SET_DYNMODEL 0x04`,
  `SET_NMEA 0x0F` are documented in PROTOCOL.md but not implemented).
- `registry.py` entry; README feature-matrix column flagged
  *experimental — untested on hardware; protocol per ringof/lbe-142x
  reverse docs*. The reply letter asks David to run it against his 1425.
- Tests: registry dispatch by PID; tail-byte decode on synthesized
  frames; inherited-behaviour smoke tests mirroring `Lbe1423`'s.

## Cross-cutting

- **PROTOCOL.md:** add the 1425 section (PID, opcodes, tail bytes), the
  bias-current byte locations with verification status per model, and
  cite `ringof/lbe-142x` `docs/reverse/` alongside `bvernoux/lbe-142x`.
- **SCHEMA-v1.md:** document `nav_clock`, `antenna_bias_ma`, and the
  1425 read-only echoes. All additions are additive within schema v1 —
  no v2 bump.
- **README.md:** feature matrix gains the 1425 column; the Mini
  `set_frequency` caveat paragraph is replaced by solver documentation.
- **Attribution:** ringof/lbe-142x is MIT; the solver port and byte maps
  credit David Goncalves in module docstrings and PROTOCOL.md.

## Error handling

- All new decodes are defensive: short reports or absent frames yield
  `None` fields, never exceptions, matching existing model style.
- `solve_pll` returning `None` surfaces as `ValueError` from
  `set_frequency` with the requested frequency in the message (CLI and
  TUI already catch and display model errors).
- NAV-CLOCK frames failing checksum are already discarded by
  `iter_messages`; a missing NAV-CLOCK in a sampling window simply
  leaves `nav_clock = None` for that probe.

## Out of scope (this round)

- `--clocklog` CSV mode (revisit if timing forensics need arises).
- NAV-CLOCK as an A-level input.
- 1425 config opcodes.
- Any bias-current A0 trigger.
