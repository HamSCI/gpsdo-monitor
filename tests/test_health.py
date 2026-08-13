"""Unit tests for the model-agnostic A-level classifier."""
from gpsdo_monitor.health import classify
from gpsdo_monitor.schema import Health, PpsStudy


def _health(**kw) -> Health:
    base = dict(pll_locked=True, outputs_enabled=True, fll_mode=False,
                gps_fix="3D", sats_used=9, fix_age_sec=0.4,
                antenna_ok=True, signal_loss_count=None)
    base.update(kw)
    return Health(**base)


def _pps(**kw) -> PpsStudy:
    base = dict(enabled=True, window_sec=60, edges=60)
    base.update(kw)
    return PpsStudy(**base)


def test_a1_on_nominal():
    lvl, reason = classify(_health(), _pps(), probe_age_sec=1.0,
                           probe_interval_sec=10, pps_expected=True)
    assert lvl == "A1"
    assert "pll_locked" in reason and "pps_present" in reason


def test_a0_on_pll_unlocked():
    lvl, reason = classify(_health(pll_locked=False), _pps(),
                           probe_age_sec=1.0, probe_interval_sec=10, pps_expected=True)
    assert lvl == "A0"
    assert "pll_unlocked" in reason


def test_a0_on_gps_no_fix():
    lvl, reason = classify(_health(gps_fix="no_fix"), _pps(),
                           probe_age_sec=1.0, probe_interval_sec=10, pps_expected=True)
    assert lvl == "A0"
    assert "gps_fix=no_fix" in reason


def test_a0_on_antenna_fault():
    lvl, reason = classify(_health(antenna_ok=False), _pps(),
                           probe_age_sec=1.0, probe_interval_sec=10, pps_expected=True)
    assert lvl == "A0"
    assert "antenna_fault" in reason


def test_a1_on_mini_no_antenna_indicator():
    """Mini has no antenna_ok flag — None must not trigger A0."""
    lvl, _ = classify(_health(antenna_ok=None), _pps(enabled=False),
                      probe_age_sec=1.0, probe_interval_sec=10, pps_expected=False)
    assert lvl == "A1"


def test_a0_on_probe_stale():
    lvl, reason = classify(_health(), _pps(),
                           probe_age_sec=25.0, probe_interval_sec=10, pps_expected=True)
    assert lvl == "A0"
    assert "probe_stale" in reason


def test_a0_on_pps_silent():
    lvl, reason = classify(_health(), _pps(edges=3),
                           probe_age_sec=1.0, probe_interval_sec=10, pps_expected=True)
    assert lvl == "A0"
    assert "pps_silent" in reason


def test_pps_silent_ignored_when_not_expected():
    """Variants with no PPS (e.g. LBE-1420, Mini) must not downgrade."""
    lvl, _ = classify(_health(), _pps(enabled=False, edges=0),
                      probe_age_sec=1.0, probe_interval_sec=10, pps_expected=False)
    assert lvl == "A1"


def test_gps_locked_is_sufficient_when_nmea_unavailable():
    """LBE-1420 has no CDC NMEA, so gps_fix is always None. The HID
    status bitmap's GPS_LOCK bit is the only signal we can use."""
    lvl, reason = classify(
        _health(gps_fix=None, sats_used=None, fix_age_sec=None,
                gps_locked=True),
        _pps(enabled=False),
        probe_age_sec=1.0, probe_interval_sec=10, pps_expected=False,
    )
    assert lvl == "A1"
    assert "gps_locked" in reason


def test_no_gps_fix_and_no_gps_lock_is_a0():
    lvl, reason = classify(
        _health(gps_fix=None, gps_locked=False),
        _pps(enabled=False),
        probe_age_sec=1.0, probe_interval_sec=10, pps_expected=False,
    )
    assert lvl == "A0"
    assert "gps_fix=none" in reason


def test_gps_fix_primary_wins_over_gps_locked():
    """When NMEA is giving us "3D" the reason string uses it (tighter
    signal) rather than falling back to the HID bit."""
    lvl, reason = classify(
        _health(gps_fix="3D", gps_locked=True),
        _pps(), probe_age_sec=1.0, probe_interval_sec=10, pps_expected=True,
    )
    assert lvl == "A1"
    assert "gps_fix=3D" in reason


def test_antenna_fault_reason_names_open_when_bias_zero():
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=False, antenna_bias_ma=0)
    level, reason = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert level == "A0"
    assert reason == "antenna_fault (bias=0mA, open?)"


def test_antenna_fault_reason_names_short_when_bias_nonzero():
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=False, antenna_bias_ma=63)
    level, reason = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert level == "A0"
    assert reason == "antenna_fault (bias=63mA, short?)"


def test_antenna_fault_reason_unchanged_without_bias():
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=False)
    level, reason = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert (level, reason) == ("A0", "antenna_fault")


def test_bias_alone_never_downgrades():
    # bias present + antenna_ok True: A-level logic must ignore bias.
    h = Health(pll_locked=True, outputs_enabled=True,
               gps_fix="3D", antenna_ok=True, fix_age_sec=1.0,
               antenna_bias_ma=0)
    level, _ = classify(
        h, PpsStudy(), probe_age_sec=0.0,
        probe_interval_sec=10, pps_expected=False,
    )
    assert level == "A1"
