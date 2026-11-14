"""MVR GPSDO B: the published schema-v1 report, the service tick, and the CLI
refusals.  /run/gpsdo/<serial>.json is what sigmond's `smd config location
--from-gps` and hf-timestd read, so an MVR must land in the same shape as a
Leo Bodnar -- and must never publish a 0,0 position."""
from __future__ import annotations

import fcntl
import json
import os
import select

import pytest

from gpsdo_monitor import cli, mvr
from gpsdo_monitor.config import Config, DeclaredDevice
from gpsdo_monitor.mvr import MvrCandidate, MvrState, build_report, feed_line

N8UR_STATUS = ("[110100] GPS: DGPS fix, 12 sats | Loop: LOCKED | "
               "CLK0 27000000Hz CLK1 27000000Hz CLK2 off")
RMC_VALID = "$GNRMC,024825.40,A,3855.12213,N,09207.65474,W,0.001,,240426,,,D*73"
GGA_FIX = "$GNGGA,024825.40,3855.12213,N,09207.65474,W,2,10,0.48,265.6,M,-30.7,M,,0000*7D"
GSA_3D = "$GNGSA,A,3,10,32,23,28,02,24,25,12,31,01,,,0.91,0.48,0.77*10"

CAND = MvrCandidate(path=b"/dev/ttyACM1", vid=0x2E8A, pid=0x000A,
                    serial="E6614C311B4B2A2B", product="Seeed XIAO RP2040",
                    manufacturer="Seeed")
DECL = DeclaredDevice(serial="E6614C311B4B2A2B", governs=("radiod:main",))
CFG = Config(probe_interval_sec=10, mdns_enabled=False)
WALL0 = 1_800_000_000.0


def _pmvr(text: str) -> str:
    body = f"PMVR,MSG,{text}"
    ck = 0
    for c in body.encode():
        ck ^= c
    return f"${body}*{ck:02X}"


def _state(lines, t=100.0) -> MvrState:
    st = MvrState()
    for ln in lines:
        feed_line(st, ln, now_mono=t, now_wall=WALL0 + t)
    return st


def _report(st, now=110.0):
    return build_report(st, candidate=CAND, declared=DECL, cfg=CFG,
                        host="n8ur-host", now_mono=now, now_wall=WALL0 + now)


def test_report_without_passthrough_has_no_position_and_names_the_command():
    r = _report(_state([N8UR_STATUS]))
    assert r is not None
    doc = json.loads(r.to_json())
    assert doc["schema"] == "v1"
    assert doc["device"]["model"] == "MVR GPSDO B"
    assert doc["device"]["pid"] == "0x000a"
    assert doc["device"]["serial"] == "E6614C311B4B2A2B"
    assert doc["device"]["hid_path"] == "/dev/ttyACM1"
    assert doc["governs"] == ["radiod:main"]
    h = doc["health"]
    assert h["pll_locked"] is True
    assert h["gps_locked"] is True
    assert h["gps_fix_type"] == "DGPS fix"
    assert h["sats_used"] == 12
    # Position unavailable: null, never 0,0 and never a made-up grid.
    assert h["latitude"] is None and h["longitude"] is None and h["grid"] is None
    o = doc["outputs"]
    assert (o["clk0_hz"], o["clk1_hz"], o["clk2_hz"]) == (27_000_000, 27_000_000, 0)
    assert doc["a_level_hint"] == "A1"
    assert any("gpsdo-monitor mvr passthrough on --persist" in a
               for a in doc["advisories"])
    assert doc["mvr"]["passthrough"] is False
    assert doc["mvr"]["loop_state"] == "LOCKED"
    assert doc["mvr"]["uptime_sec"] == 110100
    assert doc["mvr"]["last_status_age_sec"] == pytest.approx(10.0)


def test_report_with_passthrough_carries_position_from_nmea():
    st = _state([RMC_VALID, GGA_FIX, GSA_3D, _pmvr(N8UR_STATUS)])
    r = _report(st, now=101.0)
    doc = json.loads(r.to_json())
    h = doc["health"]
    assert h["gps_fix"] == "3D"
    assert h["sats_used"] == 10
    assert h["latitude"] == pytest.approx(38.918702, abs=1e-6)
    assert h["longitude"] == pytest.approx(-92.127579, abs=1e-6)
    assert h["grid"] == "EM38ww"
    assert h["altitude_m"] == pytest.approx(265.6)
    assert h["fix_age_sec"] is not None
    assert not any("passthrough" in a for a in doc["advisories"])
    assert doc["mvr"]["passthrough"] is True
    assert doc["a_level_hint"] == "A1"


def test_stale_nmea_does_not_publish_an_old_position():
    st = _state([RMC_VALID, GGA_FIX, GSA_3D], t=0.0)
    feed_line(st, N8UR_STATUS, now_mono=100.0, now_wall=WALL0 + 100)   # passthrough now off
    doc = json.loads(_report(st, now=101.0).to_json())
    assert doc["health"]["latitude"] is None
    assert doc["health"]["grid"] is None
    assert doc["health"]["gps_fix"] is None
    assert any("passthrough" in a for a in doc["advisories"])


