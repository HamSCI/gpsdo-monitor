"""Tests for the chrony NTP-SHM witness feed (Task 2 of
mini-nav-pvt-latency): struct layout, the mode-1 count/valid handshake,
NAV-PVT -> SHM sample derivation, single-owner wiring across Minis, and
the `chrony_shm_unit` config default.

No real SHM segment is opened anywhere here -- `write_sample`/
`write_nav_pvt` take anything with the right attribute names (a real
`ShmTime` or a small recording fake), and `ChronyShmFeed.open()` is
exercised against a fake libc.
"""
from __future__ import annotations

import calendar
import ctypes
import errno as errno_mod
import logging
import time

import pytest

from gpsdo_monitor import chrony_shm
from gpsdo_monitor.chrony_shm import ChronyShmFeed, ShmTime
from gpsdo_monitor.config import Config, DeclaredDevice
from gpsdo_monitor.ubx import NavPvt


# --- struct layout ---------------------------------------------------------
#
# Asserted against the documented byte offsets (see chrony_shm.py's module
# docstring / the task brief's C struct), not guessed.

def test_shmtime_size_is_96_bytes():
    assert ctypes.sizeof(ShmTime) == 96


def test_shmtime_field_offsets_match_ntp_abi():
    expected = {
        "mode": 0,
        "count": 4,
        "clockTimeStampSec": 8,
        "clockTimeStampUSec": 16,
        "receiveTimeStampSec": 24,
        "receiveTimeStampUSec": 32,
        "leap": 36,
        "precision": 40,
        "nsamples": 44,
        "valid": 48,
        "clockTimeStampNSec": 52,
        "receiveTimeStampNSec": 56,
        "dummy": 60,
    }
    for name, offset in expected.items():
        assert getattr(ShmTime, name).offset == offset, name


def test_shmtime_field_sizes():
    # time_t fields are 8 bytes (x86_64 Linux, LP64 `long`); everything
    # else in this struct is a 4-byte int/unsigned; dummy is 8 ints.
    assert ShmTime.clockTimeStampSec.size == 8
    assert ShmTime.receiveTimeStampSec.size == 8
    assert ShmTime.mode.size == 4
    assert ShmTime.dummy.size == 32


# --- write_sample: the count/valid handshake -------------------------------


class _RecordingShm:
    """A plain (non-ctypes) fake that records every field write, IN
    ORDER, as `(name, value)` pairs -- proving `write_sample` only
    needs attribute access, not a real `ShmTime` -- and letting the
    handshake order (and the actual values written to `valid`) be
    asserted directly."""

    def __init__(self) -> None:
        object.__setattr__(self, "_sets", [])
        for name in ("mode", "count", "clockTimeStampSec", "clockTimeStampUSec",
                     "receiveTimeStampSec", "receiveTimeStampUSec", "leap",
                     "precision", "nsamples", "valid",
                     "clockTimeStampNSec", "receiveTimeStampNSec"):
            setattr(self, name, 0)
        self._sets.clear()

    def __setattr__(self, name, value):
        self._sets.append((name, value))
        object.__setattr__(self, name, value)


def test_write_sample_handshake_order():
    """gpsd's `ntpshm_put` order (ntpshmwrite.c), which chrony's
    refclock_shm.c reader assumes: valid=0 FIRST -- closing the window
    where a reader could copy the still-`valid==1` previous sample
    while this write is in flight -- then count odd, fields, count
    even, valid=1 LAST. chrony's RCL_ReadShmSample rejects a copy only
    when mode==1 and the count it re-reads after copying differs from
    the count it read before, OR mode isn't 1, OR valid reads 0; it
    does NOT check count's parity, so a missing/late valid=0 is not
    otherwise caught by that check."""
    shm = _RecordingShm()
    chrony_shm.write_sample(shm, clock_sec=10, clock_nsec=0,
                            receive_sec=20, receive_nsec=0)
    order = shm._sets
    names = [n for n, _ in order]
    assert order[0] == ("valid", 0), (
        "valid must be CLEARED to 0 FIRST, before count even moves")
    assert names[1] == "count", "count bumps ODD immediately after clearing valid"
    assert names.count("count") == 2, "count bumps exactly twice per sample"
    assert names.count("valid") == 2, "valid is touched exactly twice: 0, then 1"
    assert names[-2] == "count", "count goes even just before publishing"
    assert order[-1] == ("valid", 1), "valid = 1 publishes LAST"


