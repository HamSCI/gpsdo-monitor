"""MVR GPSDO B: a serial-only GPSDO (Seeed XIAO RP2040, USB CDC, no HID).

Firmware 20261008.1 per the developer notes (N8UR, 2026-11-14).  The device
prints, on its one CDC port:

  - a periodic status line every 30 s (normal mode)
        [123] GPS: GPS fix, 9 sats | Loop: LOCKED | CLK0 27000000Hz CLK1 off CLK2 off
  - lock events the moment a state changes
        [45] GPS: LOCKED        [67] Loop: LOST
  - a boot transcript beginning with the version banner;
  - in NMEA passthrough (menu option 8, off by default): the GNSS module's
    sentences verbatim, and every line above wrapped as
        $PMVR,MSG,<text>*CS
    Position (lat/lon, grid) exists ONLY in passthrough.

⛔ The menu opens when the first byte the device RECEIVES is printable ASCII,
CR or LF.  A tty opened with default termios echoes every received byte back
to the device -- on N8UR (2026-11-14) that opened the menu and fed echoed menu
lines back as selections; an echoed "7) Restore compile-time defaults" could
have reset CLK1.  So:

  - the monitor path opens the port O_RDONLY (a write is structurally
    impossible) and puts it in raw mode, every echo flag off, no CR/NL
    translation, no software flow control (IXOFF would make the KERNEL send
    XOFF/XON bytes), BEFORE the first read;
  - only the explicit `gpsdo-monitor mvr probe|passthrough` commands
    (MvrConsole) write, and only the bytes in ALLOWED_WRITES.

The model layer elsewhere is HID-based (models/, hid_xport.py).  The MVR
does not go through it: it has its own candidate type (MvrCandidate, the same
duck-typed shape the service and index use for a HidCandidate), its own
reader, and builds its own schema-v1 DeviceReport.
"""
from __future__ import annotations

import dataclasses
import errno
import fcntl
import logging
import os
import re
import select
import termios
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from gpsdo_monitor.health import classify, fill_grid
from gpsdo_monitor.nmea import NmeaState, checksum_ok
from gpsdo_monitor.nmea import feed as nmea_feed
from gpsdo_monitor.schema import (
    Device,
    DeviceReport,
    Health,
    MvrReport,
    Outputs,
    PpsStudy,
    new_report,
)

log = logging.getLogger("gpsdo_monitor.mvr")

MVR_VID = 0x2E8A            # Raspberry Pi (RP2040)
MVR_PID = 0x000A            # generic RP2040 CDC: shared by many boards, so the
                            # id alone never declares an MVR -- content does
MODEL_NAME = "MVR GPSDO B"

SYS_TTY = Path("/sys/class/tty")

# Status lines come every 30 s.  Two can be missed by a reader that opened
# mid-period, so listen 65 s before calling an RP2040 "not an MVR".
CONFIRM_WINDOW_SEC = 65.0
# No status line for 2.5 periods: stop publishing, so the file's written_utc
# ages and consumers' staleness gates see the fault (the Mini's rule).
STATUS_STALE_SEC = 75.0
# GNSS sentences arrive at 1 Hz in passthrough.  Older than this, the NMEA
# view (fix, position) is not published: passthrough was turned off, or the
# module went quiet, and a remembered position must not pass for a live one.
NMEA_FRESH_SEC = 10.0

PASSTHROUGH_CMD = "gpsdo-monitor mvr passthrough on --persist"

# Indirection so tests can watch every open and read the reader makes.
_os_open = os.open
_os_read = os.read


# --- parsing ------------------------------------------------------------------

_STATUS_RE = re.compile(
    r"^\[(\d+)\] GPS: (?:(no data)|(.+?), (\d+) sats) \| "
    r"Loop: (LOCKED|unlocked|no data) \| (CLK0 .*)$")
_CLK_RE = re.compile(r"CLK([0-2]) (?:(\d+)Hz|(off))")
_EVENT_RE = re.compile(r"^\[(\d+)\] (GPS|Loop): (LOCKED|LOST)$")
_BANNER_RE = re.compile(r"^=== MVR GPSDO B Firmware (v\S+) \(built (.*)\) ===$")
_MENU_CLK_RE = re.compile(
    r"^\s*[123]\) Set CLK([0-2]) frequency\s+\(currently (?:(\d+) Hz|(off))\)")
