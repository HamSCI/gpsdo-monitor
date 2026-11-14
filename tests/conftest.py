"""Suite-wide guards."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_rp2040_ttys(tmp_path_factory, monkeypatch):
    """Point MVR discovery at an empty sysfs tree for every test.

    The service's tick scans /sys/class/tty for RP2040 CDC ports and
    opens any it finds to listen for an MVR.  A test must never open a
    real serial device, so no test sees the host's real tree unless it
    builds its own fake one."""
    try:
        from gpsdo_monitor import mvr
    except ImportError:          # before the module exists (TDD RED)
        return
    monkeypatch.setattr(mvr, "SYS_TTY", tmp_path_factory.mktemp("empty-sys-tty"))
