#!/usr/bin/env python3
"""Summarize a Mini NAV-PVT chrony witness feed from `refclocks.log`.

This is the Python side of `mini_witness_summary.sh` -- see
`docs/MINI_TIMING_WITNESS.md` for what it measures and why. stdlib
only, per this repo's no-new-dependency rule.

`refclocks.log` column layout (confirmed, not guessed): `man
chrony.conf(5)`, the `log` directive's `refclocks` option, checked
against chrony 4.6.1 (Debian 13 "trixie" package) and cross-checked
against that version's own source (`refclock.c`'s literal log-header
string). Nine whitespace-separated columns per data line:

    1  Date (UTC, YYYY-MM-DD)
    2  Time (UTC, HH:MM:SS.ssssss)
    3  Refid
    4  driver-poll sequence number within the interval (raw samples),
       or "-" for a filtered sample
    5  Leap status: N normal, "+"/"-" for a 61/59 s last minute
    6  PPS-source flag: 1/0, or "-" for a filtered sample
    7  Raw offset measured by the driver, or "-" for a filtered sample
    8  Cooked offset -- local clock error WITH applied corrections.
       Positive means the local (system) clock is slow. This is the
       column read below; it is populated for both raw and filtered
       samples.
    9  Assumed dispersion of the sample

RAW vs FILTERED (chrony 4.6.1's `refclock.c`): a RAW line (column 4 is a
number) is logged once per driver poll -- for the SHM driver, essentially
once per delivered message -- by `RCL_AddSample()`. A FILTERED line
(column 4 is "-") is logged once per refclock `poll` interval by
`poll_timeout()`, from chrony's own filter combining every raw sample
accumulated in that window; it is chrony's belief about the source, not a
record of one message's arrival. Since the question this tool answers is
about one message's arrival, `parse_refclocks_log()` below reads RAW
lines into the statistics and counts (not silently drops) FILTERED ones.

`chronyd` also periodically re-prints a banner (a line of "="
characters, the column-header text, another "=" line) into the same
file -- every `logbanner` writes, default 32. Those lines are skipped,
not data.

If a data line does not have exactly 9 fields, or its first two fields
do not look like a UTC date and time, this refuses to guess: chrony's
column layout has changed or this is not a refclocks.log, and reporting
statistics over misread columns would be worse than reporting nothing.

MULTIPLE LOG FILES (fix round, item M4): more than one path may be
given (e.g. a rotated `refclocks.log.1` plus the current
`refclocks.log`). Every file is parsed the same way, then every
sample -- from every file -- is merged into one time-sorted series per
refid before any statistic is computed. Merging happens by SAMPLE
TIMESTAMP, not by argument or file order, so the two files can be
passed in either order and produce the same result.

SHORT-TAU STATISTIC (fix round, item I3): plain std and MAD, above, are
computed over the WHOLE span of raw offsets, so a slow drift in the
host clock (NTP wander) or in the sample set itself shows up as extra
spread that has nothing to do with message-to-message jitter. The
short-tau statistic answers a narrower question: how much does the
offset move from ONE message to the next? For a white-noise process,
the two-sample (Allan) deviation at tau = one message period equals
the standard deviation of first differences between consecutive
samples, divided by sqrt(2) (see e.g. W.J. Riley, "Handbook of
Frequency Stability Analysis," NIST SP 1065, the standard reference
for this formula). `_short_tau_stat()` implements exactly that:
consecutive raw samples whose time gap is <= `--gap-threshold-sec`
(the same threshold that flags an outage) contribute one first
difference each; the reported value is `stdev(differences) / sqrt(2)`.
It needs at least 2 differences (3 samples) to report a value; fewer
than that reports `n_diffs` honestly but leaves `value_ms` `None`
rather than guessing from too little data.

A result of a few milliseconds here cannot, on its own, tell the Mini's
own timing apart from gpsdo-monitor's stamp noise: Python's GIL switch
interval is 5 ms, and the 10 s probe tick / 30 s stream-refresh both
hold the Mini's HID lock long enough to queue frames behind them. See
docs/MINI_TIMING_WITNESS.md for how to read this number.

FOLD BY SCHEDULE (fix round, item I3): if MINI's offset is secretly
tracking gpsdo-monitor's OWN 10 s probe tick or 30 s stream-refresh --
rather than the Mini's message timing -- that shows up as a mean
offset that depends on `(sample time) mod 10` or `mod 30`, not on
anything upstream of the daemon. `_fold_by_period()` buckets raw MINI
samples by `int(timestamp) % period` and reports the mean offset per
bucket, so a bucket that stands out from the rest is a smoking gun for
a schedule artifact rather than real Mini/host jitter.
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

DEFAULT_LOG_PATH = "/var/log/chrony/refclocks.log"
DEFAULT_MINI_REFID = "MINI"
DEFAULT_FUSE_REFID = "FUSE"
# A raw sample's cadence tracks the writer's own message rate (NAV-PVT,
# ~1 Hz), not the refclock's `poll` setting -- see the module docstring's
# RAW vs FILTERED note. This is a fixed, conservative cap comfortably
# inside one `poll 4` filter window (16 s), not a tuned fraction of it.
DEFAULT_MAX_PAIR_GAP_SEC = 8.0
# A fixed floor meant to catch a real outage, not a multiple of the
# expected per-message spacing.
DEFAULT_GAP_THRESHOLD_SEC = 40.0

_DATE_LEN = 10   # "YYYY-MM-DD"


class LayoutError(RuntimeError):
    """The line did not match the 9-column refclocks.log format."""


class Sample(NamedTuple):
    ts: datetime
    offset_s: float
    is_filtered: bool = False


def _is_banner_line(stripped: str) -> bool:
    if not stripped:
        return True
    if set(stripped) == {"="}:
        return True
    if stripped.startswith("Date (UTC)"):
        return True
    return False


def _parse_line(line: str, line_no: int, path: str) -> tuple[str, Sample] | None:
    """Return (refid, Sample) for a data line, or None for a banner/blank
    line. Raises LayoutError for anything else."""
    stripped = line.strip()
    if _is_banner_line(stripped):
        return None

    fields = stripped.split()
    if len(fields) != 9:
        raise LayoutError(
            f"{path}:{line_no}: expected 9 whitespace-separated columns "
            f"(man chrony.conf(5), 'log' -> 'refclocks'), got {len(fields)}: "
            f"{stripped!r}"
        )

    date_field, time_field, refid = fields[0], fields[1], fields[2]
    driver_poll_seq_field = fields[3]
    cooked_offset_field = fields[7]
    # Column 4: a number on a RAW sample (one driver poll), "-" on a
    # FILTERED sample (chrony's combined estimate over the whole `poll`
    # interval -- see the module docstring). Only raw samples are one
    # message's arrival, which is what this tool measures.
    is_filtered = driver_poll_seq_field == "-"

    try:
        if "." in time_field:
            ts = datetime.strptime(f"{date_field} {time_field}", "%Y-%m-%d %H:%M:%S.%f")
        else:
            ts = datetime.strptime(f"{date_field} {time_field}", "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise LayoutError(
            f"{path}:{line_no}: column 1/2 do not look like a UTC "
            f"'YYYY-MM-DD HH:MM:SS[.ffffff]' timestamp: {stripped!r} ({exc})"
        ) from exc
    ts = ts.replace(tzinfo=timezone.utc)

    if cooked_offset_field == "-":
        # Column 8 is documented as always populated; a bare "-" here
        # means this build's refclocks.log does not carry what we're
        # reading it for. Treat it as a layout mismatch rather than
        # silently dropping the sample.
        raise LayoutError(
            f"{path}:{line_no}: column 8 (cooked offset) is '-', expected "
            f"a number: {stripped!r}"
        )
    try:
        offset_s = float(cooked_offset_field)
    except ValueError as exc:
        raise LayoutError(
            f"{path}:{line_no}: column 8 (cooked offset) is not a number: "
            f"{stripped!r} ({exc})"
        ) from exc

    return refid, Sample(ts=ts, offset_s=offset_s, is_filtered=is_filtered)


class ParsedLog(NamedTuple):
    mini: list[Sample]
    fuse: list[Sample]
    mini_filtered_skipped: int
    fuse_filtered_skipped: int


def parse_refclocks_log(
    paths: list[Path], mini_refid: str, fuse_refid: str
) -> ParsedLog:
    """Parse one or more `refclocks.log`-shaped files and merge their
    samples by TIMESTAMP (not by file order -- see the module
    docstring's "MULTIPLE LOG FILES" note), so a rotated file and the
    current one can be passed in either order."""
    mini: list[Sample] = []
    fuse: list[Sample] = []
    mini_filtered_skipped = 0
    fuse_filtered_skipped = 0
    for path in paths:
        text = path.read_text()
        for line_no, line in enumerate(text.splitlines(), start=1):
            parsed = _parse_line(line, line_no, str(path))
            if parsed is None:
                continue
            refid, sample = parsed
            if refid == mini_refid:
                if sample.is_filtered:
                    mini_filtered_skipped += 1
                else:
                    mini.append(sample)
            elif refid == fuse_refid:
                if sample.is_filtered:
                    fuse_filtered_skipped += 1
                else:
                    fuse.append(sample)
    mini.sort(key=lambda s: s.ts)
    fuse.sort(key=lambda s: s.ts)
    return ParsedLog(mini, fuse, mini_filtered_skipped, fuse_filtered_skipped)


def _stats_ms(offsets_s: list[float]) -> dict:
    if not offsets_s:
        return {"n": 0, "mean_ms": None, "median_ms": None, "std_ms": None, "mad_ms": None}
    values_ms = [v * 1000.0 for v in offsets_s]
    median = statistics.median(values_ms)
    return {
        "n": len(values_ms),
        "mean_ms": statistics.mean(values_ms),
        "median_ms": median,
        "std_ms": statistics.stdev(values_ms) if len(values_ms) >= 2 else None,
        "mad_ms": statistics.median([abs(v - median) for v in values_ms]),
    }


def _rate(samples: list[Sample]) -> dict:
    n = len(samples)
    if n == 0:
        return {"n": 0, "span_sec": None, "samples_per_hour": None}
    span = (samples[-1].ts - samples[0].ts).total_seconds()
    if n < 2 or span <= 0:
        return {"n": n, "span_sec": span, "samples_per_hour": None}
    return {"n": n, "span_sec": span, "samples_per_hour": n / span * 3600.0}


def _gaps(samples: list[Sample], threshold_sec: float) -> list[dict]:
    gaps = []
    for prev, cur in zip(samples, samples[1:]):
        delta = (cur.ts - prev.ts).total_seconds()
        if delta > threshold_sec:
            gaps.append({
                "start": prev.ts.isoformat(),
                "end": cur.ts.isoformat(),
                "seconds": delta,
            })
    return gaps


def _pair_nearest(
    mini: list[Sample], fuse: list[Sample], max_gap_sec: float
) -> tuple[list[tuple[datetime, float]], int]:
    """Pair each MINI sample with its nearest-in-time FUSE sample
    (within `max_gap_sec`) and return `(paired, unpaired_count)`, where
    `paired` is `(mini_timestamp, diff_seconds)` for each successful
    pair, in MINI time order (`mini` is already sorted, so this list
    is too)."""
    if not fuse:
        return [], len(mini)
    fuse_ts = [s.ts for s in fuse]
    paired: list[tuple[datetime, float]] = []
    unpaired = 0
    for m in mini:
        i = bisect_left(fuse_ts, m.ts)
        candidates = [j for j in (i - 1, i) if 0 <= j < len(fuse)]
        if not candidates:
            unpaired += 1
            continue
        best = min(candidates, key=lambda j: abs((fuse_ts[j] - m.ts).total_seconds()))
        gap = abs((fuse_ts[best] - m.ts).total_seconds())
        if gap > max_gap_sec:
            unpaired += 1
            continue
        paired.append((m.ts, m.offset_s - fuse[best].offset_s))
    return paired, unpaired


def _consecutive_diffs_ms(
    ordered: list[tuple[datetime, float]], max_gap_sec: float
) -> list[float]:
    """First differences (in ms) between consecutive `(timestamp,
    value_ms)` pairs whose time gap is <= `max_gap_sec` -- `ordered`
    must already be time-sorted. A pair spanning a real outage (gap >
    `max_gap_sec`) contributes no difference: it isn't "one message
    period apart" in the sense the short-tau statistic needs."""
    diffs: list[float] = []
    for (t0, v0), (t1, v1) in zip(ordered, ordered[1:]):
        if (t1 - t0).total_seconds() <= max_gap_sec:
            diffs.append(v1 - v0)
    return diffs


def _short_tau_stat(diffs_ms: list[float]) -> dict:
    """Two-sample (Allan-type) deviation at tau = one message period,
    from first differences of consecutive raw samples -- see the
    module docstring's "SHORT-TAU STATISTIC" note for the formula and
    its citation. Needs >= 2 differences to take a stdev of; fewer
    reports `n_diffs` honestly and leaves `value_ms` `None`."""
    n = len(diffs_ms)
    value = statistics.stdev(diffs_ms) / math.sqrt(2) if n >= 2 else None
    return {"n_diffs": n, "value_ms": value}


def _fold_by_period(samples: list[Sample], period_sec: int) -> dict[str, dict]:
    """Mean MINI offset (ms) per `int(sample_timestamp) % period_sec`
    bucket -- see the module docstring's "FOLD BY SCHEDULE" note. Keys
    are strings (bucket numbers) so this survives a JSON round trip
    without surprises."""
    bins: dict[int, list[float]] = {}
    for s in samples:
        b = int(s.ts.timestamp()) % period_sec
        bins.setdefault(b, []).append(s.offset_s * 1000.0)
    return {
        str(b): {"n": len(vals), "mean_ms": statistics.mean(vals)}
        for b, vals in sorted(bins.items())
    }


def summarize(
    paths: list[Path],
    mini_refid: str = DEFAULT_MINI_REFID,
    fuse_refid: str = DEFAULT_FUSE_REFID,
    max_pair_gap_sec: float = DEFAULT_MAX_PAIR_GAP_SEC,
    gap_threshold_sec: float = DEFAULT_GAP_THRESHOLD_SEC,
) -> dict:
    parsed = parse_refclocks_log(paths, mini_refid, fuse_refid)
    mini, fuse = parsed.mini, parsed.fuse
    paths_desc = ", ".join(str(p) for p in paths)

    if not mini:
        raise LayoutError(
            f"no {mini_refid!r} rows found in {paths_desc} -- check the "
            f"refid in the drop-in (gpsdo-mini-witness.conf) and that "
            f"chronyd has `log refclocks` active and the feed is actually "
            f"running"
        )
    if not fuse:
        raise LayoutError(
            f"no {fuse_refid!r} rows found in {paths_desc} -- the "
            f"MINI-vs-FUSE comparison needs hf-timestd's FUSE refclock in "
            f"the same log; pass --fuse-refid if this station names it "
            f"differently"
        )

    paired, unpaired = _pair_nearest(mini, fuse, max_pair_gap_sec)
    diffs_s = [d for _, d in paired]
    diff_stats = _stats_ms(diffs_s)
    diff_stats["unpaired_mini"] = unpaired
    diff_stats["max_pair_gap_sec"] = max_pair_gap_sec

    mini_short_tau = _short_tau_stat(_consecutive_diffs_ms(
        [(s.ts, s.offset_s * 1000.0) for s in mini], gap_threshold_sec))
    diff_short_tau = _short_tau_stat(_consecutive_diffs_ms(
        [(t, d * 1000.0) for t, d in paired], gap_threshold_sec))

    return {
        "log_paths": [str(p) for p in paths],
        "mini_refid": mini_refid,
        "fuse_refid": fuse_refid,
        "mini": _stats_ms([s.offset_s for s in mini]),
        "diff_mini_minus_fuse": diff_stats,
        "mini_short_tau": mini_short_tau,
        "diff_short_tau": diff_short_tau,
        "mini_fold_10s": _fold_by_period(mini, 10),
        "mini_fold_30s": _fold_by_period(mini, 30),
        "mini_rate": _rate(mini),
        "fuse_rate": _rate(fuse),
        "mini_filtered_skipped": parsed.mini_filtered_skipped,
        "fuse_filtered_skipped": parsed.fuse_filtered_skipped,
        "gap_threshold_sec": gap_threshold_sec,
        "mini_gaps": _gaps(mini, gap_threshold_sec),
        "fuse_gaps": _gaps(fuse, gap_threshold_sec),
    }


def _fmt(v, unit=""):
    return "n/a" if v is None else f"{v:.4f}{unit}"


def render_text(result: dict) -> str:
    lines = [f"refclocks.log: {', '.join(result['log_paths'])}", ""]
    m = result["mini"]
    lines.append(f"{result['mini_refid']} (n={m['n']}, ms):")
    lines.append(f"  mean   {_fmt(m['mean_ms'])}")
    lines.append(f"  median {_fmt(m['median_ms'])}")
    lines.append(f"  std    {_fmt(m['std_ms'])}")
    lines.append(f"  mad    {_fmt(m['mad_ms'])}")
    lines.append("")

    d = result["diff_mini_minus_fuse"]
    lines.append(
        f"{result['mini_refid']} - {result['fuse_refid']} "
        f"(paired n={d['n']}, unpaired {result['mini_refid']} "
        f"{d['unpaired_mini']}, max pair gap {d['max_pair_gap_sec']}s, ms):"
    )
    lines.append(f"  mean   {_fmt(d['mean_ms'])}")
    lines.append(f"  median {_fmt(d['median_ms'])}")
    lines.append(f"  std    {_fmt(d['std_ms'])}")
    lines.append(f"  mad    {_fmt(d['mad_ms'])}")
    lines.append("")

    st = result["mini_short_tau"]
    dst = result["diff_short_tau"]
    lines.append(
        f"{result['mini_refid']} short-tau (tau = one message period, "
        f"stdev(first differences)/sqrt(2), n diffs={st['n_diffs']}, "
        f"gap <= {result['gap_threshold_sec']}s): {_fmt(st['value_ms'])} ms"
    )
    lines.append(
        f"{result['mini_refid']} - {result['fuse_refid']} short-tau "
        f"(n diffs={dst['n_diffs']}): {_fmt(dst['value_ms'])} ms"
    )
    lines.append("")

    for period, key in ((10, "mini_fold_10s"), (30, "mini_fold_30s")):
        fold = result[key]
        if fold:
            means = [b["mean_ms"] for b in fold.values()]
            spread = max(means) - min(means)
            lines.append(
                f"{result['mini_refid']} folded by (t mod {period}s): "
                f"{len(fold)} bins, spread {spread:.4f}ms "
                f"(--json for the full per-bin table)"
            )
        else:
            lines.append(f"{result['mini_refid']} folded by (t mod {period}s): no samples")
    lines.append("")

    mr, fr = result["mini_rate"], result["fuse_rate"]
    def _rate_str(tag, r):
        if r["samples_per_hour"] is None:
            return f"{tag} {r['n']} samples"
        return f"{tag} {r['n']} samples over {r['span_sec']:.1f}s ({r['samples_per_hour']:.1f}/hr)"
    lines.append(f"sample rate: {_rate_str(result['mini_refid'], mr)}; {_rate_str(result['fuse_refid'], fr)}")
    lines.append(
        f"filtered lines skipped (not raw, not counted above): "
        f"{result['mini_refid']} {result['mini_filtered_skipped']}; "
        f"{result['fuse_refid']} {result['fuse_filtered_skipped']}"
    )

    gt = result["gap_threshold_sec"]
    for tag, gaps in ((result["mini_refid"], result["mini_gaps"]), (result["fuse_refid"], result["fuse_gaps"])):
        if gaps:
            lines.append(f"{tag} gaps (> {gt}s): " + "; ".join(f"{g['seconds']:.1f}s at {g['start']} -> {g['end']}" for g in gaps))
        else:
            lines.append(f"{tag} gaps (> {gt}s): none")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "logfiles", nargs="*", default=[DEFAULT_LOG_PATH],
        help=(
            "path(s) to refclocks.log (default: "
            f"{DEFAULT_LOG_PATH}). More than one merges by sample "
            "timestamp, e.g. a rotated refclocks.log.1 plus the "
            "current refclocks.log, in either order."
        ))
    parser.add_argument("--mini-refid", default=DEFAULT_MINI_REFID)
    parser.add_argument("--fuse-refid", default=DEFAULT_FUSE_REFID)
    parser.add_argument("--max-pair-gap-sec", type=float, default=DEFAULT_MAX_PAIR_GAP_SEC)
    parser.add_argument("--gap-threshold-sec", type=float, default=DEFAULT_GAP_THRESHOLD_SEC)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the human-readable report")
    args = parser.parse_args(argv)

    paths = [Path(p) for p in args.logfiles]
    for path in paths:
        if not path.exists():
            print(f"error: {path} does not exist", file=sys.stderr)
            return 1
        if not path.is_file():
            print(f"error: {path} is not a regular file", file=sys.stderr)
            return 1
        try:
            text_probe = path.open("r")
            text_probe.close()
        except PermissionError:
            print(
                f"error: cannot read {path} (chrony's logdir is usually "
                f"root-only) -- try: sudo {sys.argv[0]} {path}",
                file=sys.stderr,
            )
            return 1

    try:
        result = summarize(
            paths,
            mini_refid=args.mini_refid,
            fuse_refid=args.fuse_refid,
            max_pair_gap_sec=args.max_pair_gap_sec,
            gap_threshold_sec=args.gap_threshold_sec,
        )
    except LayoutError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_text(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
