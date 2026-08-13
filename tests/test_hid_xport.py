"""Tests for `HidDevice.feature_get`'s numbered vs. unnumbered report
handling.

The LBE-Mini uses report_id 0 (no Report ID on the wire). Requesting
exactly `length` bytes from hidapi for an unnumbered report under-asks
the kernel by one byte (the report-id placeholder), which stalls the
transfer on real Mini hardware. The fix: for report_id == 0, ask for
`length + 1` bytes and strip the leading placeholder byte before
returning. Numbered reports (142x family, report_id != 0) are
unaffected — the returned buffer there already starts with the
id/opcode echo the model layer expects.
"""
from __future__ import annotations

import pytest

from gpsdo_monitor import hid_xport
from gpsdo_monitor.hid_xport import HidDevice


class _FakeRawHid:
    """Stand-in for the underlying `hid.device` handle. Records the
    (report_id, length) it was asked for and replays a canned buffer."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply
        self.calls: list[tuple[int, int]] = []

    def get_feature_report(self, report_id: int, length: int) -> bytes:
        self.calls.append((report_id, length))
        return self.reply


def _make_device(fake: _FakeRawHid) -> HidDevice:
    dev = HidDevice.__new__(HidDevice)
    dev._d = fake
    dev._path = b"fake-path"
    return dev


def test_feature_get_unnumbered_requests_length_plus_one_and_strips_placeholder() -> None:
    """report_id 0 (Mini): must ask hidapi for length+1 bytes, and the
    leading report-id placeholder byte must be stripped from the result."""
    canned = bytes([0]) + bytes(range(60))
    fake = _FakeRawHid(canned)
    dev = _make_device(fake)

    result = dev.feature_get(0, 60)

    assert fake.calls == [(0, 61)]
    assert result == bytes(range(60))


def test_feature_get_numbered_requests_length_unmodified() -> None:
    """report_id != 0 (142x family): length passed straight through, and
    the full canned buffer (including the id/opcode echo) is returned as-is."""
    canned = bytes(range(60))
    fake = _FakeRawHid(canned)
    dev = _make_device(fake)

    result = dev.feature_get(0x4B, 60)

    assert fake.calls == [(0x4B, 60)]
    assert result == canned


def test_feature_get_unnumbered_short_read_raises_oserror() -> None:
    """A short read (fewer than length+1 bytes) must not be silently
    accepted — it means the transfer was truncated/stalled."""
    fake = _FakeRawHid(bytes(range(60)))  # only 60, but 61 expected
    dev = _make_device(fake)

    with pytest.raises(OSError):
        dev.feature_get(0, 60)


def test_feature_get_numbered_short_read_raises_oserror() -> None:
    """Same short-read guard for the numbered-report path."""
    fake = _FakeRawHid(bytes(range(59)))  # only 59, but 60 expected
    dev = _make_device(fake)

    with pytest.raises(OSError):
        dev.feature_get(0x4B, 60)
