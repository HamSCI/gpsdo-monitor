"""LBE-Mini protocol driver.

Port of `bvernoux/lbe-142x/src/model_mini.c`. The Mini is meaningfully
different from the 142x family:

- No HID Report ID; every Feature command is a raw 60-byte payload
  with the opcode at byte 0 (transport knows about this).
- Outputs / drive strength / PLL divider chain live in the static
  feature report — no raw status bitmap.
- GPS fix and PLL-lock state come from the HID interrupt-IN endpoint
  as a status byte plus a reassembled UBX stream. The upstream
  `mini_init` bootstrap has to run first, or the stream never starts.
- OUT1 drive is discrete (8/16/24/32 mA) rather than a boolean
  high/low.

Frequency planning (the Si5351 divider solver from upstream
`mini_solve_pll`) is intentionally **not** ported here — it's a
non-trivial search and we have no hardware to validate it against.
`set_frequency` raises NotImplementedError until that happens, with a
pointer to the upstream reference. `read_status` and
`read_gps_firmware` are the two operations hf-timestd actually needs,
and both are covered.
"""
from __future__ import annotations

import logging
import time

from gpsdo_monitor.hid_xport import REPORT_SIZE
from gpsdo_monitor.models.base import Capabilities, GpsdoModel, RawStatus
from gpsdo_monitor.schema import Health, Outputs
from gpsdo_monitor.ubx import (
    CLS_MON,
    CLS_NAV,
    ID_MON_VER,
    ID_NAV_CLOCK,
    ID_NAV_PVT,
    MonVer,
    NavClock,
    decode_mini_hid_frame,
    iter_messages,
    parse_mon_ver,
    parse_nav_clock,
    parse_nav_pvt,
)

log = logging.getLogger(__name__)

# Opcodes (lbe_common.h). OPC_EN_OUT + OPC_BLINK are shared with the
# 142x family; the rest collide by value with 1420 opcodes but carry
# different payloads — context-dependent.
OPC_EN_OUT         = 0x01
OPC_BLINK          = 0x02
OPC_MINI_SET_DRIVE = 0x03
OPC_MINI_SET_PLL   = 0x04
OPC_MINI_UBX_WRAP  = 0x08
OPC_MINI_NAV_STREAM = 0x0A

INTERRUPT_REPORT_SIZE = 64       # interrupt-IN frame length


def _parse_feature(buf: bytes) -> tuple[int, int, bool]:
    """Decode the Mini's static feature report → (freq_hz, drive_ma, outputs_enabled).

    See upstream `mini_get_status` for the field map. We compute
    `freq = fin * N2_HS * N2_LS / (N3 * N1_HS * NC1_LS)`; when the
    denominator is zero (an un-programmed device or bad read) we fall
    back to zero so the caller can classify it as degraded."""
    outputs_enabled = buf[0] != 0
    drive_idx = buf[1] & 0x03       # 0..3 → 8/16/24/32 mA
    drive_ma = (drive_idx + 1) * 8

    fin  = buf[2] | (buf[3] << 8) | (buf[4] << 16)
    n3   = (buf[5] | (buf[6] << 8) | (buf[7] << 16)) + 1
    n2hs = buf[8] + 4
    n2ls = (buf[9] | (buf[10] << 8) | (buf[11] << 16)) + 1
    n1hs = buf[12] + 4
    nc1  = (buf[13] | (buf[14] << 8) | (buf[15] << 16)) + 1

    den = n3 * n1hs * nc1
    freq_hz = (fin * n2hs * n2ls) // den if den else 0
    return freq_hz, drive_ma, outputs_enabled


