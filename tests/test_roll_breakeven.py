"""Testy pre core/roll_breakeven.py — BS model, ručné nohy."""
from __future__ import annotations

import pytest

from core.roll_breakeven import ManualLeg, breakeven_spots, net_premium


@pytest.fixture
def sample_legs() -> list[ManualLeg]:
    return [
        ManualLeg(side="sell", right="C", strike=185.0, t_years=60 / 365.0, iv=0.35, contracts=1),
        ManualLeg(side="buy", right="C", strike=190.0, t_years=90 / 365.0, iv=0.33, contracts=1),
    ]


def test_net_premium_is_deterministic(sample_legs: list[ManualLeg]) -> None:
    a = net_premium(100.0, sample_legs, r=0.045)
    b = net_premium(100.0, sample_legs, r=0.045)
    assert a == b
    c = net_premium(150.0, sample_legs, r=0.045)
    assert c != a


def test_breakeven_includes_trivial_spot() -> None:
    """Pre zvolené nohy: target = net(S0) => S0 musí byť (aspoň) jeden koreň v okolí."""
    r = 0.05
    legs = [
        ManualLeg(side="sell", right="C", strike=100.0, t_years=0.2, iv=0.3, contracts=1),
        ManualLeg(side="buy", right="C", strike=110.0, t_years=0.2, iv=0.3, contracts=1),
    ]
    s0 = 95.0
    t0 = net_premium(s0, legs, r=r)
    roots = breakeven_spots(
        legs,
        r=r,
        target_net=t0,
        s_min=20.0,
        s_max=300.0,
        n_scan=400,
    )
    assert any(abs(x - s0) < 0.15 for x in roots), f"roots={roots}, expected near {s0}"


def test_leg_sell_receives_sign(sample_legs: list[ManualLeg]) -> None:
    only_sell_185 = [sample_legs[0]]
    n = net_premium(120.0, only_sell_185, r=0.045)
    assert n > 0.0
    only_buy = [ManualLeg(side="buy", right="C", strike=185.0, t_years=60 / 365.0, iv=0.35, contracts=1)]
    m = net_premium(120.0, only_buy, r=0.045)
    assert m < 0.0
    assert n == -m
