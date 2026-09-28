"""Chrony NTP-SHM witness feed for the LBE-Mini's NAV-PVT stream.

This is a WITNESS, never a source: the refclock this feeds is always
configured `noselect` in chrony.conf, so chrony measures it against the
system clock but never steers on it (see Task 1/2 briefs under
`.superpowers/sdd/2026-09-27-mini-nav-pvt-latency/`). Nothing here
corrects anything; it only lets an operator compare a receiver-reported
UTC second against the host clock chrony already runs.

The transport is the same NTP SHM segment gpsd writes and chrony's
`refclock SHM <unit>` reads (key `0x4E545030 + unit`, the "NTP0" ASCII
convention). We open it directly with `ctypes` (`shmget`/`shmat` via
libc) rather than depend on the `sysv_ipc` package, per this repo's
stdlib-first rule — no new runtime dependency.

Struct layout (`struct shmTime`, native x86_64 Linux alignment,
`time_t` = 8-byte `long`):

    int      mode;                  //  0- 3
    volatile int count;             //  4- 7
    time_t   clockTimeStampSec;     //  8-15
    int      clockTimeStampUSec;    // 16-19
    time_t   receiveTimeStampSec;   // 24-31  (20-23 pad: time_t needs 8-B align)
    int      receiveTimeStampUSec;  // 32-35
    int      leap;                  // 36-39
    int      precision;             // 40-43
    int      nsamples;              // 44-47
    volatile int valid;             // 48-51
    unsigned clockTimeStampNSec;    // 52-55
    unsigned receiveTimeStampNSec;  // 56-59
    int      dummy[8];              // 60-91
                                     // 92-95 pad: trailing struct alignment

`volatile` is a compiler hint, not an ABI feature — plain `ctypes`
fields reproduce the same 96-byte memory layout. `tests/test_chrony_shm.py`
asserts every offset against `ctypes.sizeof`/the field descriptors'
`.offset`, rather than trusting this comment.

Mode-1 writer protocol, matching gpsd's `ntpshm_put` (`ntpshmwrite.c`)
order exactly — not just "an" order that keeps count even at rest, but
the specific sequence that closes the one race that matters:

    valid = 0       -- invalidate the OLD sample first
    count += 1      -- odd: fields below are about to change under it
    <write every timestamp/meta field>
    count += 1      -- even: fields are stable again
    valid = 1       -- publish the NEW sample

chrony's `refclock_shm.c` (`RCL_ReadShmSample`) accepts a copy unless
exactly one of three things is true: the segment's `mode` isn't 1, the
`count` it re-reads after copying the struct differs from the `count`
it read before starting the copy, or `valid` reads 0. It does **not**
check whether `count` is odd or even — that parity is this writer's
own bookkeeping, not something the reader inspects. Which is exactly
why `valid = 0` has to come first, not last, and can't be skipped: a
previous sample can still read `valid == 1` right up until this write
begins. A reader that samples `count`, copies the whole struct, and
re-checks `count` — all *before* this writer's first `count += 1`
lands — sees an unchanged `count` and `valid == 1`, and accepts a
struct that starts being overwritten under it a moment later: a torn
read chrony's count-recheck alone cannot catch, because count hadn't
moved yet when the read started. Clearing `valid` first closes that
window — any reader caught in it sees `valid == 0` and retries on its
own. See `write_sample()`.
"""
from __future__ import annotations

import calendar
import ctypes
import ctypes.util
import errno
import logging
import math
import os
import time
from typing import Optional

from gpsdo_monitor.ubx import NavPvt

log = logging.getLogger(__name__)

# --- NTP SHM ABI ---------------------------------------------------------

SHM_KEY_BASE = 0x4E545030      # "NTP0"; key = SHM_KEY_BASE + unit (0-3 by convention)
MODE_1 = 1                     # count/valid sequence-lock protocol
LEAP_NONE = 0
DEFAULT_PRECISION = -10        # log2 seconds; 2**-10 s ~= 1 ms

IPC_CREAT = 0o1000              # <sys/ipc.h>, Linux
# World-writable when WE create the segment (it does not exist yet).
# chrony itself is configured `perm 0666` for this unit (Task 3
# documents the chrony.conf side) so any owner can write it; a segment
# created 0600 would lock out every other writer including, on some
# setups, chronyd's own reader-side permission probe.
_SHM_SEGMENT_MODE = 0o666

# Units already spoken for on an hf-timestd station (`hf-timestd
# shm-init` makes 0-3 <owner>:0666). gpsdo-monitor must never contend
# with an existing writer on the same segment. Unit 3 is the fleet
# convention reserved for THIS feed -- see service._build_chrony_feed,
# the single place that enforces this.
RESERVED_SHM_UNITS: dict[int, str] = {
    0: "gpsd",
    1: "hf-timestd FUSE (writer)",
    2: "hf-timestd HPPS (writer)",
}

