"""Live Textual TUI for Leo Bodnar GPSDO monitoring.

Launched via `gpsdo-monitor tui [--serial S]`. Follows the ka9q-python
pattern: the client owns its own TUI; sigmond suspends its own app and
shells out to this one.

Design scope (first cut):

- Reads HID feature reports directly from each attached device — same
  code path as `gpsdo-monitor status`, with NMEA and PPS enrichment
  deliberately skipped because a 1-2 s blocking sample on every tick
  would jitter the refresh. The HID bitmap gives us PLL / GPS-lock /
  antenna / outputs / PPS-enabled at microsecond latency, which is
  what the TUI needs.
- Refreshes once per second. `r` forces an immediate re-read; `q`
  quits.
- Per-device open-on-each-tick is fine for <10 devices on one host;
  if the daemon is running it will already hold the HID handle for
  reads, but hidraw allows concurrent readers. If a device can't be
  opened at all we show "error: <reason>" in its row rather than
  crashing the app.

When more is needed (per-satellite view, live PPS edges), spawn a
dedicated screen that owns a worker thread — same pattern as the
ka9q-python status worker.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Header, Static

from gpsdo_monitor import __version__
from gpsdo_monitor.hid_xport import HidCandidate, enumerate_lbe
from gpsdo_monitor.models import open_model

log = logging.getLogger(__name__)


@dataclass
class _Row:
    """One device's snapshot — what the DataTable renders."""

    model: str
    serial: str
    a_level: str        # "A1" | "A0" | "—" (couldn't read)
    pll: str            # "✓" | "✗" | "—"
    gps: str
    ant: str
    out1: str           # frequency pretty-printed or "—"
    out2: str
    pps: str
    clk: str
    error: str = ""


class GpsdoMonitorApp(App):
    """Single-screen live view of all attached LBE devices."""

    CSS = """
    DataTable { height: auto; }
    #footer-note { color: $text-muted; padding: 0 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_now", "Refresh"),
    ]

    def __init__(
        self,
        *,
        serial: Optional[str] = None,
        refresh_sec: float = 1.0,
    ) -> None:
        super().__init__()
        self._serial_filter = (serial or "").lower() or None
        self._refresh_sec = float(refresh_sec)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            table = DataTable(id="devices", cursor_type="row")
            table.add_columns(
                "Model", "Serial", "A",
                "PLL", "GPS", "Ant",
                "Out1", "Out2", "PPS", "Clk",
                "Note",
            )
            yield table
            yield Static(
                "HID feature-report snapshot (no NMEA / PPS edges — "
                "run `gpsdo-monitor status` for full detail). Clk = u-blox self-reported bias/drift.",
                id="footer-note",
            )
        yield Footer()

    def on_mount(self) -> None:
        self.title = f"gpsdo-monitor {__version__}"
        if self._serial_filter:
            self.sub_title = f"filter: serial={self._serial_filter}"
        self._populate()
        self.set_interval(self._refresh_sec, self._populate)

    # --- actions -------------------------------------------------------

    def action_refresh_now(self) -> None:
        self._populate()

    # --- data path -----------------------------------------------------

    def _populate(self) -> None:
        rows = [self._read_one(c) for c in self._candidates()]
        table = self.query_one("#devices", DataTable)
        table.clear()
        if not rows:
            note = (
                f"No Leo Bodnar devices matching serial={self._serial_filter!r}"
                if self._serial_filter
                else "No Leo Bodnar devices attached"
            )
            table.add_row("—", "—", "—", "—", "—", "—", "—", "—", "—", "—", note)
            return
        for r in rows:
            table.add_row(
                r.model, r.serial, r.a_level,
                r.pll, r.gps, r.ant,
                r.out1, r.out2, r.pps, r.clk,
                r.error,
            )

    def _candidates(self) -> list[HidCandidate]:
        present = enumerate_lbe()
        if self._serial_filter:
            present = [c for c in present if c.serial.lower() == self._serial_filter]
        return present

    def _read_one(self, candidate: HidCandidate) -> _Row:
        try:
            with open_model(candidate) as model:
                raw = model.get_status()
        except OSError as e:
            return _Row(
                model=candidate.model, serial=candidate.serial or "?",
                a_level="—", pll="—", gps="—", ant="—",
                out1="—", out2="—", pps="—", clk="—",
                error=f"open failed: {e}",
            )
        except NotImplementedError as e:
            return _Row(
                model=candidate.model, serial=candidate.serial or "?",
                a_level="—", pll="—", gps="—", ant="—",
                out1="—", out2="—", pps="—", clk="—",
                error=str(e),
            )
        h = raw.health
        o = raw.outputs
        pll_ok = bool(h.pll_locked)
        gps_ok = bool(h.gps_locked)
        ant_ok = h.antenna_ok
        outputs_ok = bool(h.outputs_enabled)
        return _Row(
            model=candidate.model,
            serial=candidate.serial or "?",
            a_level=_classify_a_level(pll_ok, gps_ok, ant_ok, outputs_ok),
            pll=_tick(pll_ok),
            gps=_tick(gps_ok),
            ant=_tick(ant_ok) if ant_ok is not None else "n/a",
            out1=_fmt_hz(o.out1_hz),
            out2=_fmt_hz(o.out2_hz),
            pps=_tick(o.pps_enabled) if o.pps_enabled is not None else "n/a",
            clk=_fmt_nav_clock(raw.extras.get("nav_clock")),
        )


# --- rendering helpers (kept free of Textual so they're easy to test) -


def _tick(flag: Optional[bool]) -> str:
    if flag is True:
        return "[green]✓[/]"
    if flag is False:
        return "[red]✗[/]"
    return "—"


def _fmt_hz(hz: Optional[int]) -> str:
    if hz is None:
        return "—"
    if hz >= 1_000_000:
        return f"{hz / 1_000_000:g} MHz"
    if hz >= 1_000:
        return f"{hz / 1_000:g} kHz"
    return f"{hz} Hz"


def _fmt_nav_clock(nc: object) -> str:
    """Compact NAV-CLOCK cell: receiver clock bias and drift.

    nc is a ubx.NavClock or None; drift in ns/s is numerically ppb."""
    if nc is None:
        return "—"
    return f"{nc.clk_bias_ns}ns {nc.clk_drift_ns_s:+d}ppb"


def _classify_a_level(
    pll_ok: bool, gps_ok: bool,
    ant_ok: Optional[bool], outputs_ok: bool,
) -> str:
    """Coarse HID-only A-level. Real a_level_hint classification lives
    in health.classify() and needs NMEA fix + fix_age + pps_study; this
    is a live-view approximation the operator can eyeball."""
    if not (pll_ok and gps_ok and outputs_ok):
        return "[red]A0[/]"
    if ant_ok is False:
        return "[red]A0[/]"
    return "[green]A1[/]"


# --- entry point ----------------------------------------------------------


def run_tui(
    *,
    serial: Optional[str] = None,
    refresh_sec: float = 1.0,
) -> int:
    GpsdoMonitorApp(serial=serial, refresh_sec=refresh_sec).run()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gpsdo-monitor tui",
                                description="Live TUI for attached Leo Bodnar GPSDOs.")
    p.add_argument("--serial", help="Restrict the view to one device by serial.")
    p.add_argument("--refresh-sec", type=float, default=1.0,
                   help="Refresh cadence in seconds (default 1.0)")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    return run_tui(serial=args.serial, refresh_sec=args.refresh_sec)


if __name__ == "__main__":
    sys.exit(main())
