"""Secondary-surface CLI.

Primary operator interface is `smd gpsdo ...` in sigmond. This CLI is
for manual debugging, first-time bring-up, and anyone who wants to use
gpsdo-monitor outside a sigmond-managed station.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from gpsdo_monitor import __version__
from gpsdo_monitor.config import Config
from gpsdo_monitor.discovery import match
from gpsdo_monitor.hid_xport import enumerate_lbe
from gpsdo_monitor.models import open_model
from gpsdo_monitor.schema import PpsStudy


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def _cmd_detect(_args: argparse.Namespace) -> int:
    present = enumerate_lbe()
    if not present:
        print("no Leo Bodnar devices found", file=sys.stderr)
        return 1
    for c in present:
        print(f"{c.model:10s}  pid={c.pid:#06x}  serial={c.serial!r:24s}  path={c.path.decode(errors='replace')}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.from_file(Path(args.config) if args.config else None)
    result = match(cfg.devices)
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    if not result.matched:
        return 1
    for declared, candidate in result.matched:
        pps_study = PpsStudy()
        advisory = None
        try:
            with open_model(candidate) as model:
                raw = model.get_status()
                if model.capabilities.has_nmea_cdc and candidate.serial:
                    _enrich_with_nmea(raw, candidate.serial, args.nmea_sample_sec)
                if (model.capabilities.has_pps and candidate.serial
                        and args.pps_sample_sec > 0 and raw.outputs.pps_enabled):
                    pps_study = _sample_pps(candidate.serial, args.pps_sample_sec)
                if model.capabilities.has_ubx_mon_ver:
                    advisory = _enrich_firmware(model, raw)
        except NotImplementedError as e:
            print(f"{candidate.model}  {candidate.serial}: {e}", file=sys.stderr)
            continue
        out = {
            "model": candidate.model,
            "serial": candidate.serial,
            "declared_governs": list(declared.governs),
            "health": raw.health.__dict__,
            "outputs": raw.outputs.__dict__,
            "pps_study": pps_study.__dict__,
            "firmware": raw.firmware,
            "firmware_source": raw.firmware_source,
        }
        if advisory is not None:
            out["firmware_advisory"] = advisory.__dict__
        nc = raw.extras.get("nav_clock")
        if nc is not None:
            out["nav_clock"] = {
                "clk_bias_ns": nc.clk_bias_ns,
                "clk_drift_ns_s": nc.clk_drift_ns_s,
                "t_acc_ns": nc.t_acc_ns,
                "f_acc_ps_s": nc.f_acc_ps_s,
                "note": "u-blox receiver self-report; not an independent measurement",
            }
        rc = raw.extras.get("receiver_config")
        if rc is not None:
            out["receiver_config"] = rc
        print(json.dumps(out, indent=2))
    return 0


def _enrich_with_nmea(raw, serial: str, duration_sec: float) -> None:
    """Fill Health.gps_fix / sats_used / fix_age_sec from a short NMEA sample.

    Silent no-op if we can't find a matching tty or the port refuses to
    open — status should still report HID-derived health in that case."""
    from gpsdo_monitor.nmea import find_ttys_by_usb_serial, sample
    ttys = find_ttys_by_usb_serial(serial)
    if not ttys:
        return
    try:
        st = sample(ttys[0], duration_sec=duration_sec)
    except OSError as e:
        logging.getLogger(__name__).warning("NMEA sample on %s failed: %s", ttys[0], e)
        return
    raw.health.gps_fix = st.gps_fix
    raw.health.sats_used = st.sats_used
    raw.health.fix_age_sec = st.fix_age_sec()
    raw.health.latitude = st.latitude
    raw.health.longitude = st.longitude
    raw.health.grid = st.maidenhead()
    raw.health.altitude_m = st.altitude_m


def _enrich_firmware(model, raw):
    """Try to read the GPS-module firmware via UBX-MON-VER and classify
    it against the advisory table. Returns a FirmwareAdvisory or None
    if the device didn't answer the poll."""
    from gpsdo_monitor.advisories import lookup_protver
    mv = model.read_mon_ver() if hasattr(model, "read_mon_ver") else None
    if mv is None:
        return None
    raw.firmware = f"SW={mv.sw_version} HW={mv.hw_version}" + (
        f" PROTVER={mv.protver}" if mv.protver else ""
    )
    raw.firmware_source = "ubx-mon-ver"
    return lookup_protver(mv.protver)


