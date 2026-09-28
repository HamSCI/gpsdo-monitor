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

Mode-1 writer protocol (the count/valid sequence lock chrony's
`refclock_shm.c` and gpsd's `ntpshmwrite.c` both implement):

    count += 1      -- odd: "a write is in progress"
    <write every timestamp/meta field; valid untouched>
    count += 1      -- even: "write complete", fields now stable
    valid = 1       -- publish

A reader samples `count`, copies the struct, then re-checks `count`:
if it changed (or was ever odd, or `valid` reads 0) the sample is
discarded and the reader retries next poll. See `write_sample()`.
"""
from __future__ import annotations

import calendar
import ctypes
import ctypes.util
import logging
import math
import os
from typing import Optional

from gpsdo_monitor.ubx import NavPvt

log = logging.getLogger(__name__)

# --- NTP SHM ABI ---------------------------------------------------------

SHM_KEY_BASE = 0x4E545030      # "NTP0"; key = SHM_KEY_BASE + unit (0-3 by convention)
MODE_1 = 1                     # count/valid sequence-lock protocol
LEAP_NONE = 0
DEFAULT_PRECISION = -10        # log2 seconds; 2**-10 s ~= 1 ms

IPC_CREAT = 0o1000              # <sys/ipc.h>, Linux
_SHM_SEGMENT_MODE = 0o600       # root-owned, per the task brief


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


# --- NAV-PVT -> SHM sample -------------------------------------------------
#
# UBX-NAV-PVT `valid` bitfield (u-blox protocol spec): bit0 validDate,
# bit1 validTime, bit2 fullyResolved. `NavPvt.valid_time` (ubx.py) checks
# only fullyResolved -- its own docstring treats that bit alone as
# sufficient for naming a second, which is true for that consumer. This
# feed's brief asks for the fuller set (validDate AND validTime AND
# fullyResolved), so it reads the raw `_valid` byte directly instead of
# widening that shared, already-tested property.
_VALID_DATE = 0x01
_VALID_TIME = 0x02
_VALID_FULLY_RESOLVED = 0x04
_VALID_TIME_MASK = _VALID_DATE | _VALID_TIME | _VALID_FULLY_RESOLVED


def nav_pvt_clock_stamp(pvt: NavPvt) -> Optional[tuple[int, int]]:
    """The named UTC instant `pvt` describes, as `(sec, nsec)` with
    `nsec` normalised into `[0, 1e9)`.

    Returns None when the fix is below 2D or the receiver has not
    resolved date+time+leap-seconds (validDate | validTime |
    fullyResolved, all three) — the same case the Mini's naming path
    (`nav_pvt_utc` in ubx.py) already treats as "no usable time"."""
    if pvt.fix_type < 2:
        return None
    if (pvt._valid & _VALID_TIME_MASK) != _VALID_TIME_MASK:
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
    plain fake in tests. See the module docstring for the count/valid
    ordering this follows; the short version: bump `count` odd, write
    every field except `valid`, bump `count` even, then set `valid`.
    Never blocks — a few attribute stores."""
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

        Never raises: any failure (most commonly "not root" — the
        segment is created 0600) is logged once and the feed disables
        itself for the rest of this process's life. The reader thread
        must never crash because chrony's SHM segment could not be
        opened; a witness that stops witnessing is strictly better than
        a dead NAV-PVT reader."""
        if self._shm is not None:
            return True
        try:
            libc = _get_libc()
            shmid = libc.shmget(self.key, ctypes.sizeof(ShmTime),
                                IPC_CREAT | _SHM_SEGMENT_MODE)
            if shmid < 0:
                self._log_open_failure(
                    f"shmget(0x{self.key:08x}) failed "
                    f"(errno {ctypes.get_errno()})")
                return False
            addr = libc.shmat(shmid, None, 0)
            # shmat returns (void *) -1 on error. ctypes.c_void_p restype
            # yields that back as None on some builds and as the raw
            # (very large) address on others -- check both.
            bad_addr = (1 << 64) - 1
            if addr is None or addr == 0 or addr == bad_addr:
                self._log_open_failure(
                    f"shmat(shmid={shmid}) failed (errno {ctypes.get_errno()})")
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
            self._log_open_failure(str(e))
            return False

    def _log_open_failure(self, detail: str) -> None:
        self._shm = None
        if not self._open_failed_logged:
            log.warning(
                "chrony SHM witness feed disabled (unit=%d key=0x%08x): %s "
                "-- needs root (segment is created 0%o); NAV-PVT keeps "
                "publishing to /run/gpsdo as normal",
                self.unit, self.key, detail, _SHM_SEGMENT_MODE)
            self._open_failed_logged = True

    def close(self) -> None:
        if self._addr is not None:
            try:
                _get_libc().shmdt(self._addr)
            except Exception:
                log.debug("chrony SHM detach failed (unit=%d)", self.unit,
                         exc_info=True)
            self._addr = None
        self._shm = None

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
        or not yet open()ed) must be a silent no-op, not a fault."""
        if self._shm is None:
            return
        try:
            write_nav_pvt(self._shm, pvt, real)
        except Exception:
            log.exception("chrony SHM write failed (unit=%d)", self.unit)
