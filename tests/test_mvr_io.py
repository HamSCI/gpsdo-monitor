"""MVR GPSDO B: the passive reader on a real pty, and discovery.

⛔ N8UR, 2026-11-14: a tty opened with default termios ECHOES every byte the
device sends straight back to it, and the MVR opens its menu on the first
printable byte it receives.  Echoed menu lines then came back as menu
selections.  These tests pin the two rules that make that impossible: raw
termios with every echo flag off before the first read, and no writes."""
from __future__ import annotations

import fcntl
import os
import select
import termios
import time
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from gpsdo_monitor import mvr
from gpsdo_monitor.config import DeclaredDevice
from gpsdo_monitor.discovery import match
from gpsdo_monitor.hid_xport import HidCandidate
from gpsdo_monitor.mvr import (
    MvrCandidate,
    MvrProber,
    MvrReader,
    PortBusy,
    enumerate_rp2040_ttys,
    open_port,
    passive_listen,
)

STATUS = b"[110100] GPS: DGPS fix, 12 sats | Loop: LOCKED | CLK0 27000000Hz CLK1 27000000Hz CLK2 off\r\n"

ECHO_FLAGS = termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL


def _wait(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def _drain(fd, wait=0.3) -> bytes:
    out = b""
    while True:
        r, _, _ = select.select([fd], [], [], wait)
        if not r:
            return out
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            return out
        if not chunk:
            return out
        out += chunk
        wait = 0.05


@pytest.fixture
def pty():
    master, slave = os.openpty()
    path = os.ttyname(slave)
    # The kernel default a fresh open gets: canonical, echo ON.
    attrs = termios.tcgetattr(slave)
    assert attrs[3] & termios.ECHO, "precondition: a pty starts with echo on"
    yield master, slave, path
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


# --- open_port ---------------------------------------------------------------


def test_open_port_sets_raw_with_every_echo_flag_off(pty):
    _master, _slave, path = pty
    fd, _orig = open_port(path)
    try:
        iflag, oflag, cflag, lflag, _i, _o, _cc = termios.tcgetattr(fd)
        assert lflag & ECHO_FLAGS == 0
        assert lflag & (termios.ICANON | termios.ISIG | termios.IEXTEN) == 0
        # No CR/NL translation in either direction.
        assert iflag & (termios.ICRNL | termios.INLCR | termios.IGNCR) == 0
        assert oflag & termios.OPOST == 0
        # No software flow control: IXOFF would make the KERNEL write XOFF/XON
        # bytes to the device on its own.
        assert iflag & (termios.IXON | termios.IXOFF | termios.IXANY) == 0
        assert cflag & termios.CRTSCTS == 0
    finally:
        os.close(fd)


def test_open_port_monitor_path_is_read_only(pty):
    _master, _slave, path = pty
    seen = []
    real_open = os.open

    def spy(p, flags, *a):
        seen.append(flags)
        return real_open(p, flags, *a)

    with patch.object(mvr, "_os_open", spy):
        fd, _ = open_port(path)
    try:
        assert seen and (seen[0] & os.O_ACCMODE) == os.O_RDONLY
        assert seen[0] & os.O_NOCTTY
        with pytest.raises(OSError):
            os.write(fd, b"\r")          # structurally impossible
    finally:
        os.close(fd)


def test_open_port_refuses_a_port_someone_holds(pty):
    _master, slave, path = pty
    holder = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        before = termios.tcgetattr(slave)
        with pytest.raises(PortBusy):
            open_port(path)
        # Refused BEFORE touching termios: another program's settings stand.
        assert termios.tcgetattr(slave) == before
    finally:
        os.close(holder)


# --- the reader ------------------------------------------------------------------


def test_reader_sets_raw_before_its_first_read_and_never_writes(pty):
    master, _slave, path = pty
    checks = []
    real_read = os.read

    def spy_read(fd, n):
        checks.append(termios.tcgetattr(fd)[3] & ECHO_FLAGS)
        return real_read(fd, n)

    with patch.object(mvr, "_os_read", spy_read):
        r = MvrReader(path, confirm_window_sec=5.0)
        r.start()
        try:
            # Give the reader time to open, then send the device's output.
            assert _wait(lambda: r.is_open)
            os.write(master, b"some boot noise\r\n" + STATUS)
            assert _wait(lambda: r.confirmed)
            snap = r.snapshot()
        finally:
            r.stop()

    assert checks, "the reader never read"
    assert all(c == 0 for c in checks), "a read happened with an echo flag set"
    assert snap.pll_locked is True and snap.fix_type == "DGPS fix"
    # Nothing came back to the device: no echo, no write, not one byte.
    assert _drain(master) == b""


def test_reader_rejects_an_rp2040_that_never_speaks_mvr(pty):
    master, slave, path = pty
    original = termios.tcgetattr(slave)
    r = MvrReader(path, confirm_window_sec=0.4)
    r.start()
    try:
        assert _wait(lambda: r.is_open)
        os.write(master, b"MicroPython v1.22 on 2024-01-01; Raspberry Pi Pico\r\n>>> \r\n")
        assert _wait(lambda: r.rejected, timeout=3.0)
    finally:
        r.stop()
    assert not r.confirmed
    # Left as found: termios restored, and nothing was written to it.
    assert termios.tcgetattr(slave) == original
    assert _drain(master) == b""


def test_reader_does_not_count_time_it_could_not_listen(pty):
    _master, _slave, path = pty
    holder = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    r = MvrReader(path, confirm_window_sec=0.3, reopen_backoff_sec=0.05)
    r.start()
    try:
        time.sleep(0.6)
        assert not r.rejected
        assert r.busy
    finally:
        r.stop()
        os.close(holder)


def test_control_default_termios_echoes_to_the_device(pty):
    """The null case for every zero-bytes assertion here: with the kernel
    default termios, bytes the device sends come straight back to it.  This
    is what opened N8UR's menu.  If this stopped echoing, the other tests
    could not see an echo either."""
    master, _slave, _path = pty
    os.write(master, STATUS)
    assert _drain(master).startswith(STATUS.rstrip(b"\r\n"))


def test_passive_listen_stops_at_first_evidence(pty):
    import threading
    master, slave, path = pty
    out = {}

    def run():
        t0 = time.monotonic()
        out["st"] = passive_listen(path, window_sec=5.0)
        out["dt"] = time.monotonic() - t0

    th = threading.Thread(target=run)
    th.start()
    # Let it open and go raw before the device speaks.
    assert _wait(lambda: not termios.tcgetattr(slave)[3] & termios.ECHO)
    os.write(master, STATUS)
    th.join(3.0)
    assert out["st"].evidence == "status"
    assert out["dt"] < 2.0
    assert _drain(master) == b""


# --- sysfs discovery -----------------------------------------------------------------


def _fake_tty(sys_root: Path, name: str, *, vid: str, pid: str, serial: str,
              product: str = "XIAO RP2040") -> None:
    usb = sys_root / "devices" / f"usb-{name}"
    intf = usb / f"{name}:1.0"
    intf.mkdir(parents=True)
    (usb / "idVendor").write_text(vid + "\n")
    (usb / "idProduct").write_text(pid + "\n")
    (usb / "serial").write_text(serial + "\n")
    (usb / "product").write_text(product + "\n")
    (usb / "manufacturer").write_text("Seeed\n")
    tty = sys_root / "class" / "tty" / name
    tty.mkdir(parents=True)
    (tty / "device").symlink_to(intf)


def test_enumerate_finds_only_rp2040_cdc_ttys(tmp_path):
    _fake_tty(tmp_path, "ttyACM1", vid="2e8a", pid="000a", serial="E6614C311B4B2A2B",
              product="Seeed XIAO RP2040")
    _fake_tty(tmp_path, "ttyACM0", vid="1dd2", pid="2444", serial="LBE1")
    _fake_tty(tmp_path, "ttyACM2", vid="2e8a", pid="0005", serial="MPY")  # MicroPython
    found = enumerate_rp2040_ttys(tmp_path / "class" / "tty")
    assert [c.tty for c in found] == ["/dev/ttyACM1"]
    c = found[0]
    assert c.serial == "E6614C311B4B2A2B"
    assert c.vid == 0x2E8A and c.pid == 0x000A
    assert c.model == "MVR GPSDO B"
    assert c.product == "Seeed XIAO RP2040"


class _FakeReader:
    instances: ClassVar[list[_FakeReader]] = []

    def __init__(self, path, **_kw):
        self.path = path
        self.confirmed = False
        self.rejected = False
        self.stopped = False
        self.started = False
        _FakeReader.instances.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


def test_prober_confirms_an_mvr_and_leaves_a_silent_rp2040_alone(tmp_path):
    _FakeReader.instances = []
    _fake_tty(tmp_path, "ttyACM1", vid="2e8a", pid="000a", serial="MVR1")
    _fake_tty(tmp_path, "ttyACM2", vid="2e8a", pid="000a", serial="OTHERPICO")
    p = MvrProber(sys_tty=tmp_path / "class" / "tty", reader_factory=_FakeReader)

    assert p.scan() == []                       # listening, nothing decided yet
    assert len(_FakeReader.instances) == 2
    by_path = {r.path: r for r in _FakeReader.instances}
    by_path["/dev/ttyACM1"].confirmed = True
    by_path["/dev/ttyACM2"].rejected = True

    confirmed = p.scan()
    assert [c.serial for c in confirmed] == ["MVR1"]
    assert by_path["/dev/ttyACM2"].stopped
    # Rejected stays rejected: no new reader is opened on it while it stays put.
    p.scan()
    p.scan()
    assert len(_FakeReader.instances) == 2
    assert p.reader_for(confirmed[0]) is by_path["/dev/ttyACM1"]
    p.stop()
    assert by_path["/dev/ttyACM1"].stopped


def test_prober_drops_a_vanished_device(tmp_path):
    _FakeReader.instances = []
    _fake_tty(tmp_path, "ttyACM1", vid="2e8a", pid="000a", serial="MVR1")
    p = MvrProber(sys_tty=tmp_path / "class" / "tty", reader_factory=_FakeReader)
    p.scan()
    r = _FakeReader.instances[0]
    r.confirmed = True
    assert len(p.scan()) == 1
    (tmp_path / "class" / "tty" / "ttyACM1" / "device").unlink()
    (tmp_path / "class" / "tty" / "ttyACM1").rmdir()
    assert p.scan() == []
    assert r.stopped


# --- match() with MVR candidates ----------------------------------------------------------


MVR = MvrCandidate(path=b"/dev/ttyACM1", vid=0x2E8A, pid=0x000A, serial="MVR1",
                   product="Seeed XIAO RP2040", manufacturer="Seeed")
LBE = HidCandidate(path=b"/dev/hidraw0", vid=0x1DD2, pid=0x2444, serial="LBE1",
                   product="LBE-1421", manufacturer="Leo Bodnar")


def test_match_takes_a_lone_mvr_implicitly():
    with patch("gpsdo_monitor.discovery.enumerate_lbe", return_value=[]):
        r = match([], [MVR])
    assert r.ok
    assert r.matched[0][1] is MVR


def test_match_refuses_to_guess_between_an_mvr_and_a_bodnar():
    with patch("gpsdo_monitor.discovery.enumerate_lbe", return_value=[LBE]):
        r = match([], [MVR])
    assert not r.ok
    assert "cannot disambiguate" in r.errors[0]


def test_match_declared_serial_picks_the_mvr():
    with patch("gpsdo_monitor.discovery.enumerate_lbe", return_value=[LBE]):
        r = match([DeclaredDevice(serial="mvr1", governs=("radiod:main",))], [MVR])
    assert r.ok
    assert r.matched[0][1] is MVR
    assert r.unclaimed_present == (LBE,)


def test_raw_attrs_clears_every_dangerous_flag_even_when_all_start_set():
    """A real port may arrive with flags a pty never has (IXOFF, ECHOCTL...).
    Start from all-ones so no flag is cleared merely by its default."""
    from gpsdo_monitor.mvr import raw_attrs
    ones = 0xFFFFFFFF
    cc = [0] * 32
    iflag, oflag, cflag, lflag, *_rest, out_cc = raw_attrs(
        [ones, ones, ones, ones, termios.B9600, termios.B9600, cc])
    assert lflag & (ECHO_FLAGS | termios.ECHOCTL | termios.ECHOKE | termios.ECHOPRT) == 0
    assert lflag & (termios.ICANON | termios.ISIG | termios.IEXTEN) == 0
    assert iflag & (termios.IXON | termios.IXOFF | termios.IXANY) == 0
    assert iflag & (termios.ICRNL | termios.INLCR | termios.IGNCR | termios.ISTRIP) == 0
    assert oflag & termios.OPOST == 0
    assert cflag & (termios.CRTSCTS | termios.PARENB) == 0
    assert cflag & termios.CSIZE == termios.CS8
    assert out_cc[termios.VMIN] == 1 and out_cc[termios.VTIME] == 0
