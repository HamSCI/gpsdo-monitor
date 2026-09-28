"""Long-running probe daemon.

One `DeviceWorker` per matched device owns:

  - the HID path (opened per-tick, consistent with the one-shot CLI);
  - a long-lived `NmeaReader` thread on the CDC port (1421/1423 only),
    so per-tick NMEA snapshots are non-blocking;
  - a `PpsTracker` thread on the CDC DCD line (1421/1423 only), which
    uses TIOCMIWAIT so idle CPU stays flat between edges;
  - a cache of the UBX-MON-VER firmware answer (Mini only), since that
    poll takes seconds and never changes after the first success;
  - on the Mini, ONE long-lived HID handle whose reader thread owns the
    interrupt-IN stream (see LbeMini.start_reader), so NAV-PVT and
    NAV-CLOCK are read continuously rather than 3 s out of every 10 s.

Each probe tick the `Service` calls `worker.build_report(host)`, which
assembles a schema-v1 `DeviceReport` from the HID bitmap, the NMEA
snapshot, the PPS rolling window, and the cached firmware, then writes
`/run/gpsdo/<serial>.json` atomically. `index.json` follows with the
aggregate list for fast TUI consumption.

mDNS advertisements are refreshed on every tick and withdrawn when
`match()` no longer sees the device.
"""
from __future__ import annotations

import dataclasses
import logging
import signal
import socket
import threading
import time
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gpsdo_monitor import SCHEMA_VERSION
from gpsdo_monitor.advisories import lookup_protver
from gpsdo_monitor.chrony_shm import ChronyShmFeed
from gpsdo_monitor.config import Config, DeclaredDevice
from gpsdo_monitor.discovery import DiscoveryResult, match
from gpsdo_monitor.health import classify
from gpsdo_monitor.hid_xport import HidCandidate
from gpsdo_monitor.models import REGISTRY, open_model
from gpsdo_monitor.models.lbe_mini import LbeMini
from gpsdo_monitor.nmea import (NmeaReader, find_ttys_by_usb_serial,
                                to_maidenhead)
from gpsdo_monitor.pps import PpsTracker
from gpsdo_monitor.publish import Advertiser
from gpsdo_monitor.schema import (
    Device,
    DeviceReport,
    FirmwareAdvisory,
    IndexEntry,
    IndexFile,
    NavClockReport,
    PpsStudy,
    ReceiverConfig,
    atomic_write,
    new_report,
    utc_now_iso,
)

log = logging.getLogger("gpsdo_monitor.service")


def _sanitize(serial: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in serial) or "unknown"


# --- Per-device worker --------------------------------------------------


