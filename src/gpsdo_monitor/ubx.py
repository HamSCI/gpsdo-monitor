"""u-blox UBX protocol helpers.

Scope: just enough to support the LBE-Mini's needs — frame the byte
stream out of the HID interrupt-IN endpoint, extract whole UBX
messages, and decode the messages we care about (NAV-PVT for the
fix, NAV-CLOCK for clock solution, MON-VER for firmware).
Per-satellite NAV-SAT decoding is intentionally left out; for
A-level classification we only need `fix_type` and the firmware
advisory.

Message framing:

  B5 62 <class:1> <id:1> <len_lo> <len_hi> <payload:len> <ck_a> <ck_b>

The checksum is the 8-bit Fletcher variant run over class..payload
(inclusive). Sync bytes and checksum bytes are NOT in the Fletcher
range.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

UBX_SYNC_1 = 0xB5
UBX_SYNC_2 = 0x62

CLS_NAV = 0x01
ID_NAV_PVT = 0x07
ID_NAV_CLOCK = 0x22
ID_NAV_SAT = 0x35

CLS_MON = 0x0A
ID_MON_VER = 0x04


def fletcher8(data: bytes) -> tuple[int, int]:
    """Compute the u-blox Fletcher-8 checksum over `data`.

    Returns `(ck_a, ck_b)` both in [0, 255]. Works for any slice — the
    caller must pass exactly class..payload (no sync, no checksum)."""
    ca = 0
    cb = 0
    for byte in data:
        ca = (ca + byte) & 0xFF
        cb = (cb + ca) & 0xFF
    return ca, cb


def checksum_ok(msg: bytes) -> bool:
    """True if `msg` (a full framed UBX message, sync+header+payload+cs)
    has a valid Fletcher-8 checksum."""
    if len(msg) < 8:
        return False
    ca, cb = fletcher8(msg[2:-2])
    return ca == msg[-2] and cb == msg[-1]


@dataclass
class UbxMessage:
    """A whole UBX message with its class/id and decoded payload bytes."""

    class_id: int
    msg_id: int
    payload: bytes


def iter_messages(buf: bytes) -> tuple[list[UbxMessage], int]:
    """Parse whole UBX messages out of `buf`.

    Returns `(messages, bytes_consumed)`. Partial messages at the tail
    and garbage/padding at the head are handled; callers should keep
    any unconsumed bytes and feed them together with the next read
    chunk. The mirror of upstream `mini_consume_ubx`'s inner loop, but
    simpler: we don't count resync / padding events (those are
    diagnostic, not authoritative)."""
    messages: list[UbxMessage] = []
    i = 0
    n = len(buf)
    while i + 8 <= n:
        if buf[i] != UBX_SYNC_1 or buf[i + 1] != UBX_SYNC_2:
            i += 1
            continue
        ubx_len = buf[i + 4] | (buf[i + 5] << 8)
        if ubx_len > 512:
            # Implausibly long — upstream uses 512 as the cutoff. Advance
            # past this sync pair and keep hunting.
            i += 1
            continue
        total = 8 + ubx_len
        if i + total > n:
            break   # partial; wait for more bytes
        msg = buf[i : i + total]
        if not checksum_ok(msg):
            i += 1
            continue
        messages.append(UbxMessage(msg[2], msg[3], bytes(msg[6 : 6 + ubx_len])))
        i += total
    return messages, i


@dataclass
class NavPvt:
    """Decoded UBX-NAV-PVT (subset — the fields we consume)."""

    fix_type: int              # 0=no fix, 2=2D, 3=3D
    num_sv: int
    year: int
    month: int
    day: int
    hour: int
    minute: int
    second: int
    lat_1e7: int
    lon_1e7: int
    hmsl_mm: int

    @property
    def gps_fix_str(self) -> str:
        return {0: "no_fix", 2: "2D", 3: "3D"}.get(self.fix_type, "no_fix")


def parse_nav_pvt(payload: bytes) -> NavPvt | None:
    """Decode a UBX-NAV-PVT payload. Returns None on a short buffer."""
    if len(payload) < 92:
        return None
    return NavPvt(
        year=payload[4] | (payload[5] << 8),
        month=payload[6],
        day=payload[7],
        hour=payload[8],
        minute=payload[9],
        second=payload[10],
        fix_type=payload[20],
        num_sv=payload[23],
        lon_1e7=int.from_bytes(payload[24:28], "little", signed=True),
        lat_1e7=int.from_bytes(payload[28:32], "little", signed=True),
        hmsl_mm=int.from_bytes(payload[36:40], "little", signed=True),
    )


@dataclass
class NavClock:
    """Decoded UBX-NAV-CLOCK (receiver clock solution).

    All values are the u-blox receiver's own estimate of its clock
    state — a self-report, not an independent measurement. Byte map
    confirmed against David Goncalves' ringof/lbe-142x --clocklog
    decoder (MIT)."""

    itow_ms: int          # GPS time of week of the solution
    clk_bias_ns: int      # receiver clock bias (signed)
    clk_drift_ns_s: int   # receiver clock drift (signed)
    t_acc_ns: int         # time accuracy estimate
    f_acc_ps_s: int       # frequency accuracy estimate


def parse_nav_clock(payload: bytes) -> NavClock | None:
    """Decode a 20-byte UBX-NAV-CLOCK payload. None on short buffer."""
    if len(payload) < 20:
        return None
    return NavClock(
        itow_ms=int.from_bytes(payload[0:4], "little"),
        clk_bias_ns=int.from_bytes(payload[4:8], "little", signed=True),
        clk_drift_ns_s=int.from_bytes(payload[8:12], "little", signed=True),
        t_acc_ns=int.from_bytes(payload[12:16], "little"),
        f_acc_ps_s=int.from_bytes(payload[16:20], "little"),
    )


@dataclass
class MonVer:
    """Decoded UBX-MON-VER payload."""

    sw_version: str
    hw_version: str
    extensions: list[str]

    @property
    def protver(self) -> str | None:
        """The PROTVER extension value, or None if absent.

        u-blox M7/M8 modules report one keyword per extension line,
        e.g. `PROTVER=18.00` (or `PROTVER 18.00` on very old firmware).
        Return the value so the firmware-advisory table can regex-match
        it."""
        for ext in self.extensions:
            if not ext.startswith("PROTVER"):
                continue
            tail = ext[len("PROTVER"):]
            if tail and tail[0] in "= ":
                return tail[1:].strip()
        return None


def _trim_c_string(raw: bytes) -> str:
    """Strip NUL padding and decode as ASCII (ignore errors)."""
    return raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def parse_mon_ver(payload: bytes) -> MonVer | None:
    """Decode UBX-MON-VER. Payload layout:

        [0..29]   swVersion (30B, NUL-padded ASCII)
        [30..39]  hwVersion (10B, NUL-padded ASCII)
        [40..]    zero or more 30B extension strings

    Returns None for a response shorter than the 40-byte fixed head."""
    if len(payload) < 40:
        return None
    sw = _trim_c_string(payload[0:30])
    hw = _trim_c_string(payload[30:40])
    exts: list[str] = []
    off = 40
    while off + 30 <= len(payload):
        ext = _trim_c_string(payload[off : off + 30])
        if ext:
            exts.append(ext)
        off += 30
    return MonVer(sw_version=sw, hw_version=hw, extensions=exts)


def build_message(class_id: int, msg_id: int, payload: bytes = b"") -> bytes:
    """Assemble a full UBX frame (sync + header + payload + checksum).

    Used by callers that talk UBX directly over a serial link. The
    LBE-Mini's UBX wrapper opcode accepts `{class, id, len_lo, len_hi,
    payload}` without the sync/checksum — the firmware adds those. This
    helper is for tests and for any future raw-serial path."""
    body = bytes([class_id, msg_id, len(payload) & 0xFF, (len(payload) >> 8) & 0xFF]) + payload
    ck_a, ck_b = fletcher8(body)
    return bytes([UBX_SYNC_1, UBX_SYNC_2]) + body + bytes([ck_a, ck_b])


# --- LBE-Mini interrupt-IN framing --------------------------------------


@dataclass
class MiniHidFrame:
    """One 64-byte HID interrupt-IN frame from the LBE-Mini.

    Layout (reverse-engineered from the upstream `mini_sample_nav`
    and `mini_consume_ubx` comments):

      r[0]     u8  signal loss count
      r[1]     u8  status bitmap — bit 0 "no GPS", bit 1 "no PLL lock",
                    bit 7 "this frame carries UBX bytes in r[2..]"
      r[2..63] payload (62 bytes of UBX, or padding / keepalive when
                bit 7 is clear)
    """

    signal_loss: int
    gps_signal_ok: bool
    pll_hw_locked: bool
    carries_ubx: bool
    payload: bytes   # the 62-byte tail (whether UBX or filler)


def decode_mini_hid_frame(raw: bytes) -> MiniHidFrame | None:
    """Decode one interrupt-IN frame from the Mini.

    Returns None if the frame is too short to carry status bytes. The
    `payload` field is always the raw 62-byte tail; callers must check
    `carries_ubx` before appending it to the UBX reassembly buffer —
    keepalive frames contain 0xFF / 0x00 filler which will corrupt any
    message that spans two UBX frames if folded in blindly."""
    if len(raw) < 2:
        return None
    status = raw[1]
    return MiniHidFrame(
        signal_loss=raw[0],
        gps_signal_ok=(status & 0x01) == 0,
        pll_hw_locked=(status & 0x02) == 0,
        carries_ubx=bool(status & 0x80),
        payload=bytes(raw[2:]),
    )
