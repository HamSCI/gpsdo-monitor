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
import time
from pathlib import Path

from gpsdo_monitor import __version__
from gpsdo_monitor.config import Config
from gpsdo_monitor.discovery import match
from gpsdo_monitor.health import fill_grid
from gpsdo_monitor.hid_xport import enumerate_lbe, enumerate_unsupported_lbe
from gpsdo_monitor.models import open_model
from gpsdo_monitor.schema import NAV_CLOCK_NOTE, PpsStudy, utc_now_iso


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )


def _cmd_detect(args: argparse.Namespace) -> int:
    present = enumerate_lbe()
    for c in enumerate_unsupported_lbe():
        what = f" ({c.product})" if c.product else ""
        print(f"Leo Bodnar {c.vid:04x}:{c.pid:04x}{what} serial={c.serial!r} "
              "recognised but not supported by gpsdo-monitor", file=sys.stderr)
    if not present:
        print("no supported Leo Bodnar devices found", file=sys.stderr)
    for c in present:
        print(f"{c.model:10s}  pid={c.pid:#06x}  serial={c.serial!r:24s}  path={c.path.decode(errors='replace')}")
    mvrs = _detect_mvrs(getattr(args, "mvr_listen_sec", None))
    return 0 if (present or mvrs) else 1


def _detect_mvrs(listen_sec: float | None) -> int:
    """List RP2040 CDC ttys, confirming each as an MVR by listening (never
    writing) for up to `listen_sec`.  Returns how many were confirmed.
    Costs nothing on a host with no RP2040 attached."""
    from gpsdo_monitor import mvr
    if listen_sec is None:
        listen_sec = mvr.CONFIRM_WINDOW_SEC
    found = 0
    for c in mvr.enumerate_rp2040_ttys():
        head = f"{c.vid:04x}:{c.pid:04x} {c.product or 'RP2040'} serial={c.serial!r} tty={c.tty}"
        if listen_sec <= 0:
            print(f"{head}: not checked for MVR content (--mvr-listen-sec 0)", file=sys.stderr)
            continue
        print(f"{head}: listening up to {listen_sec:.0f} s for MVR output ...", file=sys.stderr)
        try:
            st = mvr.passive_listen(c.tty, window_sec=listen_sec)
        except mvr.PortBusy:
            print(f"{head}: held by another process -- a running gpsdo-monitor "
                  "service publishes it in /run/gpsdo", file=sys.stderr)
            continue
        except OSError as e:
            print(f"{head}: cannot open: {e}", file=sys.stderr)
            continue
        if st.evidence is None:
            print(f"{head}: no MVR output in {listen_sec:.0f} s; not an MVR", file=sys.stderr)
            continue
        found += 1
        fw = f"  firmware={st.firmware}" if st.firmware else ""
        print(f"{mvr.MODEL_NAME}  serial={c.serial!r:24s}  path={c.tty}{fw}")
    return found


