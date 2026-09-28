# The Mini as a chrony timing witness

> **Audience:** operator
> **Status:** current
> **Verified against:** gpsdo-monitor Task 2 (chrony_shm.py, commits afdea64/4ee888f) and AC0G-ND's live chrony layout, 2026-09-28

This tells an operator how to turn on the LBE-Mini's chrony feed and how to
read the number it produces. It answers one question: **how tightly does a
NAV-PVT message's arrival track the second it names?**

The feed never disciplines anything. It writes a witness sample for chrony
to measure, never one chrony selects. Nothing here corrects a clock; it only
lets you watch one.

## What the number means

Each NAV-PVT with a valid fix carries two clocks: the UTC second the
receiver names (`clockTimeStamp`) and the host's own `CLOCK_REALTIME` at the
moment gpsdo-monitor finishes reassembling that message (`receiveTimeStamp`).
chrony computes `offset = clockTimeStamp − receiveTimeStamp` and calls the
result the source's offset from the system clock.

A **negative** offset means the named second arrived late: by the time the
host stamped it, the clock had already moved past it. A positive offset
would mean the opposite — the message beat the second it names, which normal
USB/processing latency should never produce. Watch for it anyway; a positive
offset would say the assumption above has a hole in it.

## Turn it on

### 1. Point gpsdo-monitor at chrony's unit 3

Unit 3 is the fleet convention for this feed (`chrony_shm.RESERVED_SHM_UNITS`
refuses units 0–2: gpsd, hf-timestd's FUSE writer, hf-timestd's HPPS writer).
Add to `/etc/gpsdo-monitor/config.toml` (see `deploy/config.example.toml`):

```toml
[monitor]
chrony_shm_unit = 3
```

Restart `gpsdo-monitor.service`. The daemon runs as `User=gpsdo`; if the SHM
segment doesn't exist yet, it creates one world-writable (`0666`). If
chronyd created the segment first — chronyd's own default is `0600` — the
feed logs "no write access" and turns itself off. The drop-in below closes
that gap from chrony's side too.

### 2. Give chrony its own drop-in

Write `/etc/chrony/conf.d/gpsdo-mini-witness.conf`:

```
refclock SHM 3 refid MINI poll 4 precision 1e-3 noselect perm 0666
log refclocks
```

Keep it in its own file. Don't add these lines to hf-timestd's
`timestd-refclocks.conf` — AC0G-ND has already lived through the trouble two
files fighting over `refid FUSE` cause
(`ops/memory/reference_chrony_duplicate_refclock_dropin.md`), and this feed
has no reason to risk the same trap.

`noselect` keeps this a witness: chronyd measures MINI against the system
clock, and never steers on it.

`perm 0666` matters only if gpsdo-monitor didn't create the segment first
(hf-timestd's `shm-init` already leaves units 0–3 at `0666` on a station
running it). It costs nothing where the segment already exists, so the
drop-in carries it unconditionally.

If `chrony.conf` names no `logdir`, add `logdir /var/log/chrony` too — check
first with `grep -r logdir /etc/chrony/`.

### 3. Add the log line, without breaking what's already logging

`log refclocks` is additive. Reading chrony 4.6.1's own parser confirms
it: each `log` line only ever turns options **on** (`conf.c`'s `parse_log()`
sets `do_log_refclocks = 1` and never clears any flag), so this drop-in's
`log refclocks` line coexists with AC0G-ND's existing
`log tracking measurements statistics` in `chrony.conf` — both stay in
effect, whichever file chronyd reads first. If a station's chrony version
turns out to disagree, the safe alternative is editing that station's
existing `log` line to add `refclocks` to it, rather than adding a second
`log` line.

### 4. Restart chrony

```
systemctl restart chrony
```

This restart clears every refclock's reach and falseticker state, not only
MINI's — a brief transient on any other `noselect` refclock sharing the
host, gone within a few polls.

## Confirm it's running

```
chronyc sourcestats
```

MINI shows a `#?` row (never selected, `noselect` in effect) with an offset
and standard deviation chrony has measured against the system clock. If the
row never appears, or its reach stays 0, the drop-in didn't load or the feed
never opened the segment — check `journalctl -u gpsdo-monitor` and
`ipcs -m | grep $(printf '0x%x' $((0x4e545030 + 3)))` for who's attached.

