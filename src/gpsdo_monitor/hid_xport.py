"""Thin wrapper over the `hidapi` Python binding.

We deliberately avoid raw hidraw ioctls: `hid.Device.get_feature_report` /
`send_feature_report` give us the same semantics cross-platform (Linux
hidraw, macOS, Windows) and the library does the bookkeeping.

The only platform-specific surface we keep is the 1421/1423 CDC port
for NMEA + 1PPS, which lives in `nmea.py` / `pps.py` and uses
`pyserial`.
"""
from __future__ import annotations

from dataclasses import dataclass

import hid  # from `hidapi` package on PyPI

REPORT_SIZE = 60
VID_LBE = 0x1DD2

PIDS: dict[int, str] = {
    0x2443: "lbe-1420",
    0x2444: "lbe-1421",
    0x226F: "lbe-1423",
    0x2211: "lbe-mini",
}


@dataclass(frozen=True)
class HidCandidate:
    path: bytes
    vid: int
    pid: int
    serial: str
    product: str
    manufacturer: str

    @property
    def model(self) -> str:
        return PIDS.get(self.pid, f"unknown-{self.pid:#06x}")


def enumerate_lbe() -> list[HidCandidate]:
    """Return every plugged-in Leo Bodnar HID device."""
    out: list[HidCandidate] = []
    for d in hid.enumerate(VID_LBE, 0):
        if d["product_id"] not in PIDS:
            continue
        out.append(HidCandidate(
            path=d["path"],
            vid=d["vendor_id"],
            pid=d["product_id"],
            serial=d.get("serial_number") or "",
            product=d.get("product_string") or "",
            manufacturer=d.get("manufacturer_string") or "",
        ))
    return out


class HidDevice:
    """Owned HID handle.

    The LBE-1420/1421/1423 use the opcode as both the Report ID on the
    wire and the echo byte at payload[0]; the Mini has no Report ID and
    the 60 bytes on the wire are the payload verbatim. We expose both
    conventions and let the model layer pick.
    """

    def __init__(self, path: bytes) -> None:
        self._d = hid.device()
        self._d.open_path(path)
        self._path = path

    @property
    def path(self) -> bytes:
        return self._path

    def close(self) -> None:
        self._d.close()

    def __enter__(self) -> "HidDevice":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- Feature reports (all four models) -------------------------------
    def feature_set(self, report_id: int, payload: bytes) -> None:
        """Send a Feature report. `payload` must be REPORT_SIZE bytes."""
        if len(payload) != REPORT_SIZE:
            raise ValueError(f"payload must be {REPORT_SIZE} bytes, got {len(payload)}")
        # hidapi expects the leading byte to be the Report ID (0 for no-ID).
        self._d.send_feature_report(bytes([report_id]) + payload)

    def feature_get(self, report_id: int, length: int = REPORT_SIZE) -> bytes:
        """Read a Feature report. Returns a `length`-byte payload buffer.

        Two conventions, matching the wire:

        - 142x family (report_id != 0, numbered reports): hidapi's
          `get_feature_report(report_id, length)` returns exactly `length`
          bytes, and buf[0] is the id/opcode echo the device sends back —
          matching upstream `lbe-142x/src/model_*.c` indexing, so raw
          status lives at buf[1], frequency at buf[6..9], etc.

        - LBE-Mini (report_id == 0, no Report ID on the wire): hidapi
          still prepends a report-id placeholder byte to the buffer it
          returns, so we must ask for `length + 1` bytes — asking for only
          `length` under-requests the kernel by one byte and stalls the
          transfer on real hardware (verified live: OSError on every
          attempt, plus usbhid interface resets in dmesg). We verify the
          extra byte came back, then strip it so the returned buffer is
          the `length`-byte payload verbatim: buf[0] here is the payload's
          first byte, not an echo.
        """
        if report_id == 0:
            buf = self._d.get_feature_report(report_id, length + 1)
            if len(buf) != length + 1:
                raise OSError(
                    f"short feature-report read: got {len(buf)} bytes, expected {length + 1}"
                )
            return bytes(buf)[1:]
        buf = self._d.get_feature_report(report_id, length)
        if len(buf) != length:
            raise OSError(
                f"short feature-report read: got {len(buf)} bytes, expected {length}"
            )
        return bytes(buf)

    # --- Interrupt IN (Mini UBX stream) ---------------------------------
    def read(self, length: int, timeout_ms: int | None = None) -> bytes:
        data = self._d.read(length, timeout_ms if timeout_ms is not None else 0)
        return bytes(data or b"")


__all__ = [
    "REPORT_SIZE",
    "VID_LBE",
    "PIDS",
    "HidCandidate",
    "HidDevice",
    "enumerate_lbe",
]
