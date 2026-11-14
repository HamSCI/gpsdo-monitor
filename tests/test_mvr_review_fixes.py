"""MVR review fixes (2026-11-14): inventory names RP2040 candidates; the
console accepts the menu prompt only after the full header; a reader never
reopens a tty number that now belongs to a different device."""
from __future__ import annotations

import io
import json
import os
import time
from contextlib import redirect_stdout
from unittest.mock import patch

import pytest

from gpsdo_monitor import cli, mvr
from gpsdo_monitor.mvr import MvrConsole, MvrConsoleError, MvrReader, MvrTimeout
from tests.test_mvr_console import FakeMvr
from tests.test_mvr_io import _fake_tty

# --- I1a: inventory -----------------------------------------------------------


def _inventory() -> dict:
    args = cli.build_parser().parse_args(["inventory", "--json"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert args.func(args) == 0
    return json.loads(buf.getvalue())


def test_inventory_counts_an_rp2040_candidate_as_hardware(tmp_path, monkeypatch):
    _fake_tty(tmp_path, "ttyACM1", vid="2e8a", pid="000a", serial="E6614C311B4B2A2B",
              product="Seeed XIAO RP2040")
    monkeypatch.setattr(mvr, "SYS_TTY", tmp_path / "class" / "tty")
    opened = []
    monkeypatch.setattr(mvr, "_os_open", lambda *a, **k: opened.append(a))
    with patch("gpsdo_monitor.cli.enumerate_lbe", return_value=[]), \
         patch("gpsdo_monitor.cli.enumerate_unsupported_lbe", return_value=[]):
        doc = _inventory()
    assert doc["hardware_present"] is True
    assert doc["mvr_candidates"] == [{"tty": "/dev/ttyACM1", "serial": "E6614C311B4B2A2B",
                                      "product": "Seeed XIAO RP2040"}]
    assert opened == []                     # sysfs only: nothing opened


def test_inventory_without_any_gpsdo_is_not_present():
    with patch("gpsdo_monitor.cli.enumerate_lbe", return_value=[]), \
         patch("gpsdo_monitor.cli.enumerate_unsupported_lbe", return_value=[]):
        doc = _inventory()
    assert doc["hardware_present"] is False
    assert doc["mvr_candidates"] == []


# --- I2: the boot transcript holds "> " after the banner -----------------------

BOOT = (
    "=== MVR GPSDO B Firmware v20261008.1 (built Oct  8 2026 09:50:46) ===\r\n"
    "\r\n"
    "GPS02-UBX Configuration\r\n"
    "  TP when locked : 25000 Hz, 50% duty\r\n"
    "Attempt 1 of 5\r\n"
    "Step 1: Configure timepulse (UBX-CFG-TP5)\r\n"
    "  [OK  ] TP1: unlocked (0% duty) -> locked timepulse  (ACK)\r\n"
)


class Chunky(FakeMvr):
    """Hands its output out 32 bytes per read, as a real port would."""

    def read(self, timeout):
        out, self.out = self.out[:32], self.out[32:]
        return out.encode("ascii")


def test_probe_does_not_take_a_boot_transcript_for_the_menu():
    """The unit reboots just as we wake it: its transcript carries the banner
    and then "-> ".  The real menu follows.  Probe must read the menu."""
    dev = Chunky(pending=BOOT)
    info = MvrConsole(dev, timeout_sec=0.3).probe()
    assert info.complete
    assert info.clk_hz == [27_000_000, 0, 0]
    assert dev.written == [b"\r", b"99\r"]


def test_a_transcript_with_no_menu_times_out_after_only_the_wake_byte():
    dev = Chunky(pending=BOOT, silent=True)
    with pytest.raises(MvrTimeout):
        MvrConsole(dev, timeout_sec=0.3).probe()
    assert dev.written == [b"\r"]


def test_probe_fails_on_an_incomplete_header():
    class NoDrive(FakeMvr):
        def _header(self):
            return "\r\n".join(ln for ln in super()._header().split("\r\n")
                               if "drive strength" not in ln)
    dev = NoDrive()
    with pytest.raises(MvrConsoleError, match="incomplete"):
        MvrConsole(dev, timeout_sec=0.3).probe()
    # It still closes the menu it opened.
    assert dev.written == [b"\r", b"99\r"]


# --- minor 3: no prompt after the persist answer -------------------------------


def test_missing_prompt_after_the_persist_answer_says_the_change_may_apply():
    class NoPromptAfterAnswer(FakeMvr):
        def _select(self, sel):
            if self.awaiting_persist:
                self.awaiting_persist = False
                self._emit("Saved -- passthrough will start ON at boot.\r\n")
                self.boot = self.passthrough
                return
            super()._select(sel)
    dev = NoPromptAfterAnswer()
    with pytest.raises(MvrTimeout, match="may still apply.*30 s"):
        MvrConsole(dev, timeout_sec=0.2).set_passthrough(True, persist=True)
    assert dev.written == [b"\r", b"8\r", b"y\r"]


# --- minor 5: a reused tty number -----------------------------------------------


def test_reader_never_opens_a_tty_whose_usb_serial_changed():
    opened = []
    r = MvrReader("/dev/ttyACM1", serial="MVR1", reopen_backoff_sec=0.01,
                  serial_lookup=lambda _p: "OTHERPICO")
    with patch.object(mvr, "_os_open", lambda *a, **k: opened.append(a) or 0):
        r.start()
        end = time.monotonic() + 2
        while not r.gone and time.monotonic() < end:
            time.sleep(0.01)
        r.stop()
    assert r.gone
    assert opened == []


def test_reader_reopens_its_own_device():
    master, slave = os.openpty()
    path = os.ttyname(slave)
    try:
        r = MvrReader(path, serial="MVR1", serial_lookup=lambda _p: "MVR1")
        r.start()
        end = time.monotonic() + 2
        while not r.is_open and time.monotonic() < end:
            time.sleep(0.01)
        assert r.is_open and not r.gone
        r.stop()
    finally:
        os.close(master)
        os.close(slave)


def test_default_serial_lookup_reads_sysfs(tmp_path, monkeypatch):
    _fake_tty(tmp_path, "ttyACM1", vid="2e8a", pid="000a", serial="MVR1")
    monkeypatch.setattr(mvr, "SYS_TTY", tmp_path / "class" / "tty")
    assert mvr.tty_usb_serial("/dev/ttyACM1") == "MVR1"
    assert mvr.tty_usb_serial("/dev/ttyACM7") is None


def test_prober_drops_a_gone_reader_and_starts_fresh(tmp_path):
    from tests.test_mvr_io import _FakeReader
    _FakeReader.instances = []
    _fake_tty(tmp_path, "ttyACM1", vid="2e8a", pid="000a", serial="MVR1")
    p = mvr.MvrProber(sys_tty=tmp_path / "class" / "tty", reader_factory=_FakeReader)
    p.scan()
    first = _FakeReader.instances[0]
    first.confirmed = True
    first.gone = True
    assert p.scan() == []
    assert first.stopped
    p.scan()
    assert len(_FakeReader.instances) == 2
