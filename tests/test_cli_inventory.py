"""CLI `inventory --json` self-describe tests (HID enumeration mocked).

Verifies the CONTRACT §3 / Phase D hardware_present field sigmond consults
to mark gpsdo-monitor core-but-dormant when no GPSDO is attached.
"""
import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

from gpsdo_monitor.cli import build_parser, _cmd_inventory
from gpsdo_monitor.hid_xport import HidCandidate


def _cand() -> HidCandidate:
    return HidCandidate(path=b"/dev/hidraw2", vid=0x1DD2, pid=0x2444,
                        serial="ABC", product="LBE-1421",
                        manufacturer="Leo Bodnar Electronics")


def _run_inventory() -> dict:
    args = build_parser().parse_args(["inventory", "--json"])
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = args.func(args)
    assert rc == 0
    return json.loads(buf.getvalue())


def test_inventory_parses_and_is_clean_json():
    with patch("gpsdo_monitor.cli.enumerate_lbe", return_value=[]):
        out = _run_inventory()
    assert out["client"] == "gpsdo-monitor"
    assert "hardware_present" in out


def test_hardware_present_true_when_device_enumerated():
    with patch("gpsdo_monitor.cli.enumerate_lbe", return_value=[_cand()]):
        out = _run_inventory()
    assert out["hardware_present"] is True


def test_hardware_present_false_when_none():
    with patch("gpsdo_monitor.cli.enumerate_lbe", return_value=[]):
        out = _run_inventory()
    assert out["hardware_present"] is False


def test_hardware_present_null_on_enumeration_error():
    with patch("gpsdo_monitor.cli.enumerate_lbe", side_effect=OSError("hid boom")):
        out = _run_inventory()
    assert out["hardware_present"] is None