def test_write_sample_leaves_count_even_and_valid_set():
    shm = ShmTime()
    chrony_shm.write_sample(shm, clock_sec=1000, clock_nsec=111_000_000,
                            receive_sec=2000, receive_nsec=222_000_000)
    assert shm.count == 2
    assert shm.count % 2 == 0
    assert shm.valid == 1
    assert shm.mode == chrony_shm.MODE_1


def test_write_sample_fields_are_not_swapped():
    """Mutation target: swapping the clock/receive assignments in
    write_sample makes this fail."""
    shm = ShmTime()
    chrony_shm.write_sample(shm, clock_sec=1000, clock_nsec=111_000_000,
                            receive_sec=2000, receive_nsec=222_000_000)
    assert shm.clockTimeStampSec == 1000
    assert shm.clockTimeStampUSec == 111_000
    assert shm.clockTimeStampNSec == 111_000_000
    assert shm.receiveTimeStampSec == 2000
    assert shm.receiveTimeStampUSec == 222_000
    assert shm.receiveTimeStampNSec == 222_000_000


def test_write_sample_second_call_advances_count_by_two():
    shm = ShmTime()
    chrony_shm.write_sample(shm, 1, 0, 1, 0)
    chrony_shm.write_sample(shm, 2, 0, 2, 0)
    assert shm.count == 4
    assert shm.valid == 1


# --- nav_pvt_clock_stamp: fix/validity gate + nano normalisation -----------


def _pvt(**over) -> NavPvt:
    base = dict(
        fix_type=3, num_sv=10, year=2026, month=9, day=27,
        hour=12, minute=0, second=30, lat_1e7=0, lon_1e7=0, hmsl_mm=0,
        nano_ns=0, t_acc_ns=25_000, _valid=0x07,   # date|time|fullyResolved
    )
    base.update(over)
    return NavPvt(**base)


def test_nav_pvt_clock_stamp_normalizes_negative_nano():
    pvt = _pvt(nano_ns=-250_000_000)
    stamp = chrony_shm.nav_pvt_clock_stamp(pvt)
    base = calendar.timegm((2026, 9, 27, 12, 0, 30, 0, 0, 0))
    assert stamp == (base - 1, 750_000_000)


def test_nav_pvt_clock_stamp_positive_nano_stays_in_second():
    pvt = _pvt(nano_ns=250_000_000)
    stamp = chrony_shm.nav_pvt_clock_stamp(pvt)
    base = calendar.timegm((2026, 9, 27, 12, 0, 30, 0, 0, 0))
    assert stamp == (base, 250_000_000)


def test_nav_pvt_clock_stamp_rejects_fix_below_2d():
    assert chrony_shm.nav_pvt_clock_stamp(_pvt(fix_type=0)) is None
    assert chrony_shm.nav_pvt_clock_stamp(_pvt(fix_type=1)) is None
    assert chrony_shm.nav_pvt_clock_stamp(_pvt(fix_type=2)) is not None
    assert chrony_shm.nav_pvt_clock_stamp(_pvt(fix_type=3)) is not None


@pytest.mark.parametrize("valid_bits", [0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06])
def test_nav_pvt_clock_stamp_requires_all_three_time_flags(valid_bits):
    # Only 0x07 (validDate | validTime | fullyResolved) is usable.
    assert chrony_shm.nav_pvt_clock_stamp(_pvt(_valid=valid_bits)) is None


def test_nav_pvt_clock_stamp_accepts_full_valid_mask():
    assert chrony_shm.nav_pvt_clock_stamp(_pvt(_valid=0x07)) is not None


def test_nav_pvt_clock_stamp_uses_the_public_time_fully_valid_property():
    # Item 4c: the gate reads NavPvt.time_fully_valid (public), not the
    # raw _valid byte directly.
    pvt = _pvt(_valid=0x07)
    assert pvt.time_fully_valid is True
    assert chrony_shm.nav_pvt_clock_stamp(pvt) is not None
    pvt2 = _pvt(_valid=0x05)   # validDate|fullyResolved, missing validTime
    assert pvt2.time_fully_valid is False
    assert chrony_shm.nav_pvt_clock_stamp(pvt2) is None


