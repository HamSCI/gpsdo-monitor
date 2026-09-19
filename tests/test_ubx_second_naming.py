"""Naming a second on a device that cannot place one.

An LBE-Mini emits no PPS — its synthesiser floor sits far above 1 Hz and
`pps_enabled` reads false.  It does know WHICH second it is, from
UBX-NAV-PVT, and the two questions differ by four orders of magnitude:
naming needs ±0.5 s, placing a boundary needs microseconds and a pulse.
"""
from __future__ import annotations

import calendar
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gpsdo_monitor.ubx import parse_nav_pvt, nav_pvt_utc

VALID = 0x07          # validDate | validTime | fullyResolved


def _pvt(year=2026, month=9, day=19, hour=16, minute=30, second=45,
         nano=0, valid=VALID, fix=3, t_acc=25_000, sv=12) -> bytes:
    p = bytearray(92)
    p[4:6] = int(year).to_bytes(2, "little")
    p[6], p[7], p[8], p[9], p[10] = month, day, hour, minute, second
    p[11] = valid
    p[12:16] = int(t_acc).to_bytes(4, "little")
    p[16:20] = int(nano).to_bytes(4, "little", signed=True)
    p[20] = fix
    p[23] = sv
    return bytes(p)


class TestDecode(unittest.TestCase):

    def test_nano_and_tacc_are_extracted(self):
        """Both sat in a buffer that was already parsed; neither was read."""
        pvt = parse_nav_pvt(_pvt(nano=-250_000_000, t_acc=25_000))
        self.assertEqual(pvt.nano_ns, -250_000_000)
        self.assertEqual(pvt.t_acc_ns, 25_000)

    def test_nano_is_signed(self):
        """u-blox reports the ROUNDED second plus a signed correction, so a
        solution just before the boundary carries a negative nano.  Reading
        it unsigned would place it ~4.3 s into the future."""
        self.assertLess(parse_nav_pvt(_pvt(nano=-1)).nano_ns, 0)

    def test_utc_applies_the_nano_correction(self):
        exp = calendar.timegm((2026, 9, 19, 16, 30, 45, 0, 0, 0)) - 0.25
        self.assertAlmostEqual(
            nav_pvt_utc(parse_nav_pvt(_pvt(nano=-250_000_000))), exp, places=9)

    def test_a_short_buffer_returns_none_rather_than_raising(self):
        self.assertIsNone(parse_nav_pvt(b"\x00" * 40))


class TestRefusals(unittest.TestCase):
    """A receiver emits a plausible-looking date before UTC resolves.
    Naming a second off one of those names the WRONG second."""

    def test_not_fully_resolved_is_refused(self):
        # validDate|validTime set, fullyResolved CLEAR
        self.assertIsNone(nav_pvt_utc(parse_nav_pvt(_pvt(valid=0x03))))

    def test_no_fix_is_refused(self):
        self.assertIsNone(nav_pvt_utc(parse_nav_pvt(_pvt(fix=0))))

    def test_a_2d_fix_is_enough_to_NAME_a_second(self):
        """Naming needs ±0.5 s.  A 2D fix resolves UTC perfectly well; it
        is position that suffers, and position is not what is asked."""
        self.assertIsNotNone(nav_pvt_utc(parse_nav_pvt(_pvt(fix=2))))

    def test_a_nonsense_date_returns_none_rather_than_raising(self):
        self.assertIsNone(nav_pvt_utc(parse_nav_pvt(_pvt(month=0, day=0))))


