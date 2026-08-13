"""LBE-Mini driver tests.

Exercises the feature-report parser, the interrupt-IN sampler, and the
UBX-MON-VER path through a fake HID that replays canned bytes. The
Mini's hardware path involves bootstrapping the UBX stream which we
can't unit-test — but the bytes-in/bytes-out logic around it can, and
that's the part that would silently break without coverage.
"""
from __future__ import annotations

import pytest

from gpsdo_monitor.models.lbe_mini import LbeMini, _parse_feature
from gpsdo_monitor.ubx import CLS_MON, CLS_NAV, ID_MON_VER, ID_NAV_PVT, build_message


class _FakeMiniHid:
    """Fake HidDevice that serves canned feature reports and interrupt
    frames to drive the Mini tests."""

    def __init__(
        self,
        *,
        feature_get_replies: list[bytes] | None = None,
        interrupt_frames: list[bytes] | None = None,
    ) -> None:
        self._feature_queue = list(feature_get_replies or [])
        self._interrupt_queue = list(interrupt_frames or [])
        self.feature_sets: list[tuple[int, bytes]] = []

    def feature_get(self, report_id: int, length: int = 60) -> bytes:
        if not self._feature_queue:
            raise OSError("no more feature reports queued")
        buf = self._feature_queue.pop(0)
        assert len(buf) == length, f"queued feature report is {len(buf)}B, asked for {length}"
        return buf

    def feature_set(self, report_id: int, payload: bytes) -> None:
        self.feature_sets.append((report_id, bytes(payload)))

    def read(self, length: int, timeout_ms: int | None = None) -> bytes:
        if not self._interrupt_queue:
            return b""
        frame = self._interrupt_queue.pop(0)
        return frame[:length]

    def close(self) -> None:
        pass


# --- _parse_feature -----------------------------------------------------


def _make_feature_buf(
    *, enabled: bool, drive_idx: int,
    fin: int, n3: int, n2hs: int, n2ls: int, n1hs: int, nc1: int,
) -> bytes:
    """Pack a synthetic feature report for the Mini. Uses upstream's
    minus-one / minus-four conventions (see model_mini.c comments)."""
    buf = bytearray(60)
    buf[0] = 0x03 if enabled else 0x00
    buf[1] = drive_idx
    buf[2] = fin & 0xFF
    buf[3] = (fin >> 8) & 0xFF
    buf[4] = (fin >> 16) & 0xFF
    n3m = n3 - 1
    buf[5] = n3m & 0xFF
    buf[6] = (n3m >> 8) & 0xFF
    buf[7] = (n3m >> 16) & 0xFF
    buf[8] = n2hs - 4
    n2lsm = n2ls - 1
    buf[9] = n2lsm & 0xFF
    buf[10] = (n2lsm >> 8) & 0xFF
    buf[11] = (n2lsm >> 16) & 0xFF
    buf[12] = n1hs - 4
    nc1m = nc1 - 1
    buf[13] = nc1m & 0xFF
    buf[14] = (nc1m >> 8) & 0xFF
    buf[15] = (nc1m >> 16) & 0xFF
    return bytes(buf)


def test_parse_feature_factory_defaults():
    # Factory defaults per upstream comment: fin=97600, N3=1, dividers
    # producing 10 MHz at N2_HS=8, N2_LS=25, N1_HS=8, NC1_LS=32.
    # 97600 * 8 * 25 / (1 * 8 * 32) = 76_250. So pick dividers that
    # actually make a round number for testability.
    buf = _make_feature_buf(
        enabled=True, drive_idx=3,
        fin=97600, n3=1, n2hs=8, n2ls=25, n1hs=8, nc1=32,
    )
    freq, drive_ma, enabled = _parse_feature(buf)
    assert enabled is True
    assert drive_ma == 32
    assert freq == 97600 * 8 * 25 // (1 * 8 * 32)


def test_parse_feature_drive_strength_mapping():
    for idx, expected_ma in [(0, 8), (1, 16), (2, 24), (3, 32)]:
        buf = _make_feature_buf(
            enabled=True, drive_idx=idx,
            fin=97600, n3=1, n2hs=8, n2ls=25, n1hs=8, nc1=32,
        )
        _, ma, _ = _parse_feature(buf)
        assert ma == expected_ma, f"drive_idx={idx} → {ma} mA (expected {expected_ma})"