# M1 (final review): a persisting SHM-write failure logs once
# immediately, then at most this often while the SAME exception TYPE
# keeps recurring. A new exception type always logs right away -- that
# is new information, not a repeat. Without this, a wedged writer
# failing on every NAV-PVT (~1 Hz) would flood the journal at that
# rate; see ChronyShmFeed._log_write_failure.
WRITE_FAIL_LOG_INTERVAL_SEC = 300.0


class ShmTime(ctypes.Structure):
    """`struct shmTime` — see the module docstring for the byte layout."""

    _fields_ = [
        ("mode", ctypes.c_int),
        ("count", ctypes.c_int),
        ("clockTimeStampSec", ctypes.c_long),
        ("clockTimeStampUSec", ctypes.c_int),
        ("receiveTimeStampSec", ctypes.c_long),
        ("receiveTimeStampUSec", ctypes.c_int),
        ("leap", ctypes.c_int),
        ("precision", ctypes.c_int),
        ("nsamples", ctypes.c_int),
        ("valid", ctypes.c_int),
        ("clockTimeStampNSec", ctypes.c_uint),
        ("receiveTimeStampNSec", ctypes.c_uint),
        ("dummy", ctypes.c_int * 8),
    ]


# --- libc shmget/shmat/shmdt, lazily bound -------------------------------
#
# A module-level indirection (`_get_libc`) rather than a bind at import
# time: tests replace it with a fake object so no real SHM is ever
# touched (see tests/test_chrony_shm.py), and production only pays the
# CDLL lookup once, on the first open().

_libc: Optional[ctypes.CDLL] = None


def _get_libc() -> ctypes.CDLL:
    global _libc
    if _libc is None:
        name = ctypes.util.find_library("c") or "libc.so.6"
        libc = ctypes.CDLL(name, use_errno=True)
        libc.shmget.restype = ctypes.c_int
        libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        libc.shmat.restype = ctypes.c_void_p
        libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        libc.shmdt.restype = ctypes.c_int
        libc.shmdt.argtypes = [ctypes.c_void_p]
        _libc = libc
    return _libc


def _time_t_is_lp64() -> bool:
    """True when `long` is 8 bytes (LP64: x86_64 Linux).

    `ShmTime.clockTimeStampSec`/`receiveTimeStampSec` are `ctypes.c_long`
    on the assumption that this holds (the byte offsets in the module
    docstring were derived and tested against exactly that). Off LP64
    (e.g. a 32-bit or Windows build) `c_long` is 4 bytes and every
    offset from `clockTimeStampUSec` onward shifts — writing through
    would silently scribble over the wrong fields. A plain function
    (rather than inlining the `ctypes.sizeof` check) so tests can
    monkeypatch it without touching real `ctypes` behaviour."""
    return ctypes.sizeof(ctypes.c_long) == 8


# --- NAV-PVT -> SHM sample -------------------------------------------------


def nav_pvt_clock_stamp(pvt: NavPvt) -> Optional[tuple[int, int]]:
    """The named UTC instant `pvt` describes, as `(sec, nsec)` with
    `nsec` normalised into `[0, 1e9)`.

    Returns None when the fix is below 2D, the receiver has not
    resolved date+time+leap-seconds (`NavPvt.time_fully_valid` --
    validDate | validTime | fullyResolved, all three; the same case
    the Mini's naming path, `nav_pvt_utc` in ubx.py, already treats as
    "no usable time"), or the reported second is a leap second (`:60`,
    positive leap-second insertion) -- `calendar.timegm` has no
    leap-second model and would silently fold that into the next
    minute rather than raise, so this feed skips it and waits for the
    next NAV-PVT (well under a second later) instead of guessing."""
    if pvt.fix_type < 2:
        return None
    if not pvt.time_fully_valid:
        return None
    if pvt.second == 60:
        return None
    try:
        base = calendar.timegm((
            pvt.year, pvt.month, pvt.day,
            pvt.hour, pvt.minute, pvt.second, 0, 0, 0,
        ))
    except (ValueError, OverflowError):
        return None
    sec = base
    nsec = pvt.nano_ns
    # u-blox `nano` is signed, roughly +/-1e9: it corrects the rounded
    # `second` field either forward or backward across the boundary.
    if nsec < 0:
        sec -= 1
        nsec += 1_000_000_000
    elif nsec >= 1_000_000_000:
        sec += 1
        nsec -= 1_000_000_000
    return int(sec), int(nsec)