def test_no_status_yet_publishes_nothing():
    assert _report(MvrState()) is None
    assert _report(_state(["=== MVR GPSDO B Firmware v20261008.1 (built x) ==="])) is None


def test_stale_status_publishes_nothing():
    """Status comes every 30 s.  No status for 75 s and the file must age,
    so consumers' staleness gates see the fault (as on the Mini)."""
    st = _state([N8UR_STATUS], t=0.0)
    assert _report(st, now=70.0) is not None
    assert _report(st, now=80.0) is None


def test_loop_unlocked_is_a0_with_an_advisory():
    st = _state(["[9] GPS: GPS fix, 9 sats | Loop: unlocked | CLK0 27000000Hz CLK1 off CLK2 off"])
    doc = json.loads(_report(st).to_json())
    assert doc["a_level_hint"] == "A0"
    assert doc["a_level_reason"] == "pll_unlocked"
    assert any("loop" in a.lower() and "unlocked" in a for a in doc["advisories"])


def test_gps_no_data_is_an_advisory():
    st = _state(["[9] GPS: no data | Loop: LOCKED | CLK0 27000000Hz CLK1 off CLK2 off"])
    doc = json.loads(_report(st).to_json())
    assert doc["health"]["gps_locked"] is False
    assert doc["health"]["gps_fix"] == "no_fix"
    assert doc["a_level_hint"] == "A0"
    assert any("no fix" in a for a in doc["advisories"])


def test_no_27mhz_output_is_an_advisory_naming_clk0_and_clk1():
    st = _state(["[9] GPS: GPS fix, 9 sats | Loop: LOCKED | CLK0 10000000Hz CLK1 off CLK2 off"])
    adv = json.loads(_report(st).to_json())["advisories"]
    hit = [a for a in adv if "27 MHz" in a]
    assert hit and "CLK0 10000000 Hz" in hit[0] and "CLK1 off" in hit[0]


def test_27mhz_on_clk1_is_not_flagged():
    st = _state(["[9] GPS: GPS fix, 9 sats | Loop: LOCKED | CLK0 10000000Hz CLK1 27000000Hz CLK2 off"])
    adv = json.loads(_report(st).to_json())["advisories"]
    assert not [a for a in adv if "27 MHz" in a]


def test_drive_and_firmware_from_the_menu_are_reported_as_read():
    lines = [
        "=== MVR GPSDO B Firmware v20261008.1 (built Oct  8 2026 09:50:46) ===",
        "  4) Set drive strength, all channels (currently 8 mA)",
        N8UR_STATUS,
    ]
    doc = json.loads(_report(_state(lines)).to_json())
    assert doc["outputs"]["drive_ma"] == 8
    assert doc["device"]["firmware"] == "v20261008.1"
    assert doc["device"]["firmware_source"] == "mvr-console"
    # 8 mA is this device's maximum: no "raise the drive" advice.
    assert not [a for a in doc["advisories"] if "drive" in a.lower()]


def test_halted_is_an_advisory():
    st = _state([N8UR_STATUS, "*** HALTED ***"])
    adv = json.loads(_report(st).to_json())["advisories"]
    assert any("HALTED" in a for a in adv)


def test_bodnar_reports_gain_only_additive_empty_fields():
    from gpsdo_monitor.schema import Device, Health, Outputs, PpsStudy, new_report
    r = new_report(host="h", probe_interval_sec=10,
                   device=Device(model="lbe-1421", pid="0x2444", serial="S", hid_path="p"),
                   governs=[], health=Health(pll_locked=True, outputs_enabled=True),
                   outputs=Outputs(), pps_study=PpsStudy(), a_level_hint="A1",
                   a_level_reason="x")
    doc = json.loads(r.to_json())
    assert doc["advisories"] == []
    assert doc["mvr"] is None
    assert doc["health"]["gps_fix_type"] is None
    assert doc["outputs"]["clk0_hz"] is None


# --- the service tick ------------------------------------------------------------


class _FakeReader:
    def __init__(self, st):
        self._st = st

    def snapshot(self):
        return self._st


class _FakeProber:
    def __init__(self, reader):
        self.reader = reader
        self.stopped = False

    def scan(self):
        return [CAND]

    def reader_for(self, cand):
        return self.reader

    def stop(self):
        self.stopped = True


