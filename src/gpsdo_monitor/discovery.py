"""Device discovery + disambiguation.

Mirrors lbe-142x's rule: if more than one device matches and the
operator hasn't declared a serial (or equivalent), refuse rather than
guess. The N:M topology cases in docs/TOPOLOGY.md depend on this.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from gpsdo_monitor.config import DeclaredDevice
from gpsdo_monitor.hid_xport import HidCandidate, enumerate_lbe


@dataclass(frozen=True)
class DiscoveryResult:
    """Result of matching declared config entries against USB reality."""

    matched: tuple[tuple[DeclaredDevice, HidCandidate], ...]
    unmatched_declared: tuple[DeclaredDevice, ...]
    unclaimed_present: tuple[HidCandidate, ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors


def match(declared: list[DeclaredDevice], extra: Sequence = ()) -> DiscoveryResult:
    """Resolve declared entries against currently-plugged devices.

    Rules:
      - If `declared` is empty and exactly one device is present, match
        it implicitly (Case A in TOPOLOGY.md).
      - If `declared` is empty and >1 devices are present, error: we
        refuse to pick.
      - If `declared` is non-empty, each entry must find a device
        whose serial matches case-insensitively. Unmatched entries and
        unclaimed devices are both reported (neither fatal on its own).

    `extra` adds candidates found outside HID -- confirmed MVR GPSDOs
    (mvr.MvrCandidate, same shape as HidCandidate) -- and the same rules
    apply across both families: a Bodnar and an MVR with nothing declared
    is still a refusal to guess.
    """
    present = list(enumerate_lbe()) + list(extra)
    errors: list[str] = []

    if not declared:
        if len(present) == 0:
            return DiscoveryResult((), (), (), (
                "no Leo Bodnar devices found" if not extra else "no GPSDO devices found",))
        if len(present) == 1:
            implicit = DeclaredDevice(serial=present[0].serial or "unknown", governs=())
            return DiscoveryResult(((implicit, present[0]),), (), (), ())
        errors.append(
            f"{len(present)} devices present but no [[monitor.device]] entries "
            "declared; cannot disambiguate. See docs/TOPOLOGY.md Case B."
        )
        return DiscoveryResult((), (), tuple(present), tuple(errors))

    remaining = list(present)
    matched: list[tuple[DeclaredDevice, HidCandidate]] = []
    unmatched: list[DeclaredDevice] = []
    for d in declared:
        hit = next(
            (c for c in remaining if c.serial.lower() == d.normalized_serial), None
        )
        if hit is None:
            unmatched.append(d)
            continue
        remaining.remove(hit)
        matched.append((d, hit))

    return DiscoveryResult(
        matched=tuple(matched),
        unmatched_declared=tuple(unmatched),
        unclaimed_present=tuple(remaining),
        errors=tuple(errors),
    )
