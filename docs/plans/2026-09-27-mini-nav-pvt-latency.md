# Measure what an LBE-mini's NAV-PVT is worth as a timing witness

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read the LBE-mini's HID stream continuously, record the host time at which every UBX-NAV-PVT is decoded, and measure — over a day on AC0G-ND — how tightly that decode time tracks the second the message names. The number decides HamSCI/hf-timestd#51: an independent timing witness if the spread is well under 1 ms and stable; confirmed not-useful for timing if it is tens of ms.

**Architecture:** gpsdo-monitor gains a per-mini reader thread that owns the HID interrupt-IN stream (instead of a 3 s sample every 10 s tick), keeps the latest decoded state for `get_status()`, and — opt-in — writes every NAV-PVT into a chrony SHM refclock. chrony, with the refclock `noselect`, does the measuring (sourcestats, refclocks.log). Differencing it against the FUSE refclock chrony already measures cancels the host clock's own error. (mjh 2026-09-27: use extant tools — gpsd cannot read the mini, which is HID-only; chrony's refclock statistics are the established method.)

**Tech Stack:** Python 3.11 (gpsdo-monitor, hidapi/hidraw), JSONL, pytest (`.venv/bin/pytest` or the repo's documented runner — check README/pyproject).

**Spec:** none separate. Decision: mjh 2026-09-27 — "the aim … remains to get the most out of the equipment in hand. I'd hate to not take advantage of real value, if it exists." Evidence so far (read-only, AC0G-ND 22:40Z): every 10 s JSON write carried a fresh NAV-PVT and NAV-CLOCK (pps_utc_sec +10 each write, fix_age 0), so the mini emits NAV-PVT at least every 3 s; gpsdo-monitor, not the device, sets today's 10 s cadence. The configured CFG-MSG rate byte (0x0A) evidently does not mean "every 10th solution" through the firmware's UBX wrap.

## Global Constraints

- Observe, never correct: nothing here feeds hf-timestd's estimate; the log is evidence for a decision. Receiver self-reports (tAcc, NAV-CLOCK) are recorded, not trusted.
- The mini's existing JSON contract (`/run/gpsdo/<serial>.json`: health.pps_utc_sec, nmea_host_monotonic_at_read, fix_age_sec, naming_source "ubx-nav-pvt", naming_sigma_ns, nav_clock, pll/gps fields, position) keeps every field and meaning; it may only get fresher.
- HID access stays single-owner: all interrupt-IN reads AND feature commands (stream enable, drive/PLL setters) for one device go through the reader thread or a lock it holds — never two threads on the handle at once.
- The chrony feed is OFF by default (`chrony_shm_unit` unset). The refclock is ALWAYS `noselect`: this is a witness, never a source. The decode stamp is taken immediately after the NAV-PVT finishes reassembling — the same instant the existing code uses for `nav_pvt_mono`.
- Light on the station: one thread, 50 ms blocking reads, no busy loop; log I/O buffered, flushed at least every 10 s.
- Every test watched failing first; mutation noted under "Mutations:" in each commit body.

---

### Task 1: Continuous mini reader

**Files:** `src/gpsdo_monitor/models/lbe_mini.py`, `src/gpsdo_monitor/service.py` (worker wiring; include the mini in the 1 Hz fast-republish loop so its JSON refreshes every second), tests `tests/test_mini.py` / `tests/test_service.py` (extend; follow their fakes for the HID handle).

- [ ] Read how `LbeMini.get_status()` samples today (the 3 s `nav_sample_sec` window, `_enable_stream`, frame decode, NAV-PVT/NAV-CLOCK parse), how `DeviceWorker` calls it, and how drive/PLL setters touch the HID handle.
- [ ] Add a reader loop owned by the device (thread started/stopped with the worker): enable the stream, then read interrupt-IN continuously (50 ms timeout), decode frames, update a lock-protected latest-state snapshot (pll, gps, sig_loss, fix, nav_clock, nav_pvt + its decode mono/real stamps, and a counter of NAV-PVT decodes), re-sending `_enable_stream` periodically (every 30 s — the old code re-sent it every tick; keep the stream alive the same way).
- [ ] `get_status()` returns from the snapshot (no 3 s blocking sample). Stale snapshot (no frame for > 15 s) reads as it does today when the sample window saw nothing.
- [ ] Publish a measured `nav_pvt_rate_hz` (decodes over the last 60 s) in the device's JSON extras — the answer to "how often does the mini actually send".
- [ ] Setters (drive level, PLL) serialize with the reader through the same lock.
- [ ] Tests: snapshot updated by fed frames; `get_status()` returns without sleeping; rate computed from counted decodes; setter + reader never overlap on the fake handle (assert via the fake's call log / a reentrancy flag); stop() joins the thread. Mutation: drop the lock around a setter → the overlap test fails.

### Task 2: Feed each NAV-PVT into a chrony SHM refclock (noselect)

Use the tool chrony already has for judging message-time sources (the way gpsd feeds serial GPS time): an NTP SHM segment. chrony measures the source against the system clock and, marked `noselect`, never lets it steer.

**Files:** a small module `src/gpsdo_monitor/chrony_shm.py` (stdlib `ctypes` shmget/shmat — no new dependency), config keys in `config.py`, wiring in the Task 1 reader, tests `tests/test_chrony_shm.py`.

- [ ] Config: `chrony_shm_unit` (int, default None = off). Key = 0x4E545030 + unit (the NTP SHM convention); segment created 0600 root if absent.
- [ ] On each decoded NAV-PVT with a valid fix: write one sample using the standard NTP SHM layout (mode 1 with the count/valid handshake; `clockTimeStamp` = the named instant `utc_s + nano`, `receiveTimeStamp` = host CLOCK_REALTIME taken at the reassembly instant — the same instant as `nav_pvt_mono`; leap 0; precision −10 (≈1 ms)). Samples with fix < 2D or an invalid time flag are not written.
- [ ] Tests (no real SHM needed — the writer takes a buffer/segment object you can fake): struct layout and field offsets match the NTP SHM definition (assert against the documented byte offsets); the count/valid handshake order; nano handling (negative nano from the receiver); invalid fix skipped; unit None → nothing opened. Mutation: swap clock/receive stamps → the field test fails.

### Task 3: chrony wiring and read-out (no new analysis code)

**Files:** `docs/MINI_TIMING_WITNESS.md` (how to enable and read it), optionally `scripts/mini_witness_summary.sh` (a thin wrapper over chronyc/awk).

- [ ] The drop-in to use on a station, its own file so it can never collide with hf-timestd's `timestd-refclocks.conf` (ND has had duplicate-refclock trouble): `/etc/chrony/conf.d/gpsdo-mini-witness.conf` containing `refclock SHM 3 refid MINI poll 4 precision 1e-3 noselect` and `log refclocks` (and `logdir /var/log/chrony` if the station's chrony.conf has none — check).
- [ ] How to read it: `chronyc sourcestats` (MINI row: offset and std dev vs the system clock) and `/var/log/chrony/refclocks.log` (every raw sample).
- [ ] The decisive comparison (document it and make the optional script do it): on stations whose system clock follows NTP (AC0G-ND does — FUSE and HPPS are `noselect` since 2026-09-10), the host clock's own error cancels by differencing two noselect refclocks chrony measures against the SAME clock: `MINI_offset(t) − FUSE_offset(t)` from refclocks.log, paired by nearest time. That is the mini against hf-timestd's fusion, independent of NTP.
- [ ] Output of the summary: MINI mean/median/std/MAD, the same for (MINI − FUSE), sample rate, gaps.

### Task 4: Live on AC0G-ND (controller)

1. Bus announcement (gpsdo-monitor restart on ND; ND has no T6 or T5 using it, only second-naming for nothing currently; hf-timestd reads its JSON).
2. Deploy gpsdo-monitor main to ND (ff + restart the unit), set `chrony_shm_unit = 3`; install the chrony drop-in; `chronyc sources` before/after must differ only by the new MINI row.
3. After 10 min: check `nav_pvt_rate_hz`, the JSON still carries every field, CPU of the process is negligible.
4. After 24 h: read `chronyc sourcestats` and run the summary on refclocks.log (MINI alone and MINI − FUSE).
5. Post the result to HamSCI/hf-timestd#51 with the numbers and the decision it supports; memory.
