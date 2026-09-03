"""The daemon restores OUT1 drive on every attach.

⛔ AC0G-ND, 2026-09-03.  Its LBE-Mini sat at 8 mA — the floor of the Mini's
8/16/24/32 ladder — and at that level the GPSDO's 27 MHz did NOT take over the
RX888's reference.  The board ran on its own oscillator ~350 ppm fast;
hf-timestd's FUSE derives from those samples and inherited the error; chrony
followed FUSE and walked the host clock TWELVE SECONDS off UTC.  Every RTP label
drifted with it and the station decoded nothing for a day — while the GPSDO
reported pll_locked, gps_fix 3D, 17 satellites and out1_hz 27000000 throughout.
Nothing in any repo set or checked the drive.

On ATTACH rather than once, deliberately: the Mini's SET_DRIVE opcode documents
no flash persistence (unlike `set_frequency`), so we cannot know whether the
value survives a power cycle.  Reasserting makes the question moot, and the log
line is the experiment that answers it.

32 mA is the Mini's own default, so the daemon restores a default rather than
imposing a preference.
"""
from unittest.mock import MagicMock, patch

from gpsdo_monitor.config import Config, DeclaredDevice
from gpsdo_monitor.hid_xport import HidCandidate
from gpsdo_monitor.service import DeviceWorker


def _cand(serial="9DC7A55644"):
    return HidCandidate(path=b"/dev/hidraw1", vid=0x1DD2, pid=0x2211,
                        serial=serial, product="mini GPS Reference Clock",
                        manufacturer="Leo Bodnar Electronics")


class _Model:
    def __init__(self, drive, has_drive=True, raises=None):
        self.capabilities = MagicMock(has_drive_ma=has_drive)
        self._drive = drive
        self.writes = []
        self._raises = raises

    def __enter__(self):
        if self._raises:
            raise self._raises
        return self

    def __exit__(self, *exc):
        return False

    def get_status(self):
        return MagicMock(outputs=MagicMock(drive_ma=self._drive))

    def set_drive_ma(self, ma):
        self.writes.append(ma)
        self._drive = ma


def _worker(model, *, min_drive_ma=32):
    w = DeviceWorker(
        declared=DeclaredDevice(serial="9DC7A55644", governs=()),
        candidate=_cand(),
        cfg=Config(min_drive_ma=min_drive_ma, pps_study_enabled=False),
    )
    with patch("gpsdo_monitor.service.open_model", return_value=model), \
         patch("gpsdo_monitor.service.find_ttys_by_usb_serial", return_value=[]):
        w.start()
    return w


def test_a_floor_drive_is_restored_on_attach():
    m = _Model(drive=8)
    _worker(m)
    assert m.writes == [32]


def test_a_device_already_at_or_above_target_is_left_alone():
    for have in (32, 24):
        m = _Model(drive=have)
        _worker(m, min_drive_ma=24)
        assert m.writes == [], f"should not rewrite at {have} mA"


def test_zero_disables_the_assertion():
    m = _Model(drive=8)
    _worker(m, min_drive_ma=0)
    assert m.writes == []


def test_a_model_without_drive_control_is_untouched():
    # The LBE-1421 has a boolean high/low, not the Mini's four-step ladder.
    m = _Model(drive=None, has_drive=False)
    _worker(m)
    assert m.writes == []


def test_an_unreadable_drive_is_not_guessed_at():
    m = _Model(drive=None)
    _worker(m)
    assert m.writes == []


def test_a_failed_open_does_not_stop_the_worker_starting():
    """Monitoring a device we could not adjust beats not monitoring it."""
    m = _Model(drive=8, raises=OSError("open failed"))
    w = _worker(m)
    assert m.writes == []
    assert w.started_mono > 0


def test_the_correction_is_logged_loudly(caplog):
    """The log line is how we learn whether the setting is volatile: a device
    that reverts announces a correction after every power cycle."""
    import logging
    caplog.set_level(logging.WARNING)
    _worker(_Model(drive=8))
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "8 mA" in joined and "32 mA" in joined
    assert "volatile" in joined
