"""Kľúč denník ↔ TWS — normalizácia typu opcie (C/P vs Call/Put)."""

from core.portfolio_data import journal_position_key


def test_journal_position_key_call_put_aliases():
    k_full = journal_position_key("spy", 450.0, "20260515", "Call", "Long",)
    k_c = journal_position_key("SPY", 450, "20260515", "C", "long")
    assert k_full == k_c


def test_journal_position_key_put_alias():
    k1 = journal_position_key("qqq", 400.5, "2025-06-20", "Put", "Short")
    k2 = journal_position_key("QQQ", 400.5, "20250620", "p", "short")
    assert k1 == k2