def test_nav_pvt_clock_stamp_skips_leap_second_60():
    # u-blox reports :60 during a positive leap-second insertion.
    # calendar.timegm has no leap-second model: it would silently fold
    # second=60 into the next minute (minute*60 + 60 == (minute+1)*60),
    # mislabeling the sample by up to a second under the hood rather
    # than raising. Skip instead of guessing -- the next NAV-PVT, well
    # under a second later, carries an ordinary second.
    pvt = _pvt(second=60)
    assert chrony_shm.nav_pvt_clock_stamp(pvt) is None


# --- write_nav_pvt: the invalid-fix / invalid-time skip --------------------


def test_write_nav_pvt_skips_invalid_fix_and_leaves_shm_untouched():
    shm = ShmTime()
    written = chrony_shm.write_nav_pvt(shm, _pvt(fix_type=0), real=1_700_000_000.5)
    assert written is False
    assert shm.count == 0
    assert shm.valid == 0


def test_write_nav_pvt_skips_unresolved_time():
    shm = ShmTime()
    written = chrony_shm.write_nav_pvt(shm, _pvt(_valid=0x03), real=1_700_000_000.5)
    assert written is False
    assert shm.count == 0


def test_write_nav_pvt_writes_a_valid_fix():
    shm = ShmTime()
    real = 1_700_000_000.5
    written = chrony_shm.write_nav_pvt(shm, _pvt(nano_ns=-250_000_000), real=real)
    assert written is True
    assert shm.valid == 1
    base = calendar.timegm((2026, 9, 27, 12, 0, 30, 0, 0, 0))
    assert (shm.clockTimeStampSec, shm.clockTimeStampNSec) == (base - 1, 750_000_000)
    assert shm.receiveTimeStampSec == 1_700_000_000
    assert shm.receiveTimeStampNSec == 500_000_000


# --- ChronyShmFeed: claim() single ownership -------------------------------


def test_claim_is_single_owner_first_wins():
    feed = ChronyShmFeed(unit=0)
    assert feed.claim("dev-a") is True
    assert feed.claim("dev-a") is True     # idempotent for the same owner
    assert feed.claim("dev-b") is False
    assert feed.claimed_by == "dev-a"


# --- ChronyShmFeed.open(): fake libc, no real SHM --------------------------


class _FailingLibc:
    """shmget always fails, with no specific errno set -- the generic
    "something went wrong" case (as opposed to the EACCES/EPERM-specific
    wording exercised by test_open_permission_failure_names_the_real_problem)."""

    def shmget(self, key, size, flags):
        return -1

    def shmat(self, shmid, addr, flags):  # pragma: no cover - not reached
        raise AssertionError("shmat must not be called after shmget fails")

    def shmdt(self, addr):
        return 0


def test_open_failure_disables_the_feed_and_logs_once(monkeypatch, caplog):
    monkeypatch.setattr(chrony_shm, "_get_libc", lambda: _FailingLibc())
    feed = ChronyShmFeed(unit=1)
    with caplog.at_level(logging.WARNING, logger="gpsdo_monitor.chrony_shm"):
        assert feed.open() is False
        assert feed.open() is False   # a second attempt still fails
    assert feed.connected is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, "the failure must be logged exactly once"

    # A disabled feed's hook is a safe no-op, never a crash.
    pvt = _pvt()
    feed.on_nav_pvt(pvt, mono=time.monotonic(), real=time.time())


class _FakeOkLibc:
    """shmget/shmat backed by a real ctypes buffer -- no SysV IPC touched."""

    def __init__(self) -> None:
        self.buf = ctypes.create_string_buffer(ctypes.sizeof(ShmTime))
        self.detached = False

    def shmget(self, key, size, flags):
        assert size == ctypes.sizeof(ShmTime)
        assert flags & chrony_shm.IPC_CREAT
        return 99

    def shmat(self, shmid, addr, flags):
        assert shmid == 99
        return ctypes.addressof(self.buf)

    def shmdt(self, addr):
        self.detached = True
        return 0


