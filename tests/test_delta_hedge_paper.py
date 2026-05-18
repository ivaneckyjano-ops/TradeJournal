"""Unit testy pre ``core.delta_hedge_paper``."""
from core.delta_hedge_paper import (
    apply_deadband,
    dollar_delta,
    hedge_action_label,
    hedge_shares_for_target,
    leg_delta_shares,
    net_delta_shares_by_ticker,
    net_delta_shares_for_ticker,
)


def test_leg_delta_shares_long_short():
    long_leg = {
        "status": "Open",
        "ticker": "AMZN",
        "leg_type": "Long",
        "contracts": 1,
        "delta_current": 0.5,
    }
    short_leg = {
        "status": "Open",
        "ticker": "AMZN",
        "leg_type": "Short",
        "contracts": 1,
        "delta_current": 0.2,
    }
    assert leg_delta_shares(long_leg) == 50.0
    assert leg_delta_shares(short_leg) == -20.0


def test_net_delta_shares_by_ticker():
    trades = [
        {"status": "Open", "ticker": "AMZN", "leg_type": "Long", "contracts": 1, "delta_current": 0.5},
        {"status": "Open", "ticker": "amzn", "leg_type": "Short", "contracts": 1, "delta_current": 0.2},
        {"status": "Closed", "ticker": "X", "leg_type": "Long", "contracts": 99, "delta_current": 1.0},
    ]
    assert net_delta_shares_by_ticker(trades) == {"AMZN": 30.0}


def test_hedge_and_dollar_delta():
    assert hedge_shares_for_target(42.5, 0.0) == -42.5
    assert hedge_shares_for_target(-10.0, 0.0) == 10.0
    assert abs(dollar_delta(42.5, 262.84) - 11170.7) < 0.05


def test_apply_deadband():
    h, inside = apply_deadband(5.0, 10.0)
    assert h == 5.0 and inside is True
    h2, inside2 = apply_deadband(15.0, 10.0)
    assert h2 == 15.0 and inside2 is False


def test_hedge_action_label():
    assert "Nakúpiť" in hedge_action_label(10.0)
    assert "Predať" in hedge_action_label(-10.0)


def test_net_delta_shares_for_ticker_synthetic():
    legs = [
        {"ticker": "MSFT", "leg_type": "Short", "contracts": 1, "delta_current": 0.317},
        {"ticker": "MSFT", "leg_type": "Long", "contracts": 1, "delta_current": 0.475},
    ]
    assert abs(net_delta_shares_for_ticker(legs, "MSFT") - 15.8) < 0.01
