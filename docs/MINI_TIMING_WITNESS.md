# The Mini as a chrony timing witness

> **Audience:** operator
> **Status:** current
> **Verified against:** gpsdo-monitor Task 2 (chrony_shm.py, commits afdea64/4ee888f), AC0G-ND's live chrony layout, and chrony 4.6.1 source (`conf.c`, `refclock.c`, `refclock_shm.c`, `logging.c`), 2026-09-28

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

A positive offset has a second, more mundane explanation, too: the host
clock itself running ahead of true UTC by more than the latency. chrony
measures MINI against whatever the system clock currently reads, not
against true UTC — so if the host clock is fast enough, it can make an
on-time (or even late) message look early. Check `chronyc tracking`'s
`System time` line before reading a positive offset as a hole in the
latency assumption; it may just be a fast host clock.

## Turn it on

### 1. Point gpsdo-monitor at chrony's unit 3

Unit 3 is the fleet convention for this feed (`chrony_shm.RESERVED_SHM_UNITS`
refuses units 0–2: gpsd, hf-timestd's FUSE writer, hf-timestd's HPPS writer).

`/etc/gpsdo-monitor/config.toml` already has a `[monitor]` table on any
station running this daemon (see `deploy/config.example.toml`) — with
`probe_interval_sec`, `min_drive_ma`, maybe a `[[monitor.device]]` list,
and so on. Add `chrony_shm_unit` as ONE MORE KEY inside that same
`[monitor]` table, above any `[[monitor.device]]` header in the file (TOML
reads everything between a table header and the next one as belonging to
it, so a key placed after `[[monitor.device]]` would land in the wrong
table). Do **not** write a second `[monitor]` block — this is not TOML
that "just" gets misread. `tomllib` refuses to parse it at all
(`TOMLDecodeError: Cannot declare ('monitor',) twice`), so gpsdo-monitor
fails to start rather than silently ignoring the second block:

```toml
[monitor]
probe_interval_sec = 10
min_drive_ma       = 32
chrony_shm_unit    = 3        # <-- add this line, still inside [monitor]

# [[monitor.device]]           # any existing device entries stay AFTER
# serial = "..."
```

Before restarting the daemon, confirm the key landed where `config.py`
actually reads it (`raw.get("monitor", {}).get("chrony_shm_unit")`,
`DEFAULT_CONFIG_PATH = /etc/gpsdo-monitor/config.toml` — check both
against the installed `config.py` first if this doc and the code ever
disagree):

```
python3 -c 'import tomllib;print(tomllib.load(open("/etc/gpsdo-monitor/config.toml","rb"))["monitor"].get("chrony_shm_unit"))'
```

This must print `3`. If it prints `None`, the key is outside `[monitor]`
— most likely after a `[[monitor.device]]` header. If this one-liner
instead prints a `TOMLDecodeError` traceback, the file has a second
`[monitor]` block; gpsdo-monitor won't start until it's merged back into
one. A second `[monitor]` block is not a "gets ignored" mistake — it's a
"nothing starts" mistake, on both the check above and the real daemon.

Restart `gpsdo-monitor.service`. The daemon runs as `User=gpsdo`; if the SHM
segment doesn't exist yet, it creates one world-writable (`0666`). If
chronyd created the segment first — chronyd's own default is `0600` — the
feed logs "no write access" and turns itself off. The drop-in below closes
that gap from chrony's side too.

### 2. Give chrony its own drop-in

Write `/etc/chrony/conf.d/gpsdo-mini-witness.conf`:

```
refclock SHM 3:perm=0666 refid MINI poll 4 precision 1e-3 noselect
log refclocks
```

`perm` is a **driver** option, not a refclock keyword. chrony attaches a
driver option to the driver parameter with a colon — `man chrony.conf(5)`
gives exactly this pattern as its own example, `refclock SHM 1:perm=0644
refid GPS2`. Writing `perm 0666` as a free-standing word, the way an
earlier draft of this doc had it, breaks the parse: chrony's
`parse_refclock()` (`conf.c`) has no top-level `perm` keyword, so it calls
`other_parse_error("Invalid refclock option")` — which is fatal. chronyd
logs the error and exits (`logging.c`'s `LOG_FATAL` macro ends in
`exit(1)`) instead of starting. Get this wrong and the service that's
supposed to be a passive witness takes chronyd down with it.

