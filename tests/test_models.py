"""Feature-report parser tests for the 1420 and 1421 drivers.

We inject a fake HID device that returns a hand-crafted 60-byte status
buffer, so these tests run without libhidapi or real hardware. The
canonical 1421 bring-up on bee1 validated the same parser against a
live device; this suite is the regression backstop for the byte-offset
bugs that burned us during that bring-up (see commit 74a16ec)."""
from __future__ import annotations

import pytest

from gpsdo_monitor.models.lbe_1420 import Lbe1420
from gpsdo_monitor.models.lbe_1421 import (
    ANT_OK_BIT,
    GPS_LOCK_BIT,
    OUT1_EN_BIT,
    OUT2_EN_BIT,
    PLL_LOCK_BIT,
    PPS_EN_BIT,
    Lbe1421,
)


class _FakeHid:
    """Minimal stand-in for hid_xport.HidDevice used in parser tests."""

    def __init__(self, feature_reports: dict[int, bytes]) -> None:
        self.feature_reports = feature_reports
        self.writes: list[tuple[int, bytes]] = []

    def feature_get(self, report_id: int, length: int = 60) -> bytes:
        buf = self.feature_reports[report_id]
        assert len(buf) == length, f"test fixture for 0x{report_id:02X} is {len(buf)}B, expected {length}"
        return buf

    def feature_set(self, report_id: int, payload: bytes) -> None:
        self.writes.append((report_id, bytes(payload)))

    def close(self) -> None:
        pass


def _make_status_1421(
    *,
    raw_bitmap: int,
    freq1_hz: int,
    freq2_hz: int,
    fll: bool = False,
    pw1_low: bool = False,
    pw2_low: bool = False,
    bias_ma: int = 0,
) -> bytes:
    """Assemble a 60-byte status buffer matching upstream's layout.

    Byte 0 is the report-id echo; real bits start at index 1. See
    `model_1421.c:m1421_get_status`."""
    buf = bytearray(60)
    buf[0] = 0x4B          # echo (opaque to parser, kept for fidelity)
    buf[1] = raw_bitmap
    buf[6:10]  = freq1_hz.to_bytes(4, "little")
    buf[14:18] = freq2_hz.to_bytes(4, "little")
    buf[18] = 1 if fll else 0
    buf[19] = 1 if pw1_low else 0
    buf[20] = 1 if pw2_low else 0
    buf[23] = bias_ma
    return bytes(buf)


def _make_status_1420(
    *,
    raw_bitmap: int,
    freq1_hz: int,
    fll: bool = False,
    pw1_low: bool = False,
    bias_ma: int = 0,
) -> bytes:
    """1420 layout: same report-id echo + status bitmap, same freq1
    offset, but power at buf[10] instead of buf[19]; no freq2; no PPS
    byte. See `model_1420.c:m1420_get_status`."""
    buf = bytearray(60)
    buf[0] = 0x4B
    buf[1] = raw_bitmap
    buf[6:10] = freq1_hz.to_bytes(4, "little")
    buf[10] = 1 if pw1_low else 0
    buf[18] = 1 if fll else 0
    buf[12] = bias_ma
    return bytes(buf)


# --- 1421 -----------------------------------------------------------------


def test_1421_all_locked_nominal():
    locked = (
        GPS_LOCK_BIT | PLL_LOCK_BIT | ANT_OK_BIT
        | OUT1_EN_BIT | OUT2_EN_BIT | PPS_EN_BIT
    )
    buf = _make_status_1421(
        raw_bitmap=locked, freq1_hz=10_000_000, freq2_hz=27_000_000,
    )
    m = Lbe1421(_FakeHid({0x4B: buf}))
    raw = m.get_status()
    assert raw.health.pll_locked is True
    assert raw.health.gps_locked is True
    assert raw.health.antenna_ok is True
    assert raw.health.outputs_enabled is True
    assert raw.health.fll_mode is False
    assert raw.outputs.out1_hz == 10_000_000
    assert raw.outputs.out2_hz == 27_000_000
    assert raw.outputs.pps_enabled is True
    assert raw.outputs.out1_power == "normal"
    assert raw.outputs.out2_power == "normal"


