"""Parsovanie textu podobného IBKR riadku."""

from datetime import date

from core.ib_row_extract import parse_ibkr_row_text


def test_parse_glw_like_row():
    s = (
        "GLW May08'26 170 CALL -1 3.99 76.773% 0.414 -0.476 4.65 5.50"
    )
    p = parse_ibkr_row_text(s)
    assert p.ticker == "GLW"
    assert p.strike == 170.0
    assert p.right == "C"
    assert abs(p.iv_raw - 76.773) < 1e-6
    assert p.expiry == date(2026, 5, 8)
    assert p.delta_current is not None and abs(p.delta_current - 0.414) < 1e-6
    assert p.has_short_qty is True


def test_iv_percent_only():
    p = parse_ibkr_row_text("random 82.5 % noise")
    assert p.iv_raw == 82.5
