"""Unit testy pre parsovanie TWS-like riadku z OCR textu."""

from core.screenshot_spread_row import (
    infer_short_leg_tag,
    parse_spread_row_line,
    build_sb_legs,
)


def test_parse_sample_diagonal_put_row():
    line = (
        "151.07  05/15/26  157.50P  13.65  05/08/26  146.00P  4.05  9.60  "
        "72.20  76.92  4.72  -0.2136  0.0352  0.1024"
    )
    p = parse_spread_row_line(line)
    assert p is not None
    assert p.spot == 151.07
    assert p.leg_a_exp == "20260515"
    assert p.leg_a_strike == 157.5
    assert p.leg_a_right == "P"
    assert p.leg_a_price == 13.65
    assert p.leg_b_exp == "20260508"
    assert p.leg_b_strike == 146.0
    assert p.leg_b_right == "P"
    assert p.leg_b_price == 4.05
    assert p.net_debit == 9.60
    assert p.pct_tokens == [72.2, 76.92, 4.72]
    assert p.net_delta_per_share is not None and abs(p.net_delta_per_share + 0.2136) < 1e-6
    assert p.gamma_per_share is not None and abs(p.gamma_per_share - 0.0352) < 1e-6
    assert p.vega_per_share is not None and abs(p.vega_per_share - 0.1024) < 1e-6


def test_infer_short_leg_earlier_expiry_b():
    line = "100.00 06/20/26 100.00C 1.00 05/15/26 99.00C 2.00 0.50"
    p = parse_spread_row_line(line)
    assert p is not None
    assert infer_short_leg_tag(p) == "B"


def test_build_sb_legs_short_a():
    line = "100.00 05/15/26 100.00P 1.00 06/20/26 100.00P 2.00 0.50"
    p = parse_spread_row_line(line)
    assert p is not None
    legs = build_sb_legs(p, short_leg="A", contracts=2, iv=0.25)
    assert len(legs) == 2
    assert legs[0]["leg_type"] == "Short"
    assert legs[1]["leg_type"] == "Long"
    assert legs[0]["contracts"] == 2