_MENU_DRIVE_RE = re.compile(r"^\s*4\) Set drive strength.*\(currently (\d+) mA\)")
_MENU_PT_RE = re.compile(
    r"^\s*8\) Toggle raw NMEA passthrough "
    r"\(currently (ON|OFF); boot default (ON|OFF)\)")
# "*** HALTED ***" in normal mode; '*' becomes "'" inside $PMVR text.
_HALT_RE = re.compile(r"^(?:\*\*\*|''') HALTED (?:\*\*\*|''')$")


@dataclass(frozen=True)
class MvrStatus:
    """One periodic status line."""

    uptime_sec: int
    gps_state: str                  # "fix" | "no data"
    fix_type: str | None            # "GPS fix", "DGPS fix", "RTK fixed", ...
    sats: int | None
    loop: str                       # "LOCKED" | "unlocked" | "no data"
    clk_hz: tuple[int, int, int]    # requested Hz per output, 0 = off


def parse_status_line(text: str) -> MvrStatus | None:
    m = _STATUS_RE.match(text.strip())
    if m is None:
        return None
    clks: dict[int, int] = {}
    for cm in _CLK_RE.finditer(m.group(6)):
        clks[int(cm.group(1))] = 0 if cm.group(3) else int(cm.group(2))
    if set(clks) != {0, 1, 2}:
        return None
    no_data = m.group(2) is not None
    return MvrStatus(
        uptime_sec=int(m.group(1)),
        gps_state="no data" if no_data else "fix",
        fix_type=None if no_data else m.group(3),
        sats=None if no_data else int(m.group(4)),
        loop=m.group(5),
        clk_hz=(clks[0], clks[1], clks[2]),
    )


def parse_lock_event(text: str) -> tuple[int, str, str] | None:
    """`[45] GPS: LOCKED` -> (45, "GPS", "LOCKED")."""
    m = _EVENT_RE.match(text.strip())
    if m is None:
        return None
    return int(m.group(1)), m.group(2), m.group(3)


def parse_banner(text: str) -> tuple[str, str] | None:
    """The version banner (boot transcript and menu header) -> (firmware, built)."""
    m = _BANNER_RE.match(text.strip())
    if m is None:
        return None
    return m.group(1), m.group(2)


def unwrap_pmvr(line: str) -> str | None:
    """`$PMVR,MSG,<text>*CS` -> <text>, or None if not one or the checksum fails.

    <text> runs from after `$PMVR,MSG,` to the LAST `*`; commas inside it are
    not escaped, so it is never split into fields.  Leading spaces are kept."""
    line = line.rstrip("\r\n")
    prefix = "$PMVR,MSG,"
    if not line.startswith(prefix):
        return None
    star = line.rfind("*")
    if star < len(prefix) - 1 or len(line) < star + 3:
        return None
    ck = 0
    for b in line[1:star].encode("ascii", errors="replace"):
        ck ^= b
    try:
        if int(line[star + 1:star + 3], 16) != ck:
            return None
    except ValueError:
        return None
    return line[len(prefix):star]


@dataclass
class MenuInfo:
    """State shown by the menu header (section 3 of the notes)."""

    firmware: str | None = None
    built: str | None = None
    clk_hz: list = field(default_factory=lambda: [None, None, None])
    drive_ma: int | None = None
    passthrough: bool | None = None
    passthrough_boot: bool | None = None
    exit_line: str | None = None

    @property
    def complete(self) -> bool:
        return (self.firmware is not None and None not in self.clk_hz
                and self.drive_ma is not None and self.passthrough is not None)


def apply_menu_line(info: MenuInfo, line: str) -> bool:
    """Fold one menu-header line into `info`; True if it was one."""
    b = parse_banner(line)
    if b is not None:
        info.firmware, info.built = b
        return True
    m = _MENU_CLK_RE.match(line)
    if m is not None:
        info.clk_hz[int(m.group(1))] = 0 if m.group(3) else int(m.group(2))
        return True
    m = _MENU_DRIVE_RE.match(line)
    if m is not None:
        info.drive_ma = int(m.group(1))
        return True
    m = _MENU_PT_RE.match(line)
    if m is not None:
        info.passthrough = m.group(1) == "ON"
        info.passthrough_boot = m.group(2) == "ON"
        return True
    return False


