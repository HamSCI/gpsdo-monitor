# gpsdo-monitor

Health monitor, mDNS advertiser, and configurator for
[Leo Bodnar](http://www.leobodnar.com/) GPS-disciplined clock sources
(LBE-1420, LBE-1421, LBE-1423, LBE-Mini).

Built for the [HamSCI](https://hamsci.org) / sigmond SDR station suite:
supplies an **actively probed A-level** signal to
[`hf-timestd`](https://github.com/HamSCI/hf-timestd)'s authority
manager — closing the "GPSDO is *probably* still disciplining the RX888
ADC" gap — but the daemon stands alone and emits a simple file + mDNS
contract any consumer can read.

## Status

Working on Linux (primary target: Debian 12+ / RX888-class Beelink EQ).
Live-validated on the LBE-1421; the LBE-1420 and LBE-Mini drivers are
ported from [bvernoux/lbe-142x](https://github.com/bvernoux/lbe-142x)
and covered by byte-level unit tests, but have not yet been exercised
against live hardware.

## What it does

1. **Detect** attached Leo Bodnar USB HIDs (VID `0x1DD2`, four known
   PIDs). Refuses to guess when more than one is attached and no
   serial has been declared.
2. **Probe** health at 10 s cadence — PLL lock, GPS fix (from NMEA on
   1421/1423 or NAV-PVT on Mini), antenna status, satellites used, fix
   age, output frequencies, 1 PPS presence, Mini signal-loss count.
3. **Publish** per-device state atomically to
   `/run/gpsdo/<serial>.json` (schema v1), plus an aggregate
   `/run/gpsdo/index.json` for TUI consumption.
4. **Advertise** over mDNS as `_gpsdo._tcp` so remote consumers
   (splitter-fed topologies) can read health without SSH.
5. **Advise** — emits `a_level_hint` (`A1` / `A0`) with a
   human-readable reason, and on the Mini parses UBX-MON-VER to report
   u-blox module firmware and flag outdated PROTVER.
6. **Configure** — set output frequencies, PPS on/off, PLL/FLL mode,
   drive strength (Mini). Primary surface is `smd gpsdo config` in
   sigmond; `gpsdo-monitor config …` is the manual-debugging path.

## What it does *not* do

- It is **not** a metrology reference. The PPS stability numbers it
  publishes are OS-millisecond bound (via `TIOCMIWAIT` on the CDC DCD
  line) and serve only as a liveness + gross-stability indicator.
  Every `pps_study` block carries that warning verbatim in its `note`
  field.
- It does not discipline the host system clock. That's chrony's job.
- It does not reverse-engineer undocumented HID opcodes — it stays
  within the feature-report layouts documented by lbe-142x.

## Hardware support matrix

| Feature            | LBE-1420 | LBE-1421 | LBE-1423 | LBE-Mini |
|--------------------|:--------:|:--------:|:--------:|:--------:|
| HID status parse   | ✓        | ✓        | ✓        | ✓        |
| NMEA fix / sats    | —        | ✓ CDC    | ✓ CDC    | ✓ NAV-PVT |
| 1 PPS edge capture | —        | ✓ DCD    | ✓ DCD    | —        |
| Firmware advisory  | —        | —        | —        | ✓ MON-VER |
| OUT2               | —        | ✓        | ✓        | —        |
| Drive strength (mA)| —        | —        | —        | 8/16/24/32 |
| Max output         | 1.6 GHz  | 1.4 GHz  | 1.4 GHz  | 810 MHz  |

`set_frequency` is implemented for the whole family. The Mini path
solves the Si-synth divider chain in pure Python
(`gpsdo_monitor/mini_pll.py`, ported from David Goncalves'
ringof/lbe-142x, MIT) and was live-validated against a bench Mini.
Mini frequency writes always persist (the hardware has no
temporary-set opcode).

## Install

### Under sigmond (preferred)

```sh
sudo smd install gpsdo-monitor
```

The catalog entry in
[`sigmond/etc/catalog.toml`](https://github.com/HamSCI/sigmond/blob/main/etc/catalog.toml)
clones this repo to `/opt/git/sigmond/gpsdo-monitor` and runs `install.sh`.
Add `[component.gpsdo-monitor] enabled = true` to
`/etc/sigmond/topology.toml` if you want the full-suite walk (`smd
install` without args) to include it.

### Standalone

```sh
git clone https://github.com/HamSCI/gpsdo-monitor /opt/git/sigmond/gpsdo-monitor
sudo /opt/git/sigmond/gpsdo-monitor/install.sh
```

The installer is idempotent and sets up:

- `libhidapi-hidraw0` from apt (if available);
- `gpsdo` system user + group via `systemd-sysusers`;
- `/etc/udev/rules.d/99-gpsdo.rules` so members of `gpsdo` can access
  `/dev/hidraw*` and `/dev/ttyACM*` for VID `0x1dd2`;
- `pip install` of this package into the system Python;
- `gpsdo-monitor.service` (enabled + started);
- a default `/etc/gpsdo-monitor/config.toml` (preserves any existing
  one).

## Usage

```sh
gpsdo-monitor detect               # enumerate attached devices
gpsdo-monitor status               # one-shot JSON: HID + NMEA + PPS sample
gpsdo-monitor status --pps-sample-sec 0   # HID + NMEA only (no 3 s PPS count)
gpsdo-monitor tui                  # live Textual view (requires [tui] extra)
gpsdo-monitor tui --serial XXX     # focus on one device in a multi-GPSDO host
sudo systemctl status gpsdo-monitor   # the daemon (written by install.sh)
```

The TUI is in the `[tui]` optional extra:

```sh
pip install 'gpsdo-monitor[tui]'
```

It mirrors the ka9q-python pattern — sigmond's radiod screen has a
"Deep dive (gpsdo tui)" button that suspends the sigmond app and
shells out to `gpsdo-monitor tui`, passing `--serial` when a
`/run/gpsdo/*.json` declares it governs the selected radiod.

## Topology combinations supported

| Case | Description                                               | Configuration |
|------|-----------------------------------------------------------|---------------|
| A    | 1 GPSDO → 1 RX888 → 1 host (default)                      | autodetect, no explicit config |
| B    | N GPSDOs on one host, each governing one radiod           | `[[monitor.device]]` with `serial` + `governs` |
| C    | N GPSDOs on N hosts                                       | run gpsdo-monitor on each host; consumers use mDNS |
| D    | 1 GPSDO → splitter → N RX888s (same or different hosts)   | single instance, `governs = ["radiod:a", "radiod:b", …]` |
| E    | No GPSDO (dev / degraded)                                 | not installed; consumers fall back to config-declared A-level |

See [docs/TOPOLOGY.md](docs/TOPOLOGY.md) for concrete examples and
[docs/SCHEMA-v1.md](docs/SCHEMA-v1.md) for the full contract.

## Integration

- **[hf-timestd](https://github.com/HamSCI/hf-timestd)**: when
  `[timing.authority_manager.gpsdo].enabled = true`, hf-timestd's
  `GpsdoProbe` reads `/run/gpsdo/*.json` every authority tick and
  supplies the `a_level_provider` that decides A1 vs A0. Any fresh
  device reporting `a_level_hint == "A1"` is sufficient; the authority
  manager cross-checks against T-level witnesses and can override.
- **[sigmond](https://github.com/HamSCI/sigmond)**: the `harmonize`
  rule `gpsdo_governor_coverage` reads the same JSON drop and enforces
  exactly-one-governor-per-local-radiod (zero warns, multiple errors)
  on every `smd validate`. The TUI radiod screen has a deep-dive
  button into this repo's own TUI (see above).

## Development

### Local-only iteration (no systemd)

```sh
uv sync --extra dev --extra tui     # or: pip install -e '.[dev,tui]'
uv run pytest -q                    # 99 tests; unit only, no hardware
```

The daemon path uses a fake pyserial + fake hidapi in
[`tests/test_service.py`](tests/test_service.py) to drive a full
`Service._tick()` against a simulated 1421, so CI can validate the
full composition without USB hardware.

### Editable install on the station host

Skip the push-and-reinstall dance when you're iterating against live
hardware. `install.sh --dev` symlinks `/opt/git/sigmond/gpsdo-monitor` at the
checkout you ran it from and pip-installs editable:

```sh
sudo ~/git/gpsdo-monitor/install.sh --dev
```

End state: the systemd daemon runs from site-packages, whose `.pth`
points at `/opt/git/sigmond/gpsdo-monitor` (→ `~/git/gpsdo-monitor`). Edit
Python, `sudo systemctl restart gpsdo-monitor.service`, done — no
`pip install` between edit and restart. Sigmond's `smd install` /
`smd status` / deploy.toml lookup all find the canonical symlink, so
the rest of the suite sees a normal install.

The `gpsdo` service user must be able to traverse your repo path. On
a host with `/home/<you>` set to mode 700 (typical for shared
machines), relocate the canonical checkout to `/opt/git/sigmond/gpsdo-monitor`
directly (owner = you, mode 755) and point a reverse symlink from
`~/git/gpsdo-monitor` if you want the dev shortcut. `install.sh --dev`
refuses to install into an unreadable tree.

*Non-Python files that don't auto-reload — re-run `install.sh --dev`
after editing any of these:*

- `deploy/gpsdo-monitor.service` (systemd unit body)
- `deploy/99-gpsdo.rules` (udev)
- `deploy/sysusers.d/gpsdo.conf`
- `pyproject.toml` (dependency changes)

### Deploying changes to production

When the station matters and copy-install discipline is desired,
[`scripts/deploy.sh`](scripts/deploy.sh) is the equivalent of
hf-timestd's pull-to-deploy:

```sh
# after committing locally and pushing:
sudo /opt/git/sigmond/gpsdo-monitor/scripts/deploy.sh --pull
```

The script refuses to run on a dirty tree (`--force-dirty` to bypass),
verifies the service user can read the source after any `git pull`,
refreshes the editable install, restarts the unit, and prints the
deployed SHA. `--dry-run` shows what it *would* do without changing
anything.

The **clean-tree check is the point** — it makes "code was edited out
of band" impossible to hide, which is how copy-install deployments
quietly drift away from git.

## Credit

The USB HID opcodes, feature-report layouts, model-PID mapping, NMEA
monitor, and UBX stream parsing in this project are ports of the
documentation and reference implementation in
[bvernoux/lbe-142x](https://github.com/bvernoux/lbe-142x) (MIT). We
stay wire-compatible with that tool and credit it as the canonical
protocol reference.

Earlier protocol work by Simon Unsworth
([simontheu/lbe-1420](https://github.com/simontheu/lbe-1420)) seeded
the lbe-142x project.

## License

MIT — see [LICENSE](LICENSE).
