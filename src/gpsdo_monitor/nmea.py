"""NMEA reader for LBE-1421 / LBE-1423 (and any other 1DD2 device that
streams NMEA on a CDC-ACM interface).

Scope intentionally narrow: we only want the three fields Health needs —
`gps_fix` ("no_fix" / "2D" / "3D"), `sats_used` (int), and `fix_age_sec`
(wall-clock seconds since the last RMC-valid sentence). Per-satellite
constellation views (GSV → sv-info) belong in a live monitor UI, not
here.

Port discovery matches the tty to its owning USB device by the same
serial string hidapi reports — this is what keeps the N:M topology
(multiple GPSDOs on one host) honest. Probe-the-port trick from
upstream is kept as a last-resort fallback.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gpsdo_monitor.hid_xport import VID_LBE


@dataclass
class NmeaState:
    """Accumulator updated one sentence at a time by `feed()`."""

    gps_fix: str | None = None          # "no_fix" | "2D" | "3D"
    sats_used: int | None = None
    last_rmc_valid_wall: float | None = None   # time.time() of latest RMC with status 'A'
    bad_checksum_count: int = 0
    # Most recent position fix (decimal degrees; +N/+E). Captured from RMC
    # (validity-gated) and GGA. Lets a Leo Bodnar (or any NMEA GPSDO) seed the
    # station's grid square at install time — see maidenhead().
    latitude: float | None = None
    longitude: float | None = None
    # Altitude above mean sea level (metres), captured from GGA field 9.
    # Comes free with the fix; PSWS/station config want the elevation.
    altitude_m: float | None = None
    # Integer UTC second the most recent RMC-valid sentence describes,
    # paired with time.monotonic() at the moment that sentence was read.
    # The pair is what hf-timestd's T5 disambig consumes — sub-second
    # part of the NMEA time is intentionally discarded because it reflects
    # sentence-emission delay, not the PPS edge.
    pps_utc_sec: int | None = None
    host_monotonic_at_read: float | None = None

    def fix_age_sec(self, *, now: float | None = None) -> float | None:
        """Wall-clock seconds since the last RMC we treated as valid.

        None if we've never seen one. A high value is not necessarily a
        downgrade trigger on its own — the classifier in `health.py`
        combines it with the other predicates."""
        if self.last_rmc_valid_wall is None:
            return None
        t = now if now is not None else time.time()
        return max(0.0, t - self.last_rmc_valid_wall)

    def maidenhead(self, precision: int = 6) -> str | None:
        """The current fix as a Maidenhead locator, or None if no position."""
        if self.latitude is None or self.longitude is None:
            return None
        return to_maidenhead(self.latitude, self.longitude, precision)


def _parse_coord(raw: str, hemi: str) -> float | None:
    """Convert an NMEA ddmm.mmmm / dddmm.mmmm + hemisphere to decimal degrees.

    Returns None on malformed input. Latitude uses 2 degree digits, longitude
    3; we infer the split from the position of the decimal point (minutes are
    always the two digits before it), so one parser handles both.
    """
    if not raw or "." not in raw:
        return None
    try:
        dot = raw.index(".")
        deg = int(raw[: dot - 2])
        minutes = float(raw[dot - 2:])
    except (ValueError, IndexError):
        return None
    val = deg + minutes / 60.0
    if hemi in ("S", "W"):
        val = -val
    return val


def to_maidenhead(lat: float, lon: float, precision: int = 6) -> str:
    """Decimal-degree lat/lon → Maidenhead grid locator.

    precision is the number of characters (4, 6, 8, …); 6 ("EM38ww") is the
    usual station default. Field/square/subsquare/extended pairs alternate
    letters and digits per the standard.
    """
    lat = max(-90.0, min(90.0, lat)) + 90.0
    lon = max(-180.0, min(180.0, lon)) + 180.0
    out = []
    # Field (A-R): 20° lon, 10° lat
    out.append(chr(ord("A") + int(lon // 20)))
    out.append(chr(ord("A") + int(lat // 10)))
    lon %= 20.0
    lat %= 10.0
    # Square (0-9): 2° lon, 1° lat
    out.append(str(int(lon // 2)))
    out.append(str(int(lat // 1)))
    lon %= 2.0
    lat %= 1.0
    # Subsquare (a-x): 5' lon, 2.5' lat
    out.append(chr(ord("a") + int(lon / (2.0 / 24))))
    out.append(chr(ord("a") + int(lat / (1.0 / 24))))
    lon %= (2.0 / 24)
    lat %= (1.0 / 24)
    # Extended square (0-9), and beyond if asked
    while len(out) < precision:
        out.append(str(int(lon / (2.0 / 240))))
        out.append(str(int(lat / (1.0 / 240))))
        break
    return "".join(out[:precision])


def checksum_ok(line: str) -> bool:
    """Validate a NMEA sentence's `*HH` checksum.

    Matches upstream `nmea_checksum_ok()` semantics: the checksum is the
    XOR of all bytes between `$` and `*`. Lines without `*` are rejected
    here; the caller can decide whether to accept a checksum-less line
    (we don't — CDC bridges rarely drop `*CS` on this device)."""
    if not line or line[0] != "$":
        return False
    star = line.find("*")
    if star < 0 or star + 3 > len(line):
        return False
    body = line[1:star]
    ck = 0
    for ch in body.encode("ascii", errors="replace"):
        ck ^= ch
    try:
        expected = int(line[star + 1 : star + 3], 16)
    except ValueError:
        return False
    return expected == ck


def _strip_fields(line: str) -> list[str] | None:
    """Return the comma-separated fields of a NMEA body, or None if the
    sentence is malformed."""
    if not line or line[0] != "$":
        return None
    star = line.find("*")
    body = line[1:star] if star >= 0 else line[1:]
    return body.split(",")


def feed(state: NmeaState, line: str, *, now: float | None = None) -> None:
    """Update `state` from one NMEA sentence.

    Tolerant of missing trailing `*CS` only if the line was already
    vetted upstream; here we require a valid checksum to count the
    sentence. Unknown sentence types are ignored."""
    if not line.startswith("$"):
        return
    if "*" in line:
        if not checksum_ok(line):
            state.bad_checksum_count += 1
            return
    fields = _strip_fields(line)
    if not fields or len(fields[0]) < 5:
        return
    t = now if now is not None else time.time()
    sentence = fields[0][2:]   # strip talker (GP, GN, GL, ...)

    if sentence == "RMC":
        # $xxRMC,utc,status,lat,N/S,lon,E/W,sog,cog,date,magvar,...
        # status 'A' = active/valid, 'V' = void.
        if len(fields) > 9 and fields[2] == "A":
            state.last_rmc_valid_wall = t
            # Position (fields: 3=lat, 4=N/S, 5=lon, 6=E/W).
            lat = _parse_coord(fields[3], fields[4])
            lon = _parse_coord(fields[5], fields[6])
            if lat is not None and lon is not None:
                state.latitude = lat
                state.longitude = lon
            time_str = fields[1]
            date_str = fields[9]
            try:
                hh = int(time_str[0:2])
                mm = int(time_str[2:4])
                ss = int(time_str[4:6])
                dd = int(date_str[0:2])
                mo = int(date_str[2:4])
                yy = int(date_str[4:6])
                dt = datetime(2000 + yy, mo, dd, hh, mm, ss, tzinfo=timezone.utc)
                state.pps_utc_sec = int(dt.timestamp())
                state.host_monotonic_at_read = time.monotonic()
            except (ValueError, IndexError):
                pass
    elif sentence == "GGA":
        # $xxGGA,utc,lat,N/S,lon,E/W,quality,numSV,hdop,...
        # quality: 0=invalid, 1=GPS, 2=DGPS, 4=RTK fix, 5=RTK float, 6=dead-reckoning, ...
        if len(fields) > 7:
            try:
                n = int(fields[7])
            except ValueError:
                n = None
            if n is not None:
                state.sats_used = n
            # Position (fields: 2=lat, 3=N/S, 4=lon, 5=E/W) — fills in when no
            # RMC has been seen yet; quality 0 leaves lat/lon empty.
            try:
                quality = int(fields[6])
            except (ValueError, IndexError):
                quality = 0
            if quality > 0:
                lat = _parse_coord(fields[2], fields[3])
                lon = _parse_coord(fields[4], fields[5])
                if lat is not None and lon is not None:
                    state.latitude = lat
                    state.longitude = lon
            # Altitude MSL (field 9, metres) — free with the fix; PSWS wants it.
            try:
                if len(fields) > 9 and fields[9]:
                    state.altitude_m = float(fields[9])
            except (ValueError, IndexError):
                pass
    elif sentence == "GSA":
        # $xxGSA,mode1,mode2,prn1..prn12,pdop,hdop,vdop
        # mode2: 1=no fix, 2=2D, 3=3D.
        if len(fields) > 2:
            state.gps_fix = {"1": "no_fix", "2": "2D", "3": "3D"}.get(fields[2], state.gps_fix)


# --- Port discovery ----------------------------------------------------


def _read_sysfs(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except OSError:
        return None


def find_ttys_by_usb_serial(
    target_serial: str, *, vid: int = VID_LBE
) -> list[Path]:
    """Return `/dev/ttyACM*` nodes whose owning USB device matches
    `vid:<any pid>` and reports `target_serial`.

    Uses sysfs rather than probing — cheaper, and unambiguous in the
    N-GPSDOs-on-one-host case."""
    want_vid = f"{vid:04x}"
    out: list[Path] = []
    sys_tty = Path("/sys/class/tty")
    if not sys_tty.is_dir():
        return out
    for tty in sorted(sys_tty.glob("ttyACM*")):
        # /sys/class/tty/ttyACMx/device is the USB interface; its parent
        # is the USB device with idVendor, idProduct, serial.
        dev_link = tty / "device"
        try:
            usb_dev = dev_link.resolve().parent
        except OSError:
            continue
        serial = _read_sysfs(usb_dev / "serial")
        vidstr = _read_sysfs(usb_dev / "idVendor")
        if vidstr is None or serial is None:
            continue
        if vidstr.lower() == want_vid and serial == target_serial:
            out.append(Path("/dev") / tty.name)
    return out


def sample(
    tty_path: Path,
    *,
    duration_sec: float = 1.5,
    baudrate: int = 9600,
) -> NmeaState:
    """One-shot NMEA sampler — open the port, read lines for up to
    `duration_sec`, return accumulated state.

    Intended for the `gpsdo-monitor status` command. For the daemon,
    use `NmeaReader` (below) which keeps the state fresh in a background
    thread so each probe tick can snapshot without blocking."""
    import serial  # pyserial; imported lazily so unit tests don't need it

    state = NmeaState()
    deadline = time.monotonic() + duration_sec
    with serial.Serial(
        str(tty_path), baudrate=baudrate, timeout=0.2,
        rtscts=False, dsrdtr=False,
    ) as s:
        while time.monotonic() < deadline:
            raw = s.readline()
            if not raw:
                continue
            try:
                line = raw.decode("ascii", errors="replace").strip()
            except UnicodeDecodeError:
                continue
            if line.startswith("$"):
                feed(state, line)
    return state


# --- Long-lived reader for the daemon ----------------------------------


class NmeaReader:
    """Background-thread NMEA reader — keeps a fresh `NmeaState` for
    consumers that poll it on their own cadence.

    Opens the tty once, reads one line at a time, updates shared
    state under a lock. `snapshot()` returns a cheap copy for the
    daemon's probe tick. `start()` / `stop()` manage the worker
    thread; `stop()` closes the serial port so the blocking readline
    returns."""

    def __init__(self, tty_path: Path, *, baudrate: int = 9600) -> None:
        self.tty_path = Path(tty_path)
        self.baudrate = baudrate
        self._state = NmeaState()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial = None  # type: ignore[assignment]
        self._open_error: str | None = None

    @property
    def open_error(self) -> str | None:
        """Reason the serial port failed to open, or None on success."""
        return self._open_error

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("NmeaReader already started")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"nmea-{self.tty_path.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout_sec: float = 2.0) -> None:
        self._stop.set()
        s = self._serial
        if s is not None:
            # Closing the port makes the blocking readline return
            # immediately so the worker can exit the loop.
            try:
                s.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=timeout_sec)
            self._thread = None

    def snapshot(self) -> NmeaState:
        """Return a shallow copy of the current state. Safe to call
        from any thread; each call materialises a fresh dataclass."""
        with self._lock:
            return NmeaState(
                gps_fix=self._state.gps_fix,
                sats_used=self._state.sats_used,
                last_rmc_valid_wall=self._state.last_rmc_valid_wall,
                bad_checksum_count=self._state.bad_checksum_count,
                pps_utc_sec=self._state.pps_utc_sec,
                host_monotonic_at_read=self._state.host_monotonic_at_read,
            )

    # Backoff cap for the reopen-on-error loop.  Read errors during a
    # transient race (another process briefly opening the device) can
    # fire in tight bursts; we don't want to hammer the kernel, but we
    # MUST keep retrying or the NMEA reader stays dead until daemon
    # restart.  Cap chosen to recover within one health-probe interval.
    _REOPEN_BACKOFF_S = 1.0

    def _run(self) -> None:
        import serial  # pyserial; imported lazily

        while not self._stop.is_set():
            try:
                self._serial = serial.Serial(
                    str(self.tty_path), baudrate=self.baudrate, timeout=0.2,
                    rtscts=False, dsrdtr=False,
                )
                # Clear any previous open_error so callers can see we
                # recovered.  Set fresh on subsequent failures.
                self._open_error = None
            except (OSError, serial.SerialException) as e:
                self._open_error = f"{type(e).__name__}: {e}"
                # Wait before retrying; respect stop signal.
                if self._stop.wait(self._REOPEN_BACKOFF_S):
                    return
                continue

            if self._read_until_error():
                # Clean stop requested.
                return
            # Read loop exited due to error — close and reopen.
            s = self._serial
            self._serial = None
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
            if self._stop.wait(self._REOPEN_BACKOFF_S):
                return

    def _read_until_error(self) -> bool:
        """Read NMEA lines from the open serial until error or stop.

        Returns True if the loop exited because :py:attr:`_stop` was set
        (clean shutdown — caller should not reopen), False if a read
        error broke the loop (caller should close + reopen).
        """
        import serial  # pyserial; for SerialException type
        while not self._stop.is_set():
            try:
                raw = self._serial.readline()
            except (OSError, serial.SerialException, TypeError):
                # Three failure modes lumped together because the
                # right response is the same — break out and let
                # _run() decide whether to reopen:
                #   - OSError: port vanished (device unplugged)
                #   - SerialException: pyserial surface of the above
                #   - TypeError: stop() closed the fd while the
                #     worker was blocked in readline; pyserial
                #     re-enters os.read with fd=None.  If stop is
                #     already set, _run() will exit cleanly.
                return self._stop.is_set()
            if not raw:
                continue
            try:
                line = raw.decode("ascii", errors="replace").strip()
            except UnicodeDecodeError:
                continue
            if not line.startswith("$"):
                continue
            with self._lock:
                feed(self._state, line)
        return True
