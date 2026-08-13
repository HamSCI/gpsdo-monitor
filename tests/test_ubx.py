"""Tests for the UBX protocol helpers used by the LBE-Mini driver."""
from __future__ import annotations

from gpsdo_monitor.ubx import (
    CLS_MON,
    CLS_NAV,
    ID_MON_VER,
    ID_NAV_CLOCK,
    ID_NAV_PVT,
    MiniHidFrame,
    build_message,
    checksum_ok,
    decode_mini_hid_frame,
    fletcher8,
    iter_messages,
    parse_mon_ver,
    parse_nav_clock,
    parse_nav_pvt,
)


# --- Fletcher-8 / framing -----------------------------------------------


def test_fletcher8_known_vector():
    # Empty buffer: both bytes stay 0. Trivial but worth pinning.
    assert fletcher8(b"") == (0, 0)


def test_fletcher8_matches_build_message():
    framed = build_message(CLS_NAV, ID_NAV_PVT, b"\x00" * 92)
    # Sync + class + id + len_lo + len_hi + payload + ck_a + ck_b
    assert framed[:2] == b"\xB5\x62"
    assert checksum_ok(framed)


def test_build_message_poll_shape():
    # A zero-length poll is 8 bytes total.
    poll = build_message(CLS_MON, ID_MON_VER)
    assert len(poll) == 8
    assert poll[0:2] == b"\xB5\x62"
    assert poll[2] == CLS_MON
    assert poll[3] == ID_MON_VER
    assert poll[4:6] == b"\x00\x00"
    assert checksum_ok(poll)


def test_checksum_ok_rejects_mutation():
    framed = bytearray(build_message(CLS_NAV, ID_NAV_PVT, b"\x00" * 92))
    framed[-1] ^= 0xFF
    assert not checksum_ok(bytes(framed))


# --- iter_messages ------------------------------------------------------


def test_iter_messages_extracts_one_full_message():
    payload = b"\x01" * 10
    framed = build_message(CLS_NAV, ID_NAV_PVT, payload)
    msgs, consumed = iter_messages(framed)
    assert consumed == len(framed)
    assert len(msgs) == 1
    assert msgs[0].class_id == CLS_NAV
    assert msgs[0].msg_id == ID_NAV_PVT
    assert msgs[0].payload == payload


def test_iter_messages_skips_leading_garbage():
    framed = build_message(CLS_NAV, ID_NAV_PVT, b"\xA5" * 4)
    buf = b"\x00\xFF\x12\x34" + framed
    msgs, consumed = iter_messages(buf)
    assert len(msgs) == 1
    assert consumed == len(buf)


def test_iter_messages_leaves_partial_tail_unconsumed():
    framed = build_message(CLS_NAV, ID_NAV_PVT, b"\xA5" * 16)
    # Drop last 5 bytes; the checksum is incomplete.
    partial = framed[:-5]
    msgs, consumed = iter_messages(partial)
    assert msgs == []
    # Consumer must be able to re-feed the leftover bytes later.
    assert consumed == 0


def test_iter_messages_drops_bad_checksum_and_recovers():
    good1 = build_message(CLS_NAV, ID_NAV_PVT, b"\x01" * 8)
    bad = bytearray(build_message(CLS_MON, ID_MON_VER, b"\x02" * 8))
    bad[-1] ^= 0xFF
    good2 = build_message(CLS_NAV, ID_NAV_PVT, b"\x03" * 8)
    msgs, _ = iter_messages(good1 + bytes(bad) + good2)
    # Recovery picks up good1 and good2; the mutated message is dropped.
    assert len(msgs) == 2
    assert msgs[0].payload == b"\x01" * 8
    assert msgs[1].payload == b"\x03" * 8


def test_iter_messages_rejects_oversized_length_field():
    # length field 0xFFFF would suggest a 65 KB payload; upstream's
    # threshold is 512 and we match it. Feed a sync-pair with that
    # garbage length and verify the parser rewinds past it without
    # locking up, then correctly recovers when a valid message follows.
    good = build_message(CLS_NAV, ID_NAV_PVT, b"\x42" * 8)
    buf = b"\xB5\x62\x00\x00\xFF\xFF" + good
    msgs, consumed = iter_messages(buf)
    assert len(msgs) == 1
    assert msgs[0].payload == b"\x42" * 8
    assert consumed == len(buf)


# --- NAV-PVT ------------------------------------------------------------


def _make_nav_pvt_payload(*, fix_type: int, num_sv: int) -> bytes:
    """Build a 92-byte NAV-PVT payload with the fields we decode."""
    p = bytearray(92)
    # iTOW at 0..3, year 4..5, month 6, day 7, hour 8, min 9, sec 10.
    p[4] = 2026 & 0xFF
    p[5] = (2026 >> 8) & 0xFF
    p[6] = 4       # month
    p[7] = 24      # day
    p[8] = 2       # hour
    p[9] = 58      # minute
    p[10] = 17     # second
    p[20] = fix_type
    p[23] = num_sv
    # Lon / lat / hmsl — use arbitrary values to verify endianness.
    p[24:28] = (-92_126_457_0 & 0xFFFFFFFF).to_bytes(4, "little")  # lon 1e7
    p[28:32] = (38_551_221_3).to_bytes(4, "little")                # lat 1e7
    p[36:40] = (265_600).to_bytes(4, "little")                     # hmsl mm
    return bytes(p)


def test_parse_nav_pvt_3d_fix():
    pvt = parse_nav_pvt(_make_nav_pvt_payload(fix_type=3, num_sv=10))
    assert pvt is not None
    assert pvt.fix_type == 3
    assert pvt.gps_fix_str == "3D"
    assert pvt.num_sv == 10
    assert pvt.year == 2026 and pvt.month == 4 and pvt.day == 24
    assert pvt.hmsl_mm == 265_600


