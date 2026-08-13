# Goncalves Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the four improvements from David Goncalves' letter: NAV-CLOCK telemetry on the Mini, the Mini `set_frequency` divider solver, antenna bias-current decode (1420/1421/1425), and a monitoring-only LBE-1425 driver.

**Architecture:** Four independent slices on one branch, ordered by testability. Model drivers put new model-specific data in `RawStatus.extras` (the existing channel for variant-specific fields); `service.build_report` maps extras into new additive schema-v1 dataclasses. Live-hardware validation is done at explicit operator checkpoints (a Mini and a 1421 are on the local bench).

**Tech Stack:** Python 3.11, pytest, dataclasses; reference C source is `ringof/lbe-142x` (MIT).

**Spec:** `docs/superpowers/specs/2026-08-13-goncalves-improvements-design.md`

## Global Constraints

- Repo: `/opt/git/sigmond/gpsdo-monitor`. Run tests with `uv run pytest` from the repo root.
- All schema changes are **additive within v1** — do NOT bump `SCHEMA_VERSION` (`ka9q` note: it lives in `src/gpsdo_monitor/__init__.py` as `"v1"`).
- A-level classification gains **no new failure predicates** — `antenna_ok is False` stays the only antenna A0 trigger; bias current may only alter the *reason string*.
- The 1425 driver is monitoring-only: no `SET_GNSS`/`SET_DYNMODEL`/`SET_NMEA` opcodes.
- NAV-CLOCK values are always labeled `"u-blox receiver self-report; not an independent measurement"`.
- Ported code and byte maps credit David Goncalves / `ringof/lbe-142x` (MIT) in docstrings and PROTOCOL.md.
- Operator checkpoints (Tasks 5, 8, 11) need local hardware and pause execution; do not skip them silently — report and wait.
- Commit after every green test cycle; messages in the conventional style used by the repo (`feat:`, `fix:`, `docs:`, `test:`).

---

### Task 1: NAV-CLOCK parser in `ubx.py`

**Files:**
- Modify: `src/gpsdo_monitor/ubx.py` (constants block at ~line 26, parsers after `parse_nav_pvt` ~line 137)
- Test: `tests/test_ubx.py`

**Interfaces:**
- Produces: `ubx.ID_NAV_CLOCK: int = 0x22`; `ubx.NavClock` dataclass with fields `itow_ms: int, clk_bias_ns: int, clk_drift_ns_s: int, t_acc_ns: int, f_acc_ps_s: int`; `ubx.parse_nav_clock(payload: bytes) -> NavClock | None`. Task 2 consumes all three.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_ubx.py`:

```python
# --- NAV-CLOCK -----------------------------------------------------------


def _nav_clock_payload(
    *, itow_ms: int, clk_bias_ns: int, clk_drift_ns_s: int,
    t_acc_ns: int, f_acc_ps_s: int,
) -> bytes:
    """Pack a 20-byte UBX-NAV-CLOCK payload (u-blox M8, PROTVER 18)."""
    return (
        itow_ms.to_bytes(4, "little")
        + clk_bias_ns.to_bytes(4, "little", signed=True)
        + clk_drift_ns_s.to_bytes(4, "little", signed=True)
        + t_acc_ns.to_bytes(4, "little")
        + f_acc_ps_s.to_bytes(4, "little")
    )


def test_parse_nav_clock_round_trip():
    payload = _nav_clock_payload(
        itow_ms=433_200_000, clk_bias_ns=-1234, clk_drift_ns_s=87,
        t_acc_ns=25, f_acc_ps_s=310,
    )
    nc = parse_nav_clock(payload)
    assert nc is not None
    assert nc.itow_ms == 433_200_000
    assert nc.clk_bias_ns == -1234          # signed survives
    assert nc.clk_drift_ns_s == 87
    assert nc.t_acc_ns == 25
    assert nc.f_acc_ps_s == 310


def test_parse_nav_clock_negative_drift():
    nc = parse_nav_clock(_nav_clock_payload(
        itow_ms=0, clk_bias_ns=5, clk_drift_ns_s=-42, t_acc_ns=1, f_acc_ps_s=1,
    ))
    assert nc is not None and nc.clk_drift_ns_s == -42


def test_parse_nav_clock_short_payload_returns_none():
    assert parse_nav_clock(b"\x00" * 19) is None


def test_nav_clock_message_id():
    assert ID_NAV_CLOCK == 0x22
```

Also extend the existing import block at the top of `tests/test_ubx.py` with `ID_NAV_CLOCK, parse_nav_clock`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /opt/git/sigmond/gpsdo-monitor && uv run pytest tests/test_ubx.py -v`
Expected: ImportError — `ID_NAV_CLOCK` not defined.

- [ ] **Step 3: Implement** — in `src/gpsdo_monitor/ubx.py`, add `ID_NAV_CLOCK = 0x22` next to `ID_NAV_PVT`/`ID_NAV_SAT` (~line 28), and after `parse_nav_pvt` add:

```python
@dataclass
class NavClock:
    """Decoded UBX-NAV-CLOCK (receiver clock solution).

    All values are the u-blox receiver's own estimate of its clock
    state — a self-report, not an independent measurement. Byte map
    confirmed against ringof/lbe-142x's --clocklog decoder."""

    itow_ms: int          # GPS time of week of the solution
    clk_bias_ns: int      # receiver clock bias (signed)
    clk_drift_ns_s: int   # receiver clock drift (signed)
    t_acc_ns: int         # time accuracy estimate
    f_acc_ps_s: int       # frequency accuracy estimate


def parse_nav_clock(payload: bytes) -> NavClock | None:
    """Decode a 20-byte UBX-NAV-CLOCK payload. None on short buffer."""
    if len(payload) < 20:
        return None
    return NavClock(
        itow_ms=int.from_bytes(payload[0:4], "little"),
        clk_bias_ns=int.from_bytes(payload[4:8], "little", signed=True),
        clk_drift_ns_s=int.from_bytes(payload[8:12], "little", signed=True),
        t_acc_ns=int.from_bytes(payload[12:16], "little"),
        f_acc_ps_s=int.from_bytes(payload[16:20], "little"),
    )
```

