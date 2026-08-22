"""1PPS edge capture on the CDC DCD line (LBE-1421 / LBE-1423).

Scope is deliberately narrow: this is a **liveness + gross stability
indicator**, not a metrology reference. Edge timestamps are taken in
Python via `time.monotonic()` after a poll of the modem-status ioctl,
so precision is OS-scheduling bound (typically 1-10 ms).

The canonical warning rides in every published report as
`PpsStudy.note` — consumers must not treat these numbers as a timing
source. hf-timestd uses the `pps_study` block only to decide A1/A0,
never as a clock correction.

This is the hardware path hf-timestd calls **T5**: the LBE-1421's 1PPS
reaching the host over USB. It is deliberately the less accurate of the
two PPS couplings — the BPSK-encoded PPS injected into the HF stream
(T6) is sample-exact, whereas a DCD edge over CDC-ACM is quantised by
USB interrupt polling and host scheduling before anything can timestamp
it. hf-timestd's T5 pairing (`t5_rtp_pairing`) therefore consumes the
NMEA-attested integer second (`health.pps_utc_sec`) from the same
`/run/gpsdo/<serial>.json` and carries that latency budget in its
`SIGMA_FLOOR_NS`; the edge timestamps captured here are not published
per-edge, so a DCD-grounded T5 pairing would be a future change on both
sides of the JSON contract.

Two entry points:

  - `sample_pps(tty_path, duration_sec)` — one-shot sampler for the
    `gpsdo-monitor status` command. Polls TIOCMGET at 5 ms and counts
    rising edges; returns a `PpsStudy` populated with what it saw.
  - `PpsTracker` — long-lived tracker for the daemon (`service.py`
    starts one per 1421/1423 when `pps_study_enabled = true`). Owns a
    thread that blocks on `TIOCMIWAIT` for CPU-free edge capture and
    keeps a rolling `window_sec`-second view that `snapshot()` turns
    into the published `pps_study` block.
"""
from __future__ import annotations

import fcntl
import os
import struct
import termios
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from gpsdo_monitor.schema import PpsStudy, utc_now_iso

# Modem-status bit for DCD. Defined in <asm/termbits.h> but not exported
# by Python's `termios` on all kernels, so we pin it explicitly.
TIOCM_CAR = 0x040   # DCD / carrier detect


def _tiocmget(fd: int) -> int:
    """Return the bitmap of modem control lines. Thin ioctl wrapper."""
    raw = fcntl.ioctl(fd, termios.TIOCMGET, struct.pack("i", 0))
    (bits,) = struct.unpack("i", raw)
    return bits


def percentile(sorted_values: list[float], q: float) -> float | None:
    """Nearest-rank percentile over a pre-sorted list.

    Returns None for an empty list. `q` in [0, 1]. Uses the `round-up`
    convention so p95 of a 20-element list is the 19th element, not
    interpolated — good enough for gross-stability monitoring."""
    if not sorted_values:
        return None
    if q <= 0:
        return sorted_values[0]
    if q >= 1:
        return sorted_values[-1]
    n = len(sorted_values)
    idx = min(n - 1, int(q * n))
    return sorted_values[idx]


def _summarise_edges(
    edge_monotonic: list[float],
    last_edge_wall: float | None,
    *,
    window_sec: int,
) -> PpsStudy:
    """Turn a list of monotonic edge timestamps into a PpsStudy."""
    intervals_ms = sorted(
        (edge_monotonic[i] - edge_monotonic[i - 1]) * 1000.0
        for i in range(1, len(edge_monotonic))
    )
    last_edge_utc: str | None = None
    if last_edge_wall is not None:
        # Render to ISO8601-UTC-with-ms; mirror schema.utc_now_iso()'s
        # format so consumers see a uniform timestamp style.
        from datetime import datetime, timezone
        dt = datetime.fromtimestamp(last_edge_wall, tz=timezone.utc)
        last_edge_utc = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
    return PpsStudy(
        enabled=True,
        window_sec=window_sec,
        edges=len(edge_monotonic),
        period_ms_p50=percentile(intervals_ms, 0.50),
        period_ms_p95=percentile(intervals_ms, 0.95),
        last_edge_utc=last_edge_utc,
    )


def sample_pps(
    tty_path: Path,
    *,
    duration_sec: float = 3.0,
    poll_ms: float = 5.0,
) -> PpsStudy:
    """Count rising DCD edges over `duration_sec`; return a PpsStudy.

    Polls TIOCMGET because it runs inside the one-shot CLI where a
    TIOCMIWAIT thread would be overkill. Resolution is bounded by
    `poll_ms` (default 5 ms), which is well below the 1 Hz signal's
    pulse width on the 1421 — we won't miss edges."""
    poll_interval = poll_ms / 1000.0
    fd = os.open(str(tty_path), os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
    try:
        deadline = time.monotonic() + duration_sec
        last_dcd = bool(_tiocmget(fd) & TIOCM_CAR)
        edge_monotonic: list[float] = []
        last_edge_wall: float | None = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                break
            time.sleep(min(poll_interval, deadline - now))
            dcd = bool(_tiocmget(fd) & TIOCM_CAR)
            if dcd and not last_dcd:
                edge_monotonic.append(time.monotonic())
                last_edge_wall = time.time()
            last_dcd = dcd
    finally:
        os.close(fd)
    return _summarise_edges(
        edge_monotonic, last_edge_wall, window_sec=int(round(duration_sec)),
    )


# --- Long-lived tracker (daemon) ---------------------------------------


@dataclass
class PpsTracker:
    """Rolling-window edge tracker for the daemon path.

    A background thread blocks on TIOCMIWAIT, so idle CPU usage is zero
    between edges (vs `sample_pps`, which polls). Snapshot is cheap
    and lock-free for the reader because we copy the deque under the
    lock."""

    window_sec: int = 60
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)
    _edges_monotonic: deque[float] = field(default_factory=deque)
    _last_edge_wall: float | None = None
    _thread: threading.Thread | None = None
    _fd: int | None = None

    def start(self, tty_path: Path) -> None:
        if self._thread is not None:
            raise RuntimeError("PpsTracker already started")
        self._fd = os.open(str(tty_path), os.O_RDWR | os.O_NOCTTY | os.O_CLOEXEC)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="pps-tracker", daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_sec: float = 2.0) -> None:
        self._stop.set()
        if self._fd is not None:
            # Closing the fd unblocks any in-flight TIOCMIWAIT with EIO.
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
            self._thread = None

    def snapshot(self) -> PpsStudy:
        with self._lock:
            edges = list(self._edges_monotonic)
            last_wall = self._last_edge_wall
        return _summarise_edges(edges, last_wall, window_sec=self.window_sec)

    def _run(self) -> None:
        assert self._fd is not None
        while not self._stop.is_set():
            try:
                # TIOCMIWAIT blocks until one of the specified lines
                # changes state. No timeout; we rely on stop() closing
                # the fd to break us out.
                fcntl.ioctl(self._fd, termios.TIOCMIWAIT, TIOCM_CAR)
            except OSError:
                return
            if self._stop.is_set():
                return
            try:
                dcd = bool(_tiocmget(self._fd) & TIOCM_CAR)
            except OSError:
                return
            if not dcd:
                continue  # trailing edge; we only count rising
            mono = time.monotonic()
            wall = time.time()
            with self._lock:
                self._edges_monotonic.append(mono)
                self._last_edge_wall = wall
                # Evict edges older than the window.
                cutoff = mono - float(self.window_sec)
                while self._edges_monotonic and self._edges_monotonic[0] < cutoff:
                    self._edges_monotonic.popleft()
