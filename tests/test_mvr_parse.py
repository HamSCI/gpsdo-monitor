"""MVR GPSDO B: parsing of the console stream (pure functions, no I/O).

Fixtures come from the developer notes (mvr_console_interface.pdf,
firmware 20261008.1) and from N8UR's unit, 2026-11-14."""
from __future__ import annotations

import pytest

from gpsdo_monitor import mvr
from gpsdo_monitor.mvr import (
    MenuInfo,
    MvrState,
    apply_menu_line,
    feed_line,
    parse_banner,
    parse_lock_event,
    parse_status_line,
    unwrap_pmvr,
)

N8UR_STATUS = ("[110100] GPS: DGPS fix, 12 sats | Loop: LOCKED | "
               "CLK0 27000000Hz CLK1 27000000Hz CLK2 off")
DOC_STATUS = "[123] GPS: GPS fix, 9 sats | Loop: LOCKED | CLK0 27000000Hz CLK1 off CLK2 off"
BANNER = "=== MVR GPSDO B Firmware v20261008.1 (built Oct  8 2026 09:50:46) ==="

# Live captures from bee1's LBE-1421 (also used by test_nmea / test_service).
RMC_VALID = "$GNRMC,024825.40,A,3855.12213,N,09207.65474,W,0.001,,240426,,,D*73"
GGA_FIX = "$GNGGA,024825.40,3855.12213,N,09207.65474,W,2,10,0.48,265.6,M,-30.7,M,,0000*7D"
GSA_3D = "$GNGSA,A,3,10,32,23,28,02,24,25,12,31,01,,,0.91,0.48,0.77*10"

# The notes' own $PMVR examples (checksums verified against the XOR rule).
PMVR_STATUS = ("$PMVR,MSG,[123] GPS: GPS fix, 9 sats | Loop: LOCKED | "
               "CLK0 27000000Hz CLK1 off CLK2 off*57")
PMVR_GPS_LOCKED = "$PMVR,MSG,[45] GPS: LOCKED*33"
PMVR_LOOP_LOST = "$PMVR,MSG,[67] Loop: LOST*45"
PMVR_CONFIG = "$PMVR,MSG,GPS02-UBX Configuration*14"
PMVR_OK_GGA = "$PMVR,MSG,  [OK  ] GGA  (Fix data)  (ACK)*0D"
PMVR_HALTED = "$PMVR,MSG,''' HALTED '''*50"


# --- status line ---------------------------------------------------------


def test_status_line_n8ur():
    st = parse_status_line(N8UR_STATUS)
    assert st is not None
    assert st.uptime_sec == 110100
    assert st.gps_state == "fix"
    assert st.fix_type == "DGPS fix"
    assert st.sats == 12
    assert st.loop == "LOCKED"
    assert st.clk_hz == (27_000_000, 27_000_000, 0)


@pytest.mark.parametrize("fix", [
    "GPS fix", "DGPS fix", "RTK fixed", "RTK float", "dead reckoning", "unknown",
])
def test_status_line_every_fix_name(fix):
    st = parse_status_line(
        f"[9] GPS: {fix}, 7 sats | Loop: unlocked | CLK0 10000000Hz CLK1 off CLK2 off")
    assert st is not None
    assert st.fix_type == fix
    assert st.gps_state == "fix"
    assert st.sats == 7
    assert st.loop == "unlocked"


def test_status_line_no_data_both_sides():
    st = parse_status_line("[5] GPS: no data | Loop: no data | CLK0 off CLK1 off CLK2 off")
    assert st is not None
    assert st.gps_state == "no data"
    assert st.fix_type is None
    assert st.sats is None
    assert st.loop == "no data"
    assert st.clk_hz == (0, 0, 0)


def test_status_line_clk2_on():
    st = parse_status_line(
        "[1] GPS: GPS fix, 4 sats | Loop: LOCKED | CLK0 off CLK1 10000000Hz CLK2 2500Hz")
    assert st.clk_hz == (0, 10_000_000, 2500)


@pytest.mark.parametrize("junk", [
    "", "[45] GPS: LOCKED", BANNER, "Setup complete. Type any character for the Si5351 menu.",
    "[x] GPS: GPS fix, 9 sats | Loop: LOCKED | CLK0 off CLK1 off CLK2 off",
])
def test_status_line_rejects_non_status(junk):
    assert parse_status_line(junk) is None


