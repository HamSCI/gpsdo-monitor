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

`chronyd` also periodically re-prints a banner (a line of "="
characters, the column-header text, another "=" line) into the same
file -- every `logbanner` writes, default 32. Those lines are skipped,
not data.

If a data line does not have exactly 9 fields, or its first two fields
do not look like a UTC date and time, this refuses to guess: chrony's
column layout has changed or this is not a refclocks.log, and reporting
statistics over misread columns would be worse than reporting nothing.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from bisect import bisect_left
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

DEFAULT_LOG_PATH = "/var/log/chrony/refclocks.log"
DEFAULT_MINI_REFID = "MINI"
DEFAULT_FUSE_REFID = "FUSE"
# Half the drop-in's nominal poll interval (`poll 4` = 2**4 = 16 s), so a
# MINI sample is never paired with a FUSE sample from the adjacent cycle.
DEFAULT_MAX_PAIR_GAP_SEC = 8.0
# A missed poll or two (poll 4 = 16 s nominal) is normal jitter; a hole
# this wide is not.
DEFAULT_GAP_THRESHOLD_SEC = 40.0

_DATE_LEN = 10   # "YYYY-MM-DD"


class LayoutError(RuntimeError):
    """The line did not match the 9-column refclocks.log format."""


class Sample(NamedTuple):
    ts: datetime
    offset_s: float


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
    cooked_offset_field = fields[7]

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

    return refid, Sample(ts=ts, offset_s=offset_s)


def parse_refclocks_log(path: Path, mini_refid: str, fuse_refid: str) -> tuple[list[Sample], list[Sample]]:
    mini: list[Sample] = []
    fuse: list[Sample] = []
    text = path.read_text()
    for line_no, line in enumerate(text.splitlines(), start=1):
        parsed = _parse_line(line, line_no, str(path))
        if parsed is None:
            continue
        refid, sample = parsed
        if refid == mini_refid:
            mini.append(sample)
        elif refid == fuse_refid:
            fuse.append(sample)
    mini.sort(key=lambda s: s.ts)
    fuse.sort(key=lambda s: s.ts)
    return mini, fuse


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


def _pair_nearest(mini: list[Sample], fuse: list[Sample], max_gap_sec: float) -> tuple[list[float], int]:
    """Pair each MINI sample with its nearest-in-time FUSE sample
    (within `max_gap_sec`) and return (diffs_seconds, unpaired_count)."""
    if not fuse:
        return [], len(mini)
    fuse_ts = [s.ts for s in fuse]
    diffs: list[float] = []
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
        diffs.append(m.offset_s - fuse[best].offset_s)
    return diffs, unpaired


def summarize(
    path: Path,
    mini_refid: str = DEFAULT_MINI_REFID,
    fuse_refid: str = DEFAULT_FUSE_REFID,
    max_pair_gap_sec: float = DEFAULT_MAX_PAIR_GAP_SEC,
    gap_threshold_sec: float = DEFAULT_GAP_THRESHOLD_SEC,
) -> dict:
    mini, fuse = parse_refclocks_log(path, mini_refid, fuse_refid)

    if not mini:
        raise LayoutError(
            f"no {mini_refid!r} rows found in {path} -- check the refid in "
            f"the drop-in (gpsdo-mini-witness.conf) and that chronyd has "
            f"`log refclocks` active and the feed is actually running"
        )
    if not fuse:
        raise LayoutError(
            f"no {fuse_refid!r} rows found in {path} -- the MINI-vs-FUSE "
            f"comparison needs hf-timestd's FUSE refclock in the same log; "
            f"pass --fuse-refid if this station names it differently"
        )

    diffs_s, unpaired = _pair_nearest(mini, fuse, max_pair_gap_sec)
    diff_stats = _stats_ms(diffs_s)
    diff_stats["unpaired_mini"] = unpaired
    diff_stats["max_pair_gap_sec"] = max_pair_gap_sec

    return {
        "log_path": str(path),
        "mini_refid": mini_refid,
        "fuse_refid": fuse_refid,
        "mini": _stats_ms([s.offset_s for s in mini]),
        "diff_mini_minus_fuse": diff_stats,
        "mini_rate": _rate(mini),
        "fuse_rate": _rate(fuse),
        "gap_threshold_sec": gap_threshold_sec,
        "mini_gaps": _gaps(mini, gap_threshold_sec),
        "fuse_gaps": _gaps(fuse, gap_threshold_sec),
    }


def _fmt(v, unit=""):
    return "n/a" if v is None else f"{v:.4f}{unit}"


def render_text(result: dict) -> str:
    lines = [f"refclocks.log: {result['log_path']}", ""]
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

    mr, fr = result["mini_rate"], result["fuse_rate"]
    def _rate_str(tag, r):
        if r["samples_per_hour"] is None:
            return f"{tag} {r['n']} samples"
        return f"{tag} {r['n']} samples over {r['span_sec']:.1f}s ({r['samples_per_hour']:.1f}/hr)"
    lines.append(f"sample rate: {_rate_str(result['mini_refid'], mr)}; {_rate_str(result['fuse_refid'], fr)}")

    gt = result["gap_threshold_sec"]
    for tag, gaps in ((result["mini_refid"], result["mini_gaps"]), (result["fuse_refid"], result["fuse_gaps"])):
        if gaps:
            lines.append(f"{tag} gaps (> {gt}s): " + "; ".join(f"{g['seconds']:.1f}s at {g['start']} -> {g['end']}" for g in gaps))
        else:
            lines.append(f"{tag} gaps (> {gt}s): none")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("logfile", nargs="?", default=DEFAULT_LOG_PATH,
                         help=f"path to refclocks.log (default: {DEFAULT_LOG_PATH})")
    parser.add_argument("--mini-refid", default=DEFAULT_MINI_REFID)
    parser.add_argument("--fuse-refid", default=DEFAULT_FUSE_REFID)
    parser.add_argument("--max-pair-gap-sec", type=float, default=DEFAULT_MAX_PAIR_GAP_SEC)
    parser.add_argument("--gap-threshold-sec", type=float, default=DEFAULT_GAP_THRESHOLD_SEC)
    parser.add_argument("--json", action="store_true", help="emit JSON instead of the human-readable report")
    args = parser.parse_args(argv)

    path = Path(args.logfile)
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
            path,
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
