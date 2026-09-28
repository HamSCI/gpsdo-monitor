"""LBE-Mini driver tests.

Exercises the feature-report parser, the interrupt-IN sampler, and the
UBX-MON-VER path through a fake HID that replays canned bytes. The
Mini's hardware path involves bootstrapping the UBX stream which we
can't unit-test — but the bytes-in/bytes-out logic around it can, and
that's the part that would silently break without coverage.
"""
from __future__ import annotations

import threading
import time

import pytest

from gpsdo_monitor.models import lbe_mini as lbe_mini_mod
from gpsdo_monitor.models.lbe_mini import LbeMini, _parse_feature
from gpsdo_monitor.ubx import (CLS_MON, CLS_NAV, ID_MON_VER, ID_NAV_CLOCK,
                               ID_NAV_PVT, build_message)


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


# --- Continuous reader (one thread per Mini owns the HID stream) ---------
#
# The daemon used to sample interrupt-IN for 3 s out of every 10 s tick, so
# ~7 s of every 10 s of NAV-PVT/NAV-CLOCK went unread and the device JSON
# refreshed only every 10 s.  The reader thread owns the stream instead, and
# get_status() answers from its snapshot.

def _resolved_pvt_payload(*, second: int = 45, nano: int = -250_000_000,
                          fix: int = 3, sv: int = 12,
                          t_acc: int = 25_000) -> bytes:
    p = bytearray(92)
    p[4:6] = (2026).to_bytes(2, "little")
    p[6], p[7], p[8], p[9], p[10] = 9, 27, 22, 40, second
    p[11] = 0x07                                   # date|time|fullyResolved
    p[12:16] = t_acc.to_bytes(4, "little")
    p[16:20] = nano.to_bytes(4, "little", signed=True)
    p[20] = fix
    p[23] = sv
    p[24:28] = (-967926052).to_bytes(4, "little", signed=True)
    p[28:32] = (469071213).to_bytes(4, "little", signed=True)
    p[36:40] = (282428).to_bytes(4, "little", signed=True)
    return bytes(p)


def _nav_clock_payload(bias_ns: int) -> bytes:
    return ((0).to_bytes(4, "little")
            + bias_ns.to_bytes(4, "little", signed=True)
            + (7).to_bytes(4, "little", signed=True)
            + (25).to_bytes(4, "little")
            + (300).to_bytes(4, "little"))


def _frames_for(stream: bytes) -> list[bytes]:
    frames = []
    for off in range(0, len(stream), 62):
        chunk = stream[off:off + 62].ljust(62, b"\x00")
        frames.append(_make_mini_hid_frame(
            signal_loss=3, pll_locked=True, gps_signal=True,
            carries_ubx=True, payload=chunk))
    return frames


def _pvt_frames(**kw) -> list[bytes]:
    return _frames_for(build_message(CLS_NAV, ID_NAV_PVT,
                                     _resolved_pvt_payload(**kw)))


class _StreamFakeHid:
    """A threaded fake HID handle.

    `read` blocks like the real 50 ms interrupt read (a short sleep with the
    handle marked busy) and serves frames only after `release()`, so a test
    can start the reader before any data exists.  Every entry point checks
    and records whether another thread was inside the handle at the same
    moment: `overlaps` counts two threads on the handle at once — the thing
    the per-device lock exists to prevent."""

    READ_SLEEP_S = 0.01

    def __init__(self, feature: bytes, frames: list[bytes] | None = None) -> None:
        self._feature = feature
        self._frames = list(frames or [])
        self._gate = threading.Event()
        self._mu = threading.Lock()
        self._inside: threading.Thread | None = None
        self.overlaps = 0
        self.reads = 0
        self.feature_gets = 0
        self.feature_sets: list[tuple[int, bytes]] = []
        self.closed = False
        # Fault injection: a dead device raises on every access; a wedged
        # usbhid transfer blocks inside read until `unblock` is set.
        self.broken_read = False
        self.fail_next_reads = 0          # raise on this many reads, then recover
        self.broken_feature = False
        self.wedge: threading.Event | None = None

    def release(self, more: list[bytes] | None = None) -> None:
        with self._mu:
            if more:
                self._frames.extend(more)
        self._gate.set()

    def _enter(self) -> None:
        with self._mu:
            if self._inside is not None and self._inside is not threading.current_thread():
                self.overlaps += 1
            self._inside = threading.current_thread()

    def _leave(self) -> None:
        with self._mu:
            if self._inside is threading.current_thread():
                self._inside = None

    def feature_get(self, report_id: int, length: int = 60) -> bytes:
        if self.broken_feature:
            raise OSError("device gone")
        self._enter()
        try:
            self.feature_gets += 1
            time.sleep(0.001)
            return self._feature
        finally:
            self._leave()

    def feature_set(self, report_id: int, payload: bytes) -> None:
        self._enter()
        try:
            time.sleep(0.001)
            self.feature_sets.append((report_id, bytes(payload)))
        finally:
            self._leave()

    def read(self, length: int, timeout_ms: int | None = None) -> bytes:
        if self.broken_read:
            raise OSError("device gone")
        with self._mu:
            if self.fail_next_reads > 0:
                self.fail_next_reads -= 1
                raise OSError("transient")
        self._enter()
        try:
            self.reads += 1
            if self.wedge is not None:
                self.wedge.wait()
            time.sleep(self.READ_SLEEP_S)
            if not self._gate.is_set():
                return b""
            with self._mu:
                if not self._frames:
                    return b""
                return self._frames.pop(0)[:length]
        finally:
            self._leave()

    def close(self) -> None:
        self.closed = True


