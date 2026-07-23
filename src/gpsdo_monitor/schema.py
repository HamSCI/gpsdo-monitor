"""Schema v1 dataclasses for the `/run/gpsdo/<serial>.json` contract.

See docs/SCHEMA-v1.md for the authoritative description. All new
fields must be additive within v1; breaking changes require a v2 bump.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from gpsdo_monitor import SCHEMA_VERSION


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


@dataclass
class Device:
    model: str
    pid: str
    serial: str
    hid_path: str
    firmware: str | None = None
    firmware_source: str = "unavailable"  # "ubx-mon-ver" | "unavailable" | "manual"
    raw_trailing_hex: str | None = None


@dataclass
class Health:
    pll_locked: bool
    outputs_enabled: bool
    fll_mode: bool | None = None
    gps_fix: str | None = None          # "no_fix" | "2D" | "3D"
    sats_used: int | None = None
    fix_age_sec: float | None = None
    # Position from the GPS fix (decimal degrees, +N/+E) + Maidenhead grid,
    # captured from NMEA RMC/GGA — lets bring-up auto-derive station location
    # from the GPSDO instead of hand-entering it.  None until a fix is acquired.
    latitude: float | None = None
    longitude: float | None = None
    grid: str | None = None
    antenna_ok: bool | None = None
    signal_loss_count: int | None = None
    gps_locked: bool | None = None      # HID status bit 0: GPS module reports lock
    # Integer UTC second of the latest RMC-valid sentence, paired with
    # time.monotonic() at the moment that sentence was read.  Consumed by
    # hf-timestd's T5 (LB-1421) disambig path — see project_t5_nmea_probe_race.
    pps_utc_sec: int | None = None
    nmea_host_monotonic_at_read: float | None = None


@dataclass
class Outputs:
    out1_hz: int | None = None
    out1_power: str | None = None       # "normal" | "low"
    out2_hz: int | None = None
    out2_power: str | None = None
    pps_enabled: bool | None = None
    drive_ma: int | None = None


@dataclass
class PpsStudy:
    enabled: bool = False
    window_sec: int = 0
    edges: int = 0
    period_ms_p50: float | None = None
    period_ms_p95: float | None = None
    last_edge_utc: str | None = None
    note: str = "OS-millisecond bound; not a metrology reference"


@dataclass
class FirmwareAdvisory:
    status: str                          # "current" | "outdated" | "unknown"
    protver: str | None = None
    notes: str | None = None


@dataclass
class DeviceReport:
    schema: str
    written_utc: str
    probe_interval_sec: int
    host: str
    device: Device
    governs: list[str]
    health: Health
    outputs: Outputs
    pps_study: PpsStudy
    a_level_hint: str                    # "A1" | "A0"
    a_level_reason: str
    firmware_advisory: FirmwareAdvisory | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)


@dataclass
class IndexEntry:
    serial: str
    model: str
    governs: list[str]
    a_level_hint: str
    written_utc: str


@dataclass
class IndexFile:
    schema: str
    written_utc: str
    host: str
    devices: list[IndexEntry] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=False)


def atomic_write(path: str, data: str, *, mode: int = 0o644) -> None:
    """Write `data` to `path` atomically (tmp + rename)."""
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".gpsdo.", dir=directory)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def new_report(
    *,
    host: str,
    probe_interval_sec: int,
    device: Device,
    governs: list[str],
    health: Health,
    outputs: Outputs,
    pps_study: PpsStudy,
    a_level_hint: str,
    a_level_reason: str,
    firmware_advisory: FirmwareAdvisory | None = None,
) -> DeviceReport:
    return DeviceReport(
        schema=SCHEMA_VERSION,
        written_utc=utc_now_iso(),
        probe_interval_sec=probe_interval_sec,
        host=host,
        device=device,
        governs=list(governs),
        health=health,
        outputs=outputs,
        pps_study=pps_study,
        a_level_hint=a_level_hint,
        a_level_reason=a_level_reason,
        firmware_advisory=firmware_advisory,
    )
