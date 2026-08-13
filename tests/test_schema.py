"""Unit tests for schema serialization and atomic write."""
import json
import os

from gpsdo_monitor.schema import (
    Device, Health, IndexEntry, IndexFile, Outputs, PpsStudy,
    atomic_write, new_report, utc_now_iso,
)


def test_report_round_trip(tmp_path):
    device = Device(
        model="lbe-1421", pid="0x2444", serial="LBE1421-ABC123",
        hid_path="/dev/hidraw2", firmware=None, firmware_source="unavailable",
    )
    health = Health(pll_locked=True, outputs_enabled=True, fll_mode=False,
                    gps_fix="3D", sats_used=9, fix_age_sec=0.4,
                    antenna_ok=True, signal_loss_count=None)
    outputs = Outputs(out1_hz=122_880_000, out1_power="normal",
                      out2_hz=10_000_000, out2_power="normal",
                      pps_enabled=True, drive_ma=None)
    pps = PpsStudy(enabled=True, window_sec=60, edges=60,
                   period_ms_p50=1000.0, period_ms_p95=1000.2,
                   last_edge_utc=utc_now_iso())

    r = new_report(
        host="bee1.local", probe_interval_sec=10, device=device,
        governs=["radiod:main"], health=health, outputs=outputs,
        pps_study=pps, a_level_hint="A1",
        a_level_reason="pll_locked && gps_fix=3D && antenna_ok && pps_present && fresh",
    )

    path = tmp_path / "LBE1421-ABC123.json"
    atomic_write(str(path), r.to_json())
    parsed = json.loads(path.read_text())

    assert parsed["schema"] == "v1"
    assert parsed["device"]["serial"] == "LBE1421-ABC123"
    assert parsed["governs"] == ["radiod:main"]
    assert parsed["a_level_hint"] == "A1"


def test_atomic_write_does_not_leave_partial_file(tmp_path):
    path = tmp_path / "out.json"
    atomic_write(str(path), '{"x": 1}')
    assert path.read_text() == '{"x": 1}'
    # no tmp files lingering
    entries = [p for p in os.listdir(tmp_path) if p.startswith(".gpsdo.")]
    assert entries == []


def test_index_round_trip(tmp_path):
    idx = IndexFile(schema="v1", written_utc=utc_now_iso(), host="bee1.local",
                    devices=[
                        IndexEntry(serial="A", model="lbe-1421", governs=["radiod:main"],
                                   a_level_hint="A1", written_utc=utc_now_iso()),
                    ])
    path = tmp_path / "index.json"
    atomic_write(str(path), idx.to_json())
    parsed = json.loads(path.read_text())
    assert parsed["devices"][0]["serial"] == "A"


def _make_minimal_report():
    device = Device(
        model="lbe-mini", pid="0x2443", serial="LBEMINI-XYZ",
        hid_path="/dev/hidraw3",
    )
    health = Health(pll_locked=True, outputs_enabled=True)
    outputs = Outputs()
    pps = PpsStudy()
    return new_report(
        host="bee1.local", probe_interval_sec=10, device=device,
        governs=["radiod:main"], health=health, outputs=outputs,
        pps_study=pps, a_level_hint="A0", a_level_reason="minimal test report",
    )


def test_nav_clock_report_serializes_with_note():
    from gpsdo_monitor.schema import NAV_CLOCK_NOTE, NavClockReport

    nc = NavClockReport(
        clk_bias_ns=-1234, clk_drift_ns_s=87,
        t_acc_ns=25, f_acc_ps_s=310,
        sampled_utc="2026-08-13T00:00:00.000Z",
    )
    assert nc.note == NAV_CLOCK_NOTE
    assert "self-report" in nc.note


def test_device_report_nav_clock_defaults_to_none_and_round_trips():
    import json

    from gpsdo_monitor.schema import NavClockReport

    report = _make_minimal_report()          # see note below
    assert report.nav_clock is None
    report.nav_clock = NavClockReport(
        clk_bias_ns=1, clk_drift_ns_s=2, t_acc_ns=3, f_acc_ps_s=4,
        sampled_utc="2026-08-13T00:00:00.000Z",
    )
    parsed = json.loads(report.to_json())
    assert parsed["nav_clock"]["clk_bias_ns"] == 1
    assert "note" in parsed["nav_clock"]


def test_receiver_config_round_trips_in_report():
    import json

    from gpsdo_monitor.schema import ReceiverConfig

    report = _make_minimal_report()
    assert report.receiver_config is None
    report.receiver_config = ReceiverConfig(
        gnss_mask=0x47, dyn_model=2, nmea_enabled=False,
    )
    parsed = json.loads(report.to_json())
    assert parsed["receiver_config"] == {
        "gnss_mask": 0x47, "dyn_model": 2, "nmea_enabled": False,
    }