# --- stream state ------------------------------------------------------------------


@dataclass
class MvrState:
    """Everything the passive reader has learned from the stream."""

    # First kind of MVR-only content seen: "status" | "event" | "banner" |
    # "pmvr" | "halted".  None: nothing yet, so not (yet) known to be an MVR.
    evidence: str | None = None
    firmware: str | None = None
    firmware_built: str | None = None
    uptime_sec: int | None = None
    gps_state: str | None = None        # "fix" | "no data" | "LOCKED" | "LOST"
    fix_type: str | None = None
    sats: int | None = None
    loop_state: str | None = None       # "LOCKED" | "unlocked" | "no data" | "LOST"
    pll_locked: bool | None = None
    gps_locked: bool | None = None
    clk_hz: list = field(default_factory=lambda: [None, None, None])
    drive_ma: int | None = None         # only the menu shows it
    passthrough: bool | None = None     # as observed in the stream
    passthrough_boot: bool | None = None
    halted: bool = False
    last_status_mono: float | None = None
    last_halt_mono: float | None = None
    last_nmea_mono: float | None = None
    bad_pmvr_checksum: int = 0
    nmea: NmeaState = field(default_factory=NmeaState)


def copy_state(st: MvrState) -> MvrState:
    return dataclasses.replace(st, clk_hz=list(st.clk_hz),
                               nmea=dataclasses.replace(st.nmea))


def feed_line(state: MvrState, line: str, *, now_mono: float, now_wall: float) -> None:
    """Update `state` from one line of the device's output."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return
    if line.startswith("$PMVR,"):
        text = unwrap_pmvr(line)
        if text is None:
            state.bad_pmvr_checksum += 1
            return
        if state.evidence is None:
            state.evidence = "pmvr"
        state.passthrough = True
        _feed_text(state, text, now_mono=now_mono, wrapped=True)
        return
    if line.startswith("$"):
        # GNSS sentence: only passthrough forwards these.  The NMEA reader
        # the Bodnars use does the parsing, so fix/sats/lat/lon come out
        # identically.
        if "*" in line and checksum_ok(line):
            state.last_nmea_mono = now_mono
            state.passthrough = True
        nmea_feed(state.nmea, line, now=now_wall)
        return
    _feed_text(state, line, now_mono=now_mono, wrapped=False)


def _feed_text(state: MvrState, text: str, *, now_mono: float, wrapped: bool) -> None:
    s = text.strip()
    st = parse_status_line(s)
    if st is not None:
        state.evidence = state.evidence or "status"
        state.uptime_sec = st.uptime_sec
        state.gps_state = st.gps_state
        state.fix_type = st.fix_type
        state.sats = st.sats
        state.loop_state = st.loop
        state.pll_locked = st.loop == "LOCKED"
        state.gps_locked = st.gps_state == "fix"
        state.clk_hz = list(st.clk_hz)
        state.last_status_mono = now_mono
        if not wrapped:
            state.passthrough = False
        return
    ev = parse_lock_event(s)
    if ev is not None:
        state.evidence = state.evidence or "event"
        uptime, what, how = ev
        state.uptime_sec = uptime
        if what == "GPS":
            state.gps_state = how
            state.gps_locked = how == "LOCKED"
        else:
            state.loop_state = how
            state.pll_locked = how == "LOCKED"
        if not wrapped:
            state.passthrough = False
        return
    if _HALT_RE.match(s):
        state.evidence = state.evidence or "halted"
        state.halted = True
        state.last_halt_mono = now_mono
        return
    info = MenuInfo()
    if apply_menu_line(info, text):
        if info.firmware is not None:
            state.evidence = state.evidence or "banner"
            state.firmware, state.firmware_built = info.firmware, info.built
            state.halted = False          # a banner means it (re)booted
        for i, hz in enumerate(info.clk_hz):
            if hz is not None:
                state.clk_hz[i] = hz
        if info.drive_ma is not None:
            state.drive_ma = info.drive_ma
        if info.passthrough is not None:
            state.passthrough = info.passthrough
            state.passthrough_boot = info.passthrough_boot


# --- port handling -------------------------------------------------------------------


class PortBusy(OSError):
    """Another process holds the port's lock (the service, or a command)."""