def test_parse_nav_pvt_no_fix():
    pvt = parse_nav_pvt(_make_nav_pvt_payload(fix_type=0, num_sv=0))
    assert pvt is not None
    assert pvt.gps_fix_str == "no_fix"


def test_parse_nav_pvt_rejects_short():
    assert parse_nav_pvt(b"\x00" * 10) is None


# --- MON-VER ------------------------------------------------------------


def _make_mon_ver_payload(sw: str, hw: str, extensions: list[str]) -> bytes:
    def pad(s: str, n: int) -> bytes:
        return s.encode("ascii").ljust(n, b"\x00")[:n]
    out = pad(sw, 30) + pad(hw, 10)
    for ext in extensions:
        out += pad(ext, 30)
    return out


def test_parse_mon_ver_m8_style():
    payload = _make_mon_ver_payload(
        sw="ROM CORE 3.01 (107888)", hw="00080000",
        extensions=["FWVER=SPG 3.01", "PROTVER=18.00", "GPS;SBAS;GAL;BDS"],
    )
    mv = parse_mon_ver(payload)
    assert mv is not None
    assert mv.sw_version == "ROM CORE 3.01 (107888)"
    assert mv.hw_version == "00080000"
    assert mv.protver == "18.00"
    assert "FWVER=SPG 3.01" in mv.extensions


def test_parse_mon_ver_legacy_space_protver():
    payload = _make_mon_ver_payload(
        sw="1.00", hw="0001", extensions=["PROTVER 13.01"],
    )
    mv = parse_mon_ver(payload)
    assert mv is not None
    assert mv.protver == "13.01"


def test_parse_mon_ver_missing_protver_returns_none():
    payload = _make_mon_ver_payload(
        sw="x", hw="y", extensions=["FWVER=something", "GPS;GLO"],
    )
    mv = parse_mon_ver(payload)
    assert mv is not None
    assert mv.protver is None


def test_parse_mon_ver_short_payload_returns_none():
    assert parse_mon_ver(b"\x00" * 20) is None


def test_parse_mon_ver_drops_empty_extension_lines():
    # Some modules pad with NULs after the last real extension.
    payload = _make_mon_ver_payload(
        sw="a", hw="b",
        extensions=["PROTVER=18.00"],
    ) + b"\x00" * 30
    mv = parse_mon_ver(payload)
    assert mv is not None
    assert mv.extensions == ["PROTVER=18.00"]


# --- Mini HID frame -----------------------------------------------------


def test_decode_mini_hid_frame_nominal():
    raw = bytearray(64)
    raw[0] = 5        # signal_loss
    raw[1] = 0x80     # bit 7 set => carries_ubx; bits 0 and 1 clear
    raw[2:] = b"\xAA" * 62
    f = decode_mini_hid_frame(bytes(raw))
    assert f == MiniHidFrame(
        signal_loss=5, gps_signal_ok=True, pll_hw_locked=True,
        carries_ubx=True, payload=b"\xAA" * 62,
    )


def test_decode_mini_hid_frame_unlocked_no_gps():
    raw = bytes([0, 0x83]) + b"\xBB" * 62  # bits 0 (no GPS), 1 (no PLL), 7
    f = decode_mini_hid_frame(raw)
    assert f is not None
    assert f.gps_signal_ok is False
    assert f.pll_hw_locked is False
    assert f.carries_ubx is True


def test_decode_mini_hid_frame_keepalive_flag():
    # bit 7 clear — frame is a keepalive; payload must not be appended.
    raw = bytes([0, 0x00]) + b"\xFF" * 62
    f = decode_mini_hid_frame(raw)
    assert f is not None
    assert f.carries_ubx is False


def test_decode_mini_hid_frame_short_returns_none():
    assert decode_mini_hid_frame(b"\x00") is None


# --- NAV-CLOCK -----------------------------------------------------------


def _nav_clock_payload(
    *, itow_ms: int, clk_bias_ns: int, clk_drift_ns_s: int,
    t_acc_ns: int, f_acc_ps_s: int,
) -> bytes:
    """Pack a 20-byte UBX-NAV-CLOCK payload (u-blox M8, PROTVER 18)."""
    return (
        itow_ms.to_bytes(4, "little")
        + clk_bias_ns.to_bytes(4, "little", signed=True)
        + clk_drift_ns_s.to_bytes(4, "little", signed=True)
        + t_acc_ns.to_bytes(4, "little")
        + f_acc_ps_s.to_bytes(4, "little")
    )


def test_parse_nav_clock_round_trip():
    payload = _nav_clock_payload(
        itow_ms=433_200_000, clk_bias_ns=-1234, clk_drift_ns_s=87,
        t_acc_ns=25, f_acc_ps_s=310,
    )
    nc = parse_nav_clock(payload)
    assert nc is not None
    assert nc.itow_ms == 433_200_000
    assert nc.clk_bias_ns == -1234          # signed survives
    assert nc.clk_drift_ns_s == 87
    assert nc.t_acc_ns == 25
    assert nc.f_acc_ps_s == 310


def test_parse_nav_clock_negative_drift():
    nc = parse_nav_clock(_nav_clock_payload(
        itow_ms=0, clk_bias_ns=5, clk_drift_ns_s=-42, t_acc_ns=1, f_acc_ps_s=1,
    ))
    assert nc is not None and nc.clk_drift_ns_s == -42


def test_parse_nav_clock_short_payload_returns_none():
    assert parse_nav_clock(b"\x00" * 19) is None


def test_nav_clock_message_id():
    assert ID_NAV_CLOCK == 0x22