def test_open_success_initializes_static_fields_and_writes_through(monkeypatch):
    fake = _FakeOkLibc()
    monkeypatch.setattr(chrony_shm, "_get_libc", lambda: fake)
    feed = ChronyShmFeed(unit=2)
    assert feed.key == chrony_shm.SHM_KEY_BASE + 2
    assert feed.open() is True
    assert feed.connected is True
    assert feed._shm.mode == chrony_shm.MODE_1
    assert feed._shm.leap == chrony_shm.LEAP_NONE
    assert feed._shm.precision == chrony_shm.DEFAULT_PRECISION

    feed.on_nav_pvt(_pvt(nano_ns=0), mono=time.monotonic(), real=1_700_000_100.0)
    assert feed._shm.valid == 1
    assert feed._shm.receiveTimeStampSec == 1_700_000_100

    feed.close()
    assert fake.detached is True
    assert feed.connected is False


# --- M2: close() clears _shm BEFORE detaching -------------------------------


def test_close_clears_shm_before_shmdt(monkeypatch):
    """`close()` must set `_shm = None` before calling `shmdt()`, not
    after. A concurrent `on_nav_pvt` call checks `self._shm is None` with
    no lock (Task 2's design: the reader thread must never block on this),
    so the segment has to read as "disabled" before it is actually
    unmapped -- otherwise a write can land on an address `shmdt` is in the
    middle of releasing. Mutation target: swapping the two lines in
    `close()` makes this fail."""
    feed_holder: list[ChronyShmFeed] = []

    class _OrderCheckingLibc(_FakeOkLibc):
        def __init__(self) -> None:
            super().__init__()
            self.shm_was_none_at_detach: bool | None = None

        def shmdt(self, addr):
            self.shm_was_none_at_detach = feed_holder[0]._shm is None
            return super().shmdt(addr)

    fake = _OrderCheckingLibc()
    monkeypatch.setattr(chrony_shm, "_get_libc", lambda: fake)
    feed = ChronyShmFeed(unit=3)
    feed_holder.append(feed)
    assert feed.open() is True
    feed.close()
    assert fake.detached is True
    assert fake.shm_was_none_at_detach is True


class _FlagCapturingLibc(_FakeOkLibc):
    """Records the flags shmget() was called with, so the CREATE mode
    bits can be asserted directly."""

    def __init__(self) -> None:
        super().__init__()
        self.shmget_flags: int | None = None

    def shmget(self, key, size, flags):
        self.shmget_flags = flags
        return super().shmget(key, size, flags)


def test_open_creates_segment_world_writable_0666(monkeypatch):
    # Absent-segment creation must use 0666, not 0600: chrony is
    # configured with `perm 0666` (Task 3) and a segment created 0600
    # would be unreadable/unwritable by anyone else, including chronyd
    # itself on some setups.
    fake = _FlagCapturingLibc()
    monkeypatch.setattr(chrony_shm, "_get_libc", lambda: fake)
    feed = ChronyShmFeed(unit=3)
    assert feed.open() is True
    assert fake.shmget_flags is not None
    assert fake.shmget_flags & 0o777 == 0o666
    assert fake.shmget_flags & chrony_shm.IPC_CREAT


class _PermissionDeniedLibc:
    """shmget fails with a specific errno -- EACCES or EPERM -- the way
    a real "segment exists, owned by someone else, narrower perm" would."""

    def __init__(self, err: int) -> None:
        self._err = err

    def shmget(self, key, size, flags):
        ctypes.set_errno(self._err)
        return -1

    def shmat(self, shmid, addr, flags):  # pragma: no cover - not reached
        raise AssertionError("shmat must not be called after shmget fails")

    def shmdt(self, addr):
        return 0