@dataclass
class DeviceWorker:
    """Owns the long-lived threads and caches for one physical device.

    Stateless CLI code paths (the `status` command) don't need this
    class — they read everything at once. The daemon does, because
    opening and closing an NMEA tty every 10 s hides freshly-lost fixes
    and the PPS edge window would never fill."""

    candidate: HidCandidate
    declared: DeclaredDevice
    cfg: Config
    nmea: Optional[NmeaReader] = None
    pps: Optional[PpsTracker] = None
    tty_path: Optional[Path] = None
    firmware: Optional[str] = None
    firmware_source: str = "unavailable"
    firmware_advisory: Optional[FirmwareAdvisory] = None
    mon_ver_tried: bool = False
    started_mono: float = 0.0
    # Last full DeviceReport from build_report() — cached so the fast
    # NMEA-only republish path can overlay just the NMEA-derived Health
    # fields without re-polling HID (which is hundreds of ms per call).
    last_report: Optional[DeviceReport] = None
    # LBE-Mini: the device's one long-lived model, whose reader thread owns
    # the HID stream.  None on every other model, and on a Mini whose open
    # failed (ticks then fall back to opening per probe, as before).
    mini: Optional[LbeMini] = None
    # Set when a Mini open produced something other than an LbeMini (only
    # test doubles do), so ticks stop retrying the continuous reader.
    ubx_reader_unsupported: bool = False
    # Last open attempt failed; later failures log at debug, not warning.
    ubx_open_failed: bool = False
    # Shared across every worker (set by Service.start() when
    # cfg.chrony_shm_unit is configured); None when the feed is off.
    # Only the first Mini to claim it gets wired -- see
    # _start_ubx_reader.
    chrony_feed: Optional[ChronyShmFeed] = None
    # One log line, not one per tick, when a second Mini finds the feed
    # already claimed.
    _chrony_shm_extra_logged: bool = False

    # --- lifecycle ---------------------------------------------------

    def start(self) -> None:
        self.started_mono = time.monotonic()
        self._assert_drive()
        # Before the tty lookup below: the Mini presents no tty, so the
        # early returns there would skip it.
        self._start_ubx_reader()
        if not self.candidate.serial:
            log.warning("device at %s has no USB serial — NMEA/PPS skipped",
                        self.candidate.path)
            return
        ttys = find_ttys_by_usb_serial(self.candidate.serial)
        if not ttys:
            return
        self.tty_path = ttys[0]

        # NMEA is the right default for any CDC-capable device; if the
        # driver says has_nmea_cdc the tty will carry $G sentences.
        # We still start the reader speculatively — if the port refuses
        # to open (permissions, contention) the reader records
        # `open_error` and returns; snapshot keeps returning empty
        # state, and classify() falls back to gps_locked from the HID
        # bitmap. That's the correct degradation.
        self.nmea = NmeaReader(self.tty_path)
        self.nmea.start()

        if self.cfg.pps_study_enabled:
            self.pps = PpsTracker(window_sec=60)
            try:
                self.pps.start(self.tty_path)
            except OSError as e:
                log.warning("PPS tracker on %s failed: %s", self.tty_path, e)
                self.pps = None

    def _assert_drive(self) -> None:
        """Restore OUT1 drive strength on attach, if this model has the control.

        ⛔ AC0G-ND, 2026-09-03.  Its LBE-Mini sat at 8 mA — the floor of the
        Mini's 8/16/24/32 ladder — and at that level the GPSDO's 27 MHz did NOT
        take over the RX888's reference.  The board ran on its own oscillator
        ~350 ppm fast; hf-timestd's FUSE derives from those samples and
        inherited the error; chrony followed FUSE and walked the host clock
        TWELVE SECONDS off UTC.  Every RTP label drifted with it and the station
        decoded nothing for a day, while the GPSDO reported pll_locked, 3D fix,
        17 satellites and out1_hz 27000000 throughout.  Raising the drive to
        32 mA took radiod's measured sample rate from +276..+400 ppm to a
        ±20 ppm scatter — governed.

        Run on every ATTACH, not once, and deliberately so: the Mini's
        SET_DRIVE opcode documents no flash persistence (unlike `set_frequency`),
        so we cannot know whether the value survives a power cycle.  Reasserting
        makes the question moot — and the log line below is the experiment that
        answers it, since a volatile device will announce a correction after
        every power-up.

        32 mA is the Mini's OWN default, so this restores a default rather than
        imposing a preference.  `min_drive_ma = 0` disables it.
        """
        want = int(getattr(self.cfg, "min_drive_ma", 0) or 0)
        if want <= 0:
            return
        try:
            with open_model(self.candidate) as model:
                if not getattr(model.capabilities, "has_drive_ma", False):
                    return
                have = model.get_status().outputs.drive_ma
                if have is None or have >= want:
                    return
                model.set_drive_ma(want)
                log.warning(
                    "%s %s: OUT1 drive was %d mA, restored to %d mA. A drive "
                    "too low does not take over the SDR's reference input: the "
                    "board keeps running on its own oscillator while this GPSDO "
                    "reports itself locked. If you see this after every power "
                    "cycle, the setting is volatile and wants a durable fix.",
                    self.candidate.model, self.candidate.serial, have, want)
        except (OSError, ValueError) as e:
            # Never let this stop the worker starting — monitoring a device we
            # could not adjust is strictly better than not monitoring it.
            log.warning("%s %s: could not assert OUT1 drive: %s",
                        self.candidate.model, self.candidate.serial, e)

    def _start_ubx_reader(self) -> None:
        """Open a Mini once and start its reader thread.

        Runs after _assert_drive, which opens and closes its own handle, so
        the two never hold the device at the same time."""
        if not self._wants_ubx_reader():
            return
        try:
            model = open_model(self.candidate)
        except (OSError, ValueError) as e:
            # Transition only: a device that stays gone would otherwise log
            # this every probe tick.
            (log.debug if self.ubx_open_failed else log.warning)(
                "%s %s: could not open for continuous read (%s); "
                "falling back to a sample per probe",
                self.candidate.model, self.candidate.serial, e)
            self.ubx_open_failed = True
            return
        if not isinstance(model, LbeMini):
            self.ubx_reader_unsupported = True
            close = getattr(model, "close", None)
            if close is not None:
                close()
            return
        if self.ubx_open_failed:
            log.info("%s %s: reopened for continuous read",
                     self.candidate.model, self.candidate.serial)
        self.ubx_open_failed = False
        model.start_reader()
        self.mini = model
        self._wire_chrony_shm(model)

    def _wire_chrony_shm(self, model: LbeMini) -> None:
        """Attach the shared chrony SHM feed's hook to `model`, if this
        worker is the one Mini allowed to feed it (Task 2 of the
        mini-nav-pvt-latency plan: one unit, one writer).  A device that
        loses the claim keeps monitoring normally -- it just never
        publishes to that SHM segment."""
        feed = self.chrony_feed
        if feed is None:
            return
        key = self.candidate.serial or self.candidate.path.decode(errors="replace")
        if feed.claim(key):
            model.on_nav_pvt = feed.on_nav_pvt
        elif not self._chrony_shm_extra_logged:
            log.warning(
                "%s %s: chrony SHM feed (unit %d) already claimed by %s; "
                "this device's NAV-PVT will not be written to it",
                self.candidate.model, self.candidate.serial,
                feed.unit, feed.claimed_by)
            self._chrony_shm_extra_logged = True

    def _wants_ubx_reader(self) -> bool:
        if self.ubx_reader_unsupported:
            return False
        cls = REGISTRY.get(self.candidate.pid)
        return cls is not None and bool(cls.capabilities.has_ubx_hid)

    def _drop_mini(self, why: str) -> None:
        """Close the Mini's long-lived handle; the next tick reopens it.

        A USB reset shorter than a probe tick keeps the worker (it is keyed
        by serial) but kills the fd, and the reader can never recover on a
        dead fd.  Dropping it lets the next tick open the device afresh,
        which is what the per-tick open used to do by construction."""
        m = self.mini
        if m is None:
            return
        self.mini = None
        log.warning("%s %s: closing the HID handle (%s); the next probe "
                    "reopens the device", self.candidate.model,
                    self.candidate.serial, why)
        try:
            m.close()
        except OSError as e:
            log.debug("closing a dead Mini handle: %s", e)

    def stop(self) -> None:
        m, self.mini = self.mini, None
        if m is not None:
            m.close()      # stops and joins the reader first
        if self.nmea is not None:
            self.nmea.stop()
            self.nmea = None
        if self.pps is not None:
            self.pps.stop()
            self.pps = None

    # --- per-tick data --------------------------------------------------

    def build_report(self, *, host: str, now: float) -> DeviceReport:
        m = self.mini
        if m is not None and m.reader_failed:
            self._drop_mini("the reader gave up on a failing handle")
            m = None
        if m is None and self._wants_ubx_reader():
            self._start_ubx_reader()      # reopen after a drop
            m = self.mini
        # A Mini with a running reader keeps its handle open; everything
        # else opens per tick, as the one-shot CLI does.
        opener = nullcontext(m) if m is not None else open_model(self.candidate)
        with opener as model:
            try:
                raw = model.get_status()
            except OSError as e:
                # Publish nothing for this tick, so the file's written_utc
                # ages and consumers see the fault; reopen next tick.
                if m is not None:
                    self._drop_mini(f"probe failed: {e}")
                raise
            # MON-VER is slow (several hundred ms) and the answer never
            # changes, so we try once and cache. Subsequent ticks reuse
            # the cached string.
            if (not self.mon_ver_tried
                    and model.capabilities.has_ubx_mon_ver):
                self.mon_ver_tried = True
                try:
                    mv = model.read_mon_ver(timeout_sec=5.0)
                except Exception:
                    log.exception("MON-VER poll failed for %s",
                                  self.candidate.serial)
                    mv = None
                if mv is not None:
                    parts = [f"SW={mv.sw_version}", f"HW={mv.hw_version}"]
                    if mv.protver is not None:
                        parts.append(f"PROTVER={mv.protver}")
                    self.firmware = " ".join(parts)
                    self.firmware_source = "ubx-mon-ver"
                    self.firmware_advisory = lookup_protver(mv.protver)

        probe_age = m.feature_age_sec() if m is not None else None
        return self._assemble(raw, host=host, now=now,
                              probe_age_sec=probe_age or 0.0)

    def _assemble(self, raw, *, host: str, now: float,
                  probe_age_sec: float) -> DeviceReport:
        """Turn one RawStatus into the published DeviceReport."""
        # NMEA enrichment: fresh snapshot for the tick.
        #
        # ⛔ Every line here belongs INSIDE the guard.  `altitude_m` sat one
        # level out, so on any device without a tty it dereferenced an
        # unbound `ns` and raised UnboundLocalError on EVERY tick.  The Mini
        # presents no CDC serial at all, so gpsdo-monitor published nothing
        # whatever on AC0G-ND from install onward — /run/gpsdo stayed empty
        # while each failed probe reopened the device, re-binding hid-generic
        # every 10 s.  B4 runs a 142x, which does present a tty, which is why
        # this never showed in production.
        if self.nmea is not None:
            ns = self.nmea.snapshot()
            _nmea_age = ns.fix_age_sec(now=now)
            if _nmea_age is not None:
                raw.health.fix_age_sec = _nmea_age
            # Same rule as the fields below, and for the same reason: a
            # snapshot taken between sentences must not blank a second the
            # model already named from UBX.  No device does both today --
            # the Mini has no tty and the 142x fills this from RMC -- but
            # the unconditional form was one `if` away from the position
            # bug documented immediately below.
            if ns.pps_utc_sec is not None:
                raw.health.pps_utc_sec = ns.pps_utc_sec
                raw.health.nmea_host_monotonic_at_read = (
                    ns.host_monotonic_at_read)
                raw.health.naming_source = getattr(
                    ns, "naming_source", None) or "nmea-rmc"
            # NMEA is the live, per-second view and wins where it HAS an
            # answer — but it must not blank a value the model already read
            # from UBX.  On a device with both, an NMEA snapshot taken
            # between sentences would otherwise erase a good position.
            if ns.gps_fix is not None:
                raw.health.gps_fix = ns.gps_fix
            if ns.sats_used is not None:
                raw.health.sats_used = ns.sats_used
            if ns.latitude is not None:
                raw.health.latitude = ns.latitude
            if ns.longitude is not None:
                raw.health.longitude = ns.longitude
            if ns.altitude_m is not None:
                raw.health.altitude_m = ns.altitude_m
            grid = ns.maidenhead()
            if grid is not None:
                raw.health.grid = grid

        # The grid is what bring-up actually consumes to place the station,
        # so derive it from whatever position arrived — NMEA or UBX.
        if (raw.health.grid is None
                and raw.health.latitude is not None
                and raw.health.longitude is not None):
            raw.health.grid = to_maidenhead(raw.health.latitude,
                                            raw.health.longitude)

        # PPS study: snapshot the rolling window. If tracker isn't
        # running (no CDC, or device config disabled it) fall back to a
        # disabled marker so consumers can tell the difference between
        # "not tracking" and "tracking with zero edges" (which IS a
        # downgrade signal).
        if self.pps is not None:
            pps_study = self.pps.snapshot()
        else:
            pps_study = PpsStudy(enabled=False, window_sec=60)

        nav_clock = None
        nc = raw.extras.get("nav_clock")
        if nc is not None:
            nav_clock = NavClockReport(
                clk_bias_ns=nc.clk_bias_ns,
                clk_drift_ns_s=nc.clk_drift_ns_s,
                t_acc_ns=nc.t_acc_ns,
                f_acc_ps_s=nc.f_acc_ps_s,
                sampled_utc=utc_now_iso(),
            )

        receiver_config = None
        rc = raw.extras.get("receiver_config")
        if rc is not None:
            receiver_config = ReceiverConfig(**rc)

        if self.firmware is not None:
            raw.firmware = self.firmware
            raw.firmware_source = self.firmware_source

        a_level, reason = classify(
            raw.health,
            pps_study,
            probe_age_sec=probe_age_sec,
            probe_interval_sec=self.cfg.probe_interval_sec,
            pps_expected=bool(raw.outputs.pps_enabled),
        )

        device = Device(
            model=self.candidate.model,
            pid=f"{self.candidate.pid:#06x}",
            serial=self.candidate.serial or "unknown",
            hid_path=self.candidate.path.decode(errors="replace"),
            firmware=raw.firmware,
            firmware_source=raw.firmware_source,
            raw_trailing_hex=raw.raw_trailing_hex,
        )

        report = new_report(
            host=host,
            probe_interval_sec=self.cfg.probe_interval_sec,
            device=device,
            governs=list(self.declared.governs),
            health=raw.health,
            outputs=raw.outputs,
            pps_study=pps_study,
            a_level_hint=a_level,
            a_level_reason=reason,
            firmware_advisory=self.firmware_advisory,
            nav_clock=nav_clock,
            receiver_config=receiver_config,
            nav_pvt_rate_hz=raw.extras.get("nav_pvt_rate_hz"),
        )
        self.last_report = report
        return report

    def refresh_fast(self, *, now: float) -> Optional[DeviceReport]:
        """The 1 Hz republish for this device, or None when it has none.

        NMEA devices overlay NMEA on the last full report, exactly as
        before.  A Mini rebuilds the report from its reader's snapshot,
        reusing the feature report the last probe tick read: the stream
        state comes from memory and nothing touches the HID handle.  Both
        need one full report first (cold start returns None)."""
        if self.nmea is not None:
            return self.refresh_nmea_only(now=now)
        # Read each once: a concurrent stop() can clear them.
        m = self.mini
        last = self.last_report
        if m is None or last is None or m.reader_failed:
            return None
        # ⛔ No HID probe for 2 intervals: stop republishing.  Rebuilding
        # from cached state would keep written_utc fresh every second and
        # hide the fault from the staleness gates that consumers apply.
        age = m.feature_age_sec()
        if age is None or age > 2 * self.cfg.probe_interval_sec:
            return None
        raw = m.get_status(reuse_feature=True)
        return self._assemble(raw, host=last.host, now=now, probe_age_sec=age)

    def refresh_nmea_only(self, *, now: float) -> Optional[DeviceReport]:
        """Build a fresh report by overlaying current NMEA state on top
        of the last full report.  Used by the fast-publish loop so the
        ``health.pps_utc_sec`` / ``health.fix_age_sec`` / ``health.gps_fix``
        fields stay fresh at NMEA cadence (~1 Hz) instead of stalling
        between probe ticks.

        Returns ``None`` if no full report has been built yet (cold
        start) or if NMEA isn't running on this device.  Does NOT touch
        HID — the cost is just an NmeaState.snapshot() and a dataclass
        copy.

        Why this exists: hf-timestd's T6 BPSK PPS disambig pairs an
        RTP-derived edge wall-time against NMEA's pps_utc_sec inside a
        ±0.5 s guard.  With the full report written only every
        probe_interval (default 10 s), pps_utc_sec was up to ~10 s
        stale at consume time and the guard rejected the pairing.  See
        project_t5_nmea_probe_race in hf-timestd's memory.
        """
        if self.last_report is None or self.nmea is None:
            return None
        ns = self.nmea.snapshot()
        new_health = dataclasses.replace(
            self.last_report.health,
            gps_fix=ns.gps_fix,
            sats_used=ns.sats_used,
            fix_age_sec=ns.fix_age_sec(now=now),
            pps_utc_sec=ns.pps_utc_sec,
            nmea_host_monotonic_at_read=ns.host_monotonic_at_read,
            latitude=ns.latitude,
            longitude=ns.longitude,
            grid=ns.maidenhead(),
            altitude_m=ns.altitude_m,
        )
        from gpsdo_monitor.schema import utc_now_iso
        new_report = dataclasses.replace(
            self.last_report,
            health=new_health,
            written_utc=utc_now_iso(),
        )
        self.last_report = new_report
        return new_report