`precision 1e-3` stays a free-standing refclock option — that part was
always correct. chrony ignores whatever precision value the SHM segment's
own struct field carries (`refclock_shm.c`'s `shm_poll()` never reads
`t.precision`); this config value is the only one that counts.

Keep the file on its own. Don't add these lines to hf-timestd's
`timestd-refclocks.conf` — AC0G-ND has already lived through the trouble two
files fighting over `refid FUSE` cause
(`ops/memory/reference_chrony_duplicate_refclock_dropin.md`), and this feed
has no reason to risk the same trap.

`noselect` keeps this a witness: chronyd measures MINI against the system
clock, and never steers on it.

`:perm=0666` matters only if gpsdo-monitor didn't create the segment first
(hf-timestd's `shm-init` already leaves units 0–3 at `0666` on a station
running it). It costs nothing where the segment already exists —
`refclock_shm.c` only consults it inside `shmget(..., IPC_CREAT | perm)`,
which is a no-op once the segment is already there — so the drop-in
carries it unconditionally.

If `chrony.conf` names no `logdir`, add `logdir /var/log/chrony` too — check
first with `grep -r logdir /etc/chrony/`.

### 3. Verify the drop-in parses, before restarting anything

```
sudo chronyd -p
```

`-p` makes chronyd read its whole configuration — `chrony.conf` plus every
file `confdir` pulls in, this drop-in included — print it in normalized
form, and exit without touching whatever chronyd is already running
(`chronyd.adoc`: "verify the syntax of the configuration"). A bad
top-level directive fails here, loudly, naming the bad line — the `perm`
example in step 2 above is exactly this kind of failure, and `-p` catches
it. That beats finding out when `systemctl restart chrony` fails instead.

`-p` checks **directives** — the words chrony's own config parser
recognizes, like the free-standing `perm 0666` mistake above. It does
NOT check **driver options** — the part after the colon, like
`:perm=0666` itself. Those aren't parsed until the refclock actually
initializes, which happens on the real start `-p` skips. So `-p`
passing is good evidence the drop-in parses; it is not proof the
`:perm=0666` value will do what you expect once chronyd actually opens
the segment. Confirm that after the restart instead, with `chronyc
sources` / `chronyc sourcestats` in "Confirm it's running" below.

### 4. Add the log line, without breaking what's already logging

`log refclocks` is additive. Reading chrony 4.6.1's own parser confirms
it: each `log` line only ever turns options **on** (`conf.c`'s `parse_log()`
sets `do_log_refclocks = 1` and never clears any flag), so this drop-in's
`log refclocks` line coexists with AC0G-ND's existing
`log tracking measurements statistics` in `chrony.conf` — both stay in
effect, whichever file chronyd reads first. If a station's chrony version
turns out to disagree, the safe alternative is editing that station's
existing `log` line to add `refclocks` to it, rather than adding a second
`log` line.

### 5. Before you restart chronyd

Restarting chronyd is not as quiet as it looks — on SOME stations. Find
out what this station's chrony unit actually pulls in, rather than
assuming a fixed list:

```
systemctl show -p Wants -p After chrony.service
```

If that prints hf-timestd units (`timestd-metrology.target`,
`timestd-l2-calibration.service`, `timestd-fusion.service`,
`timestd-core-recorder.service`, or similar), restarting chrony starts
any of them that are currently stopped — this restart is not just
"reload chrony's config," it can also start hf-timestd services that
were deliberately down. Check each one `systemctl show` named:

```
systemctl is-active <unit1> <unit2> ...
```

Some stations carry no such drop-in at all — AC0G-ND, checked
2026-09-28, has none — and there a chrony restart starts nothing else.
Run the `systemctl show` command above and read what it actually says for
THIS station; don't assume either way.

