"""The grid is derived from the position, wherever the position came from.

`gpsdo-monitor status` on an LBE-Mini reported

    "latitude": 37.3743188, "longitude": -122.2226655, "grid": null

while the device held a 3D fix on 12 satellites.  The Mini has no NMEA CDC
stream, so the CLI's `_enrich_with_nmea` -- the only thing that set `grid` on
that path -- returned early, and nothing else filled it.  The `serve` daemon
had its own inline backfill and so did NOT have the bug, which is exactly why
it went unnoticed: the two paths disagreed about the same device.

Sigmond's first-boot wizard consumes this field to place the station, so a
null grid there means an operator typing a locator the hardware is holding
(AI6VN, 2026-10-29).

Both paths now call one function.  `to_maidenhead` CLAMPS out-of-range input
rather than refusing it, so a no-fix device reporting 0.0/0.0 would otherwise
yield "JJ00aa" -- a plausible-looking locator in the Gulf of Guinea.  The
guard belongs here, not in the converter, which has callers that want the
clamp.
"""
import pytest

from gpsdo_monitor.health import fill_grid
from gpsdo_monitor.nmea import to_maidenhead
from gpsdo_monitor.schema import Health


def health(**kw) -> Health:
    base = dict(pll_locked=True, outputs_enabled=True, gps_fix="3D",
                sats_used=12, latitude=37.3743188, longitude=-122.2226655,
                gps_locked=True)
    base.update(kw)
    return Health(**base)


# ── the fault ───────────────────────────────────────────────────────────────

def test_a_mini_with_a_fix_gets_its_grid():
    h = health()
    fill_grid(h)
    assert h.grid == "CM87vi"


def test_an_existing_grid_is_not_overwritten():
    """NMEA models set it themselves; we must not second-guess the device."""
    h = health(grid="EM38ww")
    fill_grid(h)
    assert h.grid == "EM38ww"


def test_no_position_leaves_it_none():
    h = health(latitude=None, longitude=None)
    fill_grid(h)
    assert h.grid is None


# ── a position without a fix is not a location ──────────────────────────────

@pytest.mark.parametrize("fix", ["no_fix", "none", "", None])
def test_no_fix_refuses_to_place_the_station(fix):
    h = health(gps_fix=fix, gps_locked=False)
    fill_grid(h)
    assert h.grid is None, "fix=%r produced %r" % (fix, h.grid)


def test_hid_lock_bit_alone_is_enough():
    """The Mini's HID status bit can report lock before a fix string exists."""
    h = health(gps_fix=None, gps_locked=True)
    fill_grid(h)
    assert h.grid == "CM87vi"


def test_null_island_is_refused():
    """0.0/0.0 is what an unset position looks like; to_maidenhead would
    happily turn it into JJ00aa."""
    h = health(latitude=0.0, longitude=0.0)
    fill_grid(h)
    assert h.grid is None


def test_out_of_range_is_refused_rather_than_clamped():
    for lat, lon in ((91.0, 0.5), (-90.1, 0.5), (12.0, 180.5), (12.0, -180.1)):
        h = health(latitude=lat, longitude=lon)
        fill_grid(h)
        assert h.grid is None, "(%s,%s) -> %r" % (lat, lon, h.grid)


def test_non_numeric_is_refused():
    h = health(latitude="37.4", longitude="-122.2")
    fill_grid(h)
    assert h.grid is None


def test_booleans_are_not_coordinates():
    """bool is a subclass of int; True would otherwise pass a numeric check."""
    h = health(latitude=True, longitude=True)
    fill_grid(h)
    assert h.grid is None


# ── agreement + wiring ──────────────────────────────────────────────────────

@pytest.mark.parametrize("lat,lon", [
    (37.3743188, -122.2226655), (38.9, -92.3), (-33.8688, 151.2093),
    (0.0001, 0.0001), (89.9, 179.9), (-89.9, -179.9),
])
def test_agrees_with_to_maidenhead(lat, lon):
    h = health(latitude=lat, longitude=lon)
    fill_grid(h)
    assert h.grid == to_maidenhead(lat, lon)


@pytest.mark.parametrize("name", ["cli.py", "service.py"])
def test_both_paths_call_it(name):
    """A helper nobody calls is a helper that does not exist -- and the whole
    bug was one of two paths not doing this.

    Parsed, not grepped: the first version of this test searched the text for
    "fill_grid" and so was satisfied by the IMPORT line alone.  Deleting the
    call site left it green (caught by mutation, 2026-10-29).
    """
    import ast
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src" / "gpsdo_monitor"
    tree = ast.parse((src / name).read_text())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "fill_grid"]
    assert calls, "%s imports fill_grid but never calls it" % name


def test_service_no_longer_keeps_its_own_copy():
    """Two copies of this rule drifted once already."""
    from pathlib import Path
    src = Path(__file__).resolve().parent.parent / "src" / "gpsdo_monitor"
    text = (src / "service.py").read_text()
    assert "to_maidenhead(raw.health.latitude" not in text