# --- Service ------------------------------------------------------------


class Service:
    # Fast-publish cadence.  1 Hz matches the LBE-1421 NMEA emission rate —
    # the JSON file's pps_utc_sec will therefore be at most ~1 s stale, well
    # inside hf-timestd's T6 disambig ±0.5 s pairing guard.  A Mini rides
    # the same loop from its reader's snapshot.
    _FAST_NMEA_INTERVAL_S = 1.0

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stopping = threading.Event()
        self.advertiser: Optional[Advertiser] = None
        # Task 2 (mini-nav-pvt-latency): one shared chrony SHM witness
        # feed for the (at most one, per unit) Mini allowed to write it.
        # None whenever cfg.chrony_shm_unit is unset -- the feed is off
        # by default.
        self.chrony_feed: Optional[ChronyShmFeed] = None
        self._workers: dict[str, DeviceWorker] = {}
        self._last_report_hint: dict[str, str] = {}
        # Last discovery error set, so _tick can log transitions only.
        self._last_errors: tuple[str, ...] = ()
        self._fast_nmea_thread: Optional[threading.Thread] = None

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self.cfg.run_dir.mkdir(parents=True, exist_ok=True)
        if self.cfg.chrony_shm_unit is not None:
            self.chrony_feed = ChronyShmFeed(self.cfg.chrony_shm_unit)
            self.chrony_feed.open()   # logs + disables itself on failure;
                                       # never raises (see ChronyShmFeed.open)
        if self.cfg.mdns_enabled:
            try:
                self.advertiser = Advertiser()
            except Exception:
                log.exception("mDNS advertiser init failed; continuing without it")
                self.advertiser = None
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        # Fast NMEA republish loop — runs in parallel with the main
        # probe tick so per-device JSON files refresh their NMEA fields
        # (pps_utc_sec, fix_age_sec, gps_fix, sats_used) at NMEA cadence.
        self._fast_nmea_thread = threading.Thread(
            target=self._fast_nmea_loop,
            name="gpsdo-fast-nmea-publish",
            daemon=True,
        )
        self._fast_nmea_thread.start()

    def stop(self) -> None:
        self.stopping.set()
        if self._fast_nmea_thread is not None:
            self._fast_nmea_thread.join(timeout=2.0)
            self._fast_nmea_thread = None
        for w in self._workers.values():
            w.stop()
        self._workers.clear()
        if self.advertiser is not None:
            self.advertiser.close()
            self.advertiser = None
        if self.chrony_feed is not None:
            self.chrony_feed.close()
            self.chrony_feed = None

    def _fast_nmea_loop(self) -> None:
        """Background thread that republishes per-device JSON every
        :attr:`_FAST_NMEA_INTERVAL_S`: fresh NMEA fields overlaid on the
        last full report, or a Mini's reader snapshot.  Does not touch HID
        and does not re-advertise mDNS.
        """
        while not self.stopping.is_set():
            if self.stopping.wait(self._FAST_NMEA_INTERVAL_S):
                return
            now = time.time()
            # Snapshot the dict to avoid iteration-during-mutation if
            # _sync_workers fires concurrently.
            for worker in list(self._workers.values()):
                try:
                    report = worker.refresh_fast(now=now)
                except Exception:
                    log.exception("fast republish failed for %s",
                                  self._key(worker.candidate))
                    continue
                if report is not None:
                    self._write_report_file(report)

    def _on_signal(self, *_a: object) -> None:
        log.info("signal received, shutting down")
        self.stopping.set()

    # --- probe loop ----------------------------------------------------

    def run(self) -> int:
        self.start()
        try:
            while not self.stopping.is_set():
                started = time.monotonic()
                try:
                    self._tick()
                except Exception:
                    log.exception("probe tick failed")
                elapsed = time.monotonic() - started
                self.stopping.wait(max(0.0, self.cfg.probe_interval_sec - elapsed))
        finally:
            self.stop()
        return 0

    def _tick(self) -> None:
        result = match(self.cfg.devices)
        # Log discovery errors on CHANGE, not once per tick.  A decoder VM
        # with no GPSDO passed through is the normal state of a fresh
        # install — install.sh enables the unit unconditionally even though
        # the catalog marks it hardware-gated — and this wrote
        # "discovery: no Leo Bodnar devices found" at ERROR every 10 s,
        # 8,640 times a day, into the journal and onto the console.
        # What is worth recording is when the GPSDO went away and when it
        # came back, not a constant assertion that it is absent.
        if tuple(result.errors) != self._last_errors:
            for err in result.errors:
                log.error("discovery: %s", err)
            if not result.errors and self._last_errors:
                log.info("discovery: resolved")
            self._last_errors = tuple(result.errors)
        self._sync_workers(result)
        reports = self._write_reports(result)
        self._write_index(result, reports)
        self._reap_advertisements(result)

    # --- workers -------------------------------------------------------

    def _sync_workers(self, result: DiscoveryResult) -> None:
        """Create workers for newly-appeared devices, drop workers for
        vanished ones."""
        present_by_key = {
            self._key(candidate): (declared, candidate)
            for declared, candidate in result.matched
        }
        # Stop workers whose device vanished.
        for key in list(self._workers.keys()):
            if key not in present_by_key:
                log.info("device %s vanished; stopping worker", key)
                self._workers.pop(key).stop()
        # Start workers for new devices.
        for key, (declared, candidate) in present_by_key.items():
            if key in self._workers:
                w = self._workers[key]
                # Refresh declared config in case governs changed.
                w.declared = declared
                if candidate.path != w.candidate.path:
                    # Same serial, new node: a re-enumeration inside one
                    # tick.  The handle on the old path is dead.
                    log.info("device %s moved %s -> %s", key,
                             w.candidate.path.decode(errors="replace"),
                             candidate.path.decode(errors="replace"))
                    w.candidate = candidate
                    w._drop_mini("device path changed")
                continue
            log.info("device %s %s appeared; starting worker",
                     candidate.model, key)
            w = DeviceWorker(candidate=candidate, declared=declared, cfg=self.cfg,
                             chrony_feed=self.chrony_feed)
            w.start()
            self._workers[key] = w

    @staticmethod
    def _key(candidate: HidCandidate) -> str:
        return candidate.serial or candidate.path.decode(errors="replace")

    # --- reports -------------------------------------------------------

    def _write_reports(self, result: DiscoveryResult) -> dict[str, DeviceReport]:
        host = socket.getfqdn() or socket.gethostname()
        now = time.time()
        out: dict[str, DeviceReport] = {}
        for declared, candidate in result.matched:
            key = self._key(candidate)
            worker = self._workers.get(key)
            if worker is None:
                continue
            try:
                report = worker.build_report(host=host, now=now)
            except NotImplementedError as e:
                log.warning("skip %s: %s", key, e)
                continue
            except Exception:
                log.exception("probe failed for %s", key)
                continue
            self._publish_report(report)
            out[key] = report
        return out

    def _publish_report(self, report: DeviceReport) -> None:
        self._write_report_file(report)
        if self.advertiser is not None:
            try:
                self.advertiser.publish(report, probe_age_sec=0.0)
            except Exception:
                log.exception("mDNS publish failed for %s", report.device.serial)

    def _write_report_file(self, report: DeviceReport) -> None:
        """Atomic JSON write only — no mDNS re-advertisement.
        Used by the fast NMEA republish loop to refresh per-device
        files at ~1 Hz without spamming mDNS announcements."""
        filename = f"{_sanitize(report.device.serial)}.json"
        path = self.cfg.run_dir / filename
        atomic_write(str(path), report.to_json())
        self._last_report_hint[report.device.serial] = report.a_level_hint

    # --- index ---------------------------------------------------------

    def _write_index(
        self,
        result: DiscoveryResult,
        reports: dict[str, DeviceReport],
    ) -> None:
        host = socket.getfqdn() or socket.gethostname()
        entries: list[IndexEntry] = []
        for declared, candidate in result.matched:
            key = self._key(candidate)
            r = reports.get(key)
            entries.append(IndexEntry(
                serial=candidate.serial or "unknown",
                model=candidate.model,
                governs=list(declared.governs),
                a_level_hint=(r.a_level_hint if r is not None
                              else self._last_report_hint.get(candidate.serial, "A0")),
                written_utc=(r.written_utc if r is not None else utc_now_iso()),
            ))
        idx = IndexFile(
            schema=SCHEMA_VERSION,
            written_utc=utc_now_iso(),
            host=host,
            devices=entries,
        )
        atomic_write(str(self.cfg.run_dir / "index.json"), idx.to_json())

    # --- mDNS reaping --------------------------------------------------

    def _reap_advertisements(self, result: DiscoveryResult) -> None:
        if self.advertiser is None:
            return
        present_serials = {c.serial for _, c in result.matched if c.serial}
        for serial in list(self._last_report_hint.keys()):
            if serial not in present_serials:
                log.info("device %s vanished; withdrawing advertisement", serial)
                try:
                    self.advertiser.withdraw(serial)
                except Exception:
                    log.exception("mDNS withdraw failed for %s", serial)
                self._last_report_hint.pop(serial, None)