## Read the raw samples: `refclocks.log`

`/var/log/chrony/refclocks.log` records every sample chrony reads from every
`noselect`-or-not refclock, one line per sample, plus a periodic banner
chronyd reprints on its own (every 32 writes by default) to remind a reader
of the columns. `/var/log/chrony` is usually root-only; read it with `sudo`.

The column layout below comes from `man chrony.conf(5)` (the `log`
directive's `refclocks` option), checked against the chrony 4.6.1 package
for Debian 13 and cross-checked against that version's own source
(`refclock.c`'s literal log-header string). If a station runs a chrony whose
major version differs meaningfully, sanity-check the column count against
its own man page before trusting this table.

Nine whitespace-separated columns per data line:

| # | Column | Example |
|---|--------|---------|
| 1 | Date (UTC) | `2026-09-28` |
| 2 | Time (UTC) | `14:33:27.000000` |
| 3 | Refid | `MINI` |
| 4 | Driver-poll sequence within the interval, raw samples only (`-` for a filtered sample) | `7` |
| 5 | Leap status: `N` normal, `+`/`-` for a 61/59 s last minute | `N` |
| 6 | PPS-source flag: `1`/`0`, `-` for a filtered sample | `0` |
| 7 | Raw offset the driver measured, `-` for a filtered sample | `1.334000e-03` |
| 8 | **Cooked offset** — local clock error with corrections applied. Positive means the local clock is slow. | `1.234000e-03` |
| 9 | Assumed dispersion of the sample | `1.000e-03` |

**Column 8 is the number this doc and the summary script read.** It's
populated on both raw and filtered samples, unlike columns 4, 6, and 7.

## The decisive comparison: MINI against hf-timestd's own fusion

AC0G-ND's system clock follows NTP with FUSE and HPPS both `noselect` since
2026-09-10 — so chrony measures every refclock, including MINI, against the
same host clock, and that host clock's own error cancels when you difference
two refclocks against it. Pair MINI's offset with FUSE's at the nearest
timestamp: `MINI_offset(t) − FUSE_offset(t)` measures the Mini against
hf-timestd's WWV/WWVH fusion, not against whatever chrony currently thinks
the system clock reads.

## `scripts/mini_witness_summary.sh`

A thin wrapper over a stdlib-only Python helper
(`scripts/mini_witness_summary.py`) that reads `refclocks.log`, skips the
banner lines, and reports:

- MINI: mean, median, standard deviation, and MAD (median absolute
  deviation from the median, unscaled) of column 8, in milliseconds.
- MINI − FUSE: the same four statistics over the paired differences, plus
  how many MINI samples had no FUSE sample close enough to pair with.
- Sample rate for each refclock (count and samples/hour over the span the
  log covers).
- Gaps: any interval between consecutive samples on a refclock wider than
  40 s (roughly two and a half missed polls at the drop-in's `poll 4`, 16 s
  nominal spacing) — configurable with `--gap-threshold-sec`.

```
sudo scripts/mini_witness_summary.sh
sudo scripts/mini_witness_summary.sh /var/log/chrony/refclocks.log --json
```

Flags: `--mini-refid`, `--fuse-refid` (default `MINI`/`FUSE`),
`--max-pair-gap-sec` (default 8 s, half the nominal poll interval, so a
MINI sample never pairs with a FUSE sample from the adjacent poll cycle),
`--gap-threshold-sec` (default 40 s), `--json`.

If a line doesn't split into exactly 9 fields, or its first two fields
don't parse as a UTC date and time, the script refuses to guess and exits
with a message naming the file and line — a changed chrony log format
should stop the script, not feed it silently wrong numbers. The same
refusal covers a `refclocks.log` with no rows for the refid it's looking
for: report the miss, don't report zero.

## Caveats

- This measures Mini-to-host-clock latency, not GPS accuracy. A Mini with a
  poor fix but consistent USB/processing delay can still show a tight,
  repeatable offset here — that's the point (repeatability), not proof the
  named second is correct.
- `refclocks.log` grows without chrony ever rotating it. Whether a
  station's `logrotate` already covers `/var/log/chrony` hasn't been
  checked here — confirm before leaving `log refclocks` on for a long run.
- Leave the refclock `noselect`. Nothing in this design has argued the Mini
  should ever steer the host clock; it exists to let an operator watch it.
