"""A Leo Bodnar with no driver here (KJ6MKI's 1dd2:2210, 2026-11-12) is named
as recognised-but-unsupported, never as absent; and index.json's host is the
configured hostname, not a reverse lookup ("ip6-localhost")."""
import argparse
import json
from unittest.mock import patch

from gpsdo_monitor import cli, service
from gpsdo_monitor.hid_xport import HidCandidate
from gpsdo_monitor.schema import IndexFile

OLD = HidCandidate(path=b"/dev/hidraw0", vid=0x1DD2, pid=0x2210, serial="G40328",
                   product="GPS Reference Clock", manufacturer="Leo Bodnar")


def test_detect_names_an_unsupported_model(capsys):
    with patch("gpsdo_monitor.cli.enumerate_lbe", return_value=[]), \
         patch("gpsdo_monitor.cli.enumerate_unsupported_lbe", return_value=[OLD]):
        rc = cli._cmd_detect(argparse.Namespace())
    err = capsys.readouterr().err
    assert rc == 1
    assert "1dd2:2210 (GPS Reference Clock)" in err
    assert "recognised but not supported by gpsdo-monitor" in err
    assert "no supported Leo Bodnar devices found" in err


def test_inventory_lists_it_but_does_not_call_it_present(capsys):
    with patch("gpsdo_monitor.cli.enumerate_lbe", return_value=[]), \
         patch("gpsdo_monitor.cli.enumerate_unsupported_lbe", return_value=[OLD]):
        cli._cmd_inventory(argparse.Namespace())
    d = json.loads(capsys.readouterr().out)
    assert d["hardware_present"] is False
    assert d["unsupported_present"] == ["1dd2:2210:G40328"]


def test_index_carries_unsupported_entries():
    with patch("gpsdo_monitor.hid_xport.enumerate_unsupported_lbe", return_value=[OLD]):
        got = service._unsupported_entries()
    assert got == [{"vid": "1dd2", "pid": "2210", "serial": "G40328",
                    "product": "GPS Reference Clock"}]
    idx = json.loads(IndexFile(schema="v1", written_utc="t", host="h",
                               unsupported=got).to_json())
    assert idx["devices"] == [] and idx["unsupported"][0]["pid"] == "2210"


def test_hostname_is_not_a_reverse_lookup():
    with patch("socket.gethostname", return_value="KJ6MKI-Ryzen"), \
         patch("socket.getfqdn", return_value="ip6-localhost"):
        assert service._hostname() == "KJ6MKI-Ryzen"