Also update the module docstring's second sentence: the messages we decode are now NAV-PVT, NAV-CLOCK, and MON-VER.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_ubx.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gpsdo_monitor/ubx.py tests/test_ubx.py
git commit -m "feat(ubx): parse UBX-NAV-CLOCK (receiver clock bias/drift/accuracy)"
```

---

### Task 2: Mini sampler retains the newest NavClock

**Files:**
- Modify: `src/gpsdo_monitor/models/lbe_mini.py` (`_sample_nav` ~line 191, `get_status` ~line 142, import block ~line 32)
- Test: `tests/test_mini.py`

**Interfaces:**
- Consumes: `ubx.ID_NAV_CLOCK`, `ubx.parse_nav_clock`, `ubx.NavClock` (Task 1).
- Produces: `LbeMini._sample_nav()` now returns a 5-tuple `(pll, gps, sig_loss, fix, nav_clock)` where `nav_clock: ubx.NavClock | None` is the NEWEST frame seen; `LbeMini.get_status()` sets `RawStatus.extras["nav_clock"]` (a `ubx.NavClock`) when one was seen. Task 3 consumes `extras["nav_clock"]`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_mini.py` (reuse the file's `_FakeMiniHid`, `_make_mini_hid_frame`, `_make_feature_buf` helpers; look at the existing `test_get_status_*` tests around line 150 for the streaming pattern):

```python
def test_get_status_retains_newest_nav_clock():
    from gpsdo_monitor.ubx import ID_NAV_CLOCK

    def nav_clock_msg(bias_ns: int) -> bytes:
        payload = (
            (0).to_bytes(4, "little")
            + bias_ns.to_bytes(4, "little", signed=True)
            + (7).to_bytes(4, "little", signed=True)
            + (25).to_bytes(4, "little")
            + (300).to_bytes(4, "little")
        )
        return build_message(CLS_NAV, ID_NAV_CLOCK, payload)

    # Two NAV-CLOCK messages: the sampler must keep the second.
    stream = nav_clock_msg(-100) + nav_clock_msg(-250)
    frames = []
    for off in range(0, len(stream), 62):
        chunk = stream[off : off + 62].ljust(62, b"\x00")
        frames.append(_make_mini_hid_frame(
            signal_loss=0, status=0x80, payload=chunk,
        ))

    feature = _make_feature_buf(
        enabled=True, drive_idx=3,
        fin=97_600, n3=1, n2hs=10, n2ls=6250, n1hs=5, nc1=122,
    )
    hid = _FakeMiniHid(
        feature_get_replies=[feature, feature, feature],
        interrupt_frames=frames,
    )
    mini = LbeMini(hid)
    mini.nav_sample_sec = 0.1
    raw = mini.get_status()
    nc = raw.extras.get("nav_clock")
    assert nc is not None
    assert nc.clk_bias_ns == -250        # newest wins
    assert nc.clk_drift_ns_s == 7


def test_get_status_without_nav_clock_leaves_extras_empty():
    feature = _make_feature_buf(
        enabled=True, drive_idx=3,
        fin=97_600, n3=1, n2hs=10, n2ls=6250, n1hs=5, nc1=122,
    )
    hid = _FakeMiniHid(feature_get_replies=[feature, feature, feature])
    mini = LbeMini(hid)
    mini.nav_sample_sec = 0.05
    raw = mini.get_status()
    assert "nav_clock" not in raw.extras
```

NOTE: check `_make_mini_hid_frame`'s actual signature at ~line 125 of the test file and adapt the call (it asserts a 62-byte payload; the `status` byte needs bit 7 set for "carries UBX"). The trailing `\x00` padding inside a UBX-bearing frame is safe here because each message is consumed whole before padding is scanned; if the existing helper packs differently, mirror how `test_get_status` with frames does it. The two extra `feature` replies cover `_enable_stream()`'s two drain reads.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_mini.py -v -k nav_clock`
Expected: FAIL — `extras` has no `"nav_clock"` (and/or unpack error if `_sample_nav` changed arity is not yet done).

- [ ] **Step 3: Implement** — in `src/gpsdo_monitor/models/lbe_mini.py`:

1. Extend the `gpsdo_monitor.ubx` import block with `ID_NAV_CLOCK`, `NavClock`, `parse_nav_clock`.
2. Change `_sample_nav`'s signature and body:

```python
    def _sample_nav(
        self, duration_sec: float,
    ) -> tuple[bool | None, bool | None, int | None, int | None, "NavClock | None"]:
```

Inside the loop, add `nav_clock: NavClock | None = None` to the initializers, and in the `for msg in msgs:` loop, after the NAV-PVT branch:

```python
                if msg.class_id == CLS_NAV and msg.msg_id == ID_NAV_CLOCK:
                    nc = parse_nav_clock(msg.payload)
                    if nc is not None:
                        nav_clock = nc   # newest wins; bias/drift move constantly
```

Return `pll, gps, sig_loss, fix, nav_clock`. Update the docstring's tuple description.

3. In `get_status()` (~line 155) unpack the fifth element and, just before constructing `RawStatus`, add:

```python
        extras: dict[str, object] = {}
        if nav_clock is not None:
            extras["nav_clock"] = nav_clock
```

and pass `extras=extras` to `RawStatus(...)`.

- [ ] **Step 4: Run the whole Mini suite**

Run: `uv run pytest tests/test_mini.py -v`
Expected: all PASS (existing tests updated only if they unpack `_sample_nav` directly — grep for `_sample_nav(` in tests and fix arity if needed).

- [ ] **Step 5: Commit**

```bash
git add src/gpsdo_monitor/models/lbe_mini.py tests/test_mini.py
git commit -m "feat(mini): retain newest NAV-CLOCK frame in RawStatus extras"
```

---

### Task 3: `nav_clock` schema section + service/CLI publication

**Files:**
- Modify: `src/gpsdo_monitor/schema.py` (after `PpsStudy` ~line 77; `DeviceReport` ~line 87; `new_report` ~line 141)
- Modify: `src/gpsdo_monitor/service.py` (`build_report`, ~lines 175–212)
- Modify: `src/gpsdo_monitor/cli.py` (`_cmd_status` out-dict ~line 63)
- Test: `tests/test_schema.py`

**Interfaces:**
- Consumes: `RawStatus.extras["nav_clock"]` (a `ubx.NavClock`, Task 2).
- Produces: `schema.NavClockReport` dataclass (`clk_bias_ns: int, clk_drift_ns_s: int, t_acc_ns: int, f_acc_ps_s: int, sampled_utc: str, note: str`); `DeviceReport.nav_clock: NavClockReport | None = None`; `new_report(..., nav_clock: NavClockReport | None = None)`. Task 13 documents these in SCHEMA-v1.md.

- [ ] **Step 1: Write the failing test** — append to `tests/test_schema.py` (mirror the file's existing report-construction test style; check its imports first):

```python
def test_nav_clock_report_serializes_with_note():
    from gpsdo_monitor.schema import NAV_CLOCK_NOTE, NavClockReport

    nc = NavClockReport(
        clk_bias_ns=-1234, clk_drift_ns_s=87,
        t_acc_ns=25, f_acc_ps_s=310,
        sampled_utc="2026-08-13T00:00:00.000Z",
    )
    assert nc.note == NAV_CLOCK_NOTE
    assert "self-report" in nc.note


def test_device_report_nav_clock_defaults_to_none_and_round_trips():
    import json

    from gpsdo_monitor.schema import NavClockReport

    report = _make_minimal_report()          # see note below
    assert report.nav_clock is None
    report.nav_clock = NavClockReport(
        clk_bias_ns=1, clk_drift_ns_s=2, t_acc_ns=3, f_acc_ps_s=4,
        sampled_utc="2026-08-13T00:00:00.000Z",
    )
    parsed = json.loads(report.to_json())
    assert parsed["nav_clock"]["clk_bias_ns"] == 1
    assert "note" in parsed["nav_clock"]
```

NOTE: `_make_minimal_report()` — if `tests/test_schema.py` already has a helper that builds a `DeviceReport` via `new_report(...)`, reuse it; otherwise add one that calls `new_report` with a minimal `Device`/`Health`/`Outputs`/`PpsStudy` (all constructors are in `schema.py`; `Health` needs only `pll_locked` and `outputs_enabled`).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_schema.py -v -k nav_clock`
Expected: ImportError — `NavClockReport` not defined.

- [ ] **Step 3: Implement schema** — in `src/gpsdo_monitor/schema.py` after `PpsStudy`:

```python
NAV_CLOCK_NOTE = "u-blox receiver self-report; not an independent measurement"


@dataclass
class NavClockReport:
    """UBX-NAV-CLOCK snapshot from the newest frame of the last probe.

    Only devices with a UBX HID stream (LBE-Mini) produce this. The
    note is fixed: these are the receiver's own estimates of its clock
    state, not measurements against an external reference. Suggested by
    David Goncalves (ringof/lbe-142x --clocklog)."""

    clk_bias_ns: int
    clk_drift_ns_s: int
    t_acc_ns: int
    f_acc_ps_s: int
    sampled_utc: str
    note: str = NAV_CLOCK_NOTE
```

In `DeviceReport`, add after `firmware_advisory`:

```python
    nav_clock: NavClockReport | None = None
```

In `new_report`, add keyword param `nav_clock: NavClockReport | None = None` and pass it through.

- [ ] **Step 4: Wire the service** — in `service.py`'s `build_report`, after the `pps_study` block (~line 175):

```python
        nav_clock = None
        nc = raw.extras.get("nav_clock")
        if nc is not None:
            nav_clock = NavClockReport(
                clk_bias_ns=nc.clk_bias_ns,
                clk_drift_ns_s=nc.clk_drift_ns_s,
                t_acc_ns=nc.t_acc_ns,
                f_acc_ps_s=nc.f_acc_ps_s,
                sampled_utc=utc_now_iso(),
            )
```

Pass `nav_clock=nav_clock` in the `new_report(...)` call, and add `NavClockReport, utc_now_iso` to the existing `from gpsdo_monitor.schema import (...)` block (~line 51). Check `refresh_nmea_only` (~line 216): it uses `dataclasses.replace` on the last report, so `nav_clock` carries forward automatically — no change needed.

- [ ] **Step 5: Wire the CLI** — in `cli.py`'s `_cmd_status`, after the `out = {...}` dict (~line 72):

```python
        nc = raw.extras.get("nav_clock")
        if nc is not None:
            out["nav_clock"] = {
                "clk_bias_ns": nc.clk_bias_ns,
                "clk_drift_ns_s": nc.clk_drift_ns_s,
                "t_acc_ns": nc.t_acc_ns,
                "f_acc_ps_s": nc.f_acc_ps_s,
                "note": "u-blox receiver self-report; not an independent measurement",
            }
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: all PASS (service tests exercise `new_report`; a signature-order mistake shows up here).

- [ ] **Step 7: Commit**

```bash
git add src/gpsdo_monitor/schema.py src/gpsdo_monitor/service.py src/gpsdo_monitor/cli.py tests/test_schema.py
git commit -m "feat(schema): publish nav_clock section (receiver self-report) in reports and status CLI"
```

---

### Task 4: TUI column for NAV-CLOCK

**Files:**
- Modify: `src/gpsdo_monitor/tui.py` (`_Row` ~line 47, `compose` columns ~line 90, `_populate` row ~line 130, `_read_one` ~line 168, helpers ~line 184)

**Interfaces:**
- Consumes: `RawStatus.extras["nav_clock"]` (Task 2).
- Produces: nothing consumed downstream; display only.

No automated test: the TUI requires the optional `textual` extra and has no test module; the pure helper below is trivial and gets eyeballed at the Task 5 checkpoint.

- [ ] **Step 1: Implement**

1. `_Row`: add field `clk: str` after `pps` (before `error`, which has a default).
2. `compose()`: add column `"Clk"` between `"PPS"` and `"Note"`.
3. `_populate()`: add `r.clk` between `r.pps` and `r.error` in `table.add_row(...)`, and a `"—"` in the no-devices placeholder row (it must now have 11 cells).
4. Both error-path `_Row(...)` constructions in `_read_one`: add `clk="—"`.
5. Happy-path `_Row(...)`: add `clk=_fmt_nav_clock(raw.extras.get("nav_clock"))`.
6. Add the helper next to `_fmt_hz`:

```python
def _fmt_nav_clock(nc: object) -> str:
    """Compact NAV-CLOCK cell: receiver clock bias and drift.

    nc is a ubx.NavClock or None; drift in ns/s is numerically ppb."""
    if nc is None:
        return "—"
    return f"{nc.clk_bias_ns}ns {nc.clk_drift_ns_s:+d}ppb"
```

7. Footer note (`id="footer-note"` string): append `" Clk = u-blox self-reported bias/drift."`

- [ ] **Step 2: Sanity-run the suite (TUI has no tests; make sure nothing imports broke)**

Run: `uv run pytest`
Expected: all PASS.

- [ ] **Step 3: Commit**

```bash
git add src/gpsdo_monitor/tui.py
git commit -m "feat(tui): Clk column showing NAV-CLOCK bias/drift on the Mini"
```

---

### Task 5: CHECKPOINT — live NAV-CLOCK validation (OPERATOR + Mini)

**Files:** none (validation only).

- [ ] **Step 1: One-shot status against the local Mini**

Run: `cd /opt/git/sigmond/gpsdo-monitor && uv run gpsdo-monitor -v status 2>&1 | tail -40`
Expected: the Mini's JSON block contains a `nav_clock` object with plausible values — `clk_bias_ns` typically within ±1e6 ns, `clk_drift_ns_s` within ±1000, `t_acc_ns` small (tens), and the `note` string present. (If the config file doesn't declare the Mini, run with the appropriate `-c` config; `gpsdo-monitor detect` lists attached devices.)

- [ ] **Step 2: TUI eyeball**

Run: `uv run gpsdo-monitor tui` (requires the `[tui]` extra) — confirm the Clk column renders for the Mini and shows `—` for the 1421. Quit with `q`.

- [ ] **Step 3: Record the observed values in the commit message**

```bash
git commit --allow-empty -m "test(mini): live NAV-CLOCK checkpoint on bench Mini

Observed: clk_bias_ns=<value>, clk_drift_ns_s=<value>, t_acc_ns=<value>."
```

---

### Task 6: `mini_pll.py` — divider-chain solver port

**Files:**
- Create: `src/gpsdo_monitor/mini_pll.py`
- Test: `tests/test_mini_pll.py` (new)

**Interfaces:**
- Produces: `mini_pll.F_IN_HZ = 97_600`; `mini_pll.PllSolution` frozen dataclass (`fin: int, n3: int, n2_hs: int, n2_ls: int, n1_hs: int, nc1_ls: int`); `mini_pll.solve_pll(f_out_hz: int) -> PllSolution | None`. Task 7 consumes all of these.

- [ ] **Step 1: Generate the cross-check fixture from David's C solver**

```bash
S=/tmp/claude-solver && mkdir -p $S && cd $S
curl -sL https://raw.githubusercontent.com/ringof/lbe-142x/main/src/model_mini.c -o model_mini.c
sed -n '/^static int mini_solve_pll/,/^}$/p' model_mini.c > solver.c
cat > harness.c <<'EOF'
#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include "solver.c"
int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++) {
        uint32_t f = (uint32_t)strtoul(argv[i], NULL, 10);
        uint32_t fin, n3, n2hs, n2ls, n1hs, nc1;
        if (mini_solve_pll(f, &fin, &n3, &n2hs, &n2ls, &n1hs, &nc1) == 0)
            printf("%u fin=%u n3=%u n2hs=%u n2ls=%u n1hs=%u nc1=%u\n",
                   f, fin, n3, n2hs, n2ls, n1hs, nc1);
        else
            printf("%u UNSOLVABLE\n", f);
    }
    return 0;
}
EOF
gcc -o harness harness.c
./harness 10000000 810000000 97600 25000000 100000 123456789 1 809999999
```

Record the exact output — it becomes the `EXPECTED` table in the test. (The sed range relies on the function's closing brace being the first column-0 `}` after its opener — verify `solver.c` ends with `return -1;\n}` before compiling.)

- [ ] **Step 2: Write the failing tests** — create `tests/test_mini_pll.py`:

```python
"""Solver tests for the LBE-Mini divider chain.

The EXPECTED table was generated from David Goncalves' C reference
implementation (ringof/lbe-142x src/model_mini.c::mini_solve_pll, MIT)
via a standalone harness — see the implementation plan, Task 6 Step 1.
The Python port must be byte-for-byte-decision identical, so expected
tuples are exact, not merely valid."""
from __future__ import annotations