# --- lock events -----------------------------------------------------------


@pytest.mark.parametrize("line,expect", [
    ("[45] GPS: LOCKED", (45, "GPS", "LOCKED")),
    ("[67] Loop: LOST", (67, "Loop", "LOST")),
    ("[1] GPS: LOST", (1, "GPS", "LOST")),
    ("[2] Loop: LOCKED", (2, "Loop", "LOCKED")),
])
def test_lock_events(line, expect):
    assert parse_lock_event(line) == expect


def test_lock_event_rejects_status_line():
    assert parse_lock_event(DOC_STATUS) is None


# --- banner / menu header -----------------------------------------------------


def test_banner_names_firmware():
    assert parse_banner(BANNER) == ("v20261008.1", "Oct  8 2026 09:50:46")
    assert parse_banner("=== Lock History ===") is None


MENU = """=== MVR GPSDO B Firmware v20261008.1 (built Oct  8 2026 09:50:46) ===
  1) Set CLK0 frequency  (currently 27000000 Hz)
  2) Set CLK1 frequency  (currently off)
  3) Set CLK2 frequency  (currently off)
  4) Set drive strength, all channels (currently 8 mA)
  5) Show this menu again
  6) Show lock history
  7) Restore compile-time defaults (overwrites saved config)
  8) Toggle raw NMEA passthrough (currently OFF; boot default OFF)
  9) Reboot into firmware-update (UF2) mode
 99) Exit menu
> """


def test_menu_header_parses_every_state_field():
    info = MenuInfo()
    for ln in MENU.splitlines():
        apply_menu_line(info, ln)
    assert info.firmware == "v20261008.1"
    assert info.clk_hz == [27_000_000, 0, 0]
    assert info.drive_ma == 8
    assert info.passthrough is False
    assert info.passthrough_boot is False
    assert info.complete


def test_menu_header_passthrough_on_boot_off():
    info = MenuInfo()
    apply_menu_line(info, "  8) Toggle raw NMEA passthrough (currently ON; boot default OFF)")
    assert info.passthrough is True
    assert info.passthrough_boot is False


# --- $PMVR unwrapping ----------------------------------------------------------


def test_unwrap_keeps_commas_in_text():
    assert unwrap_pmvr(PMVR_STATUS) == (
        "[123] GPS: GPS fix, 9 sats | Loop: LOCKED | CLK0 27000000Hz CLK1 off CLK2 off")


def test_unwrap_keeps_leading_spaces():
    assert unwrap_pmvr(PMVR_OK_GGA) == "  [OK  ] GGA  (Fix data)  (ACK)"


def test_unwrap_halted_with_apostrophes():
    assert unwrap_pmvr(PMVR_HALTED) == "''' HALTED '''"


def test_unwrap_rejects_bad_checksum():
    assert unwrap_pmvr("$PMVR,MSG,[45] GPS: LOCKED*34") is None


def test_unwrap_ignores_other_sentences():
    assert unwrap_pmvr(GGA_FIX) is None
    assert unwrap_pmvr("[45] GPS: LOCKED") is None


def test_unwrap_text_ends_at_the_last_star():
    # A '*' never reaches the wire inside <text>, but if one did, the
    # checksum is the one after the LAST star.
    body = "PMVR,MSG,a*b"
    ck = 0
    for c in body.encode():
        ck ^= c
    assert unwrap_pmvr(f"${body}*{ck:02X}") == "a*b"


# --- the state machine ------------------------------------------------------------


def _feed(state, lines, t=100.0):
    for ln in lines:
        feed_line(state, ln, now_mono=t, now_wall=1_800_000_000.0 + t)


def test_normal_mode_status_line_fills_state_and_marks_passthrough_off():
    st = MvrState()
    _feed(st, [N8UR_STATUS])
    assert st.evidence == "status"
    assert st.pll_locked is True
    assert st.gps_locked is True
    assert st.fix_type == "DGPS fix"
    assert st.sats == 12
    assert st.clk_hz == [27_000_000, 27_000_000, 0]
    assert st.passthrough is False
    assert st.last_status_mono == 100.0
    assert st.nmea.latitude is None