def test_service_tick_publishes_an_mvr_and_never_opens_it_as_a_bodnar(tmp_path, monkeypatch):
    from gpsdo_monitor import service as svc_mod

    st = MvrState()
    now = svc_mod.time.monotonic()
    feed_line(st, N8UR_STATUS, now_mono=now, now_wall=WALL0)

    def _no_hid(*_a, **_k):
        raise AssertionError("an MVR must never reach the HID model layer")

    monkeypatch.setattr(svc_mod, "open_model", _no_hid)
    monkeypatch.setattr("gpsdo_monitor.discovery.enumerate_lbe", list)
    monkeypatch.setattr(svc_mod, "_unsupported_entries", list)
    cfg = Config(probe_interval_sec=10, run_dir=tmp_path / "run", mdns_enabled=False,
                 devices=[DECL])
    s = svc_mod.Service(cfg)
    s._mvr = _FakeProber(_FakeReader(st))
    s.cfg.run_dir.mkdir(parents=True)
    s._tick()

    doc = json.loads((tmp_path / "run" / "E6614C311B4B2A2B.json").read_text())
    assert doc["device"]["model"] == "MVR GPSDO B"
    assert doc["a_level_hint"] == "A1"
    idx = json.loads((tmp_path / "run" / "index.json").read_text())
    assert idx["devices"][0]["model"] == "MVR GPSDO B"

    # The fast loop republishes it too, from the reader's snapshot.
    w = s._workers["E6614C311B4B2A2B"]
    assert w.refresh_fast(now=svc_mod.time.time()) is not None
    s.stop()
    assert s._mvr.stopped


# --- CLI refusals -------------------------------------------------------------------


def _drain(fd, wait=0.3) -> bytes:
    r, _, _ = select.select([fd], [], [], wait)
    return os.read(fd, 4096) if r else b""


def test_mvr_probe_refuses_while_the_service_is_active(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mvr_service_active", lambda: True)
    opened = []
    monkeypatch.setattr(mvr, "open_port", lambda *a, **k: opened.append(a))
    rc = cli.main(["mvr", "probe", "--tty", "/dev/ttyACM9"])
    assert rc != 0
    assert opened == []
    assert "gpsdo-monitor service" in capsys.readouterr().err


def test_mvr_passthrough_refuses_a_port_held_by_another_process(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_mvr_service_active", lambda: False)
    master, slave = os.openpty()
    path = os.ttyname(slave)
    holder = os.open(path, os.O_RDONLY | os.O_NOCTTY)
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = cli.main(["mvr", "passthrough", "on", "--tty", path])
        assert rc != 0
        assert "held" in capsys.readouterr().err
        assert _drain(master) == b""
    finally:
        for fd in (holder, master, slave):
            os.close(fd)


def test_mvr_probe_refuses_a_tty_that_never_speaks_mvr(monkeypatch, capsys):
    """Before the wake CR, the command listens passively for MVR content.
    A port that shows none gets no bytes at all."""
    monkeypatch.setattr(cli, "_mvr_service_active", lambda: False)
    master, slave = os.openpty()
    path = os.ttyname(slave)
    try:
        rc = cli.main(["mvr", "probe", "--tty", path, "--listen-sec", "0.3"])
        assert rc != 0
        assert "no MVR" in capsys.readouterr().err
        assert _drain(master) == b""
    finally:
        os.close(master)
        os.close(slave)


def test_mvr_parser_shape():
    p = cli.build_parser()
    a = p.parse_args(["mvr", "passthrough", "off", "--persist"])
    assert a.state == "off" and a.persist is True
    a = p.parse_args(["mvr", "probe"])
    assert a.tty is None


def _serve_fake(master, dev, stop):
    """Device side of a pty: what the host writes goes to `dev`; what `dev`
    prints goes back.  Records every byte the host sent."""
    while not stop.is_set():
        r, _, _ = select.select([master], [], [], 0.05)
        if r:
            try:
                data = os.read(master, 4096)
            except OSError:
                return
            dev.write(data)
        out = dev.read(0)
        if out:
            os.write(master, out)


def test_mvr_probe_end_to_end_over_a_pty(monkeypatch, capsys):
    import termios
    import threading

    from tests.test_mvr_console import FakeMvr

    monkeypatch.setattr(cli, "_mvr_service_active", lambda: False)
    master, slave = os.openpty()
    path = os.ttyname(slave)
    dev = FakeMvr(clk1="27000000 Hz")
    stop = threading.Event()
    try:
        # The device's periodic status line, sent once the command has gone raw.
        def speak():
            end = __import__("time").monotonic() + 3
            while termios.tcgetattr(slave)[3] & termios.ECHO:
                if __import__("time").monotonic() > end:
                    return
            os.write(master, (N8UR_STATUS + "\r\n").encode())
        threading.Thread(target=speak).start()
        th = threading.Thread(target=_serve_fake, args=(master, dev, stop))
        th.start()
        rc = cli.main(["mvr", "probe", "--tty", path, "--listen-sec", "3", "--timeout", "2"])
        stop.set()
        th.join(2)
        assert rc == 0, capsys.readouterr().err
        out = json.loads(capsys.readouterr().out)
        assert out["firmware"] == "v20261008.1"
        assert (out["clk0_hz"], out["clk1_hz"], out["clk2_hz"]) == (27_000_000, 27_000_000, 0)
        assert out["drive_ma"] == 8
        assert out["passthrough"] is False
        # Exactly the documented bytes, in order, and nothing else.
        assert b"".join(dev.written) == b"\r99\r"
        # A confirmed MVR is left raw, so the next naive open cannot echo.
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
    finally:
        stop.set()
        os.close(master)
        os.close(slave)