import pytest

from gpsdo_monitor.mini_pll import F_IN_HZ, PllSolution, solve_pll

# (f_out, (fin, n3, n2_hs, n2_ls, n1_hs, nc1_ls)) — from the C harness.
# REPLACE the placeholder tuples below with the harness output from
# Task 6 Step 1 before running; a mismatch here means the port diverges
# from upstream, which is a bug in the port, not the fixture.
EXPECTED = [
    (10_000_000, (97_600, 1, 10, 6250, 5, 122)),
    # ... one line per harness frequency ...
]


@pytest.mark.parametrize("f_out,expected", EXPECTED)
def test_matches_c_reference(f_out: int, expected: tuple):
    sol = solve_pll(f_out)
    assert sol is not None
    assert (sol.fin, sol.n3, sol.n2_hs, sol.n2_ls, sol.n1_hs, sol.nc1_ls) == expected


@pytest.mark.parametrize("f_out", [f for f, _ in EXPECTED])
def test_output_formula_is_exact(f_out: int):
    sol = solve_pll(f_out)
    assert sol is not None
    # f_out = fin * N2_HS * N2_LS / (N3 * N1_HS * NC1_LS), exactly.
    assert f_out * sol.n3 * sol.n1_hs * sol.nc1_ls == sol.fin * sol.n2_hs * sol.n2_ls