@pytest.mark.parametrize("err", [errno_mod.EACCES, errno_mod.EPERM])
def test_open_permission_failure_names_the_real_problem(monkeypatch, caplog, err):
    monkeypatch.setattr(chrony_shm, "_get_libc", lambda: _PermissionDeniedLibc(err))
    feed = ChronyShmFeed(unit=3)
    with caplog.at_level(logging.WARNING, logger="gpsdo_monitor.chrony_shm"):
        assert feed.open() is False
    msg = caplog.records[-1].getMessage()
    assert "no write access to SHM segment" in msg
    assert "narrower perm" in msg
    assert "needs root" not in msg, (
        "stale wording -- the segment is 0666 now, so EACCES/EPERM means "
        "someone else already owns it with a narrower perm, not 'run as root'")


def test_open_refuses_off_lp64_platforms(monkeypatch, caplog):
    # ShmTime's time_t fields assume `long` is 8 bytes (LP64: x86_64
    # Linux). Off that, the documented byte offsets don't hold and
    # writing through would silently corrupt the segment for chrony.
    monkeypatch.setattr(chrony_shm, "_time_t_is_lp64", lambda: False)

    class _UnreachableLibc:
        def shmget(self, *a):  # pragma: no cover - must not run
            raise AssertionError("shmget must not run off LP64")

        def shmat(self, *a):  # pragma: no cover - must not run
            raise AssertionError("shmat must not run off LP64")

        def shmdt(self, addr):
            return 0

    monkeypatch.setattr(chrony_shm, "_get_libc", lambda: _UnreachableLibc())
    feed = ChronyShmFeed(unit=3)
    with caplog.at_level(logging.WARNING, logger="gpsdo_monitor.chrony_shm"):
        assert feed.open() is False
        assert feed.open() is False   # still refuses, still one log line
    assert feed.connected is False
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1


# --- config default + parsing -----------------------------------------------


def test_config_default_chrony_shm_unit_is_none():
    assert Config().chrony_shm_unit is None


