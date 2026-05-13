from __future__ import annotations

import pytest

from core.journal_pnl_curve import (
    _leg_pl_usd,
    journal_group_pl_ladder_tws_style_rows,
    journal_group_pl_rows_at_spots,
    journal_group_pl_stoploss_short_window,
    journal_group_pl_vs_spot,
    journal_spot_levels_band,
    journal_spot_levels_descending,
)


def test_journal_group_pl_stoploss_none_without_short():
    legs = [
        {
            "ticker": "T",
            "leg_type": "Long",
            "option_type": "Call",
            "strike": 100.0,
            "expiry": "20991231",
            "contracts": 1,
            "entry_price": 2.0,
            "iv_at_entry": 0.35,
        }
    ]
    assert journal_group_pl_stoploss_short_window(legs, spot_center=100.0) is None


def test_journal_group_pl_stoploss_short_window_diagonal():
    legs = [
        {
            "ticker": "T",
            "leg_type": "Long",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990717",
            "contracts": 1,
            "entry_price": 5.0,
            "iv_at_entry": 0.35,
        },
        {
            "ticker": "T",
            "leg_type": "Short",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990601",
            "contracts": 1,
            "entry_price": 4.0,
            "iv_at_entry": 0.35,
        },
    ]
    out = journal_group_pl_stoploss_short_window(
        legs, spot_center=195.0, below_usd=25.0, above_usd=4.0, n_points=24, forward_days=()
    )
    assert out is not None
    assert out["k_short"] == 190.0
    assert len(out["x_spot_minus_short"]) == 24
    assert len(out["pl_now"]) == 24
    assert out["forward_days"] == []
    assert out["spot_axis_lo"] < out["spot_axis_hi"]
    assert out["spot_axis_lo"] <= 190.0 <= out["spot_axis_hi"]


def test_journal_group_pl_stoploss_window_from_entry_premium():
    legs = [
        {
            "ticker": "T",
            "leg_type": "Long",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990717",
            "contracts": 1,
            "entry_price": 5.0,
            "iv_at_entry": 0.35,
        },
        {
            "ticker": "T",
            "leg_type": "Short",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990601",
            "contracts": 1,
            "entry_price": 3.25,
            "iv_at_entry": 0.35,
        },
    ]
    out = journal_group_pl_stoploss_short_window(legs, spot_center=195.0, n_points=30)
    assert out is not None
    assert out["short_entry_per_share"] == 3.25
    assert out["window_below_usd"] > 18.0
    assert out["spot_axis_hi"] >= 195.0


def test_journal_spot_levels_descending():
    lv = journal_spot_levels_descending(184.12, 170.0, 0.5)
    assert lv[0] == 184.12
    assert lv[-1] == 170.0
    assert all(lv[i] >= lv[i + 1] for i in range(len(lv) - 1))


def test_journal_spot_levels_band_symmetric():
    """Rebrík zahŕňa spot nad referenciou aj pod ňou; referencia ostáva v zozname."""
    c = 100.0
    lv = journal_spot_levels_band(c, above_usd=10.0, below_usd=10.0, step=2.5)
    assert lv[0] == pytest.approx(110.0)
    assert lv[-1] == pytest.approx(90.0)
    assert any(abs(x - c) < 1e-6 for x in lv)
    assert all(lv[i] >= lv[i + 1] for i in range(len(lv) - 1))


def test_journal_spot_levels_band_off_grid_includes_center():
    lv = journal_spot_levels_band(100.03, 5.0, 5.0, step=2.0)
    assert any(abs(x - 100.03) < 0.01 for x in lv)
    legs = [
        {
            "ticker": "T",
            "leg_type": "Long",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990717",
            "contracts": 1,
            "entry_price": 5.0,
            "iv_at_entry": 0.35,
        },
        {
            "ticker": "T",
            "leg_type": "Short",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990601",
            "contracts": 1,
            "entry_price": 4.0,
            "iv_at_entry": 0.35,
        },
    ]
    rows = journal_group_pl_rows_at_spots(legs, [195.0, 194.0])
    assert rows is not None
    assert len(rows) == 2
    assert rows[0]["spot"] == 195.0
    assert "pl_long_usd" in rows[0]
    assert "pl_short_usd" in rows[0]
    assert rows[0]["spot_minus_k"] == 5.0
    assert abs(rows[0]["pl_net_usd"] - (rows[0]["pl_long_usd"] + rows[0]["pl_short_usd"])) < 0.02