def raw_attrs(attrs: list) -> list:
    """termios attributes for a byte-transparent, silent port."""
    iflag, oflag, cflag, lflag, _ispeed, _ospeed, cc = attrs
    cc = list(cc)
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
               | termios.INLCR | termios.IGNCR | termios.ICRNL
               | termios.IXON | termios.IXOFF | termios.IXANY)
    oflag &= ~termios.OPOST
    echo = termios.ECHO | termios.ECHOE | termios.ECHOK | termios.ECHONL
    for extra in ("ECHOCTL", "ECHOKE", "ECHOPRT"):
        echo |= getattr(termios, extra, 0)
    lflag &= ~(echo | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CRTSCTS)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 1
    cc[termios.VTIME] = 0
    # The baud rate is ignored by USB CDC; 115200 8N1 is the nominal setting.
    return [iflag, oflag, cflag, lflag, termios.B115200, termios.B115200, cc]


def open_port(path: str, *, write: bool = False) -> tuple[int, list]:
    """Open `path`, take its lock, make it raw.  Returns (fd, original termios).

    Read-only unless `write` (only the explicit console commands pass it).
    The exclusive flock is how the service and the commands keep off each
    other's port: whoever holds it, the other refuses -- BEFORE touching
    termios, so a port someone else holds keeps their settings."""
    flags = (os.O_RDWR if write else os.O_RDONLY) | os.O_NOCTTY | os.O_NONBLOCK
    flags |= getattr(os, "O_CLOEXEC", 0)
    fd = _os_open(path, flags)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PortBusy(errno.EBUSY, f"{path} is held by another process") from None
        orig = termios.tcgetattr(fd)
        termios.tcsetattr(fd, termios.TCSANOW, raw_attrs(orig))
    except BaseException:
        os.close(fd)
        raise
    return fd, orig


def _restore_and_close(fd: int, orig: list | None) -> None:
    if orig is not None:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, orig)
        except termios.error:
            pass
    try:
        os.close(fd)
    except OSError:
        pass


class _Lines:
    """Bytes -> complete lines (CR/LF stripped).  A run with no newline past
    4 KiB is dropped: nothing this device prints is that long."""

    MAX = 4096

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, chunk: bytes) -> list[str]:
        self.buf += chunk
        *lines, self.buf = self.buf.split(b"\n")
        if len(self.buf) > self.MAX:
            self.buf = b""
        return [ln.rstrip(b"\r").decode("ascii", errors="replace") for ln in lines]


def listen_on_fd(fd: int, *, window_sec: float,
                 until: Callable[[MvrState], bool] | None = None,
                 state: MvrState | None = None,
                 clock: Callable[[], float] = time.monotonic) -> MvrState:
    """Read (never write) from an already-raw fd for up to `window_sec`,
    returning early once `until(state)` holds (default: MVR evidence seen)."""
    until = until or (lambda s: s.evidence is not None)
    state = state if state is not None else MvrState()
    lines = _Lines()
    deadline = clock() + window_sec
    while True:
        left = deadline - clock()
        if left <= 0:
            return state
        r, _, _ = select.select([fd], [], [], min(left, 0.2))
        if not r:
            continue
        chunk = _os_read(fd, 4096)
        if not chunk:
            return state
        for ln in lines.feed(chunk):
            feed_line(state, ln, now_mono=clock(), now_wall=time.time())
        if until(state):
            return state


def passive_listen(path: str, *, window_sec: float = CONFIRM_WINDOW_SEC,
                   until: Callable[[MvrState], bool] | None = None) -> MvrState:
    """Open read-only, listen, close.  A port that showed no MVR content gets
    its original termios back: it is left as it was found."""
    fd, orig = open_port(path)
    st = None
    try:
        st = listen_on_fd(fd, window_sec=window_sec, until=until)
        return st
    finally:
        _restore_and_close(fd, orig if (st is None or st.evidence is None) else None)


# --- the long-lived reader ---------------------------------------------------------------


