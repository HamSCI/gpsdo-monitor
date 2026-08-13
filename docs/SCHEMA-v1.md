# Schema v1 — runtime contracts

Two contracts ship at schema version `v1`:

1. **`/run/gpsdo/<serial>.json`** — one file per physically present
   device, written atomically on every probe tick.
2. **mDNS `_gpsdo._tcp`** — one advertisement per device, TXT records
   carrying a compact summary of the JSON above.

Both are designed to be additive-only within `v1`: consumers must
ignore unknown fields, and new fields will not change semantics of
existing ones.

## `/run/gpsdo/<serial>.json`

```jsonc
{
  "schema": "v1",
  "written_utc": "2026-04-24T00:01:12.345Z",
  "probe_interval_sec": 10,
  "host": "bee1.local",

  "device": {
    "model": "lbe-1421",                 // lbe-1420 | lbe-1421 | lbe-1423 | lbe-mini
    "pid":   "0x2444",
    "serial":"LBE1421-ABC123",
    "hid_path": "/dev/hidraw2",
    "firmware": null,                    // string if readable, else null
    "firmware_source": "unavailable",    // "ubx-mon-ver" | "unavailable" | "manual"
    "raw_trailing_hex": "00 00 …"        // optional, 1420/1421/1423 only; debug aid
  },

  "governs": ["radiod:main"],            // operator-declared; source of truth

  "health": {
    "pll_locked":     true,
    "fll_mode":       false,             // null if not applicable
    "gps_fix":        "3D",              // "no_fix" | "2D" | "3D" | null
    "sats_used":      9,
    "fix_age_sec":    0.4,
    "antenna_ok":     true,              // null on Mini (no indicator)
    "signal_loss_count": null,           // int on Mini, null elsewhere
    "outputs_enabled": true
  },

  "outputs": {
    "out1_hz":    122880000,
    "out1_power": "normal",              // "normal" | "low"
    "out2_hz":    10000000,              // null if variant has no OUT2
    "out2_power": "normal",              // null if variant has no OUT2
    "pps_enabled": true,                 // null if variant has no PPS
    "drive_ma":    null                  // 8|16|24|32 on Mini, null elsewhere
  },

  "pps_study": {
    "enabled":        true,              // false if disabled in config or unsupported
    "window_sec":     60,
    "edges":          60,
    "period_ms_p50":  1000.00,
    "period_ms_p95":  1000.18,
    "last_edge_utc":  "2026-04-24T00:01:11.998Z",
    "note":           "OS-millisecond bound; not a metrology reference"
  },

  "firmware_advisory": {                 // Mini only today; null elsewhere
    "status":  "current",                // "current" | "outdated" | "unknown"
    "protver": "18.00",
    "notes":   "u-blox M8, PROTVER 18.00 — NAV-SAT supported"
  },

  "a_level_hint":   "A1",                // "A1" | "A0"
  "a_level_reason": "pll_locked && gps_fix=3D && antenna_ok && pps_present && fresh"
}
```

Additionally an aggregate file `/run/gpsdo/index.json` lists all
presently-probed devices with `{serial, model, governs, a_level_hint,
written_utc}` entries for fast TUI consumption.

## Additive fields (added 2026-08-13, additive within v1)

Three fields were added without a schema bump — old consumers ignore
them; new consumers can rely on `schema=v1` still being accurate.

### `health.antenna_bias_ma`

`int` (mA) or `null`. Measured antenna bias current, where the model
exposes it. Verification status varies per model — see
`docs/PROTOCOL.md` for the byte-level detail:

- **1420**: byte 12, per `ringof/lbe-142x` docs, not bench-verified here.
- **1421 / 1423**: candidate byte 23, decode **disabled** — a
  2026-08-13 bench check couldn't verify it (DC-blocked antenna feed).
  Always `null` on these two models.
- **1425**: byte 23, decoded — the `ringof/lbe-142x` 1425 doc verifies
  it directly on 1425 hardware.
- **Mini**: always `null` (no bias-current hardware).

```jsonc
"health": {
  ...
  "antenna_bias_ma": 3        // int mA, or null if unsupported/undecoded
}
```

### `nav_clock`

`object | null`. LBE-Mini only (parsed from UBX-NAV-CLOCK); `null` on
every other model. The `note` field is a fixed string — always exactly:

```
u-blox receiver self-report; not an independent measurement
```

```jsonc
"nav_clock": {
  "clk_bias_ns":    1234,
  "clk_drift_ns_s": -12,
  "t_acc_ns":       30,
  "f_acc_ps_s":     5,
  "sampled_utc":    "2026-04-24T00:01:11.900Z",
  "note":           "u-blox receiver self-report; not an independent measurement"
}
```

### `receiver_config`

`object | null`. LBE-1425 only — read-only GNSS-receiver configuration
echoes decoded from the status report tail; `null` on every other
model. Raw byte values as the device reports them; decoding
`gnss_mask` into constellation names is left to the consumer.

```jsonc
"receiver_config": {
  "gnss_mask":    71,          // raw byte; 0x47 default = GPS+SBAS+Galileo+GLONASS
  "dyn_model":    2,           // raw byte; u-blox CFG-NAV5 dynModel echo
  "nmea_enabled": true
}
```

## A-level mapping

```
A1  iff  pll_locked
   &&   gps_fix in {"2D","3D"}
   &&   (antenna_ok is None or antenna_ok is True)
   &&   (pps_enabled is None or pps_present_in_window)
   &&   fix_age_sec < 30
   &&   probe_age_sec < 2 * probe_interval_sec
A0  otherwise, with `a_level_reason` naming the first failing predicate
```

The probe is a **hint**; `hf-timestd`'s authority manager is the
arbiter and may override on cross-check against T-level witnesses.

## mDNS `_gpsdo._tcp`

One service advertisement per device. Instance name = the serial
(lowercased, HID-safe). Port = 0 (we serve no TCP; this is metadata
only). TXT keys:

```
schema=v1
host=bee1.local
model=lbe-1421
serial=LBE1421-ABC123
governs=radiod:main,radiod:aux       # comma-separated
f1=122880000
f2=10000000                           # absent if variant has no OUT2
pps=true                              # absent if not applicable
a_level=A1
fresh=8                               # seconds since last successful probe
probe_age=3                           # seconds since last JSON write
```

Consumers MUST gate on `schema=v1`. Advertisements are re-published on
any TXT-field change and heartbeat every 60 s; they are withdrawn
immediately when the device disappears from `hid.enumerate()` or when
the daemon shuts down.