@pytest.mark.parametrize("f_out", [f for f, _ in EXPECTED])
def test_divider_constraints(f_out: int):
    sol = solve_pll(f_out)
    assert sol is not None
    assert 4 <= sol.n2_hs <= 11
    assert 4 <= sol.n1_hs <= 11
    assert 2 <= sol.n2_ls <= 1 << 20 and sol.n2_ls % 2 == 0
    assert sol.nc1_ls == 1 or (2 <= sol.nc1_ls <= 1 << 20 and sol.nc1_ls % 2 == 0)
    assert sol.n3 == 1
    assert sol.fin == F_IN_HZ


def test_vco_band_preferred_for_10mhz():
    sol = solve_pll(10_000_000)
    assert sol is not None
    f_osc = F_IN_HZ * sol.n2_hs * sol.n2_ls
    assert 5_000_000_000 <= f_osc <= 6_500_000_000


def test_unsolvable_returns_none():
    assert solve_pll(0) is None
    # 809,999,999 is in the Mini's range but coprime to 97,600 (odd,
    # not divisible by 5 or 61), so p = f_out > 11*2^20 and the k-loop
    # breaks at k=1 in both passes. The C harness prints UNSOLVABLE
    # for it — keep this value in sync with the harness run.
    assert solve_pll(809_999_999) is None


def test_solution_is_frozen():
    sol = solve_pll(10_000_000)
    with pytest.raises(Exception):
        sol.n3 = 2  # type: ignore[misc]
```

NOTE: confirm the `unsolvable` frequencies against the harness output (run the harness on `4294967291` too); any frequency the harness prints `UNSOLVABLE` for is a valid case here. If the harness solves it, pick one it doesn't.

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_mini_pll.py -v`
Expected: ImportError — module `gpsdo_monitor.mini_pll` does not exist.

- [ ] **Step 4: Implement** — create `src/gpsdo_monitor/mini_pll.py`:

```python
"""LBE-Mini clock-synth divider-chain solver.

Direct port of `mini_solve_pll` from ringof/lbe-142x `src/model_mini.c`
(David Goncalves, MIT). The Mini derives its output from a 97.6 kHz
disciplined reference through a divider chain:

    f_out = f_in * N2_HS * N2_LS / (N3 * N1_HS * NC1_LS)

with N2_HS, N1_HS in [4, 11]; N2_LS even in [2, 2^20]; NC1_LS = 1 or
even in [2, 2^20]; N3 fixed at 1 by upstream. The search reduces
f_out/f_in to lowest terms p/q, then walks multiples k*p / k*q looking
for a factorization that satisfies the constraints — two passes, the
first preferring a VCO (f_in * N2_HS * N2_LS) inside the synth's
native 5.0–6.5 GHz band, the second accepting any valid chain.

Pure function, no hardware dependency; iteration order matches the C
exactly so results are decision-identical with upstream (pinned by
tests/test_mini_pll.py's C-generated fixture).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

F_IN_HZ = 97_600

_VCO_MIN_HZ = 5_000_000_000
_VCO_MAX_HZ = 6_500_000_000
_HS_RANGE = range(11, 3, -1)          # 11 down to 4, matching the C loops
_LS_MAX = 1 << 20
_K_MAX = 4096


@dataclass(frozen=True)
class PllSolution:
    fin: int
    n3: int
    n2_hs: int
    n2_ls: int
    n1_hs: int
    nc1_ls: int


def solve_pll(f_out_hz: int) -> PllSolution | None:
    """Find divider values producing exactly `f_out_hz`, or None."""
    if f_out_hz <= 0:
        return None
    g = math.gcd(f_out_hz, F_IN_HZ)
    p = f_out_hz // g
    q = F_IN_HZ // g

    for band_pass in range(2):
        for k in range(1, _K_MAX + 1):
            m = k * p
            d = k * q
            if m > 11 * _LS_MAX:
                break
            if d > 11 * _LS_MAX * (1 << 19):
                break
            f_osc = F_IN_HZ * m
            if band_pass == 0 and not (_VCO_MIN_HZ <= f_osc <= _VCO_MAX_HZ):
                continue
            for nh in _HS_RANGE:
                if m % nh:
                    continue
                n2_ls = m // nh
                if n2_ls < 2 or n2_ls > _LS_MAX or n2_ls % 2:
                    continue
                for nh1 in _HS_RANGE:
                    if d % nh1:
                        continue
                    nc1 = d // nh1
                    if nc1 != 1 and not (2 <= nc1 <= _LS_MAX and nc1 % 2 == 0):
                        continue
                    return PllSolution(
                        fin=F_IN_HZ, n3=1,
                        n2_hs=nh, n2_ls=n2_ls,
                        n1_hs=nh1, nc1_ls=nc1,
                    )
    return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_mini_pll.py -v`
