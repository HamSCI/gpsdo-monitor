"""Solver tests for the LBE-Mini divider chain.

The EXPECTED table was generated from David Goncalves' C reference
implementation (ringof/lbe-142x src/model_mini.c::mini_solve_pll, MIT)
via a standalone harness — see the implementation plan, Task 6 Step 1.
The Python port must be byte-for-byte-decision identical, so expected
tuples are exact, not merely valid."""
from __future__ import annotations

import pytest

from gpsdo_monitor.mini_pll import F_IN_HZ, PllSolution, solve_pll

# (f_out, (fin, n3, n2_hs, n2_ls, n1_hs, nc1_ls)) — from the C harness.
# Harness command:
#   ./harness 10000000 810000000 97600 25000000 100000 123456789 1 809999999 4294967291
# Harness output (verbatim):
#   10000000 fin=97600 n3=1 n2hs=10 n2ls=6250 n1hs=5 nc1=122
#   810000000 fin=97600 n3=1 n2hs=10 n2ls=405000 n1hs=4 nc1=122
#   97600 fin=97600 n3=1 n2hs=4 n2ls=2 n1hs=8 nc1=1
#   25000000 fin=97600 n3=1 n2hs=10 n2ls=12500 n1hs=4 nc1=122
#   100000 fin=97600 n3=1 n2hs=5 n2ls=10250 n1hs=10 nc1=5002
#   123456789 UNSOLVABLE
#   1 fin=97600 n3=1 n2hs=4 n2ls=2 n1hs=10 nc1=78080
#   809999999 UNSOLVABLE
#   4294967291 UNSOLVABLE
EXPECTED = [
    (10_000_000, (97_600, 1, 10, 6250, 5, 122)),
    (810_000_000, (97_600, 1, 10, 405000, 4, 122)),
    (97_600, (97_600, 1, 4, 2, 8, 1)),
    (25_000_000, (97_600, 1, 10, 12500, 4, 122)),
    (100_000, (97_600, 1, 5, 10250, 10, 5002)),
    (1, (97_600, 1, 4, 2, 10, 78080)),
]


@pytest.mark.parametrize("f_out,expected", EXPECTED)
def test_matches_c_reference(f_out: int, expected: tuple):
    sol = solve_pll(f_out)
    assert sol is not None
    assert (sol.fin, sol.n3, sol.n2_hs, sol.n2_ls, sol.n1_hs, sol.nc1_ls) == expected


@pytest.mark.parametrize("f_out", [f for f, _ in EXPECTED])
def test_output_formula_is_exact(f_out: int):
    sol = solve_pll(f_out)
    assert sol is not None
    # f_out = fin * N2_HS * N2_LS / (N3 * N1_HS * NC1_LS), exactly.
    assert f_out * sol.n3 * sol.n1_hs * sol.nc1_ls == sol.fin * sol.n2_hs * sol.n2_ls


@pytest.mark.parametrize("f_out", [f for f, _ in EXPECTED])
def test_divider_constraints(f_out: int):
    sol = solve_pll(f_out)
    assert sol is not None
    assert 4 <= sol.n2_hs <= 11
    assert 4 <= sol.n1_hs <= 11
    assert 2 <= sol.n2_ls <= 1 << 20 and sol.n2_ls % 2 == 0
    assert sol.nc1_ls == 1 or (2 <= sol.nc1_ls <= 1 << 20 and sol.nc1_ls % 2 == 0)
    assert sol.n3 == 1
    assert sol.fin == F_IN_HZ


def test_vco_band_preferred_for_10mhz():
    sol = solve_pll(10_000_000)
    assert sol is not None
    f_osc = F_IN_HZ * sol.n2_hs * sol.n2_ls
    assert 5_000_000_000 <= f_osc <= 6_500_000_000


def test_unsolvable_returns_none():
    assert solve_pll(0) is None
    # 809,999,999 is in the Mini's range but coprime to 97,600 (odd,
    # not divisible by 5 or 61), so p = f_out > 11*2^20 and the k-loop
    # breaks at k=1 in both passes. The C harness prints UNSOLVABLE
    # for it — keep this value in sync with the harness run.
    assert solve_pll(809_999_999) is None


def test_solution_is_frozen():
    sol = solve_pll(10_000_000)
    with pytest.raises(Exception):
        sol.n3 = 2  # type: ignore[misc]
