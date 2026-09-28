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
