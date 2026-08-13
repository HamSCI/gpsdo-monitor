"""LBE-1421 / LBE-1423 protocol driver.

Port of `bvernoux/lbe-142x/src/model_1421.c`. The 1423 uses the same
wire format as the 1421 until evidence of divergence turns up.

Status layout (Report ID 0x4B, 60 bytes):

  offset  size  field
  1       1     status bitmap  (bit0 GPS_LOCK, bit1 PLL_LOCK, bit2 ANT_OK,
                               bit3 LED1, bit4 LED2, bit5 OUT1_EN,
                               bit6 OUT2_EN, bit7 PPS_EN)
  6..9    4     frequency1 (Hz, u32 LE)
  14..17  4     frequency2 (Hz, u32 LE)
  18      1     FLL mode (0 PLL, 1 FLL)
  19      1     OUT1 power (0 normal, 1 low)
  20      1     OUT2 power
  21..22  2     observed config-echo candidates (not decoded): live 1421
                read 0x67 (u-blox M8 default GNSS mask: GPS+SBAS+
                Galileo+QZSS+GLONASS) and 0x02 (dynModel Stationary),
                mirroring the 1425 tail layout
  23      1     candidate bias-current byte (per ringof/lbe-142x 1425
                docs) — could not be verified on our bench (DC-blocked
                antenna feed reads 0 mA both connected and open);
                decode disabled pending a bench with a powered antenna
  24..59  36    unmapped — preserved as raw_trailing_hex for later RE

Set opcodes double as the HID Report ID; frequency u32 is written at
payload offset 5 (not 1 as on the 1420). See upstream source for the
exact opcode table.
"""
from __future__ import annotations

from gpsdo_monitor.hid_xport import REPORT_SIZE
from gpsdo_monitor.models.base import Capabilities, GpsdoModel, RawStatus
from gpsdo_monitor.schema import Health, Outputs

STATUS_REPORT_ID = 0x4B

# Status bitmap (see lbe_common.h).
GPS_LOCK_BIT = 1 << 0
PLL_LOCK_BIT = 1 << 1
ANT_OK_BIT   = 1 << 2
LED1_BIT     = 1 << 3
LED2_BIT     = 1 << 4
OUT1_EN_BIT  = 1 << 5
OUT2_EN_BIT  = 1 << 6
PPS_EN_BIT   = 1 << 7

# Opcodes (lbe_common.h).
OPC_EN_OUT       = 0x01
OPC_BLINK        = 0x02
OPC_SET_F1_TEMP  = 0x05
OPC_SET_F1       = 0x06
OPC_SET_F2_TEMP  = 0x09
OPC_SET_F2       = 0x0A
OPC_SET_PLL      = 0x0B
OPC_SET_PPS      = 0x0C
OPC_SET_PWR1     = 0x0D
OPC_SET_PWR2     = 0x0E


def _u32_le(buf: bytes, off: int) -> int:
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


class Lbe1421(GpsdoModel):
    name = "lbe-1421"
    pid = 0x2444
    capabilities = Capabilities(
        has_out2=True,
        has_pps=True,
        has_pll_fll_toggle=True,
        has_antenna_flag=True,
        has_temp_frequency=True,
        has_nmea_cdc=True,
        max_freq_hz=1_400_000_000,
    )

    def _send(self, opcode: int, args: bytes, args_offset: int = 1) -> None:
        """Build a 60-byte payload with opcode echo at byte 0 and args
        packed at `args_offset`."""
        buf = bytearray(REPORT_SIZE)
        buf[0] = opcode
        end = min(REPORT_SIZE, args_offset + len(args))
        buf[args_offset:end] = args[: end - args_offset]
        self.hid.feature_set(opcode, bytes(buf))

    # --- Read path -----------------------------------------------------
    def get_status(self) -> RawStatus:
        buf = self.hid.feature_get(STATUS_REPORT_ID, REPORT_SIZE)
        raw = buf[1]
        f1  = _u32_le(buf, 6)
        f2  = _u32_le(buf, 14)
        fll = bool(buf[18])
        pw1 = bool(buf[19])
        pw2 = bool(buf[20])

        health = Health(
            pll_locked=bool(raw & PLL_LOCK_BIT),
            outputs_enabled=(raw & (OUT1_EN_BIT | OUT2_EN_BIT))
                             == (OUT1_EN_BIT | OUT2_EN_BIT),
            fll_mode=fll,
            antenna_ok=bool(raw & ANT_OK_BIT),
            # antenna_bias_ma: decode disabled. Byte 23 was a candidate
            # bias-current field (ringof/lbe-142x 1425 docs) but the bench
            # check couldn't verify it — see class docstring / PROTOCOL.md.
            gps_locked=bool(raw & GPS_LOCK_BIT),
            # NMEA fields (gps_fix, sats_used, fix_age_sec) filled in
            # by the CDC reader coroutine in nmea.py.
        )
        outputs = Outputs(
            out1_hz=f1,
            out1_power="low" if pw1 else "normal",
            out2_hz=f2,
            out2_power="low" if pw2 else "normal",
            pps_enabled=bool(raw & PPS_EN_BIT),
        )
        trailing = buf[21:].hex(" ")
        return RawStatus(
            health=health,
            outputs=outputs,
            firmware=None,
            firmware_source="unavailable",
            raw_trailing_hex=trailing,
        )

    # --- Write path (stubbed; verify byte layout against real hw) ------
    def set_frequency(self, output: int, hz: int, *, persist: bool = True) -> None:
        if hz < 1 or hz > self.capabilities.max_freq_hz:
            raise ValueError(f"frequency {hz} Hz out of range")
        if output == 1:
            op = OPC_SET_F1 if persist else OPC_SET_F1_TEMP
        elif output == 2:
            op = OPC_SET_F2 if persist else OPC_SET_F2_TEMP
        else:
            raise ValueError("output must be 1 or 2")
        args = hz.to_bytes(4, "little")
        self._send(op, args, args_offset=5)

    def set_outputs_enable(self, enable: bool) -> None:
        # 0x03 = both on, 0x00 = both off (matches upstream)
        self._send(OPC_EN_OUT, bytes([0x03 if enable else 0x00]))

    def set_power_level(self, output: int, low: bool) -> None:
        op = OPC_SET_PWR1 if output == 1 else OPC_SET_PWR2 if output == 2 else None
        if op is None:
            raise ValueError("output must be 1 or 2")
        self._send(op, bytes([1 if low else 0]))

    def set_pll_mode(self, fll: bool) -> None:
        self._send(OPC_SET_PLL, bytes([1 if fll else 0]))

    def set_1pps(self, enable: bool) -> None:
        self._send(OPC_SET_PPS, bytes([1 if enable else 0]))

    def blink(self) -> None:
        self._send(OPC_BLINK, b"")


class Lbe1423(Lbe1421):
    """Same wire format as the 1421 (upstream comment)."""

    name = "lbe-1423"
    pid = 0x226F