The restart also resets chrony's own NTP source selection: the `*`
(selected) source can change while chrony re-picks one. hf-timestd's
offset_judge tells T4 from T2 apart by whether the selected source sits
on the LAN, so a restart can shift that classification until chrony
reconverges (a few minutes, ordinarily). If T6 is enabled and one of the
`Wants=` units above is a core-recorder, `timestd-hpps-watchdog` can
restart it after HPPS goes quiet for a while — but the chrony restart
itself is over in a second or two, nowhere near the 600 s
`HPPS_LASTRX_THRESHOLD_S` threshold that watchdog uses, so on its own a
chrony restart is very unlikely to trip it.

None of this touches the MINI refclock itself: chrony reads refclocks
only from its config files, and this drop-in's effect survives a
restart on its own. You are restarting chrony here only so the NEW
`gpsdo-mini-witness.conf` drop-in takes effect — you do not need to
restart chrony again just because `config.toml`'s `chrony_shm_unit`
changed on the gpsdo-monitor side (restart `gpsdo-monitor.service` for
that instead).

Before restarting, record what the station looks like right now:

```
chronyc -n sources
chronyc tracking
systemctl show -p Wants -p After chrony.service
systemctl is-active <every unit that command named>
```

Also check whether T6 is armed, with `tomllib` rather than `grep` (a
value nested this way — `[timing.t6_pps]` inside `[timing]` — is easy to
mis-grep past a sibling table with the same short name):

```
python3 -c 'import tomllib;print(tomllib.load(open("/etc/hf-timestd/timestd-config.toml","rb")).get("timing",{}).get("t6_pps",{}).get("enabled"))'
```

And check gpsd:

```
systemctl is-active gpsd
```

gpsd matters here because it owns chrony's SHM unit 0 — if gpsd is
inactive, nothing else on the station is writing that unit, so a chrony
restart can't disturb it either way.

On a sigmond station, chrony restarts normally belong to the sigmond
reconciler, not to an operator running `systemctl restart` by hand.
**Announce the restart and get the operator's go before running it.**

Repeat the same commands after the restart, plus `chronyc sources` for
MINI's own row once it appears. "Before/after" means: the same
`chronyc -n sources` / `chronyc tracking` / `systemctl is-active` rows,
now including MINI, and the same `*` (selected) source back in place
after 5–10 minutes of reconvergence.

### 6. Restart chrony

```
systemctl restart chrony
```

This restart clears every refclock's reach and falseticker state, not only
MINI's — a brief transient on any other `noselect` refclock sharing the
host, gone within a few polls.

## Confirm it's running

```
chronyc sources
```

MINI shows a `#?` row there — never selected, `noselect` in effect. `sources`
names the row but doesn't carry the numbers. For those:

```
chronyc sourcestats
```

MINI's row here carries `NP` (sample count), `Offset`, and `Std Dev` —
chrony's own measurement of MINI against the system clock. If MINI never
appears in either command, or `sources`' Reach column for it stays 0, the
drop-in didn't load or the feed never opened the segment — check
`journalctl -u gpsdo-monitor` and
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

**Column 8 is the number this doc and the summary script read**, and it's
populated on both kinds of line. The two kinds answer different questions,
though.