def test_1421_pll_unlocked_downgrade_signal():
    buf = _make_status_1421(
        raw_bitmap=GPS_LOCK_BIT | ANT_OK_BIT | OUT1_EN_BIT | OUT2_EN_BIT,
        freq1_hz=10_000_000, freq2_hz=27_000_000,
    )
    raw = Lbe1421(_FakeHid({0x4B: buf})).get_status()
    assert raw.health.pll_locked is False
    assert raw.health.gps_locked is True


def test_1421_only_out1_enabled_reports_not_all_outputs():
    # outputs_enabled is defined as "both outputs on" — matches upstream.
    buf = _make_status_1421(
        raw_bitmap=PLL_LOCK_BIT | OUT1_EN_BIT,
        freq1_hz=10_000_000, freq2_hz=27_000_000,
    )
    raw = Lbe1421(_FakeHid({0x4B: buf})).get_status()
    assert raw.health.outputs_enabled is False


def test_1421_power_low_flags_surface_per_output():
    buf = _make_status_1421(
        raw_bitmap=PLL_LOCK_BIT | OUT1_EN_BIT | OUT2_EN_BIT,
        freq1_hz=10_000_000, freq2_hz=27_000_000,
        pw1_low=True, pw2_low=False,
    )
    raw = Lbe1421(_FakeHid({0x4B: buf})).get_status()
    assert raw.outputs.out1_power == "low"
    assert raw.outputs.out2_power == "normal"


def test_1421_set_frequency_packs_u32_at_offset_5():
    """1421 write path: args go at buf[5..8], not buf[1..4]. Any
    regression on this offset silently corrupts device state."""
    buf = _make_status_1421(raw_bitmap=0, freq1_hz=0, freq2_hz=0)
    hid = _FakeHid({0x4B: buf})
    m = Lbe1421(hid)
    m.set_frequency(1, 100_000_000, persist=True)
    assert len(hid.writes) == 1
    report_id, payload = hid.writes[0]
    assert report_id == 0x06       # OPC_SET_F1
    assert payload[0] == 0x06       # opcode echoed at byte 0
    assert payload[1:5] == b"\x00\x00\x00\x00"  # the 1421's gap bytes
    assert payload[5:9] == (100_000_000).to_bytes(4, "little")


def test_1421_set_frequency_out_of_range_rejected():
    buf = _make_status_1421(raw_bitmap=0, freq1_hz=0, freq2_hz=0)
    m = Lbe1421(_FakeHid({0x4B: buf}))
    with pytest.raises(ValueError):
        m.set_frequency(1, 2_000_000_000)


def test_1421_leaves_antenna_bias_none():
    """Bench gate not met: the DC-blocked antenna feed on our bench read
    byte 23 as 0 both connected and open, so the 1421 decode is disabled
    (see class docstring / PROTOCOL.md). Byte 23 is still captured in
    raw_trailing_hex for the paper trail and for the upcoming 1425 driver,
    which does decode it."""
    buf = _make_status_1421(
        raw_bitmap=PLL_LOCK_BIT | GPS_LOCK_BIT | ANT_OK_BIT,
        freq1_hz=10_000_000, freq2_hz=10_000_000, bias_ma=5,
    )
    model = Lbe1421(_FakeHid({0x4B: buf}))
    raw = model.get_status()
    assert raw.health.antenna_bias_ma is None
    # Paper trail unchanged: trailing hex still starts at byte 21.
    assert raw.raw_trailing_hex.split()[2] == "05"


# --- 1420 -----------------------------------------------------------------