def test_lock_events_flip_state_between_status_lines():
    st = MvrState()
    _feed(st, [N8UR_STATUS, "[110110] Loop: LOST"])
    assert st.pll_locked is False
    assert st.loop_state == "LOST"
    _feed(st, ["[110120] GPS: LOST"])
    assert st.gps_locked is False
    _feed(st, ["[110130] Loop: LOCKED", "[110131] GPS: LOCKED"])
    assert st.pll_locked is True and st.gps_locked is True


def test_no_data_means_not_locked():
    st = MvrState()
    _feed(st, ["[5] GPS: no data | Loop: no data | CLK0 27000000Hz CLK1 off CLK2 off"])
    assert st.pll_locked is False
    assert st.gps_locked is False
    assert st.loop_state == "no data"
    assert st.gps_state == "no data"


def test_banner_is_evidence_and_names_firmware():
    st = MvrState()
    _feed(st, [BANNER])
    assert st.evidence == "banner"
    assert st.firmware == "v20261008.1"


def test_plain_non_mvr_text_is_not_evidence():
    st = MvrState()
    _feed(st, ["hello from a pico", "$GNGGA,garbage", "", "[1] tick"])
    assert st.evidence is None


def test_mixed_passthrough_stream():
    """Passthrough on: GNSS sentences verbatim, MVR lines wrapped."""
    st = MvrState()
    _feed(st, [
        "$PMVR,MSG,=== MVR GPSDO B Firmware v20261008.1 (built Oct  8 2026 09:50:46) ===*"
        + _ck("PMVR,MSG,=== MVR GPSDO B Firmware v20261008.1 (built Oct  8 2026 09:50:46) ==="),
        PMVR_CONFIG,
        PMVR_OK_GGA,
        RMC_VALID, GGA_FIX, GSA_3D,
        "$GPGSV,3,1,11,10,63,137,17,07,61,098,15,05,59,290,20,08,54,157,30*"
        + _ck("GPGSV,3,1,11,10,63,137,17,07,61,098,15,05,59,290,20,08,54,157,30"),
        PMVR_STATUS,
        PMVR_GPS_LOCKED,
        PMVR_LOOP_LOST,
    ])
    assert st.evidence is not None
    assert st.firmware == "v20261008.1"
    assert st.passthrough is True
    assert st.pll_locked is False          # the Loop: LOST event came last
    assert st.gps_locked is True
    assert st.fix_type == "GPS fix"
    assert st.nmea.gps_fix == "3D"
    assert st.nmea.sats_used == 10
    assert st.nmea.latitude == pytest.approx(38.918702, abs=1e-6)
    assert st.nmea.longitude == pytest.approx(-92.127579, abs=1e-6)
    assert st.nmea.maidenhead() == "EM38ww"
    assert st.last_nmea_mono == 100.0


def test_pmvr_alone_is_evidence():
    st = MvrState()
    _feed(st, [PMVR_CONFIG])
    assert st.evidence == "pmvr"


def test_bad_pmvr_checksum_is_counted_not_trusted():
    st = MvrState()
    _feed(st, ["$PMVR,MSG,[45] GPS: LOCKED*34"])
    assert st.evidence is None
    assert st.bad_pmvr_checksum == 1
    assert st.gps_locked is None


def test_halted_is_recorded_in_both_modes():
    st = MvrState()
    _feed(st, ["*** HALTED ***"])
    assert st.halted is True
    st2 = MvrState()
    _feed(st2, [PMVR_HALTED])
    assert st2.halted is True


def test_menu_lines_in_the_stream_fill_drive_and_passthrough_boot():
    st = MvrState()
    _feed(st, MENU.splitlines())
    assert st.firmware == "v20261008.1"
    assert st.drive_ma == 8
    assert st.passthrough_boot is False
    assert st.clk_hz == [27_000_000, 0, 0]


def test_snapshot_copy_is_independent():
    st = MvrState()
    _feed(st, [GGA_FIX])
    cp = mvr.copy_state(st)
    _feed(st, [RMC_VALID])
    assert cp.nmea.last_rmc_valid_wall is None
    assert st.nmea.last_rmc_valid_wall is not None


def _ck(body: str) -> str:
    ck = 0
    for c in body.encode():
        ck ^= c
    return f"{ck:02X}"
