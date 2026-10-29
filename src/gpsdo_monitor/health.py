"""A-level classification.

Keep this logic *model-agnostic*: it consumes a normalized `Health`
dataclass and returns (A-level-string, reason-string). Variant-specific
nuances (Mini has no antenna indicator, 1420 has no OUT2/PPS, etc.)
are represented by `None` fields in the input.
"""
from __future__ import annotations

from gpsdo_monitor.schema import Health, PpsStudy

FRESH_FIX_SEC = 30.0
MIN_EDGES_PER_WINDOW = 55  # out of 60 expected in a 60 s window


def classify(
    health: Health,
    pps_study: PpsStudy,
    *,
    probe_age_sec: float,
    probe_interval_sec: int,
    pps_expected: bool,
) -> tuple[str, str]:
    """Return (a_level, reason).

    a_level is "A1" (locked + fresh) or "A0" otherwise. `reason` names
    the first failing predicate so operators can see *why* a downgrade
    fired without trawling journald.
    """
    if not health.pll_locked:
        return "A0", "pll_unlocked"
    # Primary signal is the NMEA fix string (2D/3D). When it isn't
    # available — LBE-1420 has no CDC NMEA stream; the 1421/1423 CDC
    # port may be unreadable due to permissions or contention — we
    # fall back to the HID status bitmap's GPS_LOCK bit. That's a
    # coarser "GPS module reports lock" signal, but it's what the
    # hardware itself reports when the antenna chain is working.
    if health.gps_fix in ("2D", "3D"):
        gps_bits_ok = True
    elif health.gps_locked is True:
        gps_bits_ok = True
    else:
        return "A0", f"gps_fix={health.gps_fix or 'none'}"
    if not gps_bits_ok:
        # unreachable, kept for clarity
        return "A0", "gps_unavailable"
    if health.antenna_ok is False:
        bias = health.antenna_bias_ma
        if bias is None:
            return "A0", "antenna_fault"
        if bias == 0:
            return "A0", "antenna_fault (bias=0mA, open?)"
        # ANT_OK clears on over-current (1425 doc): nonzero bias with
        # the fault bit set points at a short, not a missing antenna.
        return "A0", f"antenna_fault (bias={bias}mA, short?)"
    if health.fix_age_sec is not None and health.fix_age_sec > FRESH_FIX_SEC:
        return "A0", f"fix_age_sec={health.fix_age_sec:.0f}"
    if probe_age_sec > 2 * probe_interval_sec:
        return "A0", f"probe_stale={probe_age_sec:.0f}s"
    if pps_expected and pps_study.enabled and pps_study.edges < MIN_EDGES_PER_WINDOW:
        return "A0", f"pps_silent (edges={pps_study.edges})"

    bits = ["pll_locked"]
    if health.gps_fix in ("2D", "3D"):
        bits.append(f"gps_fix={health.gps_fix}")
    else:
        bits.append("gps_locked")
    if health.antenna_ok:
        bits.append("antenna_ok")
    if pps_expected:
        bits.append("pps_present")
    bits.append("fresh")
    return "A1", " && ".join(bits)


# ── position → grid ─────────────────────────────────────────────────────────
# ⛔ ONE copy of this rule.  `serve` carried its own inline version and the
# one-shot `status` path carried none, so the same device reported a grid from
# the daemon and "grid": null from the CLI -- which is how an LBE-Mini holding
# a 3D fix on 12 satellites came to tell sigmond's first-boot wizard it had no
# position, and an operator typed a locator the hardware was holding
# (AI6VN, 2026-10-29).
#
# ⛔ The guard lives HERE, not in to_maidenhead, which CLAMPS its input by
# design and has callers that want that.  Clamping turns an unset 0.0/0.0 into
# "JJ00aa" -- a locator that looks perfectly real, in the Gulf of Guinea.

_NO_FIX = ("", "no_fix", "none")


def _is_coord(v: object) -> bool:
    """A real number, and not a bool -- bool is a subclass of int."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fill_grid(health: Health) -> None:
    """Derive `health.grid` from the fix position, in place.

    No-op when the device already named a grid (NMEA models do), when there is
    no usable fix behind the coordinates, or when the coordinates cannot be a
    position.  A station placed from a stale or zeroed position is worse than
    one placed by hand, because nothing downstream can tell it was guessed.
    """
    if health.grid:
        return
    lat, lon = health.latitude, health.longitude
    if not (_is_coord(lat) and _is_coord(lon)):
        return
    fixed = bool(health.gps_locked) or (
        isinstance(health.gps_fix, str)
        and health.gps_fix.lower() not in _NO_FIX
    )
    if not fixed:
        return
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return
    if lat == 0.0 and lon == 0.0:
        return
    from gpsdo_monitor.nmea import to_maidenhead
    health.grid = to_maidenhead(lat, lon)
