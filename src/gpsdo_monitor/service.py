"""Long-running probe daemon.

One `DeviceWorker` per matched device owns:

  - the HID path (opened per-tick, consistent with the one-shot CLI);
  - a long-lived `NmeaReader` thread on the CDC port (1421/1423 only),
    so per-tick NMEA snapshots are non-blocking;
  - a `PpsTracker` thread on the CDC DCD line (1421/1423 only), which
    uses TIOCMIWAIT so idle CPU stays flat between edges;
  - a cache of the UBX-MON-VER firmware answer (Mini only), since that
    poll takes seconds and never changes after the first success.

Each probe tick the `Service` calls `worker.build_report(host)`, which
assembles a schema-v1 `DeviceReport` from the HID bitmap, the NMEA
snapshot, the PPS rolling window, and the cached firmware, then writes
`/run/gpsdo/<serial>.json` atomically. `index.json` follows with the
aggregate list for fast TUI consumption.

mDNS advertisements are refreshed on every tick and withdrawn when
`match()` no longer sees the device.
"""
from __future__ import annotations

import logging
import signal
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from gpsdo_monitor import SCHEMA_VERSION
from gpsdo_monitor.advisories import lookup_protver
from gpsdo_monitor.config import Config, DeclaredDevice
from gpsdo_monitor.discovery import DiscoveryResult, match
from gpsdo_monitor.health import classify
from gpsdo_monitor.hid_xport import HidCandidate
from gpsdo_monitor.models import open_model
from gpsdo_monitor.nmea import NmeaReader, find_ttys_by_usb_serial
from gpsdo_monitor.pps import PpsTracker
from gpsdo_monitor.publish import Advertiser
from gpsdo_monitor.schema import (
    Device,
    DeviceReport,
    FirmwareAdvisory,
    IndexEntry,
    IndexFile,
    PpsStudy,
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

    # --- lifecycle ---------------------------------------------------

    def start(self) -> None:
        self.started_mono = time.monotonic()
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

    def stop(self) -> None:
        if self.nmea is not None:
            self.nmea.stop()
            self.nmea = None
        if self.pps is not None:
            self.pps.stop()
            self.pps = None

    # --- per-tick data --------------------------------------------------

    def build_report(self, *, host: str, now: float) -> DeviceReport:
        with open_model(self.candidate) as model:
            raw = model.get_status()
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

        # NMEA enrichment: fresh snapshot for the tick.
        if self.nmea is not None:
            ns = self.nmea.snapshot()
            raw.health.gps_fix = ns.gps_fix
            raw.health.sats_used = ns.sats_used
            raw.health.fix_age_sec = ns.fix_age_sec(now=now)
            raw.health.pps_utc_sec = ns.pps_utc_sec
            raw.health.nmea_host_monotonic_at_read = ns.host_monotonic_at_read
            raw.health.latitude = ns.latitude
            raw.health.longitude = ns.longitude
            raw.health.grid = ns.maidenhead()
        raw.health.altitude_m = ns.altitude_m

        # PPS study: snapshot the rolling window. If tracker isn't
        # running (no CDC, or device config disabled it) fall back to a
        # disabled marker so consumers can tell the difference between
        # "not tracking" and "tracking with zero edges" (which IS a
        # downgrade signal).
        if self.pps is not None:
            pps_study = self.pps.snapshot()
        else:
            pps_study = PpsStudy(enabled=False, window_sec=60)

        if self.firmware is not None:
            raw.firmware = self.firmware
            raw.firmware_source = self.firmware_source

        probe_age_sec = 0.0   # we just read; age is ~0 by construction

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
        )
        self.last_report = report
        return report

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
        import dataclasses
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
    # Fast-publish cadence for NMEA-only republish.  1 Hz matches the
    # LBE-1421 NMEA emission rate — the JSON file's pps_utc_sec will
    # therefore be at most ~1 s stale, well inside hf-timestd's T6
    # disambig ±0.5 s pairing guard.
    _FAST_NMEA_INTERVAL_S = 1.0

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.stopping = threading.Event()
        self.advertiser: Optional[Advertiser] = None
        self._workers: dict[str, DeviceWorker] = {}
        self._last_report_hint: dict[str, str] = {}
        self._fast_nmea_thread: Optional[threading.Thread] = None

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        self.cfg.run_dir.mkdir(parents=True, exist_ok=True)
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

    def _fast_nmea_loop(self) -> None:
        """Background thread that republishes per-device JSON every
        :attr:`_FAST_NMEA_INTERVAL_S` with fresh NMEA fields overlaid
        on the last full report.  Does not touch HID and does not
        re-advertise mDNS.
        """
        while not self.stopping.is_set():
            if self.stopping.wait(self._FAST_NMEA_INTERVAL_S):
                return
            now = time.time()
            # Snapshot the dict to avoid iteration-during-mutation if
            # _sync_workers fires concurrently.
            for worker in list(self._workers.values()):
                try:
                    report = worker.refresh_nmea_only(now=now)
                except Exception:
                    log.exception("fast NMEA republish failed for %s",
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
        for err in result.errors:
            log.error("discovery: %s", err)
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
                # Refresh declared config in case governs changed.
                self._workers[key].declared = declared
                continue
            log.info("device %s %s appeared; starting worker",
                     candidate.model, key)
            w = DeviceWorker(candidate=candidate, declared=declared, cfg=self.cfg)
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
