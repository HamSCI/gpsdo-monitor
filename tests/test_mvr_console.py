"""MVR GPSDO B: the two explicit console commands, against a scripted fake.

`mvr probe` and `mvr passthrough` are the ONLY code that writes to an MVR.
Every test asserts the exact bytes sent: the wake CR, then a selection only
after the "> " prompt, and nothing else ever."""
from __future__ import annotations

import pytest

from gpsdo_monitor.mvr import ALLOWED_WRITES, MvrConsole, MvrConsoleError, MvrTimeout

HEADER = (
    "=== MVR GPSDO B Firmware v20261008.1 (built Oct  8 2026 09:50:46) ===\r\n"
    "  1) Set CLK0 frequency  (currently 27000000 Hz)\r\n"
    "  2) Set CLK1 frequency  (currently {clk1})\r\n"
    "  3) Set CLK2 frequency  (currently off)\r\n"
    "  4) Set drive strength, all channels (currently 8 mA)\r\n"
    "  5) Show this menu again\r\n"
    "  6) Show lock history\r\n"
    "  7) Restore compile-time defaults (overwrites saved config)\r\n"
    "  8) Toggle raw NMEA passthrough (currently {cur}; boot default {boot})\r\n"
    "  9) Reboot into firmware-update (UF2) mode\r\n"
    " 99) Exit menu\r\n"
    "> "
)


class FakeMvr:
    """A scripted MVR console per the developer notes, sections 1, 3 and 6.

    Opens the menu on a printable first byte (discarding it), echoes typed
    characters, takes CR-terminated selections, and closes on 99."""

    def __init__(self, *, passthrough=False, boot=False, clk1="off",
                 silent=False, no_exit_line=False, pending=""):
        self.passthrough = passthrough
        self.boot = boot
        self.clk1 = clk1
        self.silent = silent
        self.no_exit_line = no_exit_line
        self.menu_open = False
        self.awaiting_persist = False
        self.line = ""
        self.out = pending
        self.written: list[bytes] = []

    # --- port interface ----------------------------------------------------
    def write(self, data: bytes) -> None:
        self.written.append(bytes(data))
        for ch in data.decode("ascii"):
            self._byte(ch)

    def read(self, timeout: float) -> bytes:
        out, self.out = self.out, ""
        return out.encode("ascii")

    # --- device model ---------------------------------------------------------
    def _emit(self, s: str) -> None:
        if not self.silent:
            self.out += s

    def _header(self) -> str:
        return HEADER.format(clk1=self.clk1,
                             cur="ON" if self.passthrough else "OFF",
                             boot="ON" if self.boot else "OFF")

    def _byte(self, ch: str) -> None:
        if not self.menu_open:
            if 0x20 <= ord(ch) <= 0x7E or ch in "\r\n":
                self.menu_open = True           # wake byte is thrown away
                self._emit("\r\n" + self._header())
            return
        if ch in "\r\n":
            sel, self.line = self.line, ""
            self._emit("\r\n")
            self._select(sel)
            return
        self.line += ch
        self._emit(ch)                          # the firmware echoes typing

    def _select(self, sel: str) -> None:
        if self.awaiting_persist:
            self.awaiting_persist = False
            if sel in ("y", "Y"):
                self.boot = self.passthrough
                self._emit("Saved -- passthrough will start "
                           f"{'ON' if self.passthrough else 'OFF'} at boot.\r\n> ")
            else:
                self._emit("Not persisted -- boot default unchanged.\r\n> ")
            return
        if sel == "8":
            self.passthrough = not self.passthrough
            self._emit(f"NMEA passthrough turned {'ON' if self.passthrough else 'OFF'} "
                       "for this session.\r\nPersist this as the power-on default? (y/n): ")
            self.awaiting_persist = True
        elif sel == "99":
            self.menu_open = False
            if not self.no_exit_line:
                self._emit("Exiting menu, resuming NMEA passthrough.\r\n"
                           if self.passthrough else
                           "Exiting menu, back to normal status output.\r\n")
        else:
            self._emit("Not a valid option.\r\n" + self._header())


def _console(dev, timeout=0.3):
    return MvrConsole(dev, timeout_sec=timeout)