def _mvr_service_active() -> bool:
    """True when systemd says gpsdo-monitor.service is running."""
    import subprocess
    try:
        return subprocess.run(
            ["systemctl", "is-active", "--quiet", "gpsdo-monitor.service"],
            check=False, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _cmd_mvr(args: argparse.Namespace) -> int:
    """`mvr probe` / `mvr passthrough on|off [--persist]`: the only code that
    writes to an MVR.  Refuses while the service runs or anyone holds the
    port, listens passively for MVR output before sending a byte, then sends
    only the documented sequence (see mvr.ALLOWED_WRITES)."""
    from gpsdo_monitor import mvr
    if _mvr_service_active():
        print("refusing: the gpsdo-monitor service is running and owns the MVR's "
              "port.  Stop it first (sudo systemctl stop gpsdo-monitor), then "
              "start it again afterwards.", file=sys.stderr)
        return 2
    tty = args.tty
    if tty is None:
        cands = mvr.enumerate_rp2040_ttys()
        if len(cands) != 1:
            which = ", ".join(c.tty for c in cands) or "none"
            print(f"found {len(cands)} RP2040 CDC ttys ({which}); name one with --tty",
                  file=sys.stderr)
            return 2
        tty = cands[0].tty
    try:
        fd, orig = mvr.open_port(tty, write=True)
    except mvr.PortBusy:
        print(f"refusing: {tty} is held by another process (the gpsdo-monitor "
              "service, or another console)", file=sys.stderr)
        return 2
    except OSError as e:
        print(f"{tty}: cannot open: {e}", file=sys.stderr)
        return 1
    confirmed = False
    try:
        print(f"{tty}: listening up to {args.listen_sec:.0f} s for MVR output "
              "before sending anything ...", file=sys.stderr)
        st = mvr.listen_on_fd(fd, window_sec=args.listen_sec)
        if st.evidence is None:
            print(f"{tty}: no MVR output in {args.listen_sec:.0f} s; sent nothing",
                  file=sys.stderr)
            return 1
        confirmed = True
        console = mvr.MvrConsole(mvr.TtyPort(fd), timeout_sec=args.timeout)
        if args.mvr_command == "probe":
            info = console.probe()
            out = {
                "tty": tty, "firmware": info.firmware, "built": info.built,
                "clk0_hz": info.clk_hz[0], "clk1_hz": info.clk_hz[1],
                "clk2_hz": info.clk_hz[2], "drive_ma": info.drive_ma,
                "passthrough": info.passthrough,
                "passthrough_boot_default": info.passthrough_boot,
                "exit": info.exit_line,
            }
        else:
            res = console.set_passthrough(args.state == "on", persist=args.persist)
            out = {
                "tty": tty, "passthrough": res.passthrough,
                "passthrough_boot_default": res.passthrough_boot,
                "changed": res.changed, "exit": res.exit_line,
            }
        print(json.dumps(out, indent=2))
        return 0
    except (mvr.MvrConsoleError, OSError) as e:
        print(f"{tty}: {e}", file=sys.stderr)
        return 1
    finally:
        # A confirmed MVR stays raw: the next naive open must not echo.
        mvr._restore_and_close(fd, None if confirmed else orig)


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
                # ⛔ AFTER every enrichment, and on EVERY model.  The Mini has
                # no NMEA CDC, so _enrich_with_nmea -- which used to be the
                # only thing that set a grid here -- never runs for it, and
                # `status` reported "grid": null beside a valid UBX position.
                fill_grid(raw.health)
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
                "sampled_utc": utc_now_iso(),
                "note": NAV_CLOCK_NOTE,
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
    # A Leo Bodnar with no driver here is NOT "hardware present": sigmond
    # enables gpsdo-monitor on that flag, and it could not drive the device.
    # Named separately so sigmond can say why (KJ6MKI's 1dd2:2210).
    try:
        unsupported = [f"{c.vid:04x}:{c.pid:04x}:{c.serial}"
                       for c in enumerate_unsupported_lbe()]
    except Exception:                                  # noqa: BLE001
        unsupported = []
    payload = {
        "client":           "gpsdo-monitor",
        "version":          __version__,
        "contract_version": "0.8",
        "hardware_present": present,
        "unsupported_present": unsupported,
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


def _cmd_set_drive(args: argparse.Namespace) -> int:
    """Set OUT1 drive strength.

    ⛔ AC0G-ND, 2026-09-03.  Its LBE-Mini sat at 8 mA — the floor of the Mini's
    8/16/24/32 ladder — and at that level the GPSDO's 27 MHz did NOT take over
    the RX888's reference.  The board ran on its own oscillator ~350 ppm fast,
    hf-timestd's FUSE inherited the error, chrony followed FUSE and walked the
    host clock twelve seconds off UTC, and the station decoded nothing for a day
    while every health check read green.  Raising the drive to 32 mA brought
    radiod's measured sample rate from +276..+400 ppm to +2..+50 ppm — locked.

    The write path has existed per model since the driver was written and
    nothing exposed it, so the remedy required driving the library by hand.
    This is that remedy, made supported.

    ⚠ The Mini's SET_DRIVE opcode documents no flash persistence (unlike
    `set_frequency`), so re-check after any power cycle.
    """
    result = match(Config.from_file(Path(args.config) if args.config else None).devices)
    for err in result.errors:
        print(f"error: {err}", file=sys.stderr)
    if not result.matched:
        return 1
    rc = 0
    for _declared, candidate in result.matched:
        if args.serial and candidate.serial != args.serial:
            continue
        # Retry the open: the Mini re-binds hid-generic every ~10 s on some
        # hosts, so a single attempt loses the race.  And ALWAYS use the
        # context manager — a handle left open makes every later open fail
        # with "open failed", which is how this presents when it goes wrong.
        last: Exception | None = None
        for attempt in range(1, 6):
            try:
                with open_model(candidate) as model:
                    if not model.capabilities.has_drive_ma:
                        print(f"{candidate.model} {candidate.serial}: no drive-strength "
                              f"control on this model", file=sys.stderr)
                        rc = 2
                        break
                    before = model.get_status().outputs.drive_ma
                    if before == args.milliamps:
                        print(f"{candidate.model} {candidate.serial}: already "
                              f"{args.milliamps} mA")
                        break
                    model.set_drive_ma(args.milliamps)
                    time.sleep(1.5)
                    after = model.get_status().outputs.drive_ma
                    print(f"{candidate.model} {candidate.serial}: drive "
                          f"{before} mA -> {after} mA")
                    if after != args.milliamps:
                        print(f"  ⚠ readback says {after} mA, wanted "
                              f"{args.milliamps}", file=sys.stderr)
                        rc = 1
                break
            except ValueError as e:
                print(f"{candidate.model} {candidate.serial}: {e}", file=sys.stderr)
                rc = 2
                break
            except OSError as e:
                last = e
                time.sleep(2.0)
        else:
            print(f"{candidate.model} {candidate.serial}: could not open after "
                  f"5 attempts: {last}", file=sys.stderr)
            rc = 1
    return rc


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

    sp = sub.add_parser("detect", help="enumerate attached Leo Bodnar and MVR devices")
    sp.add_argument(
        "--mvr-listen-sec", type=float, default=None,
        help="seconds to listen (read-only) on each RP2040 tty for MVR output "
             "(default 65; 0 skips the check)",
    )
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

    sp = sub.add_parser(
        "set-drive",
        help="set OUT1 drive strength in mA (LBE-Mini: 8/16/24/32)",
        description=(
            "Set OUT1 drive strength.  A GPSDO whose drive is too low does not "
            "take over the SDR's reference input: the board keeps running on its "
            "own oscillator while the GPSDO reports itself locked and healthy.  "
            "AC0G-ND lost a day of decodes to exactly that at 8 mA, the Mini's "
            "floor.  Verify with radiod's \"measured sample rate\", which "
            "scatters within ~20 ppm of nominal once the front end is governed."),
    )
    sp.add_argument("milliamps", type=int,
                    help="drive strength (LBE-Mini accepts 8, 16, 24 or 32)")
    sp.add_argument("--serial", help="restrict to one device by serial")
    sp.set_defaults(func=_cmd_set_drive)

    sp = sub.add_parser(
        "mvr", help="MVR GPSDO B console commands (stop the service first)",
        description=(
            "Explicit commands that WRITE to an MVR GPSDO B's console.  The "
            "service never does; these refuse while it runs.  Each listens "
            "read-only for MVR output first, then sends one CR to wake the "
            "menu and a selection only after the prompt."),
    )
    msub = sp.add_subparsers(dest="mvr_command", required=True)

    def _mvr_common(q: argparse.ArgumentParser) -> None:
        q.add_argument("--tty", help="the MVR's tty (default: the one RP2040 CDC tty)")
        q.add_argument("--listen-sec", type=float, default=65.0,
                       help="seconds to listen for MVR output before sending (default 65)")
        q.add_argument("--timeout", type=float, default=5.0,
                       help="seconds to wait for each menu response (default 5)")

    q = msub.add_parser("probe", help="read firmware, CLK0..2, drive and passthrough "
                                      "from the menu header, then exit the menu")
    _mvr_common(q)
    q.set_defaults(func=_cmd_mvr)
    q = msub.add_parser("passthrough", help="turn NMEA passthrough on or off "
                                            "(position exists only in passthrough)")
    q.add_argument("state", choices=("on", "off"))
    q.add_argument("--persist", action="store_true",
                   help="also make it the power-on default")
    _mvr_common(q)
    q.set_defaults(func=_cmd_mvr)

    sp = sub.add_parser("config", help="configure a device (placeholder; use `smd gpsdo config`)")
    sp.set_defaults(func=_cmd_config)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)