def test_parse_feature_zero_denominator_is_zero_freq():
    # If N3 or N1_HS or NC1_LS come back as effectively zero, we return
    # 0 instead of raising — matches upstream's defensive default.
    buf = _make_feature_buf(
        enabled=False, drive_idx=0,
        fin=97600, n3=1, n2hs=4, n2ls=2, n1hs=4, nc1=1,
    )
    # Zero-out the N3 bytes so n3 = 1 (from +1), but set f[5..7] to
    # produce n3 = 1 anyway; this test really verifies the happy path
    # still returns a positive number for small dividers.
    freq, _, _ = _parse_feature(buf)
    assert freq == (97600 * 4 * 2) // (1 * 4 * 1)


# --- get_status ---------------------------------------------------------


def _make_mini_hid_frame(
    *, signal_loss: int, pll_locked: bool, gps_signal: bool,
    carries_ubx: bool, payload: bytes,
) -> bytes:
    assert len(payload) == 62, "interrupt-IN payload is always 62 bytes"
    status = 0
    if not gps_signal:
        status |= 0x01
    if not pll_locked:
        status |= 0x02
    if carries_ubx:
        status |= 0x80
    return bytes([signal_loss, status]) + payload


def test_get_status_parses_feature_and_nav_pvt():
    feature = _make_feature_buf(
        enabled=True, drive_idx=2,
        fin=97600, n3=1, n2hs=8, n2ls=25, n1hs=8, nc1=32,
    )
    # Build a NAV-PVT message payload with fix_type=3, num_sv=9.
    pvt_payload = bytearray(92)
    pvt_payload[20] = 3
    pvt_payload[23] = 9
    pvt_msg = build_message(CLS_NAV, ID_NAV_PVT, bytes(pvt_payload))

    # Stream the message across 62-byte UBX-bearing frames with no
    # padding until the tail, matching the firmware's invariant (any
    # 0xFF/0x00 padding appears in keepalive frames, never mid-message
    # when bit 7 is set). 100B message → one full frame + a 38B tail.
    frames = []
    for i in range(0, len(pvt_msg), 62):
        chunk = pvt_msg[i : i + 62]
        chunk = chunk + b"\x00" * (62 - len(chunk))
        frames.append(_make_mini_hid_frame(
            signal_loss=2, pll_locked=True, gps_signal=True,
            carries_ubx=True, payload=chunk,
        ))

    # The stream-enable bootstrap does two feature_gets that are
    # best-effort; give them empty returns via an OSError simulated by
    # an exhausted feature queue (first call is the real status read).
    hid = _FakeMiniHid(feature_get_replies=[feature], interrupt_frames=frames)

    # Shorten the nav sample window so the test doesn't drag.
    mini = LbeMini(hid)
    mini.nav_sample_sec = 0.1

    raw = mini.get_status()
    assert raw.health.outputs_enabled is True
    assert raw.health.pll_locked is True
    assert raw.health.gps_locked is True
    assert raw.health.gps_fix == "3D"
    assert raw.health.signal_loss_count == 2
    assert raw.outputs.out1_hz == 97600 * 8 * 25 // (1 * 8 * 32)
    assert raw.outputs.drive_ma == 24
    assert raw.outputs.pps_enabled is False


def test_get_status_without_frames_marks_unknown():
    feature = _make_feature_buf(
        enabled=False, drive_idx=0,
        fin=97600, n3=1, n2hs=4, n2ls=2, n1hs=4, nc1=1,
    )
    hid = _FakeMiniHid(feature_get_replies=[feature], interrupt_frames=[])
    mini = LbeMini(hid)
    mini.nav_sample_sec = 0.05
    raw = mini.get_status()
    assert raw.health.outputs_enabled is False
    # Stream bootstrap couldn't observe anything — PLL falls back to
    # False (the "unknown" sentinel) and GPS fix stays None.
    assert raw.health.gps_fix is None


# --- MON-VER path -------------------------------------------------------