def _sample_pps(serial: str, duration_sec: float) -> PpsStudy:
    """Sample DCD 1PPS edges for `duration_sec` on the tty owned by
    `serial`. Returns an empty PpsStudy if the tty can't be found or
    opened — the caller still prints HID-derived output fields."""
    from gpsdo_monitor.nmea import find_ttys_by_usb_serial
    from gpsdo_monitor.pps import sample_pps
    ttys = find_ttys_by_usb_serial(serial)
    if not ttys:
        return PpsStudy()
    try:
        return sample_pps(ttys[0], duration_sec=duration_sec)
    except OSError as e:
        logging.getLogger(__name__).warning("PPS sample on %s failed: %s", ttys[0], e)
        return PpsStudy()


def _cmd_inventory(_args: argparse.Namespace) -> int:
    """CONTRACT §3 / sigmond install-orchestration Phase D — minimal
    self-describe so sigmond can learn whether a monitorable GPSDO is
    attached without probing USB itself.  gpsdo-monitor is a v0.4 infra
    client (no per-instance inventory); this reports only the hardware
    dimension.  hardware_present is True when at least one Leo Bodnar HID
    device is enumerable, False when none, null if HID enumeration errors.
    JSON only on stdout (logging goes to stderr)."""
    try:
        present: bool | None = bool(enumerate_lbe())
    except Exception:                                  # noqa: BLE001
        present = None
    payload = {
        "client":           "gpsdo-monitor",
        "version":          __version__,
        "contract_version": "0.8",
        "hardware_present": present,
    }
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from gpsdo_monitor.service import Service
    cfg = Config.from_file(Path(args.config) if args.config else None)
    return Service(cfg).run()


def _cmd_tui(args: argparse.Namespace) -> int:
    try:
        from gpsdo_monitor.tui import run_tui
    except ImportError as exc:
        print(
            f"TUI unavailable: {exc}\n"
            "Install with: pip install 'gpsdo-monitor[tui]'",
            file=sys.stderr,
        )
        return 2
    return run_tui(serial=args.serial, refresh_sec=args.refresh_sec)


def _cmd_config(args: argparse.Namespace) -> int:
    # Placeholder — primary path is `smd gpsdo config`. Keeping this
    # stub so the parser documents the intended surface.
    print("not implemented: use `smd gpsdo config …` as the primary surface,", file=sys.stderr)
    print("or see `gpsdo-monitor config --help` for standalone options.", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gpsdo-monitor",
        description="Leo Bodnar GPSDO health monitor (secondary CLI; primary surface is `smd gpsdo`).",
    )
    p.add_argument("-V", "--version", action="version", version=f"gpsdo-monitor {__version__}")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-c", "--config", help="path to config.toml (default /etc/gpsdo-monitor/config.toml)")

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("detect", help="enumerate attached Leo Bodnar devices")
    sp.set_defaults(func=_cmd_detect)

    sp = sub.add_parser("status", help="one-shot health + output dump, JSON per device")
    sp.add_argument(
        "--nmea-sample-sec", type=float, default=1.5,
        help="seconds to listen on CDC for NMEA before reporting (default 1.5)",
    )
    sp.add_argument(
        "--pps-sample-sec", type=float, default=3.0,
        help="seconds to count DCD 1PPS edges before reporting (default 3.0; 0 disables)",
    )
    sp.set_defaults(func=_cmd_status)

    sp = sub.add_parser("inventory", help="self-describe as JSON (CONTRACT §3 hardware_present)")
    sp.add_argument("--json", action="store_true",
                    help="emit JSON (always JSON; flag accepted for contract symmetry)")
    sp.set_defaults(func=_cmd_inventory)

    sp = sub.add_parser("serve", help="run the long-lived probe daemon (systemd)")
    sp.set_defaults(func=_cmd_serve)

    sp = sub.add_parser("tui", help="launch the Textual live-view TUI ([tui] extra)")
    sp.add_argument("--serial", help="restrict to one device by serial")
    sp.add_argument("--refresh-sec", type=float, default=1.0,
                    help="refresh cadence in seconds (default 1.0)")
    sp.set_defaults(func=_cmd_tui)

    sp = sub.add_parser("config", help="configure a device (placeholder; use `smd gpsdo config`)")
    sp.set_defaults(func=_cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)