class TestTheRealMiniPathPublishesTheSecond(unittest.TestCase):
    """Drives LbeMini.get_status() itself, through the fake HID harness the
    position tests already use.

    ⚠ The class below reimplements the pairing arithmetic and so CANNOT
    notice it being wired into the wrong place.  This one can: it asserts
    on the Health object production actually builds.
    """

    def _status(self, **pvt_kw):
        from tests.test_mini import (_FakeMiniHid, _make_feature_buf,
                                     _make_mini_hid_frame)
        from gpsdo_monitor.models.lbe_mini import LbeMini
        from gpsdo_monitor.ubx import CLS_NAV, ID_NAV_PVT, build_message
        msg = build_message(CLS_NAV, ID_NAV_PVT, _pvt(**pvt_kw))
        frames = []
        for i in range(0, len(msg), 62):
            chunk = msg[i:i + 62]
            chunk = chunk + b"\x00" * (62 - len(chunk))
            frames.append(_make_mini_hid_frame(
                signal_loss=0, pll_locked=True, gps_signal=True,
                carries_ubx=True, payload=chunk))
        feature = _make_feature_buf(enabled=True, drive_idx=2, fin=97600,
                                    n3=1, n2hs=8, n2ls=25, n1hs=8, nc1=32)
        return LbeMini(_FakeMiniHid(feature_get_replies=[feature],
                                    interrupt_frames=frames)).get_status()

    def test_a_resolved_solution_names_its_second(self):
        h = self._status(nano=-250_000_000, t_acc=25_000).health
        self.assertIsNotNone(h.pps_utc_sec)
        self.assertEqual(h.pps_utc_sec % 60, 44)   # ...44.75 floors to 44
        self.assertEqual(h.naming_source, "ubx-nav-pvt")
        self.assertEqual(h.naming_sigma_ns, 25_000)
        self.assertIsNotNone(h.nmea_host_monotonic_at_read)

    def test_an_unresolved_solution_names_nothing(self):
        h = self._status(valid=0x03).health
        self.assertIsNone(h.pps_utc_sec)
        self.assertIsNone(h.naming_source)

    def test_no_fix_names_nothing(self):
        h = self._status(fix=0).health
        self.assertIsNone(h.pps_utc_sec)

    def test_the_monotonic_marks_the_boundary_not_the_decode(self):
        """The discriminating case, and the reason it is built this way.

        nano = +0.9 s means the solution's true instant is 0.9 s PAST the
        integer second it floors to, so that second began 0.9 s before the
        decode.  Pairing the second with the decode instant instead -- the
        naive form -- would publish a monotonic ~0.9 s too late, and a
        consumer ageing the reading forward would round to the wrong
        second.  A fake HID decodes within milliseconds of `before`, so
        the correct pairing lands BEFORE it and the naive one after.
        """
        import time
        before = time.monotonic()
        h = self._status(nano=900_000_000).health
        self.assertIsNotNone(h.nmea_host_monotonic_at_read)
        self.assertLess(
            h.nmea_host_monotonic_at_read, before,
            "the published monotonic must mark when the second BEGAN; "
            "landing after `before` means it marks the decode instead")

    def test_naming_a_second_does_not_claim_a_pps(self):
        """The Mini emits no pulse.  Filling the naming second must not
        make the device look PPS-capable to anything downstream."""
        st = self._status(nano=-250_000_000)
        self.assertIsNotNone(st.health.pps_utc_sec)
        self.assertFalse(bool(st.outputs.pps_enabled))


class TestBoundaryConsistentPairing(unittest.TestCase):
    """The published pair is (integer second, monotonic at which THAT
    SECOND BEGAN).  Pairing the second with the decode instant instead
    leaves up to a full second of unknown fraction in it, and a consumer
    ageing the reading forward rounds to the wrong second."""

    def _pair(self, nano, mono_at_decode):
        pvt = parse_nav_pvt(_pvt(nano=nano))
        utc = nav_pvt_utc(pvt)
        sec = int(math.floor(utc))
        return sec, mono_at_decode - (utc - sec)

    def test_the_monotonic_lands_on_the_second_boundary(self):
        # Decoded at monotonic 1000.0, 0.25 s BEFORE the stamped second:
        # nano = -0.25 means the instant is ...44.75, so second 44 began
        # 0.75 s before the decode.
        sec, mono = self._pair(-250_000_000, 1000.0)
        self.assertEqual(sec % 60, 44)
        self.assertAlmostEqual(mono, 1000.0 - 0.75, places=9)

    def test_ageing_the_pair_forward_recovers_the_right_second(self):
        """The property that matters: at any later monotonic, second +
        elapsed must round to the second actually in progress."""
        sec, mono = self._pair(-250_000_000, 1000.0)
        for elapsed in (0.0, 0.4, 0.9, 1.1, 7.3, 9.9):
            with self.subTest(elapsed=elapsed):
                now_mono = 1000.0 + elapsed
                aged = sec + (now_mono - mono)
                true_utc = nav_pvt_utc(
                    parse_nav_pvt(_pvt(nano=-250_000_000))) + elapsed
                self.assertLess(abs(aged - true_utc), 1e-6)

    def test_pairing_with_the_decode_instant_would_be_wrong(self):
        """Mutation guard: the naive pairing is off by the fraction, which
        is what makes a consumer round to the wrong second."""
        sec, good = self._pair(-250_000_000, 1000.0)
        naive = 1000.0
        self.assertAlmostEqual(abs(naive - good), 0.75, places=9)


if __name__ == "__main__":
    unittest.main()
