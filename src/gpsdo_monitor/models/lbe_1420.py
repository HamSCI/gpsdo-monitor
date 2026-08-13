"""LBE-1420 protocol driver.

Port of `bvernoux/lbe-142x/src/model_1420.c`. The 1420 is the single-
output predecessor of the 1421 and differs from it in several ways
that matter to us:

- No OUT2; no 1PPS; no CDC-NMEA monitor (upstream `lbe_ops_1420` has
  no `.monitor` entry, so there is no tty NMEA stream to consume).
- The firmware does not mirror the outputs-enable bit back in the
  feature report, so the parser always reports `outputs_enabled=True`
  (upstream does the same, see `m1420_get_status`).
- `out1_power_low` is at buf[10] (not buf[19] as on the 1421).
- SET-frequency payload packs the u32 at payload offset 1 rather than
  the 1421's offset 5.
- Power-level and temp-frequency opcodes are unique to the 1420
  (SET_F1_TEMP=0x03, SET_F1=0x04, SET_PWR1=0x07).

Status layout (Report ID 0x4B, 60 bytes, same as 1421/1423):

  offset  size  field
  1       1     status bitmap (bit0 GPS_LOCK, bit1 PLL_LOCK,
                               bit2 ANT_OK, bits 3..4 LEDs,
                               bits 5..7 unused on 1420)
  6..9    4     frequency1 (Hz, u32 LE)
  10      1     OUT1 power (0 normal, 1 low)
  12      1     antenna bias current, mA (per ringof/lbe-142x
                LBE-1420-config-v1.08.md; NOT verified on our bench —
                no local 1420)
  18      1     FLL mode (0 PLL, 1 FLL)
  21..59  39    unmapped — preserved as raw_trailing_hex
"""
from __future__ import annotations

from gpsdo_monitor.hid_xport import REPORT_SIZE
from gpsdo_monitor.models.base import Capabilities, GpsdoModel, RawStatus
from gpsdo_monitor.models.lbe_1421 import (
    ANT_OK_BIT,
    GPS_LOCK_BIT,
    PLL_LOCK_BIT,
    _u32_le,
)
from gpsdo_monitor.schema import Health, Outputs

STATUS_REPORT_ID = 0x4B

# Opcodes (lbe_common.h). EN_OUT / BLINK / SET_PLL are shared across
# the 142x family; the rest are 1420-specific.
OPC_EN_OUT       = 0x01
OPC_BLINK        = 0x02
OPC_SET_F1_TEMP  = 0x03
OPC_SET_F1       = 0x04
OPC_SET_PWR1     = 0x07
OPC_SET_PLL      = 0x0B


class Lbe1420(GpsdoModel):
    name = "lbe-1420"
    pid = 0x2443
    capabilities = Capabilities(
        has_out2=False,
        has_pps=False,
        has_pll_fll_toggle=True,
        has_antenna_flag=True,
        has_temp_frequency=True,
        has_nmea_cdc=False,
        max_freq_hz=1_600_000_000,
    )

    def _send(self, opcode: int, args: bytes) -> None:
        """1420 convention: args start at payload offset 1 (right after
        the opcode echo). Contrast with the 1421, which leaves a 4-byte
        gap and places args at offset 5."""
        buf = bytearray(REPORT_SIZE)
        buf[0] = opcode
        end = min(REPORT_SIZE, 1 + len(args))
        buf[1:end] = args[: end - 1]
        self.hid.feature_set(opcode, bytes(buf))

    # --- Read path -----------------------------------------------------
    def get_status(self) -> RawStatus:
        buf = self.hid.feature_get(STATUS_REPORT_ID, REPORT_SIZE)
        raw = buf[1]
        f1  = _u32_le(buf, 6)
        pw1 = bool(buf[10])
        fll = bool(buf[18])
        bias = buf[12]

        health = Health(
            pll_locked=bool(raw & PLL_LOCK_BIT),
            # Upstream hard-codes outputs_enabled=True: the 1420 firmware
            # does not reflect the enable-state bit back in the status
            # report. Publishing the raw bit here would mis-report the
            # device as disabled on every sample.
            outputs_enabled=True,
            fll_mode=fll,
            antenna_ok=bool(raw & ANT_OK_BIT),
            antenna_bias_ma=bias,
            gps_locked=bool(raw & GPS_LOCK_BIT),
        )
        outputs = Outputs(
            out1_hz=f1,
            out1_power="low" if pw1 else "normal",
            pps_enabled=False,
        )
        return RawStatus(
            health=health,
            outputs=outputs,
            firmware=None,
            firmware_source="unavailable",
            raw_trailing_hex=buf[21:].hex(" "),
        )

    # --- Write path ----------------------------------------------------
    def set_frequency(self, output: int, hz: int, *, persist: bool = True) -> None:
        if output != 1:
            raise ValueError("LBE-1420 only has output 1")
        if hz < 1 or hz > self.capabilities.max_freq_hz:
            raise ValueError(f"frequency {hz} Hz out of range")
        op = OPC_SET_F1 if persist else OPC_SET_F1_TEMP
        self._send(op, hz.to_bytes(4, "little"))

    def set_outputs_enable(self, enable: bool) -> None:
        # Upstream: 0x01 = on, 0x00 = off (1420 enables a single output).
        self._send(OPC_EN_OUT, bytes([0x01 if enable else 0x00]))

    def set_power_level(self, output: int, low: bool) -> None:
        if output != 1:
            raise ValueError("LBE-1420 only has output 1")
        self._send(OPC_SET_PWR1, bytes([1 if low else 0]))

    def set_pll_mode(self, fll: bool) -> None:
        self._send(OPC_SET_PLL, bytes([1 if fll else 0]))

    def blink(self) -> None:
        self._send(OPC_BLINK, b"")
