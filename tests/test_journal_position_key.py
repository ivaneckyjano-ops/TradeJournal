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


def test_ibkr_pos_key_matches_journal_position_key():
    """sync_positions_to_db a Casopis musia zdieľať rovnakú normalizáciu ako journal_position_key."""
    from core import ibkr

    tup = journal_position_key("QQQ", 400.0, "20260619", "Put", "Short")
    assert ibkr._pos_key("QQQ", 400.0, "20260619", "Short", "Put") == "|".join(str(x) for x in tup)
