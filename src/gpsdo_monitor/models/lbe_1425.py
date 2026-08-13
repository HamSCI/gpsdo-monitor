"""LBE-1425 protocol driver — monitoring only.

The 1425 rides the 1421 wire format (Report ID 0x4B, 60-byte status)
and adds GNSS-receiver configurability. Byte map from David Goncalves'
reverse-engineering notes (ringof/lbe-142x
`docs/reverse/LBE-1425-config-v1.10.md`, MIT):

  21  GNSS constellation mask echo (default 0x47 = GPS+SBAS+Gal+GLO)
  22  u-blox dynamic platform model echo (CFG-NAV5 dynModel)
  23  measured antenna bias current, mA
  24  NMEA output enable echo

Byte 23 is decoded here, unlike on the 1421 parent (where the decode
ships disabled — a bench check there couldn't verify it: the
DC-blocked antenna feed read 0 mA both connected and open). The 1425
doc verifies byte 23 directly on 1425 hardware, so this class sets
``Health.antenna_bias_ma`` itself rather than inheriting a decode from
`Lbe1421._parse_status`.

The 1425's config opcodes (0x03 SET_GNSS, 0x04 SET_DYNMODEL, 0x0F
SET_NMEA) are deliberately NOT implemented — gpsdo-monitor watches; it
doesn't reconfigure receivers. The frequency/output control opcodes
inherited from the 1421 are likewise retained but unused: "monitoring-only"
means no GNSS-receiver reconfiguration opcodes, not a fully inert driver.
"""
from __future__ import annotations

from gpsdo_monitor.models.base import RawStatus
from gpsdo_monitor.models.lbe_1421 import Lbe1421


class Lbe1425(Lbe1421):
    name = "lbe-1425"
    pid = 0x2269

    def _parse_status(self, buf: bytes) -> RawStatus:
        raw = super()._parse_status(buf)
        raw.health.antenna_bias_ma = buf[23]
        raw.extras["receiver_config"] = {
            "gnss_mask": buf[21],
            "dyn_model": buf[22],
            "nmea_enabled": bool(buf[24]),
        }
        return raw
