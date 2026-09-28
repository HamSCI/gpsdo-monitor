"""Tests for `scripts/mini_witness_summary.sh` (Task 3 of
mini-nav-pvt-latency): the operator-facing read-out of the Mini's
chrony NTP-SHM witness feed.

The script itself is exercised end to end as a subprocess -- it is a
thin bash wrapper over a stdlib-only Python helper
(`scripts/mini_witness_summary.py`), and the point of these tests is
to prove the WHOLE pipeline (shell wrapper -> Python parser ->
statistics) against a fixture `refclocks.log`, not to unit-test
internals no operator will ever call directly.

Fixtures under `tests/fixtures/`:
  - refclocks_good.log       -- 4 MINI + 4 FUSE samples, two banner
                                 blocks (one mid-file), one real gap.
  - refclocks_bad_layout.log -- a line with the wrong column count,
                                 to prove the parser fails loudly
                                 instead of silently mis-reading it.
  - refclocks_no_mini.log    -- FUSE-only, to prove a missing refid
                                 is reported clearly, not as zeros.
  - refclocks_edge.log       -- pairing edge cases (a MINI sample too
                                 far from any FUSE sample; one exactly
                                 at --max-pair-gap-sec; one whose
                                 nearest FUSE sample precedes it) plus
                                 one filtered-format line (chronyd's
                                 `-` placeholder columns) per refclock,
                                 to prove filtered lines are excluded
                                 from the raw-sample statistics.
  - refclocks_two_mini.log   -- exactly 2 MINI + 2 FUSE rows, for the
                                 short-tau statistic's "only one
                                 first-difference" edge case (I3).
  - refclocks_good.log.1 +
    refclocks_good_current.log -- refclocks_good.log's own 4+4 rows
                                 split across two files at its ~58s
                                 gap, as a real logrotate would, for
                                 the multi-file merge (M4).

Expected statistics below are computed independently with `statistics`
module from the same raw floats the fixture encodes (column 8, "Cooked
offset", per `man chrony.conf(5)` -- see docs/MINI_TIMING_WITNESS.md),
in milliseconds, so a column-index or stdev-formula bug in the script
shows up as a numeric mismatch here rather than being silently
rubber-stamped by hand-checked constants.
"""
from __future__ import annotations

import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "mini_witness_summary.sh"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

# Raw "Cooked offset" column (index 7, 0-based) from refclocks_good.log,
# in seconds, in file order.
_MINI_OFFSETS_S = [1.234e-03, 2.000e-03, -5.000e-04, 3.000e-03]
_FUSE_OFFSETS_S = [9.000e-04, 1.100e-03, 1.000e-03, 1.200e-03]


def _run(fixture_name: str, *extra_args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), str(FIXTURES / fixture_name), *extra_args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _stats_ms(values_s: list[float]) -> dict:
    values_ms = [v * 1000 for v in values_s]
    return {
        "n": len(values_ms),
        "mean_ms": statistics.mean(values_ms),
        "median_ms": statistics.median(values_ms),
        "std_ms": statistics.stdev(values_ms),
        "mad_ms": statistics.median(
            [abs(v - statistics.median(values_ms)) for v in values_ms]
        ),
    }


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), f"{SCRIPT} missing"
    assert SCRIPT.stat().st_mode & 0o111, f"{SCRIPT} is not executable"


def test_good_fixture_mini_statistics_json():
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    expected = _stats_ms(_MINI_OFFSETS_S)
    mini = out["mini"]
    assert mini["n"] == expected["n"]
    assert mini["mean_ms"] == pytest.approx(expected["mean_ms"], abs=1e-9)
    assert mini["median_ms"] == pytest.approx(expected["median_ms"], abs=1e-9)
    assert mini["std_ms"] == pytest.approx(expected["std_ms"], abs=1e-9)
    assert mini["mad_ms"] == pytest.approx(expected["mad_ms"], abs=1e-9)


def test_good_fixture_diff_statistics_json():
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    diffs_s = [m - f for m, f in zip(_MINI_OFFSETS_S, _FUSE_OFFSETS_S)]
    expected = _stats_ms(diffs_s)
    diff = out["diff_mini_minus_fuse"]
    assert diff["n"] == expected["n"]
    assert diff["unpaired_mini"] == 0
    assert diff["mean_ms"] == pytest.approx(expected["mean_ms"], abs=1e-9)
    assert diff["median_ms"] == pytest.approx(expected["median_ms"], abs=1e-9)
    assert diff["std_ms"] == pytest.approx(expected["std_ms"], abs=1e-9)
    assert diff["mad_ms"] == pytest.approx(expected["mad_ms"], abs=1e-9)