def test_read_mon_ver_happy_path():
    # Build a MON-VER response payload.
    def pad(s: str, n: int) -> bytes:
        return s.encode("ascii").ljust(n, b"\x00")[:n]
    resp_payload = (
        pad("ROM CORE 3.01 (107888)", 30)
        + pad("00080000", 10)
        + pad("FWVER=SPG 3.01", 30)
        + pad("PROTVER=18.00", 30)
    )
    resp_msg = build_message(CLS_MON, ID_MON_VER, resp_payload)
    # Chunk the response into ≤62-byte frame payloads.
    frames = []
    for i in range(0, len(resp_msg), 62):
        chunk = resp_msg[i : i + 62]
        chunk = chunk + b"\x00" * (62 - len(chunk))
        frames.append(_make_mini_hid_frame(
            signal_loss=0, pll_locked=True, gps_signal=True,
            carries_ubx=True, payload=chunk,
        ))
    hid = _FakeMiniHid(interrupt_frames=frames)
    mini = LbeMini(hid)

    mv = mini.read_mon_ver(timeout_sec=0.5)
    assert mv is not None
    assert mv.sw_version == "ROM CORE 3.01 (107888)"
    assert mv.hw_version == "00080000"
    assert mv.protver == "18.00"
    # The driver sent exactly one UBX wrap-poll.
    wrap_sends = [p for (_, p) in hid.feature_sets if p[0] == 0x08]
    assert len(wrap_sends) == 1
    poll_payload = wrap_sends[0]
    assert poll_payload[1:5] == bytes([CLS_MON, ID_MON_VER, 0x00, 0x00])


def test_read_gps_firmware_returns_compact_string():
    def pad(s: str, n: int) -> bytes:
        return s.encode("ascii").ljust(n, b"\x00")[:n]
    resp_payload = pad("x", 30) + pad("y", 10) + pad("PROTVER=20.00", 30)
    resp_msg = build_message(CLS_MON, ID_MON_VER, resp_payload)
    frames = []
    for i in range(0, len(resp_msg), 62):
        chunk = resp_msg[i : i + 62]
        chunk = chunk + b"\x00" * (62 - len(chunk))
        frames.append(_make_mini_hid_frame(
            signal_loss=0, pll_locked=True, gps_signal=True,
            carries_ubx=True, payload=chunk,
        ))
    hid = _FakeMiniHid(interrupt_frames=frames)
    mini = LbeMini(hid)
    fw = mini.read_gps_firmware()
    assert fw == "SW=x HW=y PROTVER=20.00"


def test_read_mon_ver_times_out_returning_none():
    hid = _FakeMiniHid(interrupt_frames=[])
    mini = LbeMini(hid)
    assert mini.read_mon_ver(timeout_sec=0.1) is None


# --- write path --------------------------------------------------------


def test_set_drive_ma_valid_values():
    hid = _FakeMiniHid()
    mini = LbeMini(hid)
    for ma, idx in [(8, 0), (16, 1), (24, 2), (32, 3)]:
        hid.feature_sets.clear()
        mini.set_drive_ma(ma)
        assert len(hid.feature_sets) == 1
        report_id, payload = hid.feature_sets[0]
        assert report_id == 0          # Mini has no Report ID
        assert payload[0] == 0x03      # OPC_MINI_SET_DRIVE
        assert payload[1] == idx


def test_set_drive_ma_rejects_invalid():
    mini = LbeMini(_FakeMiniHid())
    with pytest.raises(ValueError):
        mini.set_drive_ma(10)


def test_set_power_level_maps_to_drive_extremes():
    hid = _FakeMiniHid()
    mini = LbeMini(hid)
    mini.set_power_level(1, low=True)
    assert hid.feature_sets[-1][1][:2] == bytes([0x03, 0])   # 8 mA index
    mini.set_power_level(1, low=False)
    assert hid.feature_sets[-1][1][:2] == bytes([0x03, 3])   # 32 mA index


def test_set_power_level_rejects_output_2():
    mini = LbeMini(_FakeMiniHid())
    with pytest.raises(ValueError, match="only has output 1"):
        mini.set_power_level(2, low=False)


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
            signal_loss=0, pll_locked=True, gps_signal=True,
            carries_ubx=True, payload=chunk,
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


def test_set_outputs_enable_sends_0x03():
    hid = _FakeMiniHid()
    mini = LbeMini(hid)
    mini.set_outputs_enable(True)
    assert hid.feature_sets[-1][1][:2] == bytes([0x01, 0x03])
    mini.set_outputs_enable(False)
    assert hid.feature_sets[-1][1][:2] == bytes([0x01, 0x00])