Expected: all PASS. If `test_matches_c_reference` fails, the port's iteration order diverges from the C — fix the port, never the fixture.

- [ ] **Step 6: Commit**

```bash
git add src/gpsdo_monitor/mini_pll.py tests/test_mini_pll.py
git commit -m "feat(mini): port mini_solve_pll divider solver (ringof/lbe-142x, MIT)"
```

---

### Task 7: `LbeMini.set_frequency`

**Files:**
- Modify: `src/gpsdo_monitor/models/lbe_mini.py` (`set_frequency` ~line 303, module docstring lines 16–22)
- Modify: `README.md` (Mini caveat paragraph, ~lines 65–68)
- Test: `tests/test_mini.py`

**Interfaces:**
- Consumes: `mini_pll.solve_pll`, `mini_pll.PllSolution` (Task 6); `LbeMini._send`, `OPC_MINI_SET_PLL = 0x04` (existing).
- Produces: working `LbeMini.set_frequency(output, hz, *, persist=True)`; raises `ValueError` on `output != 1`, out-of-range `hz`, unsolvable `hz`, or `persist=False` (upstream has no temporary-set on the Mini).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_mini.py`:

```python
# --- set_frequency -------------------------------------------------------


def test_set_frequency_packs_upstream_payload():
    hid = _FakeMiniHid()
    mini = LbeMini(hid)
    mini.set_frequency(1, 10_000_000)
    assert len(hid.feature_sets) == 1
    report_id, buf = hid.feature_sets[0]
    assert report_id == 0            # Mini uses no HID report ID
    assert buf[0] == 0x04            # OPC_MINI_SET_PLL
    # Solver result for 10 MHz (pinned by test_mini_pll.py):
    # fin=97600, n3=1, n2_hs=10, n2_ls=6250, n1_hs=5, nc1_ls=122.
    # Payload uses upstream's minus-1 / minus-4 offset encodings.
    p = buf[1:20]
    assert p[0:3] == (97_600).to_bytes(3, "little")       # fin
    assert p[3:6] == (0).to_bytes(3, "little")            # N3-1
    assert p[6] == 10 - 4                                 # N2_HS-4
    assert p[7:10] == (6250 - 1).to_bytes(3, "little")    # N2_LS-1
    assert p[10] == 5 - 4                                 # N1_HS-4
    assert p[11:14] == (122 - 1).to_bytes(3, "little")    # NC1_LS-1
    assert p[14:17] == (122 - 1).to_bytes(3, "little")    # NC2 mirrors NC1
    assert p[17] == 0                                     # SKEW
    assert p[18] == 9                                     # BW
    assert all(b == 0 for b in buf[20:])                  # rest of report zeroed


def test_set_frequency_rejects_bad_args():
    mini = LbeMini(_FakeMiniHid())
    with pytest.raises(ValueError):
        mini.set_frequency(2, 10_000_000)          # Mini has one output
    with pytest.raises(ValueError):
        mini.set_frequency(1, 0)                    # below range
    with pytest.raises(ValueError):
        mini.set_frequency(1, 900_000_000)          # above 810 MHz cap
    with pytest.raises(ValueError):
        mini.set_frequency(1, 10_000_000, persist=False)   # no temp-set on Mini


def test_set_frequency_unsolvable_raises_with_frequency_in_message():
    mini = LbeMini(_FakeMiniHid())
    # 809,999,999 Hz is inside the Mini's range but has no divider
    # chain — same value test_mini_pll.py pins as unsolvable.
    with pytest.raises(ValueError, match="no valid PLL divider chain"):
        mini.set_frequency(1, 809_999_999)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_mini.py -v -k set_frequency`
Expected: FAIL — `NotImplementedError` raised by the current stub.

- [ ] **Step 3: Implement** — replace the `set_frequency` stub in `lbe_mini.py`:

```python
    def set_frequency(self, output: int, hz: int, *, persist: bool = True) -> None:
        """Program OUT1 via the divider solver (opcode 0x04, SET_PLL).

        Payload layout per ringof/lbe-142x `mini_set_frequency` (MIT):
        3-byte LE fields with upstream's minus-1 / minus-4 encodings,
        NC2 mirroring NC1 on the single-output Mini, SKEW=0, BW=9.
        The write persists in device flash; upstream has no temporary
        variant on the Mini, so `persist=False` is rejected."""
        if output != 1:
            raise ValueError("LBE-Mini only has output 1")
        if not persist:
            raise ValueError("temporary frequency is not supported on the Mini")
        if hz < 1 or hz > self.capabilities.max_freq_hz:
            raise ValueError(f"frequency {hz} Hz out of range")
        sol = solve_pll(hz)
        if sol is None:
            raise ValueError(f"no valid PLL divider chain for {hz} Hz")
        p = bytearray(19)
        p[0:3] = sol.fin.to_bytes(3, "little")
        p[3:6] = (sol.n3 - 1).to_bytes(3, "little")
        p[6] = sol.n2_hs - 4
        p[7:10] = (sol.n2_ls - 1).to_bytes(3, "little")
        p[10] = sol.n1_hs - 4
        nc1_minus_1 = (sol.nc1_ls - 1).to_bytes(3, "little")
        p[11:14] = nc1_minus_1
        p[14:17] = nc1_minus_1
        p[17] = 0   # SKEW
        p[18] = 9   # BW
        self._send(OPC_MINI_SET_PLL, bytes(p))
