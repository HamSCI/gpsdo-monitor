"""Config reader for /etc/gpsdo-monitor/config.toml.

Also hosts `DeclaredDevice` so that config parsing doesn't pull the
hidapi system library into import scope — we want `from gpsdo_monitor
import config` to work on hosts that don't have libhidapi installed
(e.g. a consumer running hf-timestd's GpsdoMdnsProbe).
"""
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("/etc/gpsdo-monitor/config.toml")
DEFAULT_RUN_DIR     = Path("/run/gpsdo")
DEFAULT_PROBE_SEC   = 10
# Drive strength the daemon restores on any device that has the control.
# 32 mA is the LBE-Mini's OWN default (the top of its 8/16/24/32 ladder), so
# this restores a default rather than imposing a preference.  0 disables the
# assertion entirely.  See DeviceWorker._assert_drive for why it exists.
DEFAULT_MIN_DRIVE_MA = 32


@dataclass(frozen=True)
class DeclaredDevice:
    """A `[[monitor.device]]` entry from config.toml."""

    serial: str
    governs: tuple[str, ...] = ()

    @property
    def normalized_serial(self) -> str:
        return self.serial.strip().lower()


@dataclass
class Config:
    probe_interval_sec: int = DEFAULT_PROBE_SEC
    run_dir: Path = DEFAULT_RUN_DIR
    pps_study_enabled: bool = True
    mdns_enabled: bool = True
    min_drive_ma: int = DEFAULT_MIN_DRIVE_MA
    devices: list[DeclaredDevice] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: Path | None = None) -> "Config":
        path = path or DEFAULT_CONFIG_PATH
        if not path.exists():
            return cls()
        raw = tomllib.loads(path.read_text())
        mon = raw.get("monitor", {})
        devices = [
            DeclaredDevice(
                serial=d["serial"],
                governs=tuple(d.get("governs", ())),
            )
            for d in mon.get("device", [])
        ]
        return cls(
            probe_interval_sec=int(mon.get("probe_interval_sec", DEFAULT_PROBE_SEC)),
            run_dir=Path(mon.get("run_dir", DEFAULT_RUN_DIR)),
            pps_study_enabled=bool(mon.get("pps_study_enabled", True)),
            mdns_enabled=bool(mon.get("mdns_enabled", True)),
            min_drive_ma=int(mon.get("min_drive_ma", DEFAULT_MIN_DRIVE_MA)),
            devices=devices,
        )
