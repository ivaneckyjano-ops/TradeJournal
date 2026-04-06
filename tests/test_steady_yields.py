"""Unit testy pre Steady Yields (bez IBKR)."""
import unittest

from core.steady_yields.apr import (
    aggregate_roll_events_cash,
    annualized_apr_pct,
    build_yield_summary,
    cost_basis_remaining,
    leap_long_cost_usd,
    short_roll_net_from_trades_closed,
    trades_for_group,
)
from core.steady_yields.alerts import short_premium_profit_pct
from core.steady_yields.engine import estimate_roll_net_credit, traffic_light
from core.steady_yields.scanner import apply_sector_caps, liquidity_passes, spread_pct_of_mid
from core.steady_yields.suggestion import build_roll_up_and_out_suggestion, next_expiry_after


class TestApr(unittest.TestCase):
    def test_trades_for_group(self):
        t = [
            {"group_id": "A", "id": 1},
            {"group_id": "B", "id": 2},
        ]
        self.assertEqual(len(trades_for_group(t, "A")), 1)

    def test_leap_long_cost(self):
        t = [
            {"leg_type": "Long", "contracts": 1, "entry_price": 5.0},
            {"leg_type": "Short", "contracts": 1, "entry_price": 1.0},
        ]
        self.assertEqual(leap_long_cost_usd(t), 500.0)

    def test_aggregate_roll_events(self):
        ev = [
            {"net_premium": 100, "commission": 2},
            {"net_premium": 50, "commission": 1},
        ]
        s = aggregate_roll_events_cash(ev)
        self.assertEqual(s["net_after_comm"], 147.0)

    def test_annualized_apr(self):
        self.assertIsNone(annualized_apr_pct(100, 0, 30))
        self.assertEqual(annualized_apr_pct(365, 1000, 365), 36.5)

    def test_build_yield_summary(self):
        trades = [
            {
                "group_id": "G1",
                "leg_type": "Long",
                "contracts": 1,
                "entry_price": 10.0,
                "status": "Open",
            },
        ]
        events = [{"occurred_at": "2025-01-01", "net_premium": 200, "commission": 1, "id": 1},
                  {"occurred_at": "2025-12-31", "net_premium": 100, "commission": 0, "id": 2}]
        prof = {"expected_apr_pct": 20.0, "leap_initial_cost": 0}
        s = build_yield_summary(group_id="G1", trades=trades, roll_events=events, profile=prof)
        self.assertEqual(s["leap_basis_usd"], 1000.0)
        self.assertEqual(s["total_credits_used_usd"], 299.0)


class TestAlerts(unittest.TestCase):
    def test_short_premium_profit_pct(self):
        self.assertIsNone(short_premium_profit_pct(0, 0.5))
        self.assertIsNone(short_premium_profit_pct(2.0, None))
        self.assertEqual(short_premium_profit_pct(2.0, 0.5), 75.0)
        self.assertEqual(short_premium_profit_pct(1.0, 1.0), 0.0)


class TestEngine(unittest.TestCase):
    def test_traffic_green(self):
        tl = traffic_light(abs_delta=0.10, dte=45)
        self.assertEqual(tl.level, "green")

    def test_traffic_custom_thresholds(self):
        tl = traffic_light(abs_delta=0.30, dte=45, delta_green_max=0.35, delta_red_min=0.50)
        self.assertEqual(tl.level, "green")

    def test_traffic_red(self):
        tl = traffic_light(abs_delta=0.45, dte=45)
        self.assertEqual(tl.level, "red")

    def test_roll_credit_positive(self):
        adv = estimate_roll_net_credit(
            close_short_bid=0.5,
            close_short_ask=0.55,
            open_short_bid=0.8,
            open_short_ask=0.85,
            contracts=1,
            slippage_per_contract=0.02,
        )
        self.assertTrue(adv.ok)
        self.assertIsNotNone(adv.est_net_credit_per_contract)


class TestSuggestion(unittest.TestCase):
    def test_next_expiry(self):
        ex = ["20260116", "20260220", "20260320"]
        self.assertEqual(next_expiry_after(ex, "20260116"), "20260220")

    def test_build_roll_suggestion(self):
        s = build_roll_up_and_out_suggestion(
            expirations=["20260116", "20260220"],
            strikes=[100.0, 105.0, 110.0],
            current_expiry="20260116",
            current_strike=100.0,
            right="C",
        )
        self.assertEqual(s["next_expiry"], "20260220")
        self.assertEqual(s["next_strike"], 105.0)
        self.assertEqual(len(s["suggested_contracts"]), 2)


class TestScanner(unittest.TestCase):
    def test_spread_pct(self):
        self.assertAlmostEqual(spread_pct_of_mid(1.0, 1.04), 100 * 0.04 / 1.02, places=2)

    def test_liquidity(self):
        ok, _ = liquidity_passes(
            open_interest=600, bid=1.0, ask=1.01, min_oi=500, max_spread_pct=2.0
        )
        self.assertTrue(ok)

    def test_sector_caps(self):
        sel, rej = apply_sector_caps(
            ["A", "B", "C", "D"],
            {"A": "Tech", "B": "Tech", "C": "Tech", "D": "Fin"},
            max_per_sector=2,
        )
        self.assertEqual(len(sel), 3)
        self.assertTrue(len(rej) >= 1)


if __name__ == "__main__":
    unittest.main()
