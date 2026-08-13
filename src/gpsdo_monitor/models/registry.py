"""PID → model-class dispatch."""
from __future__ import annotations

from gpsdo_monitor.hid_xport import HidCandidate, HidDevice
from gpsdo_monitor.models.base import GpsdoModel
from gpsdo_monitor.models.lbe_1420 import Lbe1420
from gpsdo_monitor.models.lbe_1421 import Lbe1421, Lbe1423
from gpsdo_monitor.models.lbe_1425 import Lbe1425
from gpsdo_monitor.models.lbe_mini import LbeMini

REGISTRY: dict[int, type[GpsdoModel]] = {
    Lbe1420.pid: Lbe1420,
    Lbe1421.pid: Lbe1421,
    Lbe1423.pid: Lbe1423,
    Lbe1425.pid: Lbe1425,
    LbeMini.pid: LbeMini,
}


def open_model(candidate: HidCandidate) -> GpsdoModel:
    """Open the HID device and return the right model driver instance."""
    cls = REGISTRY.get(candidate.pid)
    if cls is None:
        raise ValueError(f"no driver for PID {candidate.pid:#06x}")
    hid_dev = HidDevice(candidate.path)
    return cls(hid_dev)
