"""The mini GPS is HID-only, and both consequences bit AC0G-ND.

Fargo came up 2026-09-02 with a Bodnar mini GPS Reference Clock (1dd2:2211)
and gpsdo-monitor produced NOTHING — /run/gpsdo stayed empty while the
journal logged, every 10 s from install onward::

    service.py:167  raw.health.altitude_m = ns.altitude_m
    UnboundLocalError: cannot access local variable 'ns' where it is not
                       associated with a value

That line sits one indent level OUTSIDE the `if self.nmea is not None:`
block that binds `ns`.  The mini has no CDC serial port at all — "GPS fix
and PLL-lock state come from the HID interrupt-IN endpoint as a status byte
plus a reassembled UBX stream" (lbe_mini's own docstring) — so `self.nmea`
is None on this hardware and the crash is unconditional, not fix-dependent.
B4 runs an LBE-1421, which DOES present a tty, which is why three months of
production never saw it.  It also explains the visible symptom: each failed
probe reopened the device, so hid-generic re-bound every 10 s with the
instance counter climbing.

Underneath it, a second gap.  `lbe_mini.get_status` parsed NAV-PVT and kept
only `fix_type`, discarding `lat_1e7`, `lon_1e7`, `hmsl_mm` and `num_sv` —
while the Health fields that carry position were filled only from NMEA.  So
a mini-equipped station could never auto-derive its grid even with a fix,
though Health's own comment promises exactly that ("lets bring-up
auto-derive station location from the GPSDO instead of hand-entering it")
and the station-adoption design counts on it for Fargo.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gpsdo_monitor.config import Config, DeclaredDevice
from gpsdo_monitor.hid_xport import HidCandidate
from gpsdo_monitor.models.base import RawStatus
from gpsdo_monitor.models.lbe_mini import LbeMini
from gpsdo_monitor.schema import Health, Outputs
from gpsdo_monitor.service import DeviceWorker
from gpsdo_monitor.ubx import CLS_NAV, ID_NAV_PVT, build_message

from tests.test_mini import _FakeMiniHid, _make_feature_buf, _make_mini_hid_frame


# --- 1 · the crash -------------------------------------------------------


def _worker_without_nmea(tmp_path) -> DeviceWorker:
    cand = HidCandidate(
        path=b"/dev/hidraw-fake", vid=0x1DD2, pid=0x2211,
        serial="9DC7A55644", product="mini GPS Reference Clock",
        manufacturer="Leo Bodnar Electronics",
    )
    return DeviceWorker(
        candidate=cand,
        declared=DeclaredDevice(serial="9DC7A55644", governs=()),
        cfg=Config(run_dir=tmp_path / "run", mdns_enabled=False,
                   devices=[]),
        nmea=None,          # ⛔ the mini presents no tty
        pps=None,
    )


def _stub_model(monkeypatch, raw: RawStatus):
    from gpsdo_monitor import service as svc
    from contextlib import contextmanager

    class _Model:
        capabilities = type("C", (), {"has_ubx_mon_ver": False})()

        def get_status(self):
            return raw

    @contextmanager
    def _open(_cand):
        yield _Model()

    monkeypatch.setattr(svc, "open_model", _open)


def _bare_raw(**health) -> RawStatus:
    return RawStatus(
        health=Health(pll_locked=True, outputs_enabled=True, **health),
        outputs=Outputs(out1_hz=10_000_000, out1_power="normal",
                        pps_enabled=False),
        firmware=None, firmware_source="unavailable", raw_trailing_hex="",
        extras={},
    )


def test_build_report_survives_a_device_with_no_tty(monkeypatch, tmp_path):
    # THE live crash.  A device without NMEA must still produce a report:
    # a monitor that raises publishes nothing at all, so every consumer —
    # the location authority, mag-recorder's lat/lon, bring-up's grid
    # auto-derivation — sees a station with no GPSDO rather than one
    # without a fix.
    _stub_model(monkeypatch, _bare_raw())
    w = _worker_without_nmea(tmp_path)
    report = w.build_report(host="AC0G-ND", now=1000.0)
    assert report is not None
    assert report.health.altitude_m is None, \
        'no NMEA and no UBX position means unknown, not a crash'


def test_ubx_position_reaches_health_without_any_tty(monkeypatch, tmp_path):
    # Fargo: EN16ov is roughly 46.87 N, 96.79 W.
    _stub_model(monkeypatch, _bare_raw(
        gps_fix="3D", sats_used=9,
        latitude=46.875, longitude=-96.7917, altitude_m=274.0,
    ))
    w = _worker_without_nmea(tmp_path)
    report = w.build_report(host="AC0G-ND", now=1000.0)
    assert report.health.altitude_m == pytest.approx(274.0)
    assert report.health.latitude == pytest.approx(46.875)
    # The grid is what bring-up actually consumes, so it must be derived
    # from whatever position arrived — NMEA or UBX.
    assert report.health.grid is not None
    assert report.health.grid.upper().startswith("EN16")


# --- 2 · NAV-PVT position must not be thrown away ------------------------


def _nav_pvt_frames(*, fix_type: int, num_sv: int,
                    lat_1e7: int, lon_1e7: int, hmsl_mm: int) -> list[bytes]:
    payload = bytearray(92)
    payload[20] = fix_type
    payload[23] = num_sv
    payload[24:28] = lon_1e7.to_bytes(4, "little", signed=True)
    payload[28:32] = lat_1e7.to_bytes(4, "little", signed=True)
    payload[36:40] = hmsl_mm.to_bytes(4, "little", signed=True)
    msg = build_message(CLS_NAV, ID_NAV_PVT, bytes(payload))
    frames = []
    for i in range(0, len(msg), 62):
        chunk = msg[i:i + 62]
        chunk = chunk + b"\x00" * (62 - len(chunk))
        frames.append(_make_mini_hid_frame(
            signal_loss=0, pll_locked=True, gps_signal=True,
            carries_ubx=True, payload=chunk,
        ))
    return frames


def _mini_with(frames) -> LbeMini:
    feature = _make_feature_buf(enabled=True, drive_idx=2, fin=97600,
                                n3=1, n2hs=8, n2ls=25, n1hs=8, nc1=32)
    mini = LbeMini(_FakeMiniHid(feature_get_replies=[feature],
                                interrupt_frames=frames))
    mini.nav_sample_sec = 0.1
    return mini


def test_get_status_keeps_the_position_nav_pvt_carried():
    raw = _mini_with(_nav_pvt_frames(
        fix_type=3, num_sv=11,
        lat_1e7=468750000, lon_1e7=-967917000, hmsl_mm=274000,
    )).get_status()
    assert raw.health.gps_fix == "3D"
    assert raw.health.sats_used == 11
    assert raw.health.latitude == pytest.approx(46.875, abs=1e-6)
    assert raw.health.longitude == pytest.approx(-96.7917, abs=1e-6)
    assert raw.health.altitude_m == pytest.approx(274.0, abs=0.01)


def test_a_fixless_nav_pvt_reports_no_position_rather_than_zeros():
    # ⛔ Guards the guard.  A receiver with no antenna sends NAV-PVT with
    # fix_type 0 and lat/lon ZERO.  Publishing 0,0 would put the station in
    # the Gulf of Guinea and — worse — the location authority would re-grid
    # a real station to it.  Absent position must read as unknown.
    raw = _mini_with(_nav_pvt_frames(
        fix_type=0, num_sv=0, lat_1e7=0, lon_1e7=0, hmsl_mm=0,
    )).get_status()
    assert raw.health.gps_fix == "no_fix"
    assert raw.health.latitude is None
    assert raw.health.longitude is None
    assert raw.health.altitude_m is None


# --- 3 · the fix must also read as FRESH ---------------------------------


def test_a_ubx_fix_reports_its_age_so_consumers_can_trust_it():
    """⛔ sigmond's location authority rejects a fix with no age.

    `sigmond-location-check` is the station's location authority — "a live
    GPSDO position is DEFINITIVE" — and it gates on freshness::

        age = h.get('fix_age_sec')
        if age is None or age > 120:
            continue          # -> NOGNSS

    `fix_age_sec` was filled ONLY from NMEA (`ns.fix_age_sec(now=now)`), so on
    a Mini it stayed None forever and the authority discarded every fix the
    device ever produced.  On AC0G-ND that left hf-timestd computing WWV path
    lengths from the CENTRE of grid EN16ov — 46.89583333, -96.79166667 —
    while a 21-satellite fix read 46.9071213, -96.7926052.  1.26 km, which is
    4.2 us of path error against a T6 floor of 0.11 us.

    A NAV-PVT solution decoded during THIS probe is ~0 s old by construction,
    the same reasoning `build_report` already applies to `probe_age_sec`.  So
    the age is knowable and must be stated; None means "unknown", and a
    consumer is right to distrust it.
    """
    raw = _mini_with(_nav_pvt_frames(
        fix_type=3, num_sv=21,
        lat_1e7=469071213, lon_1e7=-967926052, hmsl_mm=282428,
    )).get_status()
    assert raw.health.fix_age_sec is not None, \
        'a fix with no stated age is discarded by the location authority'
    assert raw.health.fix_age_sec == pytest.approx(0.0, abs=2.0)


def test_a_fixless_receiver_states_no_age_either():
    # Guards the guard: no fix must not masquerade as a FRESH fix, or the
    # authority would re-grid a station to whatever lat/lon accompanied it.
    raw = _mini_with(_nav_pvt_frames(
        fix_type=0, num_sv=0, lat_1e7=0, lon_1e7=0, hmsl_mm=0,
    )).get_status()
    assert raw.health.fix_age_sec is None
    assert raw.health.latitude is None