def test_journal_group_pl_ladder_tws_style_rows_two_spots_two_legs():
    legs = [
        {
            "ticker": "T",
            "leg_type": "Long",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990717",
            "contracts": 1,
            "entry_price": 5.0,
            "iv_at_entry": 0.35,
        },
        {
            "ticker": "T",
            "leg_type": "Short",
            "option_type": "Call",
            "strike": 190.0,
            "expiry": "20990601",
            "contracts": 1,
            "entry_price": 4.0,
            "iv_at_entry": 0.35,
        },
    ]
    rows = journal_group_pl_ladder_tws_style_rows(legs, [195.0, 194.0])
    assert rows is not None
    # 2 spots × (2 legs + 1 net) = 6
    assert len(rows) == 6
    assert rows[2]["kontrakt"] == "Σ NET"
    assert rows[2]["spot"] == 195.0
    assert rows[5]["kontrakt"] == "Σ NET"
    assert rows[5]["spot"] == 194.0


def test_short_negative_entry_price_matches_positive_ib_credit_convention():
    """IB ukladá short avgCost často ako záporný $/akcia; P&L musí sedieť s kladnou prémiou."""
    leg_pos = {
        "ticker": "T",
        "leg_type": "Short",
        "option_type": "Call",
        "strike": 190.0,
        "expiry": "20990601",
        "contracts": 1,
        "entry_price": 4.0,
        "iv_at_entry": 0.35,
    }
    leg_neg = {**leg_pos, "entry_price": -4.0}
    S, Tn, iv, r = 195.0, max(1.0 / 365.0, 50 / 365.0), 0.35, 0.045
    a = _leg_pl_usd(S, leg_pos, Tn, iv, r)
    b = _leg_pl_usd(S, leg_neg, Tn, iv, r)
    assert abs(a - b) < 0.02


def test_journal_group_pl_vs_spot_single_long_call():
    legs = [
        {
            "ticker": "TEST",
            "leg_type": "Long",
            "option_type": "Call",
            "strike": 200.0,
            "expiry": "20991231",
            "contracts": 1,
            "entry_price": 4.0,
            "iv_at_entry": 0.35,
        }
    ]
    out = journal_group_pl_vs_spot(legs, spot_center=195.0, spot_min=150.0, spot_max=240.0, n_points=20)
    assert out is not None
    assert len(out["spots"]) == 20
    assert len(out["pl_now"]) == 20
    assert out["ticker"] == "TEST"
    assert set(out.get("pl_fwd_by_day", {})) == {2, 3, 5}
    assert len(out["pl_fwd_by_day"][2]) == 20


def test_journal_group_pl_vs_spot_forward_days_disabled():
    legs = [
        {
            "ticker": "TEST",
            "leg_type": "Long",
            "option_type": "Call",
            "strike": 200.0,
            "expiry": "20991231",
            "contracts": 1,
            "entry_price": 4.0,
            "iv_at_entry": 0.35,
        }
    ]
    out = journal_group_pl_vs_spot(legs, spot_center=195.0, spot_min=150.0, spot_max=240.0, n_points=20, forward_days=())
    assert out is not None
    assert out.get("pl_fwd_by_day") == {}
    assert out.get("forward_days") == []


def test_journal_group_pl_vs_spot_rejects_multi_ticker():
    legs = [
        {"ticker": "A", "leg_type": "Long", "option_type": "Call", "strike": 100, "expiry": "20991231", "contracts": 1, "entry_price": 1},
        {"ticker": "B", "leg_type": "Long", "option_type": "Call", "strike": 100, "expiry": "20991231", "contracts": 1, "entry_price": 1},
    ]
    assert journal_group_pl_vs_spot(legs) is None
