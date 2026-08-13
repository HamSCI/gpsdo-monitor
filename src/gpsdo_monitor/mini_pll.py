"""LBE-Mini clock-synth divider-chain solver.

Direct port of `mini_solve_pll` from ringof/lbe-142x `src/model_mini.c`
(David Goncalves, MIT). The Mini derives its output from a 97.6 kHz
disciplined reference through a divider chain:

    f_out = f_in * N2_HS * N2_LS / (N3 * N1_HS * NC1_LS)

with N2_HS, N1_HS in [4, 11]; N2_LS even in [2, 2^20]; NC1_LS = 1 or
even in [2, 2^20]; N3 fixed at 1 by upstream. The search reduces
f_out/f_in to lowest terms p/q, then walks multiples k*p / k*q looking
for a factorization that satisfies the constraints — two passes, the
first preferring a VCO (f_in * N2_HS * N2_LS) inside the synth's
native 5.0–6.5 GHz band, the second accepting any valid chain.

Pure function, no hardware dependency; iteration order matches the C
exactly so results are decision-identical with upstream (pinned by
tests/test_mini_pll.py's C-generated fixture).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

F_IN_HZ = 97_600

_VCO_MIN_HZ = 5_000_000_000
_VCO_MAX_HZ = 6_500_000_000
_HS_RANGE = range(11, 3, -1)          # 11 down to 4, matching the C loops
_LS_MAX = 1 << 20
_K_MAX = 4096


@dataclass(frozen=True)
class PllSolution:
    fin: int
    n3: int
    n2_hs: int
    n2_ls: int
    n1_hs: int
    nc1_ls: int


def solve_pll(f_out_hz: int) -> PllSolution | None:
    """Find divider values producing exactly `f_out_hz`, or None."""
    if f_out_hz <= 0:
        return None
    g = math.gcd(f_out_hz, F_IN_HZ)
    p = f_out_hz // g
    q = F_IN_HZ // g

    for band_pass in range(2):
        for k in range(1, _K_MAX + 1):
            m = k * p
            d = k * q
            if m > 11 * _LS_MAX:
                break
            if d > 11 * _LS_MAX * (1 << 19):
                break
            f_osc = F_IN_HZ * m
            if band_pass == 0 and not (_VCO_MIN_HZ <= f_osc <= _VCO_MAX_HZ):
                continue
            for nh in _HS_RANGE:
                if m % nh:
                    continue
                n2_ls = m // nh
                if n2_ls < 2 or n2_ls > _LS_MAX or n2_ls % 2:
                    continue
                for nh1 in _HS_RANGE:
                    if d % nh1:
                        continue
                    nc1 = d // nh1
                    if nc1 != 1 and not (2 <= nc1 <= _LS_MAX and nc1 % 2 == 0):
                        continue
                    return PllSolution(
                        fin=F_IN_HZ, n3=1,
                        n2_hs=nh, n2_ls=n2_ls,
                        n1_hs=nh1, nc1_ls=nc1,
                    )
    return None