def _split_seconds(t: float) -> tuple[int, int]:
    """`t` (POSIX seconds) as `(sec, nsec)`, `nsec` normalised to `[0, 1e9)`."""
    sec = math.floor(t)
    nsec = round((t - sec) * 1_000_000_000)
    if nsec >= 1_000_000_000:
        sec += 1
        nsec -= 1_000_000_000
    return int(sec), int(nsec)


def write_sample(
    shm, clock_sec: int, clock_nsec: int, receive_sec: int, receive_nsec: int,
    *, leap: int = LEAP_NONE, precision: int = DEFAULT_PRECISION,
) -> None:
    """Write one sample into `shm` using chrony's mode-1 handshake.

    `shm` is anything with the `ShmTime` field names as attributes — a
    real `ShmTime` (or a `ctypes.POINTER(ShmTime)` `.contents`), or a
    plain fake in tests. See the module docstring for why this exact
    order matters; the short version: invalidate the old sample first
    (`valid = 0`), bump `count` odd, write every other field, bump
    `count` even, then set `valid = 1`. Never blocks — a few attribute
    stores."""
    shm.valid = 0
    shm.count += 1
    shm.mode = MODE_1
    shm.leap = leap
    shm.precision = precision
    shm.nsamples = 1
    shm.clockTimeStampSec = clock_sec
    shm.clockTimeStampUSec = clock_nsec // 1000
    shm.clockTimeStampNSec = clock_nsec
    shm.receiveTimeStampSec = receive_sec
    shm.receiveTimeStampUSec = receive_nsec // 1000
    shm.receiveTimeStampNSec = receive_nsec
    shm.count += 1
    shm.valid = 1


def write_nav_pvt(shm, pvt: NavPvt, real: float) -> bool:
    """Write one NAV-PVT decode into `shm`, if it carries a usable fix+time.

    `real` is `time.time()` read at the same instant the message finished
    reassembling (the reader's `nav_pvt_mono`/`nav_pvt_real` pair) — the
    receiveTimeStamp. Returns whether a sample was written; a fix below
    2D or an unresolved time is silently skipped (returns False), which
    is the normal state before first fix and not worth logging every
    NAV-PVT."""
    clock = nav_pvt_clock_stamp(pvt)
    if clock is None:
        return False
    clock_sec, clock_nsec = clock
    recv_sec, recv_nsec = _split_seconds(real)
    write_sample(shm, clock_sec, clock_nsec, recv_sec, recv_nsec)
    return True


# --- The feed --------------------------------------------------------------