class LbeMini(GpsdoModel):
    name = "lbe-mini"
    pid = 0x2211
    capabilities = Capabilities(
        has_out2=False,
        has_pps=False,
        has_pll_fll_toggle=False,
        has_antenna_flag=False,
        has_drive_ma=True,
        has_ubx_hid=True,
        has_ubx_mon_ver=True,
        max_freq_hz=810_000_000,
    )

    # How long get_status() will sample the interrupt-IN stream to pull
    # PLL-lock, GPS-signal, and fix_type out of it. Matches upstream's
    # 60-frame × 50 ms = 3 s window. Exposed as a class attribute so
    # callers / tests can tighten it.
    nav_sample_sec: float = 3.0

    # UBX wrap command: opcode 0x08, payload = {class, id, len_lo, len_hi}.
    # The firmware prepends B5 62 and appends the Fletcher-8 checksum
    # itself, so we only hand it the four-byte header.
    def _send(self, opcode: int, args: bytes) -> None:
        buf = bytearray(REPORT_SIZE)
        buf[0] = opcode
        end = min(REPORT_SIZE, 1 + len(args))
        buf[1:end] = args[: end - 1]
        # Mini uses no HID Report ID on the wire; hidapi's feature_set
        # still wants a report_id byte (0 for no-ID reports).
        self.hid.feature_set(0, bytes(buf))

    def _send_ubx_poll(self, class_id: int, msg_id: int) -> None:
        self._send(OPC_MINI_UBX_WRAP, bytes([class_id, msg_id, 0, 0]))

    # --- Stream enable (idempotent) ------------------------------------

    def _enable_stream(self) -> None:
        """Send the three UBX CFG-MSG frames plus the NAV_STREAM refresh
        that turn on NAV-SAT, NAV-CLOCK, and NAV-PVT on the interrupt-IN
        endpoint. Mirrors upstream `mini_enable_gps_stream`."""
        sat_cfg   = bytes([0x06, 0x01, 0x08, 0x00, 0x01, 0x35, 0x14])
        clock_cfg = bytes([0x06, 0x01, 0x08, 0x00, 0x01, 0x22, 0x14])
        pvt_cfg   = bytes([0x06, 0x01, 0x08, 0x00, 0x01, 0x07, 0x0A])
        self._send(OPC_MINI_NAV_STREAM, bytes([0x04]))
        # Upstream drains two feature reads here to flush a stale state
        # that otherwise produces ghost frames. Best-effort; ignore
        # errors because hidapi will raise if the device has nothing
        # queued yet, which is a normal state on a cold open.
        for _ in range(2):
            try:
                self.hid.feature_get(0, REPORT_SIZE)
            except OSError:
                pass
        self._send(OPC_MINI_UBX_WRAP, sat_cfg)
        self._send(OPC_MINI_UBX_WRAP, clock_cfg)
        self._send(OPC_MINI_UBX_WRAP, pvt_cfg)

    # --- Read path -----------------------------------------------------

    def get_status(self) -> RawStatus:
        buf = self.hid.feature_get(0, REPORT_SIZE)
        freq_hz, drive_ma, outputs_enabled = _parse_feature(buf)

        # Kick the stream bootstrap once per call so status works from
        # a cold open. The Mini keeps its stream config across opens
        # but the vendor tool still re-sends it — the reads/writes are
        # cheap and idempotent.
        try:
            self._enable_stream()
        except OSError as e:
            log.debug("Mini stream enable failed (harmless on first boot): %s", e)

        pll_locked, gps_signal_ok, signal_loss, fix_type, nav_clock = self._sample_nav(
            self.nav_sample_sec,
        )

        gps_fix: str | None = None
        if fix_type is not None:
            gps_fix = {0: "no_fix", 2: "2D", 3: "3D"}.get(fix_type, "no_fix")
        elif gps_signal_ok is True:
            gps_fix = None   # we saw the signal-present bit but no NAV-PVT yet

        # The Mini has no antenna detector, no PPS on the status side,
        # no separate outputs_enabled bit beyond the feature-report byte.
        health = Health(
            pll_locked=bool(pll_locked) if pll_locked is not None else False,
            outputs_enabled=outputs_enabled,
            gps_fix=gps_fix,
            antenna_ok=None,
            signal_loss_count=signal_loss,
            gps_locked=gps_signal_ok,
        )
        outputs = Outputs(
            out1_hz=freq_hz,
            out1_power="low" if drive_ma <= 8 else "normal",
            pps_enabled=False,
            drive_ma=drive_ma,
        )
        extras: dict[str, object] = {}
        if nav_clock is not None:
            extras["nav_clock"] = nav_clock
        return RawStatus(
            health=health,
            outputs=outputs,
            firmware=None,
            firmware_source="unavailable",
            raw_trailing_hex=buf[16:].hex(" "),
            extras=extras,
        )

    # --- Interrupt-IN stream sampler -----------------------------------

    def _sample_nav(
        self, duration_sec: float,
    ) -> tuple[bool | None, bool | None, int | None, int | None, NavClock | None]:
        """Read interrupt-IN frames for up to `duration_sec` and return
        `(pll_hw_locked, gps_signal_ok, signal_loss_count, fix_type,
        nav_clock)`.

        Any return field is None when we never saw a frame that told us
        about it. Upstream treats "no frames at all" as "PLL locked"
        (defensive default); we return None so the caller can decide
        whether to fall back to a last-known value or mark the device
        as degraded. `nav_clock` is the newest UBX-NAV-CLOCK frame seen
        in the window — bias/drift move constantly, so later frames
        overwrite earlier ones."""
        deadline = time.monotonic() + duration_sec
        pll: bool | None = None
        gps: bool | None = None
        sig_loss: int | None = None
        fix: int | None = None
        nav_clock: NavClock | None = None
        ubx_buf = b""
        while time.monotonic() < deadline:
            raw = self.hid.read(INTERRUPT_REPORT_SIZE, timeout_ms=50)
            if not raw:
                continue
            frame = decode_mini_hid_frame(raw)
            if frame is None:
                continue
            pll = frame.pll_hw_locked
            gps = frame.gps_signal_ok
            sig_loss = frame.signal_loss
            if not frame.carries_ubx:
                continue
            ubx_buf += frame.payload
            msgs, consumed = iter_messages(ubx_buf)
            if consumed:
                ubx_buf = ubx_buf[consumed:]
            for msg in msgs:
                if msg.class_id == CLS_NAV and msg.msg_id == ID_NAV_PVT:
                    pvt = parse_nav_pvt(msg.payload)
                    if pvt is not None and fix is None:
                        fix = pvt.fix_type
                if msg.class_id == CLS_NAV and msg.msg_id == ID_NAV_CLOCK:
                    nc = parse_nav_clock(msg.payload)
                    if nc is not None:
                        nav_clock = nc   # newest wins; bias/drift move constantly
        return pll, gps, sig_loss, fix, nav_clock

    # --- MON-VER -------------------------------------------------------

    def read_gps_firmware(self) -> str | None:
        """Return a compact firmware string like
        `SW=ROM CORE 3.01 (107888) HW=00080000 PROTVER=18.00`, or None
        if the module doesn't answer the poll in ~10 s."""
        mv = self.read_mon_ver()
        if mv is None:
            return None
        parts = [f"SW={mv.sw_version}", f"HW={mv.hw_version}"]
        if mv.protver is not None:
            parts.append(f"PROTVER={mv.protver}")
        return " ".join(parts)

    def read_mon_ver(self, *, timeout_sec: float = 10.0) -> MonVer | None:
        """Send a UBX-MON-VER poll and collect the response from the
        interrupt-IN stream. Returns the decoded struct or None on
        timeout. Cold-start callers should run `_enable_stream()` first
        (get_status does that implicitly) so the module is willing to
        stream answers at all."""
        try:
            self._send_ubx_poll(CLS_MON, ID_MON_VER)
        except OSError as e:
            log.warning("Mini MON-VER poll send failed: %s", e)
            return None

        deadline = time.monotonic() + timeout_sec
        ubx_buf = b""
        while time.monotonic() < deadline:
            raw = self.hid.read(INTERRUPT_REPORT_SIZE, timeout_ms=50)
            if not raw:
                continue
            frame = decode_mini_hid_frame(raw)
            if frame is None or not frame.carries_ubx:
                continue
            ubx_buf += frame.payload
            msgs, consumed = iter_messages(ubx_buf)
            if consumed:
                ubx_buf = ubx_buf[consumed:]
            for msg in msgs:
                if msg.class_id == CLS_MON and msg.msg_id == ID_MON_VER:
                    return parse_mon_ver(msg.payload)
        return None

    # --- Write path ----------------------------------------------------

    def set_outputs_enable(self, enable: bool) -> None:
        # Upstream: 0x03 = both on (vendor GUI sends 3), 0x00 = off.
        self._send(OPC_EN_OUT, bytes([0x03 if enable else 0x00]))

    def set_drive_ma(self, ma: int) -> None:
        if ma not in (8, 16, 24, 32):
            raise ValueError(f"drive {ma} mA not in {{8, 16, 24, 32}}")
        idx = (ma // 8) - 1
        self._send(OPC_MINI_SET_DRIVE, bytes([idx]))

    def set_power_level(self, output: int, low: bool) -> None:
        if output != 1:
            raise ValueError("LBE-Mini only has output 1")
        # Map the boolean high/low API onto the two drive-strength
        # extremes (8 mA = low, 32 mA = default), consistent with
        # upstream's CLI fallback.
        self.set_drive_ma(8 if low else 32)

    def blink(self) -> None:
        # Upstream GUI semantics: 0x02 0x01 starts blinking, 0x02 0x00
        # stops. Advertised "3 second" behaviour is emulated: start,
        # sleep, stop.
        self._send(OPC_BLINK, bytes([0x01]))
        time.sleep(3.0)
        self._send(OPC_BLINK, bytes([0x00]))

    def set_frequency(self, output: int, hz: int, *, persist: bool = True) -> None:
        # The Si5351 divider-chain solver from upstream mini_solve_pll
        # is non-trivial and not yet ported. Leave it explicit so nobody
        # silently assumes it works.
        raise NotImplementedError(
            "LBE-Mini set_frequency requires the Si5351 divider solver; "
            "see bvernoux/lbe-142x src/model_mini.c::mini_solve_pll"
        )