def test_good_fixture_sample_rate_and_gaps():
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    mini_rate = out["mini_rate"]
    assert mini_rate["n"] == 4
    assert mini_rate["span_sec"] == pytest.approx(90.0, abs=1e-6)
    assert mini_rate["samples_per_hour"] == pytest.approx(160.0, abs=1e-3)

    fuse_rate = out["fuse_rate"]
    assert fuse_rate["n"] == 4
    assert fuse_rate["span_sec"] == pytest.approx(90.005, abs=1e-6)

    # One real gap in each series (~58 s, the fixture's deliberate hole),
    # against the default 40 s gap-threshold -- nothing else in the
    # ~16 s-spaced fixture should cross it.
    assert len(out["mini_gaps"]) == 1
    assert out["mini_gaps"][0]["seconds"] == pytest.approx(57.995, abs=1e-3)
    assert len(out["fuse_gaps"]) == 1
    assert out["fuse_gaps"][0]["seconds"] == pytest.approx(57.995, abs=1e-3)


def test_good_fixture_human_readable_output_has_no_crash_and_mentions_mini():
    proc = _run("refclocks_good.log")
    assert proc.returncode == 0, proc.stderr
    assert "MINI" in proc.stdout
    assert "FUSE" in proc.stdout


def test_banner_and_header_lines_are_not_mistaken_for_samples():
    # refclocks_good.log carries a "====" + header banner block at the
    # top AND a second one mid-file (chrony reprints it periodically,
    # `logbanner`, default every 32 writes) -- both must be skipped, or
    # `n` for MINI/FUSE would be wrong, or the header text itself
    # (columns split as "Date", "(UTC)", "Time", ...) would trip the
    # bad-layout guard.
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["mini"]["n"] == 4
    assert out["fuse_rate"]["n"] == 4


def test_bad_layout_fails_loudly_not_silently():
    proc = _run("refclocks_bad_layout.log", "--json")
    assert proc.returncode != 0
    assert proc.stdout == ""
    # Fails LOUDLY: says something a human can act on, not a bare traceback.
    assert "refclocks.log" in proc.stderr or "layout" in proc.stderr.lower()


def test_missing_refid_is_reported_not_silently_zero():
    proc = _run("refclocks_no_mini.log", "--json")
    assert proc.returncode != 0
    assert "MINI" in proc.stderr


def test_missing_log_file_reported_clearly():
    proc = _run("does-not-exist.log")
    assert proc.returncode != 0
    assert "does-not-exist.log" in proc.stderr


# --- Fix round 1, item 3: pairing edge cases --------------------------------
#
# refclocks_edge.log (all offsets in the "Cooked offset" column, seconds,
# converted to ms below):
#
#   MINI@00:00:00.000000  1.500e-03   <-> nearest FUSE@00:00:00.000000  1.000e-03   gap 0.000s   PAIRED
#   MINI@00:00:11.000000  5.000e-03   <-> nearest FUSE is 9s away (F@20s) / 11s away (F@0s), both > 8s   UNPAIRED
#   MINI@00:00:28.000000  2.800e-03   <-> nearest FUSE@00:00:20.000000  2.000e-03   gap 8.000s   PAIRED (boundary, FUSE precedes MINI)
#   MINI@00:00:39.000000  3.500e-03   <-> nearest FUSE@00:00:40.000000  3.000e-03   gap 1.000s   PAIRED (FUSE follows MINI)
#
# plus one filtered-format MINI line (99.000ms) and one filtered-format FUSE
# line (-99.000ms) -- wildly-off-scale values that would be obvious if
# wrongly counted.

_EDGE_MINI_RAW_N = 4
_EDGE_FUSE_RAW_N = 3
_EDGE_DIFF_MS = [0.500, 0.800, 0.500]  # M@0s, M@28s, M@39s (M@11s excluded)


