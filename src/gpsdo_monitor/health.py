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