def test_config_parses_chrony_shm_unit(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[monitor]\nchrony_shm_unit = 2\n")
    cfg = Config.from_file(p)
    assert cfg.chrony_shm_unit == 2


def test_config_missing_chrony_shm_unit_parses_as_none(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[monitor]\nprobe_interval_sec = 5\n")
    cfg = Config.from_file(p)
    assert cfg.chrony_shm_unit is None


# --- service._build_chrony_feed: the ONE choke point ------------------------
#
# Previously this guard was copy-pasted into the test itself ("decorative" --
# it proved the copy worked, not the code). It now lives at
# service._build_chrony_feed and is exercised directly.


def test_build_chrony_feed_returns_none_and_opens_nothing_when_unit_is_none(monkeypatch):
    from gpsdo_monitor.service import _build_chrony_feed

    def _boom(self):
        raise AssertionError("open() must not run when chrony_shm_unit is None")
    monkeypatch.setattr(ChronyShmFeed, "open", _boom)
    assert _build_chrony_feed(Config(chrony_shm_unit=None)) is None


def test_build_chrony_feed_refuses_reserved_units_and_names_the_owner(monkeypatch, caplog):
    from gpsdo_monitor.service import _build_chrony_feed

    def _boom(self):
        raise AssertionError("open() must not run for a reserved unit")
    monkeypatch.setattr(ChronyShmFeed, "open", _boom)

    assert chrony_shm.RESERVED_SHM_UNITS.keys() == {0, 1, 2}
    for unit, owner in chrony_shm.RESERVED_SHM_UNITS.items():
        caplog.clear()
        with caplog.at_level(logging.ERROR, logger="gpsdo_monitor.service"):
            result = _build_chrony_feed(Config(chrony_shm_unit=unit))
        assert result is None, f"unit {unit} ({owner}) must be refused"
        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(errors) == 1, f"unit {unit} must log exactly one ERROR"
        assert owner in errors[0].getMessage(), (
            f"the refusal must NAME the owner ({owner!r}) of unit {unit}")


def test_build_chrony_feed_accepts_unit_3(monkeypatch):
    from gpsdo_monitor.service import _build_chrony_feed
    monkeypatch.setattr(ChronyShmFeed, "open", lambda self: True)
    feed = _build_chrony_feed(Config(chrony_shm_unit=3))
    assert isinstance(feed, ChronyShmFeed)
    assert feed.unit == 3


def test_build_chrony_feed_mutation_ignoring_none_is_caught():
    """The fix-round-1 mutation target for item 3: a `_build_chrony_feed`
    that forgot its `unit is None -> None` guard must fail some test.
    This test documents what that failure looks like -- it does not
    itself mutate the source (the source mutation + full-suite run is
    done by hand and recorded in the commit body's "Mutations:" section
    and task-2-report.md, matching how the swap-mutation for write_sample
    is handled elsewhere in this file)."""
    from gpsdo_monitor.service import _build_chrony_feed
    # A correct helper never even constructs a ChronyShmFeed for None,
    # so it never reaches the unit arithmetic in ChronyShmFeed.__init__.
    # A mutant missing the early return falls through to
    # `ChronyShmFeed(None)`, whose `SHM_KEY_BASE + None` raises TypeError
    # -- so this call not raising IS the assertion.
    assert _build_chrony_feed(Config(chrony_shm_unit=None)) is None


# --- wiring: the Task 1 hook feeds this writer with mono/real --------------


def test_reader_hook_feeds_the_shm_with_mono_and_real():
    from tests.test_mini import _StreamFakeHid, _feature, _pvt_frames
    from gpsdo_monitor.models.lbe_mini import LbeMini

    hid = _StreamFakeHid(_feature(), _pvt_frames(second=45, nano=-250_000_000, fix=3))
    mini = LbeMini(hid)
    shm = ShmTime()
    seen: list[tuple[NavPvt, float, float]] = []

    def hook(pvt, mono, real):
        seen.append((pvt, mono, real))
        chrony_shm.write_nav_pvt(shm, pvt, real)

    mini.on_nav_pvt = hook
    mini.start_reader()
    try:
        before = time.time()
        hid.release()
        deadline = time.monotonic() + 3.0
        while not seen and time.monotonic() < deadline:
            time.sleep(0.005)
        after = time.time()
    finally:
        mini.stop_reader()

    assert len(seen) == 1
    pvt, mono, real = seen[0]
    assert pvt.fix_type == 3
    assert before <= real <= after
    # The writer used exactly the hook's own (pvt, real) -- not some other
    # clock read independently.
    expect_clock = chrony_shm.nav_pvt_clock_stamp(pvt)
    assert (shm.clockTimeStampSec, shm.clockTimeStampNSec) == expect_clock
    expect_recv_sec, expect_recv_nsec = chrony_shm._split_seconds(real)
    assert shm.receiveTimeStampSec == expect_recv_sec
    assert shm.receiveTimeStampNSec == expect_recv_nsec
    assert shm.valid == 1


# --- wiring: single-owner across two DeviceWorkers (two Minis) -------------


def test_second_mini_worker_does_not_get_the_feed(monkeypatch, tmp_path):
    from tests.test_mini import _StreamFakeHid, _feature, _pvt_frames
    from gpsdo_monitor.models.lbe_mini import LbeMini
    from gpsdo_monitor import service as svc_mod
    from gpsdo_monitor.service import DeviceWorker
    from tests.test_service import _mini_candidate

    hid_a = _StreamFakeHid(_feature(), _pvt_frames())
    hid_b = _StreamFakeHid(_feature(), _pvt_frames())

    def _open(cand):
        return LbeMini(hid_a if cand.serial == "AAA" else hid_b)

    monkeypatch.setattr(svc_mod, "open_model", _open)
    monkeypatch.setattr(svc_mod, "find_ttys_by_usb_serial", lambda _s: [])

    feed = ChronyShmFeed(unit=3)   # 3 = the fleet convention; 0-2 are reserved
    # Simulate an already-open feed without touching real SHM: the hook
    # only needs a `_shm`-shaped target to write into.
    feed._shm = ShmTime()

    cfg = Config(run_dir=tmp_path / "run", mdns_enabled=False, min_drive_ma=0,
                pps_study_enabled=False, devices=[], chrony_shm_unit=3)

    cand_a = _mini_candidate("AAA")
    cand_b = _mini_candidate("BBB")
    wa = DeviceWorker(candidate=cand_a, declared=DeclaredDevice(serial="AAA"),
                      cfg=cfg, chrony_feed=feed)
    wb = DeviceWorker(candidate=cand_b, declared=DeclaredDevice(serial="BBB"),
                      cfg=cfg, chrony_feed=feed)
    wa.start()
    wb.start()
    try:
        assert wa.mini is not None and wb.mini is not None
        assert wa.mini.on_nav_pvt == feed.on_nav_pvt
        assert wb.mini.on_nav_pvt is None
        assert feed.claimed_by == "AAA"
    finally:
        wa.stop()
        wb.stop()


# --- M1: on_nav_pvt logs a write failure on transition only ----------------


def test_on_nav_pvt_logs_write_failure_once_then_throttles(monkeypatch, caplog):
    """A wedged SHM write (e.g. a torn segment) must not log once per
    NAV-PVT (~1 Hz). It logs once immediately, stays quiet for repeats of
    the SAME exception type inside WRITE_FAIL_LOG_INTERVAL_SEC, logs again
    once that interval has passed, and logs immediately for a NEW
    exception type regardless of the interval. Mutation target: removing
    the throttle (always logging) or removing the new-type escape (never
    logging again) both make this fail."""
    fake = _FakeOkLibc()
    monkeypatch.setattr(chrony_shm, "_get_libc", lambda: fake)
    feed = ChronyShmFeed(unit=3)
    assert feed.open() is True

    def _boom(shm, pvt, real):
        raise RuntimeError("boom")

    monkeypatch.setattr(chrony_shm, "write_nav_pvt", _boom)

    clock = [1000.0]
    monkeypatch.setattr(chrony_shm.time, "monotonic", lambda: clock[0])

    pvt = _pvt()
    logger_name = "gpsdo_monitor.chrony_shm"
    with caplog.at_level(logging.ERROR, logger=logger_name):
        feed.on_nav_pvt(pvt, mono=0.0, real=0.0)
        feed.on_nav_pvt(pvt, mono=0.0, real=0.0)
        feed.on_nav_pvt(pvt, mono=0.0, real=0.0)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1, (
        "repeated failures of the SAME kind must not log per NAV-PVT")

    # Still inside the throttle window: stays quiet.
    clock[0] += chrony_shm.WRITE_FAIL_LOG_INTERVAL_SEC / 2
    with caplog.at_level(logging.ERROR, logger=logger_name):
        feed.on_nav_pvt(pvt, mono=0.0, real=0.0)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1

    # Past the throttle window: logs again.
    clock[0] += chrony_shm.WRITE_FAIL_LOG_INTERVAL_SEC
    with caplog.at_level(logging.ERROR, logger=logger_name):
        feed.on_nav_pvt(pvt, mono=0.0, real=0.0)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 2

    # A NEW exception type logs immediately, even inside the window.
    def _boom_different(shm, pvt, real):
        raise ValueError("a different kind of failure")

    monkeypatch.setattr(chrony_shm, "write_nav_pvt", _boom_different)
    with caplog.at_level(logging.ERROR, logger=logger_name):
        feed.on_nav_pvt(pvt, mono=0.0, real=0.0)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 3


def test_no_feed_configured_leaves_hook_unset(monkeypatch, tmp_path):
    from tests.test_mini import _StreamFakeHid, _feature, _pvt_frames
    from gpsdo_monitor.models.lbe_mini import LbeMini
    from gpsdo_monitor import service as svc_mod
    from gpsdo_monitor.service import DeviceWorker
    from tests.test_service import _mini_candidate

    hid = _StreamFakeHid(_feature(), _pvt_frames())
    monkeypatch.setattr(svc_mod, "open_model", lambda _c: LbeMini(hid))
    monkeypatch.setattr(svc_mod, "find_ttys_by_usb_serial", lambda _s: [])

    cfg = Config(run_dir=tmp_path / "run", mdns_enabled=False, min_drive_ma=0,
                pps_study_enabled=False, devices=[], chrony_shm_unit=None)
    w = DeviceWorker(candidate=_mini_candidate("AAA"),
                     declared=DeclaredDevice(serial="AAA"), cfg=cfg,
                     chrony_feed=None)
    w.start()
    try:
        assert w.mini is not None
        assert w.mini.on_nav_pvt is None
    finally:
        w.stop()