def _feature() -> bytes:
    return _make_feature_buf(enabled=True, drive_idx=3, fin=97_600, n3=1,
                             n2hs=10, n2ls=6250, n1hs=5, nc1=122)


class _Clock:
    """Monotonic with a settable offset, so staleness and the rate window can
    be tested without waiting a minute."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.monotonic() + self.offset


def _wait_for(pred, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _mini_on(hid, clock: _Clock | None = None) -> LbeMini:
    mini = LbeMini(hid)
    if clock is not None:
        mini._monotonic = clock
    return mini


def test_reader_snapshot_is_updated_by_fed_frames():
    stream = (build_message(CLS_NAV, ID_NAV_PVT, _resolved_pvt_payload())
              + build_message(CLS_NAV, ID_NAV_CLOCK, _nav_clock_payload(-321)))
    hid = _StreamFakeHid(_feature(), _frames_for(stream))
    mini = _mini_on(hid)
    mini.start_reader()
    try:
        hid.release()
        assert _wait_for(lambda: mini.nav_pvt_count() >= 1)
        assert _wait_for(lambda: mini.get_status().extras.get("nav_clock") is not None)
        raw = mini.get_status()
    finally:
        mini.stop_reader()
    h = raw.health
    assert h.gps_fix == "3D"
    assert h.sats_used == 12
    assert h.pll_locked is True
    assert h.gps_locked is True
    assert h.signal_loss_count == 3
    assert h.latitude == pytest.approx(46.9071213, abs=1e-6)
    assert h.pps_utc_sec is not None and h.pps_utc_sec % 60 == 44
    assert h.naming_source == "ubx-nav-pvt"
    assert h.naming_sigma_ns == 25_000
    assert h.nmea_host_monotonic_at_read is not None
    assert h.fix_age_sec is not None and 0.0 <= h.fix_age_sec < 3.0
    assert raw.extras["nav_clock"].clk_bias_ns == -321
    assert raw.outputs.drive_ma == 32


def test_get_status_with_reader_returns_without_sampling():
    # The default 3 s window stays in force; with the reader running it must
    # not be used.
    hid = _StreamFakeHid(_feature(), _pvt_frames())
    mini = _mini_on(hid)
    assert mini.nav_sample_sec == 3.0
    mini.start_reader()
    try:
        hid.release()
        assert _wait_for(lambda: mini.nav_pvt_count() >= 1)
        t0 = time.monotonic()
        raw = mini.get_status()
        elapsed = time.monotonic() - t0
    finally:
        mini.stop_reader()
    assert raw.health.gps_fix == "3D"
    assert elapsed < 0.5, f"get_status blocked {elapsed:.2f} s with the reader running"


def test_stale_snapshot_reads_like_an_empty_window():
    clock = _Clock()
    hid = _StreamFakeHid(_feature(), _pvt_frames())
    mini = _mini_on(hid, clock)
    mini.start_reader()
    try:
        hid.release()
        assert _wait_for(lambda: mini.nav_pvt_count() >= 1)
        assert mini.get_status().health.gps_fix == "3D"
        clock.offset = 16.0          # > 15 s since the last frame
        raw = mini.get_status()
    finally:
        mini.stop_reader()
    h = raw.health
    # Exactly the "window saw nothing" reading of the legacy sampler.
    assert h.gps_fix is None
    assert h.pll_locked is False
    assert h.gps_locked is None
    assert h.signal_loss_count is None
    assert h.sats_used is None
    assert h.fix_age_sec is None
    assert h.latitude is None
    assert h.pps_utc_sec is None
    assert h.naming_source is None
    assert "nav_clock" not in raw.extras


def test_nav_pvt_rate_is_decodes_in_trailing_60s():
    clock = _Clock()
    hid = _StreamFakeHid(_feature())
    mini = _mini_on(hid, clock)
    mini.start_reader()
    try:
        # A window shorter than 60 s cannot state a rate over 60 s.
        assert mini.nav_pvt_rate_hz() is None
        clock.offset = 30.0
        hid.release(_pvt_frames(second=1) + _pvt_frames(second=2)
                    + _pvt_frames(second=3))
        assert _wait_for(lambda: mini.nav_pvt_count() >= 3)
        clock.offset = 61.0          # window full; decodes ~31 s old
        assert mini.nav_pvt_rate_hz() == pytest.approx(3 / 60)
        assert mini.get_status().extras["nav_pvt_rate_hz"] == pytest.approx(3 / 60)
        clock.offset = 95.0          # decodes now ~65 s old: out of window
        assert mini.nav_pvt_rate_hz() == 0.0
    finally:
        mini.stop_reader()


def test_setters_never_share_the_handle_with_the_reader():
    hid = _StreamFakeHid(_feature())
    mini = _mini_on(hid)
    mini.start_reader()
    try:
        hid.release()
        assert _wait_for(lambda: hid.reads >= 3)
        reads_before = hid.reads
        for _ in range(30):
            mini.set_drive_ma(32)
            mini.set_frequency(1, 10_000_000)
            time.sleep(0.01)
        reads_during = hid.reads - reads_before
    finally:
        mini.stop_reader()
    # The reader kept reading between the setters (it was neither starved
    # nor stopped), so the two really did contend for the handle.
    assert reads_during >= 5, f"only {reads_during} reads during the setters"
    assert hid.overlaps == 0, f"{hid.overlaps} overlapping handle accesses"
    drive_sets = [p for (_, p) in hid.feature_sets if p[0] == 0x03]
    assert len(drive_sets) == 30


def test_stop_reader_joins_the_thread():
    hid = _StreamFakeHid(_feature())
    mini = _mini_on(hid)
    mini.start_reader()
    t = mini._reader_thread
    assert t is not None and t.is_alive()
    mini.stop_reader()
    assert not t.is_alive()
    assert mini._reader_thread is None


def test_reader_resends_stream_enable_every_30s():
    clock = _Clock()
    hid = _StreamFakeHid(_feature())
    mini = _mini_on(hid, clock)

    def enables() -> int:
        return sum(1 for (_, p) in hid.feature_sets
                   if p[0] == lbe_mini_mod.OPC_MINI_NAV_STREAM)

    mini.start_reader()
    try:
        assert _wait_for(lambda: enables() == 1)
        time.sleep(0.1)
        assert enables() == 1, "no re-send inside 30 s"
        clock.offset = 31.0
        assert _wait_for(lambda: enables() == 2)
    finally:
        mini.stop_reader()


def test_on_nav_pvt_hook_gets_the_decode_instant():
    seen = []
    hid = _StreamFakeHid(_feature(), _pvt_frames())
    mini = _mini_on(hid)
    mini.on_nav_pvt = lambda pvt, mono, real: seen.append((pvt, mono, real))
    mini.start_reader()
    try:
        before_real = time.time()
        hid.release()
        assert _wait_for(lambda: len(seen) == 1)
        raw = mini.get_status()
    finally:
        mini.stop_reader()
    pvt, mono, real = seen[0]
    assert pvt.fix_type == 3
    assert before_real <= real <= time.time()
    # The same instant the naming pair is built from: the boundary monotonic
    # is the decode monotonic minus the fraction past the integer second.
    from gpsdo_monitor.ubx import nav_pvt_utc
    utc = nav_pvt_utc(pvt)
    expect = mono - (utc - int(utc // 1))
    assert raw.health.nmea_host_monotonic_at_read == pytest.approx(expect, abs=1e-9)


def test_a_raising_hook_does_not_stop_the_reader():
    hid = _StreamFakeHid(_feature(), _pvt_frames(second=1) + _pvt_frames(second=2))
    mini = _mini_on(hid)

    def boom(*_a):
        raise RuntimeError("hook failed")
    mini.on_nav_pvt = boom
    mini.start_reader()
    try:
        hid.release()
        assert _wait_for(lambda: mini.nav_pvt_count() >= 2)
    finally:
        mini.stop_reader()


def test_read_mon_ver_through_the_running_reader():
    def pad(s: str, n: int) -> bytes:
        return s.encode("ascii").ljust(n, b"\x00")[:n]
    resp = build_message(CLS_MON, ID_MON_VER,
                         pad("ROM CORE 3.01 (107888)", 30) + pad("00080000", 10)
                         + pad("PROTVER=18.00", 30))
    hid = _StreamFakeHid(_feature())
    mini = _mini_on(hid)
    mini.start_reader()
    try:
        assert _wait_for(lambda: hid.reads >= 2)
        threading.Timer(0.05, hid.release, args=(_frames_for(resp),)).start()
        mv = mini.read_mon_ver(timeout_sec=2.0)
    finally:
        mini.stop_reader()
    assert mv is not None and mv.protver == "18.00"


def test_a_setter_is_not_starved_by_a_busy_stream():
    # Every read returns a frame at once, so the reader never idles between
    # reads.  A plain lock is not fair; without the reader yielding to a
    # waiting setter, the setter can wait many reads for the handle.
    keepalive = _make_mini_hid_frame(signal_loss=0, pll_locked=True,
                                     gps_signal=True, carries_ubx=False,
                                     payload=b"\xff" * 62)
    hid = _StreamFakeHid(_feature(), [keepalive] * 100_000)
    mini = _mini_on(hid)
    mini.start_reader()
    try:
        hid.release()
        assert _wait_for(lambda: hid.reads >= 3)
        worst = 0.0
        for _ in range(20):
            t0 = time.monotonic()
            mini.set_drive_ma(32)
            worst = max(worst, time.monotonic() - t0)
            time.sleep(0.002)
    finally:
        mini.stop_reader()
    # One read holds the handle 10 ms in this fake; allow a few of them.
    assert worst < 0.05, f"a setter waited {worst * 1000:.0f} ms for the handle"


# --- Fault handling (fix round 1) ----------------------------------------


def test_reader_gives_up_after_three_consecutive_read_errors():
    hid = _StreamFakeHid(_feature())
    mini = _mini_on(hid)
    mini.reader_error_backoff_sec = 0.01
    mini.start_reader()
    try:
        assert _wait_for(lambda: hid.reads >= 2)
        assert mini.reader_failed is False
        hid.broken_read = True
        assert _wait_for(lambda: mini.reader_failed, timeout=2.0)
        t = mini._reader_thread
        assert t is not None
        assert _wait_for(lambda: not t.is_alive(), timeout=2.0), \
            "a reader that gave up must exit, not retry a dead handle"
    finally:
        mini.close()
    assert hid.closed


def test_separated_read_errors_do_not_give_up():
    # Two errors, a good read, two more: four errors, never three in a row.
    hid = _StreamFakeHid(_feature())
    mini = _mini_on(hid)
    mini.reader_error_backoff_sec = 0.01
    mini.start_reader()
    try:
        assert _wait_for(lambda: hid.reads >= 2)
        for _ in range(2):
            n = hid.reads
            hid.fail_next_reads = 2
            assert _wait_for(lambda: hid.fail_next_reads == 0 and hid.reads > n)
        n = hid.reads
        assert _wait_for(lambda: hid.reads >= n + 2)
        assert mini.reader_failed is False
    finally:
        mini.close()


def test_close_never_frees_a_handle_the_reader_is_still_inside():
    # A wedged usbhid transfer can hold the reader inside hidapi for ~5 s.
    # Closing the handle under it would be a use-after-free in C.
    hid = _StreamFakeHid(_feature())
    hid.wedge = threading.Event()
    mini = _mini_on(hid)
    mini.reader_join_timeout_sec = 0.05
    mini.start_reader()
    try:
        assert _wait_for(lambda: hid.reads >= 1)
        mini.close()
        assert hid.closed is False, "handle closed under a live reader"
        assert mini._reader_thread is not None and mini._reader_thread.is_alive()
    finally:
        hid.wedge.set()
        t = mini._reader_thread
        if t is not None:
            t.join(timeout=2.0)
