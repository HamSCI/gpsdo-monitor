"""CLI `set-drive` tests (HID mocked).

⛔ AC0G-ND, 2026-09-03.  Its LBE-Mini sat at 8 mA — the floor of the Mini's
8/16/24/32 ladder — and at that level the GPSDO's 27 MHz did NOT take over the
RX888's reference.  The board ran on its own oscillator ~350 ppm fast,
hf-timestd's FUSE inherited the error, chrony followed FUSE and walked the host
clock twelve seconds off UTC, and the station decoded nothing for a day while
every health check read green.  Raising the drive to 32 mA took radiod's
measured sample rate from +276..+400 ppm to +2..+50 ppm — locked.

The write path had existed per model since the driver was written and nothing
exposed it: `gpsdo-monitor config` is a placeholder pointing at
`smd gpsdo config`, a command that does not exist.  The remedy had to be
applied by driving the library by hand.  These tests hold the supported
surface, and in particular the two traps that cost time doing it by hand: the
handle must be closed (a held one makes every later open fail) and the open
must be retried (the Mini re-binds hid-generic every ~10 s on some hosts).
"""
import io
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import MagicMock, patch

from gpsdo_monitor.cli import build_parser
from gpsdo_monitor.hid_xport import HidCandidate


def _cand(serial="9DC7A55644"):
    return HidCandidate(path=b"/dev/hidraw1", vid=0x1DD2, pid=0x2211,
                        serial=serial, product="mini GPS Reference Clock",
                        manufacturer="Leo Bodnar Electronics")


class _FakeModel:
    """Context-manager model whose drive value actually changes on write."""

    def __init__(self, drive=8, has_drive=True, open_failures=0):
        self.capabilities = MagicMock(has_drive_ma=has_drive)
        self._drive = drive
        self.writes = []
        self.entered = 0
        self.exited = 0
        self._open_failures = open_failures

    def __enter__(self):
        if self._open_failures > 0:
            self._open_failures -= 1
            raise OSError("open failed")
        self.entered += 1
        return self

    def __exit__(self, *exc):
        self.exited += 1
        return False

    def get_status(self):
        return MagicMock(outputs=MagicMock(drive_ma=self._drive))

    def set_drive_ma(self, ma):
        if ma not in (8, 16, 24, 32):
            raise ValueError(f"drive {ma} mA not in {{8, 16, 24, 32}}")
        self.writes.append(ma)
        self._drive = ma


def _run(argv, model, matched=None):
    matched = matched if matched is not None else [(MagicMock(governs=[]), _cand())]
    args = build_parser().parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with patch("gpsdo_monitor.cli.match",
               return_value=MagicMock(errors=[], matched=matched)), \
         patch("gpsdo_monitor.cli.Config") as cfg, \
         patch("gpsdo_monitor.cli.open_model", return_value=model), \
         patch("gpsdo_monitor.cli.time.sleep"):
        cfg.from_file.return_value = MagicMock(devices=[])
        with redirect_stdout(out), redirect_stderr(err):
            rc = args.func(args)
    return rc, out.getvalue(), err.getvalue()


def test_raises_drive_from_the_floor_to_32():
    m = _FakeModel(drive=8)
    rc, out, _ = _run(["set-drive", "32"], m)
    assert rc == 0
    assert m.writes == [32]
    assert "8 mA -> 32 mA" in out


def test_the_handle_is_always_closed():
    """A held handle makes every later open fail with "open failed"."""
    m = _FakeModel(drive=8)
    _run(["set-drive", "32"], m)
    assert m.entered == m.exited == 1


def test_a_transient_open_failure_is_retried():
    """The Mini re-binds hid-generic every ~10 s on some hosts."""
    m = _FakeModel(drive=8, open_failures=2)
    rc, out, _ = _run(["set-drive", "32"], m)
    assert rc == 0
    assert m.writes == [32]


def test_persistent_open_failure_reports_and_fails():
    m = _FakeModel(drive=8, open_failures=99)
    rc, _, err = _run(["set-drive", "32"], m)
    assert rc == 1
    assert "could not open after 5 attempts" in err


def test_already_at_target_writes_nothing():
    m = _FakeModel(drive=32)
    rc, out, _ = _run(["set-drive", "32"], m)
    assert rc == 0
    assert m.writes == []
    assert "already 32 mA" in out


def test_invalid_value_is_rejected_without_writing():
    m = _FakeModel(drive=8)
    rc, _, err = _run(["set-drive", "10"], m)
    assert rc == 2
    assert m.writes == []
    assert "not in {8, 16, 24, 32}" in err


def test_model_without_drive_control_is_refused():
    # The LBE-1421 has a boolean high/low, not the Mini's four-step ladder.
    m = _FakeModel(drive=None, has_drive=False)
    rc, _, err = _run(["set-drive", "32"], m)
    assert rc == 2
    assert "no drive-strength control" in err
    assert m.writes == []


def test_no_matched_device_is_an_error():
    rc, _, _ = _run(["set-drive", "32"], _FakeModel(), matched=[])
    assert rc == 1


def test_serial_filter_skips_other_devices():
    m = _FakeModel(drive=8)
    rc, out, _ = _run(["set-drive", "32", "--serial", "OTHER"], m)
    assert rc == 0
    assert m.writes == []
    assert out == ""