def test_edge_fixture_filtered_lines_excluded_from_counts():
    proc = _run("refclocks_edge.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    # 5 MINI lines total in the fixture, one filtered -> 4 raw.
    assert out["mini"]["n"] == _EDGE_MINI_RAW_N
    # 4 FUSE lines total in the fixture, one filtered -> 3 raw.
    assert out["fuse_rate"]["n"] == _EDGE_FUSE_RAW_N
    assert out["mini_filtered_skipped"] == 1
    assert out["fuse_filtered_skipped"] == 1
    # The filtered samples' outlandish offsets (99ms / -99ms) must not
    # leak into the raw mean.
    assert out["mini"]["mean_ms"] < 10.0


def test_edge_fixture_far_sample_is_unpaired_and_excluded_from_diff():
    proc = _run("refclocks_edge.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    diff = out["diff_mini_minus_fuse"]
    assert diff["unpaired_mini"] == 1
    assert diff["n"] == len(_EDGE_DIFF_MS)
    expected = _stats_ms([v / 1000 for v in _EDGE_DIFF_MS])
    assert diff["mean_ms"] == pytest.approx(expected["mean_ms"], abs=1e-9)
    assert diff["median_ms"] == pytest.approx(expected["median_ms"], abs=1e-9)


def test_edge_fixture_boundary_sample_at_exactly_max_gap_is_paired():
    # MINI@00:00:28.000000 sits EXACTLY --max-pair-gap-sec (8.0s, the
    # default) from its nearest FUSE sample (FUSE@00:00:20.000000), and
    # that nearest FUSE sample PRECEDES it (exercises the bisect "i-1"
    # candidate, not just "i"). Mutation: `>` -> `>=` in the pairing gap
    # check would exclude this sample (unpaired_mini would read 2, not 1;
    # diff n would read 2, not 3, and mean/median would shift since
    # 0.800ms -- this sample's diff -- would drop out).
    proc = _run("refclocks_edge.log", "--json", "--max-pair-gap-sec", "8.0")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    diff = out["diff_mini_minus_fuse"]
    assert diff["unpaired_mini"] == 1
    assert diff["n"] == 3
    expected = _stats_ms([v / 1000 for v in _EDGE_DIFF_MS])
    assert diff["mean_ms"] == pytest.approx(expected["mean_ms"], abs=1e-9)


def test_edge_fixture_tighter_pair_gap_excludes_the_boundary_sample():
    # Same fixture, but with a pairing window just under 8.0s: the
    # boundary MINI sample (gap exactly 8.000s) must now fall out too.
    proc = _run("refclocks_edge.log", "--json", "--max-pair-gap-sec", "7.9")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    diff = out["diff_mini_minus_fuse"]
    assert diff["unpaired_mini"] == 2
    assert diff["n"] == 2


# --- I3: short-tau (two-sample / Allan-type) statistic ----------------------
#
# Implemented per the task brief: the standard deviation of first
# differences between consecutive raw samples (gap <= --gap-threshold-sec),
# divided by sqrt(2). For a white-noise process this equals the two-sample
# (Allan) deviation at tau = one message period -- see
# docs/MINI_TIMING_WITNESS.md and the module docstring in
# mini_witness_summary.py for the formula and its citation.
#
# refclocks_good.log's MINI offsets (ms, file order): 1.234, 2.000, -0.500,
# 3.000, at t = 0s, 16.01s, 32.005s, 90.0s. Consecutive gaps: 16.01s,
# 15.995s, 57.995s. The last gap exceeds the default 40s
# --gap-threshold-sec (it's the fixture's deliberate outage), so only the
# first two first-differences are included.

_MINI_TS = [
    datetime(2026, 9, 28, 0, 0, 0, 0, tzinfo=timezone.utc),
    datetime(2026, 9, 28, 0, 0, 16, 10000, tzinfo=timezone.utc),
    datetime(2026, 9, 28, 0, 0, 32, 5000, tzinfo=timezone.utc),
    datetime(2026, 9, 28, 0, 1, 30, 0, tzinfo=timezone.utc),
]


def _expected_short_tau(values_ms: list[float]) -> float:
    diffs = [b - a for a, b in zip(values_ms, values_ms[1:])]
    return statistics.stdev(diffs) / (2 ** 0.5)


def test_good_fixture_mini_short_tau_statistic():
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    mini_ms = [v * 1000.0 for v in _MINI_OFFSETS_S]
    expected_value = _expected_short_tau(mini_ms[:3])  # gap#3 excluded

    st = out["mini_short_tau"]
    assert st["n_diffs"] == 2
    assert st["value_ms"] == pytest.approx(expected_value, abs=1e-9)


def test_good_fixture_diff_short_tau_statistic():
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    diffs_ms = [(m - f) * 1000.0 for m, f in zip(_MINI_OFFSETS_S, _FUSE_OFFSETS_S)]
    expected_value = _expected_short_tau(diffs_ms[:3])  # gap#3 excluded

    st = out["diff_short_tau"]
    assert st["n_diffs"] == 2
    assert st["value_ms"] == pytest.approx(expected_value, abs=1e-9)


def test_short_tau_with_only_one_diff_reports_none_but_counts_it():
    # Two MINI samples 5s apart (well inside the gap threshold) give
    # exactly ONE first-difference -- not enough to take a stdev of, so
    # value_ms must be None even though n_diffs correctly reads 1.
    proc = _run("refclocks_two_mini.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    st = out["mini_short_tau"]
    assert st["n_diffs"] == 1
    assert st["value_ms"] is None


def test_short_tau_reported_in_human_readable_output():
    proc = _run("refclocks_good.log")
    assert proc.returncode == 0, proc.stderr
    assert "short-tau" in proc.stdout


# --- I3: fold by the daemon's own schedule (10s tick / 30s stream refresh) -


def _expected_fold(period_sec: int) -> dict:
    bins: dict[int, list[float]] = {}
    for ts, off_s in zip(_MINI_TS, _MINI_OFFSETS_S):
        b = int(ts.timestamp()) % period_sec
        bins.setdefault(b, []).append(off_s * 1000.0)
    return {
        str(b): {"n": len(v), "mean_ms": statistics.mean(v)}
        for b, v in bins.items()
    }


@pytest.mark.parametrize("period,key", [(10, "mini_fold_10s"), (30, "mini_fold_30s")])
def test_good_fixture_fold_by_schedule_period(period, key):
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)

    expected = _expected_fold(period)
    got = out[key]
    assert set(got.keys()) == set(expected.keys())
    for b in expected:
        assert got[b]["n"] == expected[b]["n"]
        assert got[b]["mean_ms"] == pytest.approx(expected[b]["mean_ms"], abs=1e-9)


# --- M4: multiple log files (e.g. a rotated file + the current one) --------
#
# refclocks_good.log.1 + refclocks_good_current.log together carry exactly
# the same four MINI / four FUSE rows as refclocks_good.log, split at the
# fixture's deliberate ~58s gap -- as a real rotation would.


def _run_multi(*fixture_names: str, json_out: bool = True) -> subprocess.CompletedProcess:
    args = [str(FIXTURES / name) for name in fixture_names]
    if json_out:
        args.append("--json")
    return subprocess.run([str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def test_multiple_logfiles_merge_to_the_same_stats_as_the_single_file():
    single = _run("refclocks_good.log", "--json")
    assert single.returncode == 0, single.stderr
    expected = json.loads(single.stdout)

    for order in (
        ("refclocks_good.log.1", "refclocks_good_current.log"),
        ("refclocks_good_current.log", "refclocks_good.log.1"),
    ):
        proc = _run_multi(*order)
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert out["mini"] == expected["mini"]
        assert out["diff_mini_minus_fuse"] == expected["diff_mini_minus_fuse"]
        assert out["mini_rate"]["n"] == expected["mini_rate"]["n"]
        assert out["mini_gaps"] == expected["mini_gaps"]
        assert out["mini_short_tau"] == expected["mini_short_tau"]
        assert set(out["log_paths"]) == {str(FIXTURES / n) for n in order}


def test_multiple_logfiles_human_readable_output_has_no_crash():
    proc = _run_multi("refclocks_good.log.1", "refclocks_good_current.log", json_out=False)
    assert proc.returncode == 0, proc.stderr
    assert "MINI" in proc.stdout
    assert "FUSE" in proc.stdout


def test_single_logfile_still_works_positionally():
    # Backward compatibility: one positional path, no change in behaviour.
    proc = _run("refclocks_good.log", "--json")
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["log_paths"] == [str(FIXTURES / "refclocks_good.log")]
