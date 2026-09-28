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

Frequency planning (the Si5351 divider solver) is ported in pure Python
(`gpsdo_monitor.mini_pll`, ported from David Goncalves' ringof/lbe-142x,
MIT). `set_frequency` is supported and live-validated against a bench Mini.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from gpsdo_monitor.hid_xport import REPORT_SIZE
from gpsdo_monitor.mini_pll import solve_pll
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
    NavPvt,
    decode_mini_hid_frame,
    iter_messages,
    parse_mon_ver,
    parse_nav_clock,
    nav_pvt_utc,
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

# Continuous reader (see LbeMini.start_reader).
READER_READ_TIMEOUT_MS = 50      # one interrupt-IN read; the lock is held this long at most
STREAM_REFRESH_SEC = 30.0        # re-send the stream-enable bootstrap this often
SNAPSHOT_STALE_SEC = 15.0        # older than this reads as "saw nothing"
NAV_PVT_RATE_WINDOW_SEC = 60.0   # nav_pvt_rate_hz counts decodes over this window

# Called by the reader on every decoded NAV-PVT with the host monotonic and
# realtime clocks read at the same instant, right after the message finished
# reassembling (the instant that also stamps the naming pair).
NavPvtHook = Callable[[NavPvt, float, float], None]


@dataclass
class _NavSnapshot:
    """What the reader thread last saw on the interrupt-IN stream.

    Each value carries the monotonic at which it arrived, so get_status()
    can refuse anything older than SNAPSHOT_STALE_SEC."""

    last_frame_mono: float | None = None
    pll: bool | None = None
    gps: bool | None = None
    sig_loss: int | None = None
    nav_clock: NavClock | None = None
    nav_clock_mono: float | None = None
    nav_pvt: NavPvt | None = None
    nav_pvt_mono: float | None = None
    nav_pvt_real: float | None = None
    nav_pvt_count: int = 0
    mon_ver: MonVer | None = None
    mon_ver_seq: int = 0


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

    def __init__(self, hid) -> None:
        super().__init__(hid)
        # ONE lock guards every touch of the HID handle: interrupt reads,
        # feature reads, and every feature command (stream enable, drive,
        # PLL, outputs, blink, UBX polls).  Reentrant, so a command that
        # sends several reports (the stream bootstrap) can hold it across
        # the whole sequence.  The reader holds it only around one 50 ms
        # read, so a setter waits at most about that long.
        self._hid_lock = threading.RLock()
        # Non-reader threads waiting for the handle.  The reader yields
        # while this is non-zero: a plain Lock is not fair, and a loop that
        # releases and re-acquires at once could starve a setter.
        self._hid_waiters = 0
        self._waiters_lock = threading.Lock()
        # Latest-state snapshot the reader writes and get_status() reads.
        self._state_cond = threading.Condition(threading.Lock())
        self._snap = _NavSnapshot()
        self._pvt_stamps: deque[float] = deque()
        self._last_feature: bytes | None = None
        self._reader_thread: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_started_mono: float | None = None
        # Clock seam: tests shift it to age the snapshot without waiting.
        self._monotonic: Callable[[], float] = time.monotonic
        # Hook point for the chrony SHM witness (plan Task 2).
        self.on_nav_pvt: NavPvtHook | None = None

    @contextmanager
    def _hid_access(self) -> Iterator[None]:
        """Hold the device's HID lock.  Every handle access goes through here."""
        if threading.current_thread() is self._reader_thread:
            with self._hid_lock:
                yield
            return
        with self._waiters_lock:
            self._hid_waiters += 1
        acquired = False
        try:
            self._hid_lock.acquire()
            acquired = True
            with self._waiters_lock:
                self._hid_waiters -= 1
            yield
        finally:
            if acquired:
                self._hid_lock.release()
            else:
                with self._waiters_lock:
                    self._hid_waiters -= 1

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
        with self._hid_access():
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
        # One lock hold across the whole sequence, so no other command can
        # land between the refresh and the CFG-MSG frames.
        with self._hid_access():
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

    def get_status(self, *, reuse_feature: bool = False) -> RawStatus:
        """Return the device state.

        With the reader thread running (the daemon), everything the
        interrupt-IN stream carries comes from its snapshot and this call
        does not block on the stream.  Without it (one-shot CLI, TUI) the
        call samples the stream for `nav_sample_sec`, as it always has.

        `reuse_feature=True` answers from the last feature report read,
        when there is one, instead of issuing a new control transfer.  The
        1 Hz republish uses it: output settings change only when someone
        sets them, and the 10 s probe tick re-reads them."""
        buf = self._last_feature if reuse_feature else None
        if buf is None:
            with self._hid_access():
                buf = self.hid.feature_get(0, REPORT_SIZE)
            self._last_feature = buf

        extras: dict[str, object] = {}
        if self._reader_thread is not None:
            (pll_locked, gps_signal_ok, signal_loss, fix_type, nav_clock,
             nav_pvt, nav_pvt_mono) = self._snapshot_view()
            # The snapshot can be up to SNAPSHOT_STALE_SEC old, so the fix
            # states its real age rather than the sampler's ~0.
            fix_age = (max(0.0, self._monotonic() - nav_pvt_mono)
                       if nav_pvt_mono is not None else None)
            extras["nav_pvt_rate_hz"] = self.nav_pvt_rate_hz()
        else:
            # Kick the stream bootstrap once per call so status works from
            # a cold open. The Mini keeps its stream config across opens
            # but the vendor tool still re-sends it — the reads/writes are
            # cheap and idempotent.
            try:
                self._enable_stream()
            except OSError as e:
                log.debug("Mini stream enable failed (harmless on first boot): %s", e)
            (pll_locked, gps_signal_ok, signal_loss, fix_type, nav_clock,
             nav_pvt, nav_pvt_mono) = self._sample_nav(self.nav_sample_sec)
            fix_age = 0.0
        return self._build_status(
            buf, pll_locked, gps_signal_ok, signal_loss, fix_type, nav_clock,
            nav_pvt, nav_pvt_mono, fix_age, extras)

    def _build_status(
        self, buf: bytes, pll_locked: bool | None, gps_signal_ok: bool | None,
        signal_loss: int | None, fix_type: int | None,
        nav_clock: NavClock | None, nav_pvt: NavPvt | None,
        nav_pvt_mono: float | None, fix_age: float | None,
        extras: dict[str, object],
    ) -> RawStatus:
        freq_hz, drive_ma, outputs_enabled = _parse_feature(buf)

        gps_fix: str | None = None
        if fix_type is not None:
            gps_fix = {0: "no_fix", 2: "2D", 3: "3D"}.get(fix_type, "no_fix")
        elif gps_signal_ok is True:
            gps_fix = None   # we saw the signal-present bit but no NAV-PVT yet

        # Position, from NAV-PVT and nowhere else: the Mini presents no CDC
        # serial port, so the NMEA path that fills these fields on the 142x
        # family does not exist here.
        #
        # ⛔ Only with an actual fix.  A receiver with no antenna still sends
        # NAV-PVT, with fix_type 0 and lat/lon ZERO — and publishing 0,0
        # would place the station in the Gulf of Guinea AND invite the
        # location authority to re-grid a real station to it.  Absent
        # position must read as unknown, which is what None means to every
        # consumer here.
        latitude = longitude = altitude_m = None
        sats_used = None
        fix_age_sec = None
        if nav_pvt is not None:
            sats_used = nav_pvt.num_sv
            if nav_pvt.fix_type >= 2:
                latitude = nav_pvt.lat_1e7 / 1e7
                longitude = nav_pvt.lon_1e7 / 1e7
                altitude_m = nav_pvt.hmsl_mm / 1000.0
                # The solution was decoded during THIS probe, so its age is
                # ~0 by construction — the same reasoning build_report
                # already applies to probe_age_sec, bounded by the sample
                # window (nav_sample_sec).
                #
                # ⛔ Not cosmetic.  sigmond's location authority discards a
                # fix whose age it cannot read ("if age is None or age > 120:
                # continue"), and fix_age_sec used to be filled only from
                # NMEA — which this device does not have.  So every fix the
                # Mini ever produced was thrown away as stale, and AC0G-ND
                # computed WWV path lengths from its grid-square CENTRE,
                # 1.26 km from the real antenna: 4.2 us of path error
                # against a T6 floor of 0.11 us.
                #
                # With the reader running, the solution comes from its
                # snapshot, and `fix_age` carries the time since decode.
                fix_age_sec = fix_age

        # --- Naming a second, on a device that cannot PLACE one --------
        #
        # ⛔ The Mini emits NO PPS.  Its synthesiser floor sits far above
        # 1 Hz and `pps_enabled` reads false; nothing here places a second
        # BOUNDARY.  But NAV-PVT already tells us WHICH second it is, and
        # the two questions differ by four orders of magnitude:
        # "which integer second?" needs +/-0.5 s, "where is the edge?"
        # needs microseconds and a pulse.
        #
        # hf-timestd keeps them apart on its side: resolve_t5_capability
        # lights T5 only on MEASURED pps_study edges, so filling this
        # cannot promote a pulse-less device to a tier that needs a pulse
        # (T6_ACCEPTANCE_CRITERIA / gpsdo_capability.attach_second_namer).
        # Until now the field stayed null for the Mini and DASI-009.AI6VN
        # published naming_unavailable while the answer sat on its USB bus.
        #
        # The pair is boundary-consistent: `pps_utc_sec` is the integer
        # second, and the monotonic beside it is when THAT SECOND BEGAN,
        # back-computed from NAV-PVT's `nano` correction.  Pairing the
        # second with the decode instant instead would leave up to a full
        # second of unknown fraction in it, and a consumer ageing the
        # reading forward would round to the wrong second.
        pps_utc_sec = None
        naming_mono = None
        naming_sigma_ns = None
        if nav_pvt is not None and nav_pvt_mono is not None:
            utc_exact = nav_pvt_utc(nav_pvt)
            if utc_exact is not None:
                pps_utc_sec = int(math.floor(utc_exact))
                naming_mono = nav_pvt_mono - (utc_exact - pps_utc_sec)
                # The receiver's own tAcc: a self-report, not an
                # independent measurement, but the only honest sigma a
                # pulse-less device can offer.  Far better than assuming.
                naming_sigma_ns = nav_pvt.t_acc_ns

        # The Mini has no antenna detector, no PPS on the status side,
        # no separate outputs_enabled bit beyond the feature-report byte.
        health = Health(
            pll_locked=bool(pll_locked) if pll_locked is not None else False,
            outputs_enabled=outputs_enabled,
            gps_fix=gps_fix,
            sats_used=sats_used,
            fix_age_sec=fix_age_sec,
            latitude=latitude,
            longitude=longitude,
            altitude_m=altitude_m,
            antenna_ok=None,
            signal_loss_count=signal_loss,
            gps_locked=gps_signal_ok,
            pps_utc_sec=pps_utc_sec,
            nmea_host_monotonic_at_read=naming_mono,
            naming_source="ubx-nav-pvt" if pps_utc_sec is not None else None,
            naming_sigma_ns=naming_sigma_ns,
        )
        outputs = Outputs(
            out1_hz=freq_hz,
            out1_power="low" if drive_ma <= 8 else "normal",
            pps_enabled=False,
            drive_ma=drive_ma,
        )
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
    ) -> tuple[bool | None, bool | None, int | None, int | None,
               NavClock | None, NavPvt | None, float | None]:
        """Read interrupt-IN frames for up to `duration_sec` and return
        `(pll_hw_locked, gps_signal_ok, signal_loss_count, fix_type,
        nav_clock, nav_pvt, nav_pvt_monotonic)`.

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
        nav_pvt: NavPvt | None = None
        nav_pvt_mono: float | None = None
        ubx_buf = b""
        while time.monotonic() < deadline:
            with self._hid_access():
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
                        # Keep the whole solution.  NAV-PVT is the Mini's
                        # ONLY position source — it presents no CDC serial,
                        # so there is no NMEA behind it — and dropping
                        # lat/lon/hMSL here left a Mini station unable to
                        # derive its own grid (AC0G-ND 2026-09-02).
                        nav_pvt = pvt
                        # Monotonic at the moment this solution was
                        # decoded, NOT at the end of the sample window:
                        # the window is nav_sample_sec long, so pairing
                        # the reading with the window's end would age
                        # the second by up to that much before anyone
                        # consumed it.
                        nav_pvt_mono = time.monotonic()
                if msg.class_id == CLS_NAV and msg.msg_id == ID_NAV_CLOCK:
                    nc = parse_nav_clock(msg.payload)
                    if nc is not None:
                        nav_clock = nc   # newest wins; bias/drift move constantly
        return pll, gps, sig_loss, fix, nav_clock, nav_pvt, nav_pvt_mono

    # --- Continuous reader (daemon) ------------------------------------
    #
    # One thread per Mini owns the interrupt-IN stream for the life of the
    # device.  The 3 s window above ran once per 10 s probe tick, so ~7 s of
    # every 10 s of NAV-PVT and NAV-CLOCK went unread and the JSON refreshed
    # every 10 s.  The reader reads everything, keeps the newest state in a
    # snapshot, and get_status() answers from it.

    def start_reader(self) -> None:
        """Start the reader thread (idempotent)."""
        if self._reader_thread is not None:
            return
        self._reader_stop.clear()
        self._reader_started_mono = self._monotonic()
        t = threading.Thread(target=self._reader_loop,
                             name="gpsdo-mini-reader", daemon=True)
        self._reader_thread = t
        t.start()

    def stop_reader(self, *, timeout_sec: float = 2.0) -> None:
        """Stop the reader thread and join it."""
        t = self._reader_thread
        if t is None:
            return
        self._reader_stop.set()
        t.join(timeout=timeout_sec)
        if t.is_alive():
            log.warning("Mini reader did not stop within %.1f s", timeout_sec)
        self._reader_thread = None
        self._reader_started_mono = None

    def close(self) -> None:
        self.stop_reader()
        super().close()

    def nav_pvt_count(self) -> int:
        """NAV-PVT messages the reader has decoded since it started."""
        with self._state_cond:
            return self._snap.nav_pvt_count

    def nav_pvt_rate_hz(self) -> float | None:
        """NAV-PVT decodes over the trailing 60 s, divided by 60.

        The measured answer to "how often does the Mini actually send".
        None until the reader has run a full window: a shorter one would
        under-read the rate and look like a slow device."""
        now = self._monotonic()
        started = self._reader_started_mono
        if started is None or now - started < NAV_PVT_RATE_WINDOW_SEC:
            return None
        cutoff = now - NAV_PVT_RATE_WINDOW_SEC
        with self._state_cond:
            n = sum(1 for m in self._pvt_stamps if m >= cutoff)
        return n / NAV_PVT_RATE_WINDOW_SEC

    def _snapshot_view(
        self,
    ) -> tuple[bool | None, bool | None, int | None, int | None,
               NavClock | None, NavPvt | None, float | None]:
        """The snapshot in `_sample_nav`'s return shape, minus anything stale.

        No frame for SNAPSHOT_STALE_SEC reads exactly as a sample window
        that saw nothing; a NAV-PVT or NAV-CLOCK that old reads as one the
        window never saw."""
        now = self._monotonic()

        def fresh(mono: float | None) -> bool:
            return mono is not None and now - mono <= SNAPSHOT_STALE_SEC

        with self._state_cond:
            s = self._snap
            if not fresh(s.last_frame_mono):
                return None, None, None, None, None, None, None
            nav_clock = s.nav_clock if fresh(s.nav_clock_mono) else None
            if fresh(s.nav_pvt_mono):
                nav_pvt, nav_pvt_mono = s.nav_pvt, s.nav_pvt_mono
            else:
                nav_pvt, nav_pvt_mono = None, None
            fix = nav_pvt.fix_type if nav_pvt is not None else None
            return s.pll, s.gps, s.sig_loss, fix, nav_clock, nav_pvt, nav_pvt_mono

    def _reader_loop(self) -> None:
        ubx_buf = b""
        next_enable = self._monotonic()     # enable at once, then every 30 s
        failing = False
        timeout_s = READER_READ_TIMEOUT_MS / 1000.0
        while not self._reader_stop.is_set():
            if self._monotonic() >= next_enable:
                try:
                    self._enable_stream()
                except OSError as e:
                    log.debug("Mini stream enable failed: %s", e)
                next_enable = self._monotonic() + STREAM_REFRESH_SEC
            # Let a waiting setter in before taking the handle again.
            while self._hid_waiters and not self._reader_stop.is_set():
                time.sleep(0.001)
            t0 = time.monotonic()
            try:
                with self._hid_access():
                    raw = self.hid.read(INTERRUPT_REPORT_SIZE,
                                        timeout_ms=READER_READ_TIMEOUT_MS)
            except OSError as e:
                # Log the transition, not every failed read: a device gone
                # for a minute would otherwise write 1,200 lines.
                if not failing:
                    log.warning("Mini interrupt read failing: %s", e)
                    failing = True
                ubx_buf = b""
                self._reader_stop.wait(1.0)
                continue
            if failing:
                log.info("Mini interrupt read recovered")
                failing = False
            if not raw:
                # A read that returns empty well inside its timeout would
                # otherwise spin this loop at full CPU.
                spent = time.monotonic() - t0
                if spent < timeout_s / 2:
                    self._reader_stop.wait(timeout_s - spent)
                continue
            ubx_buf = self._ingest(raw, ubx_buf)

    def _ingest(self, raw: bytes, ubx_buf: bytes) -> bytes:
        """Fold one interrupt-IN frame into the snapshot; return the
        unconsumed UBX tail."""
        frame = decode_mini_hid_frame(raw)
        if frame is None:
            return ubx_buf
        with self._state_cond:
            s = self._snap
            s.last_frame_mono = self._monotonic()
            s.pll = frame.pll_hw_locked
            s.gps = frame.gps_signal_ok
            s.sig_loss = frame.signal_loss
        if not frame.carries_ubx:
            return ubx_buf
        ubx_buf += frame.payload
        msgs, consumed = iter_messages(ubx_buf)
        if consumed:
            ubx_buf = ubx_buf[consumed:]
        # iter_messages keeps a partial tail; a tail longer than any legal
        # message (8 + 512) can only be garbage.
        if len(ubx_buf) > 1024:
            ubx_buf = ubx_buf[-520:]
        for msg in msgs:
            if msg.class_id == CLS_NAV and msg.msg_id == ID_NAV_PVT:
                pvt = parse_nav_pvt(msg.payload)
                if pvt is None:
                    continue
                # The decode instant: the message has just finished
                # reassembling.  Both clocks read here, together.
                mono = self._monotonic()
                real = time.time()
                with self._state_cond:
                    s = self._snap
                    s.nav_pvt = pvt
                    s.nav_pvt_mono = mono
                    s.nav_pvt_real = real
                    s.nav_pvt_count += 1
                    self._pvt_stamps.append(mono)
                    cutoff = mono - NAV_PVT_RATE_WINDOW_SEC
                    while self._pvt_stamps and self._pvt_stamps[0] < cutoff:
                        self._pvt_stamps.popleft()
                hook = self.on_nav_pvt
                if hook is not None:
                    try:
                        hook(pvt, mono, real)
                    except Exception:
                        log.exception("on_nav_pvt hook failed")
            elif msg.class_id == CLS_NAV and msg.msg_id == ID_NAV_CLOCK:
                nc = parse_nav_clock(msg.payload)
                if nc is not None:
                    with self._state_cond:
                        self._snap.nav_clock = nc
                        self._snap.nav_clock_mono = self._monotonic()
            elif msg.class_id == CLS_MON and msg.msg_id == ID_MON_VER:
                mv = parse_mon_ver(msg.payload)
                if mv is not None:
                    with self._state_cond:
                        self._snap.mon_ver = mv
                        self._snap.mon_ver_seq += 1
                        self._state_cond.notify_all()
        return ubx_buf

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
        if self._reader_thread is not None:
            return self._read_mon_ver_via_reader(timeout_sec)
        try:
            self._send_ubx_poll(CLS_MON, ID_MON_VER)
        except OSError as e:
            log.warning("Mini MON-VER poll send failed: %s", e)
            return None

        deadline = time.monotonic() + timeout_sec
        ubx_buf = b""
        while time.monotonic() < deadline:
            with self._hid_access():
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

    def _read_mon_ver_via_reader(self, timeout_sec: float) -> MonVer | None:
        """With the reader running it owns the stream: send the poll, then
        wait for the reader to decode an answer newer than the poll."""
        with self._state_cond:
            seq0 = self._snap.mon_ver_seq
        try:
            self._send_ubx_poll(CLS_MON, ID_MON_VER)
        except OSError as e:
            log.warning("Mini MON-VER poll send failed: %s", e)
            return None
        deadline = time.monotonic() + timeout_sec
        with self._state_cond:
            while self._snap.mon_ver_seq == seq0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._state_cond.wait(remaining)
            return self._snap.mon_ver

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
        """Program OUT1 via the divider solver (opcode 0x04, SET_PLL).

        Payload layout per ringof/lbe-142x `mini_set_frequency` (MIT):
        3-byte LE fields with upstream's minus-1 / minus-4 encodings,
        NC2 mirroring NC1 on the single-output Mini, SKEW=0, BW=9.
        The write persists in device flash; upstream has no temporary
        variant on the Mini, so `persist=False` is rejected."""
        if output != 1:
            raise ValueError("LBE-Mini only has output 1")
        if not persist:
            raise ValueError("temporary frequency is not supported on the Mini")
        if hz < 1 or hz > self.capabilities.max_freq_hz:
            raise ValueError(f"frequency {hz} Hz out of range")
        sol = solve_pll(hz)
        if sol is None:
            raise ValueError(f"no valid PLL divider chain for {hz} Hz")
        p = bytearray(19)
        p[0:3] = sol.fin.to_bytes(3, "little")
        p[3:6] = (sol.n3 - 1).to_bytes(3, "little")
        p[6] = sol.n2_hs - 4
        p[7:10] = (sol.n2_ls - 1).to_bytes(3, "little")
        p[10] = sol.n1_hs - 4
        nc1_minus_1 = (sol.nc1_ls - 1).to_bytes(3, "little")
        p[11:14] = nc1_minus_1
        p[14:17] = nc1_minus_1
        p[17] = 0   # SKEW
        p[18] = 9   # BW
        self._send(OPC_MINI_SET_PLL, bytes(p))