def test_1420_locked_reports_outputs_enabled_despite_missing_bit():
    """The 1420 firmware does not mirror the outputs-enable bit back.
    The parser must always report outputs_enabled=True rather than
    parroting the missing bit (which would mis-classify every nominal
    locked device as down)."""
    buf = _make_status_1420(
        raw_bitmap=GPS_LOCK_BIT | PLL_LOCK_BIT | ANT_OK_BIT,
        freq1_hz=10_000_000,
    )
    raw = Lbe1420(_FakeHid({0x4B: buf})).get_status()
    assert raw.health.pll_locked is True
    assert raw.health.antenna_ok is True
    assert raw.health.outputs_enabled is True


def test_1420_pps_always_disabled():
    """The 1420 has no 1PPS output. Even if a stray bit in the status
    bitmap looked like PPS_EN, the parser should hard-code disabled."""
    buf = _make_status_1420(
        raw_bitmap=PLL_LOCK_BIT | PPS_EN_BIT,  # stray high bit
        freq1_hz=10_000_000,
    )
    raw = Lbe1420(_FakeHid({0x4B: buf})).get_status()
    assert raw.outputs.pps_enabled is False


def test_1420_reads_power_from_byte_10_not_byte_19():
    """Regression: 1420 power lives at buf[10]. If we mistakenly read
    the 1421's byte 19, a power-normal 1420 looks "low" whenever byte
    19 happens to be nonzero (which it is for any 1421-shaped report)."""
    buf = _make_status_1420(
        raw_bitmap=PLL_LOCK_BIT, freq1_hz=10_000_000, pw1_low=True,
    )
    raw = Lbe1420(_FakeHid({0x4B: buf})).get_status()
    assert raw.outputs.out1_power == "low"

    buf_normal = _make_status_1420(
        raw_bitmap=PLL_LOCK_BIT, freq1_hz=10_000_000, pw1_low=False,
    )
    raw_normal = Lbe1420(_FakeHid({0x4B: buf_normal})).get_status()
    assert raw_normal.outputs.out1_power == "normal"


def test_1420_freq2_not_exposed():
    buf = _make_status_1420(raw_bitmap=0, freq1_hz=50_000_000)
    raw = Lbe1420(_FakeHid({0x4B: buf})).get_status()
    assert raw.outputs.out1_hz == 50_000_000
    assert raw.outputs.out2_hz is None


def test_1420_set_frequency_packs_u32_at_offset_1():
    """1420 write path: args go at buf[1..4], not buf[5..8]. Inverse
    of the 1421 regression."""
    buf = _make_status_1420(raw_bitmap=0, freq1_hz=0)
    hid = _FakeHid({0x4B: buf})
    m = Lbe1420(hid)
    m.set_frequency(1, 50_000_000, persist=True)
    assert len(hid.writes) == 1
    report_id, payload = hid.writes[0]
    assert report_id == 0x04       # OPC_SET_F1 for 1420
    assert payload[0] == 0x04       # opcode echoed
    assert payload[1:5] == (50_000_000).to_bytes(4, "little")


def test_1420_set_temp_frequency_uses_different_opcode():
    buf = _make_status_1420(raw_bitmap=0, freq1_hz=0)
    hid = _FakeHid({0x4B: buf})
    m = Lbe1420(hid)
    m.set_frequency(1, 25_000_000, persist=False)
    report_id, payload = hid.writes[0]
    assert report_id == 0x03       # OPC_SET_F1_TEMP for 1420
    assert payload[0] == 0x03


def test_1420_rejects_output_2():
    buf = _make_status_1420(raw_bitmap=0, freq1_hz=0)
    m = Lbe1420(_FakeHid({0x4B: buf}))
    with pytest.raises(ValueError, match="only has output 1"):
        m.set_frequency(2, 10_000_000)
    with pytest.raises(ValueError, match="only has output 1"):
        m.set_power_level(2, True)


def test_1420_decodes_antenna_bias_current():
    buf = _make_status_1420(
        raw_bitmap=PLL_LOCK_BIT | GPS_LOCK_BIT | ANT_OK_BIT,
        freq1_hz=10_000_000, bias_ma=4,
    )
    model = Lbe1420(_FakeHid({0x4B: buf}))
    raw = model.get_status()
    assert raw.health.antenna_bias_ma == 4
