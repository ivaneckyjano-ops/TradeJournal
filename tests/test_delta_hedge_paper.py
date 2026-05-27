"""Unit testy pre ``core.delta_hedge_paper``."""
import pandas as pd

from core.delta_hedge_paper import (
    apply_deadband,
    dollar_delta,
    hedge_action_label,
    hedge_option_alternative_hint,
    preferred_short_expiry_for_ticker,
    hedge_recommendation_label,
    hedge_shares_for_target,
    hedge_table_recommendation_cells,
    leg_delta_shares,
    net_delta_shares_by_ticker,
    net_delta_shares_for_ticker,
    pick_real_option_hedge_contract,
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


def test_hedge_option_alternative_hint_signs():
    h45 = hedge_option_alternative_hint(45.0)
    assert "Kúp 1 call kontrakt" in h45
    assert "0.45" in h45
    assert "Kúp 1 put kontrakt" in hedge_option_alternative_hint(-45.0)
    assert hedge_option_alternative_hint(0.0) == ""


def test_hedge_recommendation_label_deadband():
    assert "deadband" in hedge_recommendation_label(3.0, inside_deadband=True)
    assert "Nakúpiť" in hedge_recommendation_label(45.0, inside_deadband=False)
    assert "Kúp 1 call kontrakt" in hedge_recommendation_label(45.0, inside_deadband=False)


def test_hedge_table_recommendation_cells_split():
    s, o = hedge_table_recommendation_cells(45.0, inside_deadband=False)
    assert "Nakúpiť" in s
    assert "Kúp 1 call kontrakt" in o
    assert "—" in hedge_table_recommendation_cells(0.0, inside_deadband=True)[1]


def test_pick_real_option_hedge_contract_call():
    df = pd.DataFrame(
        [
            {"expiry": "2026-06-19", "strike": 270.0, "option_type": "Call", "delta": 0.31, "open_interest": 10, "volume": 1},
            {"expiry": "2026-06-19", "strike": 265.0, "option_type": "Call", "delta": 0.47, "open_interest": 100, "volume": 20},
            {"expiry": "2026-06-19", "strike": 260.0, "option_type": "Call", "delta": 0.62, "open_interest": 50, "volume": 5},
        ]
    )
    picked = pick_real_option_hedge_contract(df, 48.0)
    assert picked is not None
    assert picked["contracts"] == 1
    assert abs(picked["delta"] - 0.47) < 1e-9
    assert abs(float(picked["strike"]) - 265.0) < 1e-9


def test_pick_real_option_hedge_contract_put_multiple():
    df = pd.DataFrame(
        [
            {"expiry": "2026-07-17", "strike": 190.0, "option_type": "Put", "delta": -0.22, "open_interest": 20, "volume": 2},
            {"expiry": "2026-07-17", "strike": 195.0, "option_type": "Put", "delta": -0.41, "open_interest": 200, "volume": 30},
            {"expiry": "2026-07-17", "strike": 200.0, "option_type": "Put", "delta": -0.58, "open_interest": 30, "volume": 3},
        ]
    )
    picked = pick_real_option_hedge_contract(df, -130.0)
    assert picked is not None
    assert picked["contracts"] == 2
    assert abs(picked["delta"] - (-0.58)) < 1e-9


def test_pick_real_option_hedge_contract_prefers_short_expiry_floor():
    df = pd.DataFrame(
        [
            {"expiry": "2099-06-20", "strike": 270.0, "option_type": "Call", "delta": 0.46, "open_interest": 10, "volume": 1},
            {"expiry": "2099-07-18", "strike": 265.0, "option_type": "Call", "delta": 0.42, "open_interest": 100, "volume": 20},
        ]
    )
    picked = pick_real_option_hedge_contract(df, 43.0, preferred_expiry="2099-07-10", min_dte=1)
    assert picked is not None
    assert picked["expiry"] == "2099-07-18"


def test_preferred_short_expiry_for_ticker():
    legs = [
        {"status": "Open", "ticker": "GLW", "leg_type": "Short", "option_type": "Call", "expiry": "20260717"},
        {"status": "Open", "ticker": "GLW", "leg_type": "Short", "option_type": "Put", "expiry": "2026-06-20"},
        {"status": "Open", "ticker": "GLW", "leg_type": "Long", "option_type": "Call", "expiry": "2026-06-13"},
        {"status": "Open", "ticker": "GLW", "leg_type": "Short", "option_type": "STK", "expiry": ""},
    ]
    assert preferred_short_expiry_for_ticker(legs, "GLW") == "2026-06-20"


def test_net_delta_shares_for_ticker_synthetic():
    legs = [
        {"ticker": "MSFT", "leg_type": "Short", "contracts": 1, "delta_current": 0.317},
        {"ticker": "MSFT", "leg_type": "Long", "contracts": 1, "delta_current": 0.475},
    ]
    assert abs(net_delta_shares_for_ticker(legs, "MSFT") - 15.8) < 0.01