def _only_allowed(dev):
    assert all(w in ALLOWED_WRITES for w in dev.written), dev.written


def test_allowed_writes_are_exactly_the_documented_bytes():
    assert ALLOWED_WRITES == frozenset({b"\r", b"99\r", b"8\r", b"y\r", b"n\r"})


def test_probe_reads_the_header_and_exits():
    dev = FakeMvr(clk1="27000000 Hz")
    info = _console(dev).probe()
    assert dev.written == [b"\r", b"99\r"]
    assert info.firmware == "v20261008.1"
    assert info.clk_hz == [27_000_000, 27_000_000, 0]
    assert info.drive_ma == 8
    assert info.passthrough is False and info.passthrough_boot is False
    assert info.exit_line.startswith("Exiting menu")
    assert dev.menu_open is False


def test_probe_ignores_stream_noise_before_the_prompt():
    dev = FakeMvr(pending="[110100] GPS: DGPS fix, 12 sats | Loop: LOCKED | "
                          "CLK0 27000000Hz CLK1 off CLK2 off\r\n")
    info = _console(dev).probe()
    assert info.complete
    assert dev.written == [b"\r", b"99\r"]


def test_probe_times_out_cleanly_and_sends_nothing_more():
    dev = FakeMvr(silent=True)
    with pytest.raises(MvrTimeout):
        _console(dev, timeout=0.2).probe()
    assert dev.written == [b"\r"]


def test_probe_reports_a_missing_exit_line():
    dev = FakeMvr(no_exit_line=True)
    with pytest.raises(MvrTimeout):
        _console(dev, timeout=0.2).probe()
    assert dev.written == [b"\r", b"99\r"]


def test_probe_refuses_a_prompt_without_an_mvr_header():
    class NotMvr(FakeMvr):
        def _header(self):
            return "Some other firmware\r\n> "
    dev = NotMvr()
    with pytest.raises(MvrConsoleError):
        _console(dev).probe()
    assert dev.written == [b"\r"]


@pytest.mark.parametrize("persist,answer", [(False, b"n\r"), (True, b"y\r")])
def test_passthrough_on(persist, answer):
    dev = FakeMvr(passthrough=False, boot=False)
    res = _console(dev).set_passthrough(True, persist=persist)
    assert dev.written == [b"\r", b"8\r", answer, b"99\r"]
    assert dev.passthrough is True
    assert dev.boot is persist
    assert res.passthrough is True
    assert res.changed is True
    _only_allowed(dev)


def test_passthrough_off_persisted():
    dev = FakeMvr(passthrough=True, boot=True)
    res = _console(dev).set_passthrough(False, persist=True)
    assert dev.written == [b"\r", b"8\r", b"y\r", b"99\r"]
    assert dev.passthrough is False and dev.boot is False
    assert res.passthrough is False


def test_passthrough_already_in_state_does_not_toggle():
    dev = FakeMvr(passthrough=True, boot=True)
    res = _console(dev).set_passthrough(True, persist=True)
    assert dev.written == [b"\r", b"99\r"]
    assert res.changed is False


def test_persist_when_session_matches_but_boot_default_does_not():
    # ON now, OFF at boot; asked for ON --persist: toggle off unsaved, then
    # back on saved.  Never leaves the session in the wrong state.
    dev = FakeMvr(passthrough=True, boot=False)
    _console(dev).set_passthrough(True, persist=True)
    assert dev.written == [b"\r", b"8\r", b"n\r", b"8\r", b"y\r", b"99\r"]
    assert dev.passthrough is True and dev.boot is True


def test_passthrough_stops_at_a_missing_persist_question():
    class NoQuestion(FakeMvr):
        def _select(self, sel):
            if sel == "8":
                return                          # firmware said nothing
            super()._select(sel)
    dev = NoQuestion()
    with pytest.raises(MvrTimeout):
        _console(dev, timeout=0.2).set_passthrough(True, persist=False)
    assert dev.written == [b"\r", b"8\r"]


def test_console_refuses_any_other_byte():
    dev = FakeMvr()
    c = _console(dev)
    with pytest.raises(ValueError):
        c._send(b"7\r")
    assert dev.written == []