```

Add `from gpsdo_monitor.mini_pll import solve_pll` to the imports. Update the module docstring (lines 16–22): the solver is now ported (`gpsdo_monitor.mini_pll`, credit ringof/lbe-142x) — delete the NotImplementedError paragraph.

- [ ] **Step 4: Run the Mini suite and full suite**

Run: `uv run pytest tests/test_mini.py -v && uv run pytest`
Expected: all PASS.

- [ ] **Step 5: Update README** — replace the caveat paragraph (~lines 65–68):

```markdown
`set_frequency` is implemented for the whole family. The Mini path
solves the Si-synth divider chain in pure Python
(`gpsdo_monitor/mini_pll.py`, ported from David Goncalves'
ringof/lbe-142x, MIT) and was live-validated against a bench Mini.
Mini frequency writes always persist (the hardware has no
temporary-set opcode).
```

- [ ] **Step 6: Commit**

```bash
git add src/gpsdo_monitor/models/lbe_mini.py tests/test_mini.py README.md
git commit -m "feat(mini): implement set_frequency via ported divider solver"
```

---

### Task 8: CHECKPOINT — live `set_frequency` validation (OPERATOR + Mini)

**Files:** none (validation only).

- [ ] **Step 1: Set/read-back/restore cycle on the bench Mini**

Run (adjust nothing else; this restores the original frequency at the end):

```bash
cd /opt/git/sigmond/gpsdo-monitor && uv run python - <<'EOF'
from gpsdo_monitor.hid_xport import enumerate_lbe, HidDevice
from gpsdo_monitor.models import open_model

minis = [c for c in enumerate_lbe() if c.model == "lbe-mini"]
assert minis, "no Mini attached"
with open_model(minis[0]) as m:
    before = m.get_status().outputs.out1_hz
    print("before:", before, "Hz")
    # Guard: prove we can restore BEFORE changing anything. If the
    # read-back frequency isn't exactly expressible (divider rounding),
    # abort rather than strand the device at the test frequency.
    from gpsdo_monitor.mini_pll import solve_pll
    assert solve_pll(before) is not None, f"cannot restore {before} Hz; aborting untouched"
    m.set_frequency(1, 10_000_000)
with open_model(minis[0]) as m:
    after = m.get_status().outputs.out1_hz
    print("after set(10 MHz):", after, "Hz")
    assert after == 10_000_000, "read-back mismatch"
    m.set_frequency(1, before)
with open_model(minis[0]) as m:
    restored = m.get_status().outputs.out1_hz
    print("restored:", restored, "Hz")
    assert restored == before, "restore failed"
print("OK")
EOF
```

Expected: `OK`, with `after set(10 MHz): 10000000 Hz`. If read-back mismatches, STOP — do not retry with other frequencies; report the observed feature-report bytes.

- [ ] **Step 2: Record the checkpoint**

```bash
git commit --allow-empty -m "test(mini): live set_frequency checkpoint on bench Mini

Set 10 MHz, read back exact, restored original <value> Hz."
```

---

### Task 9: `antenna_bias_ma` in Health + A-level reason sharpening

**Files:**
- Modify: `src/gpsdo_monitor/schema.py` (`Health`, after `antenna_ok` ~line 48)
- Modify: `src/gpsdo_monitor/health.py` (`classify`, antenna branch ~line 47)
- Test: `tests/test_health.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Health.antenna_bias_ma: int | None = None` (Task 10 populates it); sharpened A0 reason strings `antenna_fault (bias=0mA, open?)` / `antenna_fault (bias=<n>mA, short?)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_health.py` (mirror its existing `classify(...)` call style — check how existing tests build `Health` and call `classify` first, and reuse their kwargs):

```python
def test_antenna_fault_reason_names_open_when_bias_zero():
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=False, antenna_bias_ma=0)
    level, reason = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert level == "A0"
    assert reason == "antenna_fault (bias=0mA, open?)"


def test_antenna_fault_reason_names_short_when_bias_nonzero():
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=False, antenna_bias_ma=63)
    level, reason = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert level == "A0"
    assert reason == "antenna_fault (bias=63mA, short?)"


def test_antenna_fault_reason_unchanged_without_bias():
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=False)
    level, reason = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert (level, reason) == ("A0", "antenna_fault")


def test_bias_alone_never_downgrades():
    # bias present + antenna_ok True: A-level logic must ignore bias.
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=True, fix_age_sec=1.0,
               antenna_bias_ma=0)
    level, _ = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert level == "A1"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_health.py -v -k bias`
Expected: FAIL — `Health` has no `antenna_bias_ma`.

- [ ] **Step 3: Implement**

`schema.py` — in `Health`, after `antenna_ok`:

```python
    # Measured antenna bias current (mA) where the model exposes it
    # (1420 byte 12, 1421/1425 byte 23 — per ringof/lbe-142x reverse
    # docs). Distinguishes open (0 mA) from short (high) where the
    # ANT_OK bit alone can't. Diagnostic only: never an A-level input.
    antenna_bias_ma: int | None = None
```

`health.py` — replace the two-line antenna branch:

```python
    if health.antenna_ok is False:
        bias = health.antenna_bias_ma
        if bias is None:
            return "A0", "antenna_fault"
        if bias == 0:
            return "A0", "antenna_fault (bias=0mA, open?)"
        # ANT_OK clears on over-current (1425 doc): nonzero bias with
        # the fault bit set points at a short, not a missing antenna.
        return "A0", f"antenna_fault (bias={bias}mA, short?)"
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_health.py tests/test_schema.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gpsdo_monitor/schema.py src/gpsdo_monitor/health.py tests/test_health.py
git commit -m "feat(health): antenna_bias_ma field sharpens antenna_fault reason (open vs short)"
```

---

### Task 10: Bias-current decode in the 1420/1421 drivers

**Files:**
- Modify: `src/gpsdo_monitor/models/lbe_1421.py` (`get_status` ~line 81, layout docstring lines 6–21)
- Modify: `src/gpsdo_monitor/models/lbe_1420.py` (`get_status` ~line 77, layout docstring lines 18–27)
- Modify: `docs/PROTOCOL.md` (status layout tables)
- Test: `tests/test_models.py`

**Interfaces:**
- Consumes: `Health.antenna_bias_ma` (Task 9).
- Produces: `Lbe1421.get_status()` (and by inheritance `Lbe1423`) sets `health.antenna_bias_ma = buf[23]`; `Lbe1420.get_status()` sets `health.antenna_bias_ma = buf[12]`.

- [ ] **Step 1: Write the failing tests** — in `tests/test_models.py`, extend the two status-builder helpers with a bias kwarg and add tests:

In `_make_status_1421(...)` add parameter `bias_ma: int = 0` and, in the body, `buf[23] = bias_ma`. In `_make_status_1420(...)` add `bias_ma: int = 0` and `buf[12] = bias_ma`. Then append:

```python
def test_1421_decodes_antenna_bias_current():
    buf = _make_status_1421(
        raw_bitmap=PLL_LOCK_BIT | GPS_LOCK_BIT | ANT_OK_BIT,
        freq1_hz=10_000_000, freq2_hz=10_000_000, bias_ma=5,
    )
    model = Lbe1421(_FakeHid({0x4B: buf}))
    raw = model.get_status()
    assert raw.health.antenna_bias_ma == 5
    # Paper trail unchanged: trailing hex still starts at byte 21.
    assert raw.raw_trailing_hex.split()[2] == "05"