class MvrReader:
    """Background thread: passively read one RP2040 tty, keep an MvrState.

    Until MVR content appears, it is only listening to find out.  After
    `confirm_window_sec` of actual listening with none, it marks itself
    `rejected`, restores the port's termios and closes it: an RP2040 that is
    not an MVR is left alone.  Time the port was held by someone else does
    not count toward the window."""

    def __init__(self, tty_path: str, *, confirm_window_sec: float = CONFIRM_WINDOW_SEC,
                 reopen_backoff_sec: float = 1.0) -> None:
        self.tty_path = str(tty_path)
        self.confirm_window_sec = confirm_window_sec
        self.reopen_backoff_sec = reopen_backoff_sec
        self._state = MvrState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.rejected = False
        self.busy = False
        self.is_open = False
        self.open_error: str | None = None
        self.listened_sec = 0.0

    @property
    def confirmed(self) -> bool:
        return self._state.evidence is not None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("MvrReader already started")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"mvr-{Path(self.tty_path).name}", daemon=True)
        self._thread.start()

    def stop(self, *, timeout_sec: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
            self._thread = None

    def snapshot(self) -> MvrState:
        with self._lock:
            return copy_state(self._state)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                fd, orig = open_port(self.tty_path)
            except PortBusy as e:
                self.busy, self.open_error = True, str(e)
                if self._stop.wait(self.reopen_backoff_sec):
                    return
                continue
            except OSError as e:
                self.busy, self.open_error = False, f"{type(e).__name__}: {e}"
                if self._stop.wait(self.reopen_backoff_sec):
                    return
                continue
            self.busy, self.open_error, self.is_open = False, None, True
            outcome = "error"
            try:
                outcome = self._read(fd)
            finally:
                self.is_open = False
                _restore_and_close(fd, orig if outcome == "reject" else None)
            if outcome in ("stop", "reject"):
                return
            if self._stop.wait(self.reopen_backoff_sec):
                return

    def _read(self, fd: int) -> str:
        lines = _Lines()
        last = time.monotonic()
        while not self._stop.is_set():
            r, _, _ = select.select([fd], [], [], 0.2)
            now = time.monotonic()
            if not self.confirmed:
                self.listened_sec += now - last
            last = now
            if r:
                try:
                    chunk = _os_read(fd, 4096)
                except BlockingIOError:
                    chunk = None
                except OSError as e:
                    self.open_error = f"{type(e).__name__}: {e}"
                    return "error"
                if chunk == b"":
                    self.open_error = "EOF"
                    return "error"
                if chunk:
                    with self._lock:
                        for ln in lines.feed(chunk):
                            feed_line(self._state, ln, now_mono=time.monotonic(),
                                      now_wall=time.time())
            if not self.confirmed and self.listened_sec >= self.confirm_window_sec:
                self.rejected = True
                log.info("%s: no MVR content in %.0f s of listening; not an MVR, "
                         "leaving it alone", self.tty_path, self.listened_sec)
                return "reject"
        return "stop"


# --- discovery -----------------------------------------------------------------------------


@dataclass(frozen=True)
class MvrCandidate:
    """An RP2040 CDC tty.  Same duck-typed shape as hid_xport.HidCandidate
    (path/vid/pid/serial/product/manufacturer/model), so discovery.match(),
    the service's worker keys and index.json treat both alike.  `path` is
    the tty node, as bytes, as HidCandidate.path is."""

    path: bytes
    vid: int
    pid: int
    serial: str
    product: str = ""
    manufacturer: str = ""

    @property
    def model(self) -> str:
        return MODEL_NAME

    @property
    def tty(self) -> str:
        return self.path.decode(errors="replace")


def _read_sysfs(p: Path) -> str | None:
    try:
        return p.read_text().strip()
    except OSError:
        return None


def enumerate_rp2040_ttys(sys_tty: Path | None = None) -> list[MvrCandidate]:
    """Every /dev/ttyACM* whose USB device is 2e8a:000a.  sysfs only: this
    opens nothing."""
    root = Path(sys_tty) if sys_tty is not None else SYS_TTY
    out: list[MvrCandidate] = []
    if not root.is_dir():
        return out
    for tty in sorted(root.glob("ttyACM*")):
        try:
            usb = (tty / "device").resolve().parent
        except OSError:
            continue
        vid, pid = _read_sysfs(usb / "idVendor"), _read_sysfs(usb / "idProduct")
        if vid is None or pid is None:
            continue
        try:
            if int(vid, 16) != MVR_VID or int(pid, 16) != MVR_PID:
                continue
        except ValueError:
            continue
        out.append(MvrCandidate(
            path=str(Path("/dev") / tty.name).encode(),
            vid=MVR_VID, pid=MVR_PID,
            serial=_read_sysfs(usb / "serial") or "",
            product=_read_sysfs(usb / "product") or "",
            manufacturer=_read_sysfs(usb / "manufacturer") or "",
        ))
    return out


class MvrProber:
    """Owns one MvrReader per RP2040 tty; `scan()` returns the confirmed MVRs.

    A reader that is rejected is closed and its tty remembered, so it is not
    opened again while it stays plugged in.  The reader of a confirmed MVR
    keeps running: the service's worker reads its snapshot each tick."""

    def __init__(self, *, sys_tty: Path | None = None,
                 confirm_window_sec: float = CONFIRM_WINDOW_SEC,
                 reader_factory: Callable[..., MvrReader] | None = None) -> None:
        self.sys_tty = sys_tty
        self.confirm_window_sec = confirm_window_sec
        self.reader_factory = reader_factory or MvrReader
        self._readers: dict[tuple[str, str], tuple[MvrCandidate, MvrReader]] = {}
        self._rejected: set[tuple[str, str]] = set()

    @staticmethod
    def _key(c: MvrCandidate) -> tuple[str, str]:
        return (c.tty, c.serial)

    def scan(self) -> list[MvrCandidate]:
        try:
            present = enumerate_rp2040_ttys(self.sys_tty)
        except OSError:
            present = []
        keys = {self._key(c): c for c in present}
        for k in list(self._readers):
            if k not in keys:
                _c, r = self._readers.pop(k)
                r.stop()
        self._rejected &= set(keys)
        for k, c in keys.items():
            if k in self._rejected or k in self._readers:
                continue
            log.info("%s: RP2040 %04x:%04x (%s) -- listening up to %.0f s for an MVR",
                     c.tty, c.vid, c.pid, c.product or "?", self.confirm_window_sec)
            r = self.reader_factory(c.tty, confirm_window_sec=self.confirm_window_sec)
            r.start()
            self._readers[k] = (c, r)
        confirmed: list[MvrCandidate] = []
        for k, (c, r) in list(self._readers.items()):
            if r.rejected:
                r.stop()
                del self._readers[k]
                self._rejected.add(k)
            elif r.confirmed:
                confirmed.append(c)
        return confirmed

    def reader_for(self, c: MvrCandidate) -> MvrReader | None:
        entry = self._readers.get(self._key(c))
        return entry[1] if entry is not None else None

    def stop(self) -> None:
        for _c, r in self._readers.values():
            r.stop()
        self._readers.clear()


# --- the report ----------------------------------------------------------------------------


def _fmt_clk(hz: int | None) -> str:
    if hz is None:
        return "unknown"
    return "off" if hz == 0 else f"{hz} Hz"


def advisories(st: MvrState, health: Health, *, fresh_status: bool,
               nmea_fresh: bool) -> list[str]:
    out: list[str] = []
    if st.halted:
        out.append("MVR HALTED: the firmware stopped after a GNSS or Si5351 "
                   "configuration failure; fix the cause, then power-cycle it")
    if fresh_status and st.loop_state != "LOCKED":
        out.append(f"loop {st.loop_state}: the MVR's PLL is not locked")
    if health.gps_locked is False:
        out.append(f"GPS has no fix (MVR reports GPS: {st.gps_state})")
    c0, c1 = st.clk_hz[0], st.clk_hz[1]
    if c0 is not None and c1 is not None and 27_000_000 not in (c0, c1):
        out.append(f"no output at 27 MHz (CLK0 {_fmt_clk(c0)}, CLK1 {_fmt_clk(c1)}); "
                   "an RX888 wants a 27 MHz reference -- check which output feeds it")
    if not nmea_fresh:
        if st.passthrough:
            out.append(f"position unavailable: NMEA passthrough is on but no GNSS "
                       f"sentence arrived in {NMEA_FRESH_SEC:.0f} s")
        else:
            out.append("position unavailable: the MVR reports position only in NMEA "
                       f"passthrough, which is off; enable it with `{PASSTHROUGH_CMD}` "
                       "(stop the gpsdo-monitor service first)")
    return out


def build_report(st: MvrState, *, candidate: MvrCandidate, declared, cfg, host: str,
                 now_mono: float, now_wall: float | None = None) -> DeviceReport | None:
    """Schema-v1 report for one MVR, or None when there is nothing current
    to say (no status line yet, or none for STATUS_STALE_SEC)."""
    now_wall = now_wall if now_wall is not None else time.time()

    def _fresh(t: float | None, limit: float) -> bool:
        return t is not None and (now_mono - t) <= limit

    fresh_status = _fresh(st.last_status_mono, STATUS_STALE_SEC)
    fresh_halt = st.halted and _fresh(st.last_halt_mono, STATUS_STALE_SEC)
    if not (fresh_status or fresh_halt):
        return None
    nmea_fresh = _fresh(st.last_nmea_mono, NMEA_FRESH_SEC)

    clk = st.clk_hz
    health = Health(
        pll_locked=bool(fresh_status and st.pll_locked),
        outputs_enabled=any(bool(hz) for hz in clk),
        gps_locked=st.gps_locked if fresh_status else None,
        sats_used=st.sats if st.gps_locked else None,
        gps_fix_type=st.fix_type if st.gps_locked else None,
    )
    if health.gps_locked is False:
        health.gps_fix = "no_fix"
    if nmea_fresh:
        ns = st.nmea
        if ns.gps_fix is not None:
            health.gps_fix = ns.gps_fix
        if ns.sats_used is not None:
            health.sats_used = ns.sats_used
        health.fix_age_sec = ns.fix_age_sec(now=now_wall)
        health.latitude = ns.latitude
        health.longitude = ns.longitude
        health.altitude_m = ns.altitude_m
        health.grid = ns.maidenhead()
    fill_grid(health)

    outputs = Outputs(drive_ma=st.drive_ma,
                      clk0_hz=clk[0], clk1_hz=clk[1], clk2_hz=clk[2])

    a_level, reason = classify(
        health, PpsStudy(enabled=False, window_sec=0),
        probe_age_sec=0.0, probe_interval_sec=cfg.probe_interval_sec,
        pps_expected=False)
    if fresh_halt:
        a_level, reason = "A0", "mvr_halted"

    device = Device(
        model=MODEL_NAME,
        pid=f"{candidate.pid:#06x}",
        serial=candidate.serial or "unknown",
        hid_path=candidate.tty,
        firmware=st.firmware,
        firmware_source="mvr-console" if st.firmware else "unavailable",
    )
    block = MvrReport(
        uptime_sec=st.uptime_sec,
        gps_state=st.gps_state,
        loop_state=st.loop_state,
        passthrough=st.passthrough,
        passthrough_boot_default=st.passthrough_boot,
        last_status_age_sec=(round(now_mono - st.last_status_mono, 1)
                             if st.last_status_mono is not None else None),
        halted=st.halted,
        firmware_built=st.firmware_built,
        bad_pmvr_checksum=st.bad_pmvr_checksum,
    )
    return new_report(
        host=host,
        probe_interval_sec=cfg.probe_interval_sec,
        device=device,
        governs=list(declared.governs),
        health=health,
        outputs=outputs,
        pps_study=PpsStudy(enabled=False, window_sec=0),
        a_level_hint=a_level,
        a_level_reason=reason,
        advisories=advisories(st, health, fresh_status=fresh_status,
                              nmea_fresh=nmea_fresh),
        mvr=block,
    )


# --- the explicit console commands (the only writers) ----------------------------------------

# Every byte sequence the console may ever send.  "\r" wakes the menu; "8"
# toggles passthrough; "y"/"n" answers its persist question; "99" exits.
ALLOWED_WRITES = frozenset({b"\r", b"99\r", b"8\r", b"y\r", b"n\r"})


class MvrConsoleError(RuntimeError):
    pass


class MvrTimeout(MvrConsoleError):
    pass


@dataclass
class PassthroughResult:
    passthrough: bool
    passthrough_boot: bool | None
    changed: bool
    before: MenuInfo
    exit_line: str


class TtyPort:
    """The console's transport over a raw, read-write fd."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            _r, w, _x = select.select([], [self.fd], [], 2.0)
            if not w:
                raise MvrTimeout("port not writable")
            n = os.write(self.fd, view)
            view = view[n:]

    def read(self, timeout: float) -> bytes:
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return b""
        try:
            return _os_read(self.fd, 4096)
        except BlockingIOError:
            return b""


class MvrConsole:
    """Drives the menu over `port` (anything with write(bytes) and
    read(timeout) -> bytes).  Sends a selection only after it has seen the
    prompt that asks for it, so a missing answer stops the sequence instead
    of typing into the unknown; the menu closes itself after 30 s idle."""

    PROMPT = "> "

    def __init__(self, port, *, timeout_sec: float = 5.0) -> None:
        self.port = port
        self.timeout_sec = timeout_sec
        self._buf = ""

    def _send(self, data: bytes) -> None:
        if data not in ALLOWED_WRITES:
            raise ValueError(f"refusing to send {data!r} to an MVR")
        self._buf = ""            # wait only on what follows this send
        self.port.write(data)

    def _wait_for(self, *tokens: str) -> tuple[str, str]:
        """Read until one of `tokens` appears; return (token, text so far)."""
        deadline = time.monotonic() + self.timeout_sec
        while True:
            for t in tokens:
                if t in self._buf:
                    return t, self._buf
            left = deadline - time.monotonic()
            if left <= 0:
                raise MvrTimeout(f"no {' or '.join(repr(t) for t in tokens)} "
                                 f"within {self.timeout_sec:.0f} s")
            chunk = self.port.read(min(left, 0.1))
            if chunk:
                self._buf += chunk.decode("ascii", errors="replace")

    def _open_menu(self) -> MenuInfo:
        self._send(b"\r")
        _t, text = self._wait_for(self.PROMPT)
        info = MenuInfo()
        for ln in text.replace("\r", "").split("\n"):
            apply_menu_line(info, ln)
        if info.firmware is None:
            raise MvrConsoleError("a prompt came back without the MVR GPSDO B "
                                  "menu header; sent nothing further")
        return info

    def _exit_menu(self) -> str:
        """Send 99 and return the whole "Exiting menu, ..." line."""
        self._send(b"99\r")
        marker = "Exiting menu"
        deadline = time.monotonic() + self.timeout_sec
        while True:
            at = self._buf.find(marker)
            if at >= 0 and "\n" in self._buf[at:]:
                return self._buf[at:].split("\n")[0].strip()
            left = deadline - time.monotonic()
            if left <= 0:
                raise MvrTimeout(f"no complete {marker!r} line within "
                                 f"{self.timeout_sec:.0f} s (the menu closes "
                                 "itself after 30 s idle)")
            chunk = self.port.read(min(left, 0.1))
            if chunk:
                self._buf += chunk.decode("ascii", errors="replace")

    def probe(self) -> MenuInfo:
        info = self._open_menu()
        info.exit_line = self._exit_menu()
        return info

    def set_passthrough(self, on: bool, *, persist: bool) -> PassthroughResult:
        before = self._open_menu()
        if before.passthrough is None:
            exit_line = self._exit_menu()
            raise MvrConsoleError(f"menu header showed no passthrough state ({exit_line})")
        if before.passthrough == on:
            if persist and before.passthrough_boot != on:
                answers = [b"n\r", b"y\r"]   # away unsaved, then back saved
            else:
                answers = []
        else:
            answers = [b"y\r" if persist else b"n\r"]
        boot = before.passthrough_boot
        for ans in answers:
            self._send(b"8\r")
            self._wait_for("(y/n):")
            self._send(ans)
            tok, _ = self._wait_for("Saved", "Not persisted")
            if tok == "Saved":
                boot = None               # learned below from the exit state
            self._wait_for(self.PROMPT)
        exit_line = self._exit_menu()
        now_on = "resuming NMEA passthrough" in exit_line
        if not now_on and "back to normal status output" not in exit_line:
            raise MvrConsoleError(f"unrecognised exit line: {exit_line!r}")
        if now_on != on:
            raise MvrConsoleError(f"passthrough is {'ON' if now_on else 'OFF'} after "
                                  f"the change, wanted {'ON' if on else 'OFF'}")
        if boot is None:
            boot = now_on                 # "Saved" stores the session state
        return PassthroughResult(passthrough=now_on, passthrough_boot=boot,
                                 changed=bool(answers), before=before,
                                 exit_line=exit_line)