A **raw** line (column 4 holds a number) logs one driver poll. For MINI and
FUSE that means essentially one delivered message: `chrony_shm.py` writes a
fresh SHM sample on every NAV-PVT, and this drop-in doesn't slow the
driver's own poll rate down to `poll 4` — that setting paces something
else (next paragraph). A **filtered** line (column 4 holds `-`) logs
chrony's own combined estimate over the refclock's whole `poll` interval,
built from every raw sample chrony accumulated in that window
(`refclock.c`'s `poll_timeout()`). It answers "what does chrony currently
believe this source reads" — an input to source selection, not a record of
one message's arrival.

The question this doc opened with is about one message's arrival. So **the
summary script reads raw lines only** and skips filtered ones — counting
them, not silently dropping them; `--json` output says how many it left
out per refclock.

## The decisive comparison: MINI against hf-timestd's own fusion

AC0G-ND's system clock follows NTP. FUSE and HPPS have both run `noselect`
since 2026-09-10. So chrony measures every refclock — MINI included —
against that same host clock. Difference two refclocks measured against
the same clock, and the host clock's own error cancels out of the result.

Pair MINI's offset with FUSE's at the nearest timestamp:
`MINI_offset(t) − FUSE_offset(t)` measures the Mini against hf-timestd's
WWV/WWVH fusion, not against whatever chrony currently thinks the system
clock reads.

The pairing window matters here only if the host clock drifts during it.
Once chrony has locked — skew held to a few ppm, its normal steady state —
the host clock moves a few tens of nanoseconds over the default 8 s
`--max-pair-gap-sec` window: far below the millisecond offsets this
comparison measures. A host clock still slewing hard needs fixing on its
own terms first; ops has seen that state before
(`ops/memory/project_host_clock_runaway_20260904.md`), and this comparison
means little while it lasts.

## `scripts/mini_witness_summary.sh`

A thin wrapper over a stdlib-only Python helper
(`scripts/mini_witness_summary.py`) that reads `refclocks.log`, skips the
banner lines, and reports:

- MINI: mean, median, standard deviation, and MAD (median absolute
  deviation from the median, unscaled) of column 8 on **raw** lines only,
  in milliseconds. Filtered lines (column 4 = `-`) are counted and
  reported separately, never folded into these statistics — see "raw vs
  filtered" above.
- MINI − FUSE: the same four statistics over the paired differences, plus
  how many MINI samples had no FUSE sample close enough to pair with.
- **Short-tau**: see the next section. This is the decision statistic for
  "well under 1 ms and stable" — read it before the plain std above.
- **Folded by schedule**: the mean MINI offset grouped by `(sample time)
  mod 10 s` and `mod 30 s`, so a bucket that stands out flags an artifact
  tied to gpsdo-monitor's own probe tick or stream refresh rather than
  real Mini/host jitter. The full per-bucket table is in `--json` output
  (`mini_fold_10s` / `mini_fold_30s`); the text report prints only the
  spread across buckets, since a 30-bucket table doesn't read well on a
  terminal.
- Sample rate for each refclock (count and samples/hour over the span the
  log covers).
- Gaps: any interval between consecutive raw samples on a refclock wider
  than 40 s — configurable with `--gap-threshold-sec`. Raw samples track
  the writer's own message rate, not `poll 4` (that setting paces
  chrony's filtered estimate, not the raw log — see above), so 40 s is a
  fixed, conservative floor meant to catch a real outage, not a tuned
  multiple of anything.

```
sudo scripts/mini_witness_summary.sh
sudo scripts/mini_witness_summary.sh /var/log/chrony/refclocks.log --json
```

More than one log file merges by sample timestamp, so a rotated file and
the current one — in either order — read as one continuous run:

```
sudo scripts/mini_witness_summary.sh /var/log/chrony/refclocks.log.1 \
    /var/log/chrony/refclocks.log --json
```

Include the rotated file whenever the run you're measuring straddles a
rotation — otherwise the samples on the older side of the cut are
silently missing from the count, the rate, and the gap list.

Flags: `--mini-refid`, `--fuse-refid` (default `MINI`/`FUSE`),
`--max-pair-gap-sec` (default 8 s — a fixed cap comfortably inside one
`poll 4` filter window, chosen to tolerate ordinary jitter between MINI's
and FUSE's message arrivals without pairing across a real outage on
either one), `--gap-threshold-sec` (default 40 s — also the cutoff for
which consecutive samples count as "one message period apart" for the
short-tau statistic below), `--json`.

If a line doesn't split into exactly 9 fields, or its first two fields
don't parse as a UTC date and time, the script refuses to guess and exits
with a message naming the file and line — a changed chrony log format
should stop the script, not feed it silently wrong numbers. The same
refusal covers a `refclocks.log` with no rows for the refid it's looking
for: report the miss, don't report zero.

### The short-tau statistic: what actually answers "well under 1 ms"?

Plain std (above) is measured over the whole run, so it also picks up
anything that moves slowly over that span — chiefly the host clock's own
NTP wander. It answers "how spread out were the samples," not "how much
does one message's offset move from the next."

The short-tau statistic answers that narrower question. For each pair of
consecutive raw MINI samples no more than `--gap-threshold-sec` apart, take
the difference between them. The **standard deviation of those
differences, divided by √2**, is the value reported.

Be precise about what that is. The textbook two-sample deviation of a
series x sampled at spacing τ is

```
σ(τ) = √( ½ · mean( (x[i+1] − x[i])² ) )
```

— the root-mean-square first difference, with no mean subtracted. What
the script actually computes is `stdev(diffs) / √2`, using a mean-SUBTRACTED
standard deviation (Python's `statistics.stdev`). Subtracting the mean
difference removes a constant drift — a steady frequency offset — that the
textbook formula would not remove. That's a deliberate choice, not a
rounding difference: it means the script's number and the textbook σ(τ)
are related but not identical quantities.

This estimator equals the Allan deviation of the offset series only **in
expectation**, for white noise: it's unbiased across many hypothetical
repeats of the same measurement, not an exact reading of "the" Allan
deviation from one finite run, and it says nothing about non-white noise
(drift, flicker) beyond removing a constant term. It is also **not**
Riley's phase-data AVAR (W.J. Riley, *Handbook of Frequency Stability
Analysis*, NIST SP 1065) — that formula takes SECOND differences of
phase/time data to get a true frequency-noise Allan deviation. MINI's
offsets here are differenced once, not twice. Riley is the right
reference for the general family of two-sample-deviation statistics; it
is not a citation for this exact formula.

τ is not pinned at exactly "one message period," either. It's whatever a
given pair's spacing happens to be — anywhere from just over 0 s up to
`--gap-threshold-sec` (40 s by default), since the Mini's message rate
isn't perfectly regular and a real outage gets excluded rather than
bridged. The script reports `median_spacing_sec` alongside the value, so
you can see what τ actually meant for a given run instead of assuming a
single number.

The script reports the same statistic for the MINI − FUSE differences
too, when there are enough consecutive paired points — using a
**different pairing** than the plain MINI − FUSE numbers above. Pairing
every MINI sample to its own nearest FUSE sample (as the plain numbers
do) lets several consecutive MINI samples share ONE FUSE sample; a
difference built from two diffs that both used the same FUSE sample is
really just the difference between two MINI offsets, with FUSE canceled
out completely. The short-tau series instead pairs one point per FUSE
sample — the nearest MINI sample to each one — so every included
difference spans two distinct FUSE samples. When FUSE is polled less
often than `--gap-threshold-sec`, this can honestly yield too few
consecutive pairs to report a value, even `n_diffs: 0` — that's a correct
statement about FUSE's polling rate, not a bug.

Three numbers, three different questions:

- **Plain std** bounds the answer from above — it includes host NTP
  wander over the whole run, so it can only overstate the Mini's own
  jitter.
- **MINI − FUSE std** also bounds it from above — FUSE carries its own
  millisecond-level noise, so subtracting FUSE doesn't remove all
  contamination.
- **Short-tau** is the tightest read of the three, but even it cannot,
  by itself, separate the Mini's real timing from gpsdo-monitor's own
  stamp noise. The daemon reads the decode timestamp from Python, whose
  GIL switches threads roughly every 5 ms; the 10 s probe tick and 30 s
  stream-refresh both briefly hold the Mini's HID lock, queuing frames
  behind them. A short-tau result of a few milliseconds is consistent
  with a Mini that's tighter than that — it just can't prove it. Check
  the folded-by-schedule numbers next: if the outliers line up with `mod
  10 s` or `mod 30 s`, that's the daemon's own schedule, not the Mini.

## Caveats

- This measures Mini-to-host-clock latency, not GPS accuracy. A Mini with a
  poor fix but consistent USB/processing delay can still show a tight,
  repeatable offset here — that's the point (repeatability), not proof the
  named second is correct.
- `refclocks.log` grows without chrony ever rotating it. Whether a
  station's `logrotate` already covers `/var/log/chrony` hasn't been
  checked here — confirm before leaving `log refclocks` on for a long run.
  If `logrotate` does rotate it mid-run, pass both the rotated file and
  the current one to `mini_witness_summary.sh` (see above) so the run
  reads as continuous instead of losing its older half.
- Leave the refclock `noselect`. Nothing in this design has argued the Mini
  should ever steer the host clock; it exists to let an operator watch it.