def test_1420_decodes_antenna_bias_current():
    buf = _make_status_1420(
        raw_bitmap=PLL_LOCK_BIT | GPS_LOCK_BIT | ANT_OK_BIT,
        freq1_hz=10_000_000, bias_ma=4,
    )
    model = Lbe1420(_FakeHid({0x4B: buf}))
    raw = model.get_status()
    assert raw.health.antenna_bias_ma == 4
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_models.py -v -k bias`
Expected: FAIL — `antenna_bias_ma` is None.

- [ ] **Step 3: Implement**

`lbe_1421.py` `get_status`: after `pw2 = bool(buf[20])` add `bias = buf[23]`, and add `antenna_bias_ma=bias,` to the `Health(...)` construction. Update the class/layout docstring line for offsets 21..59:

```
  21..22  2     unmapped
  23      1     antenna bias current, mA (ringof/lbe-142x; live-verified
                against a bench 1421 — see PROTOCOL.md)
  24..59  36    unmapped — preserved as raw_trailing_hex for later RE
```

(`raw_trailing_hex` itself still starts at byte 21 for continuity of the existing paper trail.)

`lbe_1420.py` `get_status`: after `fll = bool(buf[18])` add `bias = buf[12]`, add `antenna_bias_ma=bias,` to `Health(...)`, and document byte 12 in the layout docstring with the caveat `(per ringof/lbe-142x LBE-1420-config-v1.08.md; NOT verified on our bench — no local 1420)`.

- [ ] **Step 4: Update `docs/PROTOCOL.md`** — in the 1421/1423 table, split the `21..59` row into `21..22 unmapped`, `23 antenna bias current (mA)`, `24..59 unmapped`; in the 1420 section add a row `12 — antenna bias current (mA; 0 = no antenna)`. Add a verification-status note under each: 1421 byte 23 = *pending Task 11 bench check* (updated to "live-verified" by Task 11), 1420 byte 12 = *per ringof/lbe-142x docs, unverified on our bench; David Goncalves offered bench confirmation*. Add `https://github.com/ringof/lbe-142x` (`docs/reverse/`) to the See-also list.

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_models.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/gpsdo_monitor/models/lbe_1421.py src/gpsdo_monitor/models/lbe_1420.py docs/PROTOCOL.md tests/test_models.py
git commit -m "feat(models): decode antenna bias current (1420 byte 12, 1421 byte 23)"
```

---

### Task 11: CHECKPOINT — live 1421 bias-current verification (OPERATOR + 1421)

**Files:**
- Possibly modify: `src/gpsdo_monitor/models/lbe_1421.py`, `docs/PROTOCOL.md` (contingency + status update)

- [ ] **Step 1: Probe with antenna connected**

```bash
cd /opt/git/sigmond/gpsdo-monitor && uv run python - <<'EOF'
from gpsdo_monitor.hid_xport import enumerate_lbe
from gpsdo_monitor.models import open_model

devs = [c for c in enumerate_lbe() if c.model in ("lbe-1421", "lbe-1423")]
assert devs, "no 1421 attached"
with open_model(devs[0]) as m:
    raw = m.get_status()
print("antenna_ok:", raw.health.antenna_ok)
print("bias_ma:", raw.health.antenna_bias_ma)
print("trailing:", raw.raw_trailing_hex)
EOF
```

Expected with antenna connected: `antenna_ok: True`, `bias_ma` a small nonzero value (the 1425 reads ~5 mA; the 1421 should be similar).

- [ ] **Step 2: Repeat with the antenna disconnected**

Unscrew the antenna, wait ~5 s, run the same snippet.
Expected: `bias_ma: 0` (and typically `antenna_ok: False`). Reconnect the antenna afterward and confirm the value returns.

- [ ] **Step 3a (verified): update PROTOCOL.md and record**

Change the 1421 byte-23 verification note to `live-verified on bench 1421 (open→0 mA, connected→<n> mA), 2026-08-13`. Commit:

```bash
git add docs/PROTOCOL.md
git commit -m "docs(protocol): 1421 byte-23 bias current live-verified on bench

Connected: <n> mA; antenna removed: 0 mA."
```

- [ ] **Step 3b (contingency — byte 23 does NOT behave as documented):** revert the 1421 decode to `antenna_bias_ma=None` (keep the 1420/1425 decodes and their caveats), record the observed trailing-hex bytes in PROTOCOL.md's unmapped-region note, delete `test_1421_decodes_antenna_bias_current`, and commit with the findings — they go to David. Do not guess alternative offsets.

---

### Task 12: LBE-1425 monitoring-only driver

**Files:**
- Create: `src/gpsdo_monitor/models/lbe_1425.py`
- Modify: `src/gpsdo_monitor/models/lbe_1421.py` (split `get_status` → `_parse_status`)
- Modify: `src/gpsdo_monitor/models/registry.py`
- Modify: `src/gpsdo_monitor/schema.py` (ReceiverConfig + DeviceReport + new_report)
- Modify: `src/gpsdo_monitor/service.py` (`build_report`)
- Modify: `src/gpsdo_monitor/cli.py` (`_cmd_status`)
- Test: `tests/test_models.py`, `tests/test_schema.py`

**Interfaces:**
- Consumes: `Lbe1421` internals (Task 10's bias decode), `Health.antenna_bias_ma`.
- Produces: `Lbe1425` with `pid = 0x2269`, registered in `REGISTRY`; `RawStatus.extras["receiver_config"] = {"gnss_mask": int, "dyn_model": int, "nmea_enabled": bool}`; `schema.ReceiverConfig` dataclass (`gnss_mask: int | None, dyn_model: int | None, nmea_enabled: bool | None`); `DeviceReport.receiver_config: ReceiverConfig | None = None`; `new_report(..., receiver_config=None)`.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_models.py`:

```python
def test_1425_registry_dispatch():
    from gpsdo_monitor.models.lbe_1425 import Lbe1425
    from gpsdo_monitor.models.registry import REGISTRY

    assert REGISTRY[0x2269] is Lbe1425
    assert Lbe1425.name == "lbe-1425"


def test_1425_decodes_tail_echoes():
    from gpsdo_monitor.models.lbe_1425 import Lbe1425

    buf = bytearray(_make_status_1421(
        raw_bitmap=PLL_LOCK_BIT | GPS_LOCK_BIT | ANT_OK_BIT,
        freq1_hz=10_000_000, freq2_hz=10_000_000, bias_ma=5,
    ))
    buf[21] = 0x47      # GNSS mask: GPS+SBAS+Galileo+GLONASS (1425 default)
    buf[22] = 0x02      # dynModel: Stationary
    buf[24] = 0x01      # NMEA output enabled
    model = Lbe1425(_FakeHid({0x4B: bytes(buf)}))
    raw = model.get_status()
    assert raw.health.antenna_bias_ma == 5           # inherited decode
    assert raw.extras["receiver_config"] == {
        "gnss_mask": 0x47, "dyn_model": 0x02, "nmea_enabled": True,
    }
    assert raw.health.pll_locked                     # 1421 parse still intact
```

