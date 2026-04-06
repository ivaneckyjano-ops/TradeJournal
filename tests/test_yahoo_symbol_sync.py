"""Unit testy pre Yahoo sync (bez siete)."""
import unittest

from core.yahoo_symbol_sync import (
    compute_iv_rank_from_history,
    yahoo_symbol_for_api,
)


class TestYahooHelpers(unittest.TestCase):
    def test_yahoo_symbol_for_api(self):
        self.assertEqual(yahoo_symbol_for_api("BRK.B"), "BRK-B")
        self.assertEqual(yahoo_symbol_for_api("AAPL"), "AAPL")

    def test_iv_rank_from_history(self):
        self.assertIsNone(compute_iv_rank_from_history(30.0, []))
        self.assertIsNone(compute_iv_rank_from_history(30.0, [20.0, 22.0, 24.0]))
        past = [20.0, 40.0, 30.0, 25.0, 35.0]
        self.assertEqual(compute_iv_rank_from_history(30.0, past), 50.0)
        self.assertEqual(compute_iv_rank_from_history(20.0, past), 0.0)
        self.assertEqual(compute_iv_rank_from_history(40.0, past), 100.0)


if __name__ == "__main__":
    unittest.main()