class ChronyShmFeed:
    """Owns one NTP SHM segment and the single Mini allowed to write it.

    Config carries at most one `chrony_shm_unit`, but a station can have
    more than one Mini attached; only the first one whose reader starts
    gets wired to `on_nav_pvt` (`claim()`), so two devices never contend
    on the same segment. The rest keep running — they just don't feed
    this witness. See `DeviceWorker._start_ubx_reader` in service.py."""

    def __init__(self, unit: int) -> None:
        self.unit = unit
        self.key = SHM_KEY_BASE + unit
        self._shm: Optional[ShmTime] = None
        self._shmid: Optional[int] = None
        self._addr: Optional[int] = None
        self._open_failed_logged = False
        self._claimed_by: Optional[str] = None
        # M1: transition-only logging for on_nav_pvt write failures --
        # see WRITE_FAIL_LOG_INTERVAL_SEC / _log_write_failure.
        self._write_fail_last_type: Optional[type] = None
        self._write_fail_last_log_mono: Optional[float] = None

    @property
    def claimed_by(self) -> Optional[str]:
        return self._claimed_by

    @property
    def connected(self) -> bool:
        return self._shm is not None

    def claim(self, owner_key: str) -> bool:
        """First caller wins; the same caller may re-claim idempotently.

        Returns False for anyone else — the caller should leave its
        device's `on_nav_pvt` unset and log once, not retry."""
        if self._claimed_by is None:
            self._claimed_by = owner_key
            return True
        return self._claimed_by == owner_key

    def open(self) -> bool:
        """Attach to (creating if absent) the SHM segment.

        Never raises: any failure is logged once and the feed disables
        itself for the rest of this process's life. The reader thread
        must never crash because chrony's SHM segment could not be
        opened; a witness that stops witnessing is strictly better than
        a dead NAV-PVT reader."""
        if self._shm is not None:
            return True
        if not _time_t_is_lp64():
            self._log_open_failure(
                "ctypes.c_long is not 8 bytes on this platform; ShmTime's "
                "time_t layout assumes LP64 (x86_64 Linux) and does not "
                "hold here")
            return False
        try:
            libc = _get_libc()
            shmid = libc.shmget(self.key, ctypes.sizeof(ShmTime),
                                IPC_CREAT | _SHM_SEGMENT_MODE)
            if shmid < 0:
                err = ctypes.get_errno()
                self._log_open_failure(
                    f"shmget(0x{self.key:08x}) failed (errno {err})", err)
                return False
            addr = libc.shmat(shmid, None, 0)
            # shmat returns (void *) -1 on error. ctypes.c_void_p restype
            # yields that back as None on some builds and as the raw
            # (very large) address on others -- check both.
            bad_addr = (1 << 64) - 1
            if addr is None or addr == 0 or addr == bad_addr:
                err = ctypes.get_errno()
                self._log_open_failure(
                    f"shmat(shmid={shmid}) failed (errno {err})", err)
                return False
            self._shmid = shmid
            self._addr = addr
            self._shm = ctypes.cast(addr, ctypes.POINTER(ShmTime)).contents
            self._shm.mode = MODE_1
            self._shm.leap = LEAP_NONE
            self._shm.precision = DEFAULT_PRECISION
            self._shm.nsamples = 1
            log.info("chrony SHM witness feed opened: unit=%d key=0x%08x",
                     self.unit, self.key)
            return True
        except OSError as e:
            self._log_open_failure(str(e), getattr(e, "errno", 0) or 0)
            return False

    def _log_open_failure(self, detail: str, errnum: int = 0) -> None:
        self._shm = None
        if self._open_failed_logged:
            return
        if errnum in (errno.EACCES, errno.EPERM):
            # The segment exists 0666 by construction (see
            # _SHM_SEGMENT_MODE) whenever WE create it, so EACCES/EPERM
            # here means someone else got there first with a narrower
            # perm -- not "you need root". Name that, not the old
            # (wrong, once we stopped creating 0600) guess.
            reason = (f"no write access to SHM segment 0x{self.key:08x}; "
                     f"chronyd or another owner created it with a "
                     f"narrower perm")
        else:
            reason = detail
        log.warning(
            "chrony SHM witness feed disabled (unit=%d key=0x%08x): %s "
            "-- NAV-PVT keeps publishing to /run/gpsdo as normal",
            self.unit, self.key, reason)
        self._open_failed_logged = True

    def close(self) -> None:
        # M2: clear _shm FIRST, before detaching. on_nav_pvt checks
        # `self._shm is None` with no lock (the reader must never block
        # on this), so the feed has to read as disabled before the
        # segment is actually unmapped -- otherwise a write in flight can
        # land on an address shmdt() is in the middle of releasing.
        self._shm = None
        if self._addr is not None:
            try:
                _get_libc().shmdt(self._addr)
            except Exception:
                log.debug("chrony SHM detach failed (unit=%d)", self.unit,
                         exc_info=True)
            self._addr = None

    # --- the Task 1 hook -------------------------------------------------

    def on_nav_pvt(self, pvt: NavPvt, mono: float, real: float) -> None:
        """`LbeMini.on_nav_pvt` signature (`NavPvtHook`): called by the
        reader thread right after a NAV-PVT finishes reassembling.
        `mono` is unused here — the SHM sample only needs `real`
        (CLOCK_REALTIME at the same instant) as its receiveTimeStamp —
        but the hook keeps the shared signature.

        Never blocks the reader for long (a few attribute stores) and
        never raises out of here: `LbeMini._ingest` already wraps hook
        calls in try/except, but a segment that is None (feed disabled,
        or not yet open()ed) must be a silent no-op, not a fault.

        Round 2, item 7: `self._shm` is read exactly ONCE, into `shm`,
        and that binding is used for both the None-check and the write.
        Reading the attribute twice (once to check, once as the call
        argument) leaves a TOCTOU window: `close()` clears `_shm` with
        no lock (by design -- M2), so a close() landing between the two
        reads could hand `write_nav_pvt` a `None` the check never saw,
        turning a benign shutdown race into a manufactured write
        failure."""
        shm = self._shm
        if shm is None:
            return
        try:
            write_nav_pvt(shm, pvt, real)
        except Exception as exc:
            self._log_write_failure(exc)

    def _log_write_failure(self, exc: Exception) -> None:
        """M1: log a SHM write failure on transition only -- once
        immediately, then at most every WRITE_FAIL_LOG_INTERVAL_SEC
        while the same exception TYPE keeps recurring, or immediately
        again for a NEW type. Must be called from inside the `except`
        block that caught `exc` (uses `log.exception`, which reads the
        active exception via `sys.exc_info()`)."""
        now = time.monotonic()
        exc_type = type(exc)
        is_new_type = exc_type is not self._write_fail_last_type
        stale = (self._write_fail_last_log_mono is None
                 or now - self._write_fail_last_log_mono
                 >= WRITE_FAIL_LOG_INTERVAL_SEC)
        if not (is_new_type or stale):
            return
        log.exception("chrony SHM write failed (unit=%d)", self.unit)
        self._write_fail_last_type = exc_type
        self._write_fail_last_log_mono = now
