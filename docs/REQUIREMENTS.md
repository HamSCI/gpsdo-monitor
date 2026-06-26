# gpsdo-monitor — Requirements Specification

**Status:** v0.1 baseline (retroactive). **Owner:** Michael Hauan (AC0G).
**Last reconciled against code:** gpsdo-monitor `0.1.0` / deploy `0.1.0` (2026-06-25).
**Prefix:** `GDM`.

> Retroactive application of [sigmond/docs/REQUIREMENTS-TEMPLATE.md](https://github.com/HamSCI/sigmond/blob/main/docs/REQUIREMENTS-TEMPLATE.md)
> to an **infra** component (cf. the full-contract pilots hf-timestd and
> superdarn-sounder). gpsdo-monitor is *not* a full client-contract conformant
> recorder: it has a CLI and an mDNS/file contract rather than the
> inventory/validate/config surface (§8.3). The sigmond↔component interface that
> does apply is referenced from the
> [client contract](https://github.com/HamSCI/sigmond/blob/main/docs/CLIENT-CONTRACT.md)
> (v0.8, §3 `hardware_present` amendment) — not restated here. Provenance tags:
> `[DOC]` documented · `[CODE]` implicit-in-code · `[NEW]` surfaced by this review.
> Status: ✅ implemented/verified · 🟡 partial/unverified · ⬜ planned.

## 1. Context & problem statement

A DASI2 station's entire timing chain rests on a GPS-disciplined oscillator (a
Leo Bodnar GPSDO) feeding the RX888 ADC clock. radiod's RTP sample counter — the
"steel ruler" the rest of the suite annotates with UTC — is only as authoritative
as that GPSDO's discipline. The failure mode that motivated this component is
silent: the GPSDO can lose GPS lock, lose its antenna, or drop PPS while radiod
keeps streaming samples at a now-undisciplined rate, and nothing downstream
notices until timing products quietly degrade. There was no actively-probed
signal telling the suite "the GPSDO is *still* disciplining the ADC right now."

gpsdo-monitor closes that gap. It enumerates attached Leo Bodnar USB HID devices
(LBE-1420 / LBE-1421 / LBE-1423 / LBE-Mini), probes their health on a fixed
cadence (PLL lock, GPS fix, satellites, antenna, 1 PPS presence, output
frequencies), and publishes a graded **A-level** hint (`A1`/`A0`) with a
human-readable reason. It is the producer of the contract §3 `hardware_present`
hardware dimension and the A-level provider that hf-timestd's authority manager
subscribes to when deciding its top tier.

Its defining design principle: it is a **liveness + gross-stability indicator,
not a metrology reference**. Its PPS numbers are OS-millisecond-bound
(`TIOCMIWAIT` on the CDC DCD line) and every PPS block ships that warning
verbatim. It publishes a simple file (`/run/gpsdo/<serial>.json`) plus an mDNS
advertisement (`_gpsdo._tcp`) so any consumer — same-host or splitter-fed across
a LAN — can read GPSDO state without SSH, and so the sigmond TUI can deep-dive a
GPSDO governing a selected radiod.

## 2. Goals & objectives

- Actively detect every attached Leo Bodnar GPSDO and **probe** its health
  (PLL/GPS/antenna/sats/PPS/outputs) on a fixed cadence (default 10 s).
- Emit a graded **A-level** hint (`A1`/`A0`) with a named first-failing-predicate
  reason, so any GPSDO failure an operator cares about surfaces as a visible `A0`
  within ≤ 2 × probe_interval (≤ 20 s default).
- Publish a stable, additive **schema-v1** contract — per-device JSON, an
  aggregate index, and an mDNS advertisement — that consumers read without
  re-deriving field layout.
- Be **discoverable** so a remote / splitter-fed consumer (hf-timestd on a
  different host) can read A-level by serial, decoupled from GPSDO-host routing.
- Map GPSDO → radiod **governance** (`governs`) so sigmond can enforce
  exactly-one-governor-per-local-radiod.
- Run usefully **standalone** (a single attached GPSDO, zero config) and as a
  sigmond-managed infra component.

## 3. Non-goals / out of scope

- **Being a metrology reference.** PPS stability is OS-millisecond bound and is a
  liveness/gross-stability indicator only — never a timing source. (Owner of real
  timing: hf-timestd; §18 producer.)
- **Disciplining the host clock.** That is chrony's job.
- **Deciding the timing tier.** gpsdo-monitor emits a *hint*; hf-timestd's
  authority manager is the arbiter and may override on cross-check against
  T-level witnesses.
- **Reverse-engineering undocumented HID opcodes.** It stays within the
  feature-report layouts documented by `bvernoux/lbe-142x`.
- **Full client-contract conformance.** It is an infra component: no
  `validate`, no per-instance inventory, no `config init/edit/show/apply` flow
  (the canonical config surface is `smd gpsdo config` in sigmond). See §8.3.
- **Frequency planning on the Mini.** The Si5351 divider-chain solver is not
  ported (`set_frequency` raises `NotImplementedError`).

## 4. Stakeholders & actors

Station operator · the **Leo Bodnar GPSDO** hardware (USB HID + optional CDC) ·
`hf-timestd`'s authority manager (the primary consumer — `GpsdoProbe` local /
`GpsdoMdnsProbe` remote, reads A-level into `authority.json`) · `sigmond`
(`smd install`/lifecycle, the `gpsdo_governor_coverage` harmonize rule, the TUI
GPSDO/radiod deep-dive screens, `smd gpsdo config`) · `radiod` (the governed
consumer of the GPSDO's clock — referenced by `governs`, not directly contacted)
· `chrony` (separate host-clock disciplinarian; not driven here) · the upstream
`bvernoux/lbe-142x` protocol reference (vendored byte layouts) · LAN mDNS
consumers (`_gpsdo._tcp`).

## 5. Assumptions & constraints

- `GDM-C-001` `[DOC]` ✅ Target platform SHALL be Linux (primary: Debian 12+ /
  RX888-class Beelink EQ); HID access via libhidapi-hidraw / libusb.
- `GDM-C-002` `[CODE]` ✅ Python ≥ 3.11, stdlib-first; the only required deps are
  `hidapi`, `zeroconf`, `pyserial`. The TUI (`textual`) is an optional `[tui]`
  extra and a lazy import.
- `GDM-C-003` `[DOC]` ✅ The component SHALL stay **wire-compatible** with
  `bvernoux/lbe-142x` and SHALL NOT use undocumented HID opcodes.
- `GDM-C-004` `[CODE]` ✅ The `gpsdo` service user SHALL access only
  `/dev/hidraw*`, `/dev/bus/usb/*` (char-usb_device, libusb wheel path), and
  `/dev/ttyACM*` for VID `0x1DD2`, via the shipped udev rule; everything else is
  locked down (`ProtectSystem=strict`, `PrivateDevices=false` with explicit
  `DeviceAllow`).
- `GDM-C-005` `[DOC]` ✅ The PPS path SHALL be treated as OS-millisecond-bound and
  SHALL NOT be presented as a metrology reference (the `note` warning is
  mandatory on every `pps_study`).
- `GDM-C-006` `[CODE]` ✅ Config parsing SHALL NOT pull libhidapi into import
  scope (`config.py` is hidapi-free) so a consumer host without libhidapi can
  still import the package (e.g. for the mDNS-only consumer path).
- `GDM-C-007` `[DOC]` 🟡 Only the **LBE-1421** is live-hardware-validated;
  LBE-1420 / LBE-1423 / LBE-Mini drivers are ported from upstream and covered by
  byte-level unit tests but unexercised against live hardware. *(gap —
  `GDM-F-090`.)*

## 6. Functional requirements

### 6.1 Detection & disambiguation
- `GDM-F-001` `[DOC]` ✅ SHALL enumerate attached Leo Bodnar USB HIDs by VID
  `0x1DD2` and the four known PIDs (1420 `0x2443`, 1421 `0x2444`, 1423 `0x226F`,
  Mini `0x2211`), dispatching to the per-model driver by PID.
- `GDM-F-002` `[DOC]` ✅ When exactly one device is present and no
  `[[monitor.device]]` is declared, SHALL match it implicitly (Case A); when > 1
  device is present without declarations, SHALL **refuse to guess** and emit an
  error (matches lbe-142x `--pid` refusal semantics).
- `GDM-F-003` `[CODE]` ✅ When `[[monitor.device]]` entries are declared, SHALL
  match each by case-insensitive serial, reporting unmatched declarations and
  unclaimed present devices (neither fatal on its own).

### 6.2 Health probing
- `GDM-F-010` `[DOC]` ✅ SHALL probe device health on a fixed cadence
  (`probe_interval_sec`, default 10 s): PLL lock, FLL mode, GPS fix, satellites
  used, fix age, antenna status, output frequencies, output power, 1 PPS
  presence, and (Mini) signal-loss count.
- `GDM-F-011` `[DOC]` ✅ SHALL source GPS fix / sats / fix-age from **NMEA** over
  the CDC port on the 1421/1423, and from **UBX NAV-PVT** on the interrupt-IN
  endpoint on the Mini.
- `GDM-F-012` `[CODE]` ✅ SHALL degrade gracefully to the HID status-bitmap
  GPS_LOCK bit when the NMEA CDC stream is unavailable (1420 has none; 1421/1423
  port may be unreadable), still reporting HID-derived health.
- `GDM-F-013` `[CODE]` ✅ SHALL capture 1 PPS as DCD-line edges via `TIOCMIWAIT`
  on the 1421/1423 (idle CPU flat between edges), maintaining a 60 s rolling
  window; the Mini and 1420 have no PPS path.
- `GDM-F-014` `[CODE]` ✅ The daemon SHALL run a per-device long-lived
  `DeviceWorker` (NMEA reader thread + PPS tracker thread + cached firmware) and
  a 1 Hz **fast-NMEA republish** loop so `pps_utc_sec`/`fix_age_sec`/`gps_fix`
  stay fresh (≤ ~1 s) between probe ticks — required by hf-timestd's T6 BPSK-PPS
  ±0.5 s pairing guard.

### 6.3 A-level classification
- `GDM-F-020` `[DOC]` ✅ SHALL emit `a_level_hint ∈ {A1, A0}` with `a_level_reason`
  naming the **first failing predicate**. `A1` iff `pll_locked` ∧ GPS fix (2D/3D
  or HID GPS_LOCK) ∧ (`antenna_ok` is None|True) ∧ `fix_age_sec < 30` ∧
  `probe_age_sec < 2 × interval` ∧ (PPS not expected ∨ ≥ 55/60 edges); else `A0`.
- `GDM-F-021` `[DOC]` ✅ Classification SHALL be **model-agnostic** — it consumes
  a normalized `Health` dataclass where variant-specific gaps (Mini no antenna,
  1420 no PPS) are `None` fields, not driver branches.
- `GDM-F-022` `[DOC]` ✅ The hint SHALL be advisory; consumers (hf-timestd) MAY
  override on T-level cross-check (the producer does not assert authority).

### 6.4 Publication
- `GDM-F-030` `[DOC]` ✅ SHALL write per-device state **atomically** to
  `/run/gpsdo/<serial>.json` (schema v1) on every probe tick, plus the 1 Hz
  fast-NMEA overlay republish.
- `GDM-F-031` `[DOC]` ✅ SHALL write an aggregate `/run/gpsdo/index.json`
  (`{serial, model, governs, a_level_hint, written_utc}` per device) for fast TUI
  consumption.
- `GDM-F-032` `[CODE]` ✅ On device disappearance from HID enumeration SHALL stop
  the worker, drop it from the index, and (mDNS) withdraw the advertisement.

### 6.5 mDNS advertisement
- `GDM-F-040` `[DOC]` ✅ SHALL advertise each device as `_gpsdo._tcp` (instance
  name = sanitized serial, port 0 — metadata only), TXT carrying `schema=v1`,
  `host`, `model`, `serial`, `governs`, `f1`/`f2`, `pps`, `a_level`, `fresh`,
  `probe_age`.
- `GDM-F-041` `[DOC]` ✅ Advertisements SHALL re-publish on any TXT change,
  heartbeat ≤ 60 s, and be withdrawn immediately when the device disappears or
  the daemon shuts down; consumers MUST gate on `schema=v1`.
- `GDM-F-042` `[CODE]` ✅ mDNS SHALL be optional (`mdns_enabled`); advertiser-init
  failure SHALL be logged and the daemon SHALL continue (file contract is
  authoritative).

### 6.6 Firmware advisory (Mini)
- `GDM-F-050` `[DOC]` ✅ On the Mini SHALL poll **UBX-MON-VER** (once, cached),
  report u-blox SW/HW/PROTVER, and classify it against
  `data/firmware_advisories.toml` into `{current, outdated, unknown}`.
- `GDM-F-051` `[CODE]` ✅ For the 142x family SHALL emit `firmware = null`,
  `firmware_source = "unavailable"`, and preserve the unmapped status bytes as
  `raw_trailing_hex` for future reverse-engineering.

### 6.7 Configuration (write path)
- `GDM-F-060` `[DOC]` 🟡 SHALL support configuring output frequencies, PPS
  on/off, PLL/FLL mode, and (Mini) drive strength. `set_frequency` is
  implemented for the 142x family; on the Mini it is **`NotImplementedError`**
  pending the Si5351 divider solver. *(gap — `GDM-F-091`.)*
- `GDM-F-061` `[DOC]` 🟡 The canonical operator config surface SHALL be
  `smd gpsdo config` (sigmond); the local `gpsdo-monitor config` subcommand is a
  **placeholder stub** (exits 2 with a pointer) for manual debugging only.
  *(gap — `GDM-F-092`.)*

### 6.8 CLI & self-description (infra surface)
- `GDM-F-070` `[DOC]` ✅ SHALL expose a CLI: `detect`, `status`
  (`--nmea-sample-sec`/`--pps-sample-sec`), `serve` (the daemon), `tui`
  (`--serial`/`--refresh-sec`, `[tui]` extra), `config` (stub), `inventory`.
- `GDM-F-071` `[CODE]` ✅ SHALL implement a **minimal** `inventory --json` per
  contract §3 reporting only `{client, version, contract_version, hardware_present}`
  (`hardware_present` = true when ≥1 Leo Bodnar HID is enumerable, false when
  none, null on enumeration error), pure-JSON on stdout — so sigmond can mark it
  core-but-dormant when no GPSDO is attached. It SHALL NOT implement
  `validate`/per-instance inventory/`config init|edit|show|apply` (infra-only).

## 7. Quality / non-functional requirements

- `GDM-Q-001` `[DOC]` ✅ Any operator-relevant failure (PLL unlock, antenna
  fault, GPS loss, USB unplug, daemon crash, PPS silent) SHALL surface as a
  visible `A0` with a named reason within ≤ 2 × probe_interval (≤ 20 s default).
- `GDM-Q-002` `[CODE]` ✅ Per-device JSON and `index.json` SHALL be written
  **atomically** (`atomic_write`); consumers read whole-or-not-at-all.
- `GDM-Q-003` `[CODE]` ✅ A failed probe tick, a refused/contended tty, a failed
  mDNS init, or a slow MON-VER poll SHALL be caught and logged and SHALL NOT
  crash the daemon (`Restart=on-failure`, `RestartSec=5` as backstop).
- `GDM-Q-004` `[DOC]` ✅ Consumers SHALL be able to distinguish "PPS not tracking"
  (`pps_study.enabled=false`) from "tracking with zero edges" (a downgrade
  signal) — the two are encoded distinctly.
- `GDM-Q-005` `[CODE]` ✅ The schema SHALL be **additive-only within v1**:
  consumers MUST ignore unknown fields; new fields MUST NOT change existing
  semantics.
- `GDM-Q-006` `[CODE]` ✅ The daemon SHALL run hardened (`User=gpsdo`,
  `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, kernel-tunable/module
  protection) with device access narrowed to the GPSDO nodes.
- `GDM-Q-007` `[CODE]` ✅ stdout of `inventory` SHALL be pure JSON (logging to
  stderr) so sigmond can parse it.
- `GDM-Q-008` `[DOC]` ✅ Install SHALL be idempotent and standalone-safe (no
  sigmond required): apt libhidapi, `gpsdo` sysuser/group, udev rule, editable
  install, unit enable+start, default config preserved.
- `GDM-Q-009` `[CODE]` 🟡 Remote/multi-host correctness depends on serial-keyed
  mDNS (Case D decoupling consumers from GPSDO-host routing); the splitter /
  multi-host paths are documented but not live-validated. *(gap — `GDM-Q-010`.)*

## 8. External interfaces

### 8.1 Inputs
- **Hardware:** Leo Bodnar GPSDO over USB HID feature reports (VID `0x1DD2`;
  PIDs 0x2443/0x2444/0x226F/0x2211), plus CDC `/dev/ttyACM*` (NMEA + DCD PPS on
  1421/1423) and HID interrupt-IN UBX (Mini).
- **Config:** `/etc/gpsdo-monitor/config.toml` `[monitor]` — `probe_interval_sec`
  (default 10), `run_dir` (`/run/gpsdo`), `pps_study_enabled`, `mdns_enabled`,
  and `[[monitor.device]]` (`serial` + `governs[]`, the GPSDO→radiod governance
  map). All optional; zero config = Case A autodetect.
- **Advisory data:** `data/firmware_advisories.toml` (PROTVER → advisory).

### 8.2 Outputs
- **Per-device state:** `/run/gpsdo/<serial>.json` (schema v1) — `device`,
  `governs`, `health` (`pll_locked`, `fll_mode`, `gps_fix`, `sats_used`,
  `fix_age_sec`, `antenna_ok`, `signal_loss_count`, `outputs_enabled`),
  `outputs` (`out1_hz`/`out1_power`/`out2_*`/`pps_enabled`/`drive_ma`),
  `pps_study` (`enabled`, `window_sec`, `edges`, `period_ms_p50/p95`,
  `last_edge_utc`, mandatory `note`), `firmware_advisory` (Mini), `a_level_hint`,
  `a_level_reason`.
- **Aggregate:** `/run/gpsdo/index.json`.
- **mDNS:** `_gpsdo._tcp` TXT (§6.5 / `GDM-F-040`).
- **Logs:** journald (the `serve` daemon); `status`/`inventory` JSON on stdout.

### 8.3 Contracts / APIs (reference, not restated)
- `GDM-I-001` `[DOC]` ✅ **Infra component, not full-contract.** `deploy.toml`
  declares `kind="infra"` and `[systemd].units=["gpsdo-monitor.service"]`
  (concrete, non-templated). sigmond installs/starts/stops/monitors the single
  unit but does NOT expect `validate`/per-instance inventory/config flow. It is
  registered in sigmond's catalog as `local_radiod_infra` and as a `dasi2`
  optional / `hardware_gated` ("GPSDO, USB/HID") component, skipped-with-a-warning
  when no GPSDO is attached.
- `GDM-I-002` `[CODE]` ✅ Implements the contract **§3 `hardware_present`
  amendment** only: `inventory --json` reports `client`, `version`,
  `contract_version="0.8"`, `hardware_present` (Phase D dormancy). Full field
  semantics: contract §3.
- `GDM-I-003` `[DOC]` ✅ Is the A-level **producer** the contract §18 timing
  authority consumes indirectly: hf-timestd's `GpsdoProbe`/`GpsdoMdnsProbe` reads
  `/run/gpsdo/*.json` (or mDNS) each authority tick and supplies the
  `a_level_provider` (A1/A0) — but hf-timestd, not this component, owns the
  authority decision. The file/mDNS contract is schema-v1 (§9), versioned
  independently of the client contract.
- `GDM-I-004` `[DOC]` ✅ The `governs` map is consumed by sigmond's
  `gpsdo_governor_coverage` harmonize rule on every `smd validate`: every local
  radiod in `coordination.toml` MUST have exactly one governor (zero warns,
  multiple errors).

## 9. Data requirements

Runtime-only, no persistent store (state lives under `/run`, lost on reboot by
design — it is re-derived on next probe). Schema **v1** is the durable contract:
per-device JSON + index + mDNS TXT, additive-only (`GDM-Q-005`). `written_utc`
and `probe_interval_sec` stamp every record; `probe_age` / `fresh` let consumers
judge staleness. No retention/volume budget (ephemeral). Reference data:
`firmware_advisories.toml` (PROTVER table, reconcile against u-blox releases).

## 10. Dependencies & development sequence

**Runtime deps:** `hidapi ≥ 0.14` (HID feature-report I/O), `zeroconf ≥ 0.131`
(mDNS), `pyserial ≥ 3.5` (CDC NMEA + DCD PPS). Optional `[tui]`: `textual`.
Build/dev: `pytest`, `pytest-cov`, `ruff`. System: `libhidapi-hidraw0` (apt),
`gpsdo` sysuser, udev rule. Installed via sigmond's shared `_ensure_uv` helper
(uv-based, editable sibling install); standalone-safe.

**Development sequence (intended, recovered as requirement):** port the upstream
`bvernoux/lbe-142x` byte layouts for all four models → live-validate the
**LBE-1421** (the station's hardware) end-to-end → wire the daemon (probe →
publish → advertise → advise) → add the §3 `hardware_present` inventory (Phase D
dormancy) and the sigmond harmonize/TUI integration → 1 Hz fast-NMEA republish
to satisfy hf-timestd's T6 ±0.5 s guard. **Deferred:** live-validating the
1420/1423/Mini drivers; the Mini Si5351 `set_frequency` solver; the full
`gpsdo-monitor config` / `smd gpsdo config` write surface.

## 11. Acceptance criteria & verification

- Driver correctness → `uv run pytest` (99 unit tests; byte-level model parses +
  a full `Service._tick()` against a fake pyserial/hidapi simulated 1421, no
  hardware).
- Detection/disambiguation → `gpsdo-monitor detect` enumerates; multi-device
  no-config refusal is asserted in discovery tests.
- A-level correctness → `gpsdo-monitor status` JSON + the failure-mode matrix in
  docs/TOPOLOGY.md (each failure → named `A0` within ≤ 20 s).
- Contract/infra surface → `gpsdo-monitor inventory --json` (pure JSON, correct
  `hardware_present`) consumed by `smd status`; `gpsdo_governor_coverage`
  harmonize rule passes on `smd validate`.
- Integration → hf-timestd `GpsdoProbe` reads A1 from a live 1421; mDNS visible
  to a remote consumer.
- Live-hardware coverage → currently **LBE-1421 only** (the open verification
  gap for the other three models).

## 12. Risks & open questions

- `GDM-F-090` `[NEW]` 🟡 **Partial model validation:** only the LBE-1421 is
  live-hardware-validated; 1420/1423/Mini drivers are byte-tested only. SHALL be
  validated against live hardware or the matrix kept honestly marked "ported,
  unvalidated." *(candidate #18 issue.)*
- `GDM-F-091` `[NEW]` ⬜ **Mini `set_frequency` stub:** raises
  `NotImplementedError` pending the Si5351 divider-chain solver
  (`mini_solve_pll`). SHALL be ported + live-validated before any Mini
  frequency-config claim.
- `GDM-F-092` `[NEW]` ⬜ **`config` write surface incomplete:** the local
  `gpsdo-monitor config` subcommand is a stub and `smd gpsdo config` is the
  declared-but-unbuilt canonical surface. SHALL be implemented (frequencies / PPS
  / PLL-FLL / drive) or the documentation reconciled to "read-only monitor."
- `GDM-Q-010` `[NEW]` ⬜ **Multi-host/splitter paths unvalidated:** Cases C/D
  (mDNS serial-keyed remote consumption, splitter governance) are documented but
  not live-validated; the serial-keyed decoupling that prevents the ScreenPi4
  hostname-strand incident class needs an end-to-end test.
- `GDM-F-093` `[NEW]` ⬜ **`raw_trailing_hex` reverse-engineering owed:** the
  1421/1423 status bytes 21..59 (candidate firmware/build-date region) are
  preserved but uncharacterized; firmware readback for the 142x family stays
  `unavailable` until decoded.

## 13. Traceability

| Requirement | #18 issue | Verification | PSWS #6 |
|---|---|---|---|
| GDM-I-003 (A-level producer) | Clients: hf-timestd authority | hf-timestd GpsdoProbe reads A1 | #6:50 (timing-tiering) |
| GDM-I-002 (§3 hardware_present) | infra: install-orchestration Phase D | `inventory --json` + `smd status` | — |
| GDM-I-004 (governs coverage) | sigmond: harmonize | `smd validate` gpsdo_governor_coverage | #6:31 (sensor integ.) |
| GDM-F-090 (model validation) | *(new — file)* | live 1420/1423/Mini bring-up | — |
| GDM-F-091 (Mini set_frequency) | *(new — file)* | Si5351 solver + hardware test | — |
| GDM-F-092 (config write surface) | *(new — file)* | smd gpsdo config round-trip | — |
| GDM-Q-010 (multi-host/splitter) | *(new — file)* | remote mDNS consume test | — |

*New rows (GDM-F-090/091/092/093, GDM-Q-010) are this review's surfaced gaps;
promote to #18 (infra / Clients: hf-timestd authority).*