And to `tests/test_schema.py`:

```python
def test_receiver_config_round_trips_in_report():
    import json

    from gpsdo_monitor.schema import ReceiverConfig

    report = _make_minimal_report()
    assert report.receiver_config is None
    report.receiver_config = ReceiverConfig(
        gnss_mask=0x47, dyn_model=2, nmea_enabled=False,
    )
    parsed = json.loads(report.to_json())
    assert parsed["receiver_config"] == {
        "gnss_mask": 0x47, "dyn_model": 2, "nmea_enabled": False,
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_models.py tests/test_schema.py -v -k "1425 or receiver_config"`
Expected: ImportError — no module `lbe_1425` / no `ReceiverConfig`.

- [ ] **Step 3: Refactor 1421 for subclass parsing** — in `lbe_1421.py`, split `get_status` into:

```python
    def get_status(self) -> RawStatus:
        buf = self.hid.feature_get(STATUS_REPORT_ID, REPORT_SIZE)
        return self._parse_status(buf)

    def _parse_status(self, buf: bytes) -> RawStatus:
        # ... the existing body from `raw = buf[1]` down to the
        # RawStatus(...) return, unchanged ...
```

Run `uv run pytest tests/test_models.py -v` — must stay green before continuing.

- [ ] **Step 4: Create `src/gpsdo_monitor/models/lbe_1425.py`:**

```python
"""LBE-1425 protocol driver — monitoring only.

The 1425 rides the 1421 wire format (Report ID 0x4B, 60-byte status)
and adds GNSS-receiver configurability. Byte map from David Goncalves'
reverse-engineering notes (ringof/lbe-142x
`docs/reverse/LBE-1425-config-v1.10.md`, MIT):

  21  GNSS constellation mask echo (default 0x47 = GPS+SBAS+Gal+GLO)
  22  u-blox dynamic platform model echo (CFG-NAV5 dynModel)
  23  measured antenna bias current, mA (decoded by the 1421 parent)
  24  NMEA output enable echo

EXPERIMENTAL: no 1425 has been on our bench; the decode follows the
doc above and awaits hardware confirmation. The 1425's config opcodes
(0x03 SET_GNSS, 0x04 SET_DYNMODEL, 0x0F SET_NMEA) are deliberately NOT
implemented — gpsdo-monitor watches; it doesn't reconfigure receivers.
"""
from __future__ import annotations

from gpsdo_monitor.models.base import RawStatus
from gpsdo_monitor.models.lbe_1421 import Lbe1421


class Lbe1425(Lbe1421):
    name = "lbe-1425"
    pid = 0x2269

    def _parse_status(self, buf: bytes) -> RawStatus:
        raw = super()._parse_status(buf)
        raw.extras["receiver_config"] = {
            "gnss_mask": buf[21],
            "dyn_model": buf[22],
            "nmea_enabled": bool(buf[24]),
        }
        return raw
```

Register it in `registry.py` (import `Lbe1425`, add `Lbe1425.pid: Lbe1425` to `REGISTRY`).

- [ ] **Step 5: Schema + service + CLI wiring**

`schema.py`, after `NavClockReport`:

```python
@dataclass
class ReceiverConfig:
    """Read-only GNSS-receiver configuration echoes (LBE-1425 only).

    Raw values as the status report carries them; decoding the GNSS
    bitmask into constellation names is the consumer's business."""

    gnss_mask: int | None = None
    dyn_model: int | None = None
    nmea_enabled: bool | None = None
```

`DeviceReport`: add `receiver_config: ReceiverConfig | None = None` after `nav_clock`. `new_report`: add and pass through `receiver_config: ReceiverConfig | None = None`.

`service.py` `build_report`, next to the nav_clock block:

```python
        receiver_config = None
        rc = raw.extras.get("receiver_config")
        if rc is not None:
            receiver_config = ReceiverConfig(**rc)
```

Pass `receiver_config=receiver_config` to `new_report(...)`; add `ReceiverConfig` to the schema import block.

`cli.py` `_cmd_status`, next to the nav_clock block:

```python
        rc = raw.extras.get("receiver_config")
        if rc is not None:
            out["receiver_config"] = rc
```

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add src/gpsdo_monitor/models/lbe_1425.py src/gpsdo_monitor/models/lbe_1421.py src/gpsdo_monitor/models/registry.py src/gpsdo_monitor/schema.py src/gpsdo_monitor/service.py src/gpsdo_monitor/cli.py tests/test_models.py tests/test_schema.py
git commit -m "feat(models): LBE-1425 monitoring-only driver (experimental, per ringof/lbe-142x docs)"
```

---

### Task 13: Documentation sweep + final verification

**Files:**
- Modify: `docs/PROTOCOL.md`, `docs/SCHEMA-v1.md`, `README.md`

**Interfaces:** none — docs only.

- [ ] **Step 1: PROTOCOL.md** — add the LBE-1425 to the model/PID table (`0x2269`, "1421 wire format + GNSS config echoes; HID Feature + CDC"); add a `### LBE-1425` status-layout subsection listing tail bytes 21–24 (from the Task 12 module docstring) and naming the three unimplemented config opcodes; note that David's 1420 doc reports CDC NMEA and a u-blox M10 on his 1420 unit (bvernoux's driver and ours assume no CDC — likely a hardware/firmware revision difference, unresolved); ensure `ringof/lbe-142x` appears in See-also (done in Task 10 — verify).

- [ ] **Step 2: SCHEMA-v1.md** — document the three additive fields with one example block each, marked "added 2026-08-13, additive within v1": `health.antenna_bias_ma` (int mA | null; note per-model verification status), `nav_clock` (object | null; include the fixed `note` string), `receiver_config` (object | null; 1425 only). Follow the file's existing field-description format.

- [ ] **Step 3: README.md** — add an LBE-1425 column to the feature matrix (HID status ✓, NMEA ✓ CDC, PPS ✓ DCD, OUT2 ✓, max 1.4 GHz footnoted "per 1421 protocol; 1425 doc notes asymmetric limits — unverified") with an `*experimental — untested on hardware*` footnote; add a bias-current row (1420 ✓†, 1421 ✓, 1423 —?, 1425 ✓†, Mini —; † = per ringof docs, not bench-verified here — adjust the 1421 cell per Task 11's outcome).

- [ ] **Step 4: Final full verification**

Run: `uv run pytest`
Expected: all PASS, zero warnings introduced. Then `git log --oneline main..HEAD` — confirm every task committed.

- [ ] **Step 5: Commit**

```bash
git add docs/PROTOCOL.md docs/SCHEMA-v1.md README.md
git commit -m "docs: 1425 protocol section, schema v1 additive fields, README matrix update"
```

---

## Post-plan

After all tasks: use superpowers:finishing-a-development-branch (merge to main per repo trunk-based convention). Then the follow-up letter to David goes out with the Task 5/8/11 checkpoint results and the ask list (1420 byte-12 confirmation, 1421 short-case behaviour, 1425 driver bench run).
