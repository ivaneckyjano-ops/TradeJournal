import pandas as pd
import pytest

from core import csv_spread_variant as m


def test_underlying_ticker_from_norm():
    assert m.underlying_ticker_from_norm({"ticker": "mrvl"}) == "MRVL"
    assert m.underlying_ticker_from_norm({"stock": "AAPL US"}) == "AAPL"
    assert m.underlying_ticker_from_norm({"price": "100"}) == ""


def test_parse_expiry_variants():
    assert m.parse_expiry_to_yyyymmdd("20260717") == "20260717"
    assert m.parse_expiry_to_yyyymmdd("2026-07-17") == "20260717"
    assert m.parse_expiry_to_yyyymmdd("17.07.2026") == "20260717"


def test_calendar_legs_need_strike():
    row = pd.Series({"Exp Leg1": "2026-06-18", "Exp Leg2": "2026-07-17"})
    legs, err, _notice = m.calendar_legs_from_variant_row(row, spot=100.0, iv=0.25, contracts=1)
    assert err
    assert legs == []


def test_diagonal_row_two_different_strikes():
    row = pd.Series(
        {
            "Exp Leg1": "2026-06-18",
            "Exp Leg2": "2026-09-18",
            "Leg1 Strike": "150",
            "Leg2 Strike": "165",
            "Right": "Call",
        }
    )
    legs, err, notice = m.calendar_legs_from_variant_row(row, spot=200.0, iv=0.25, contracts=1)
    assert err is None
    assert len(legs) == 2
    assert legs[0]["strike"] == 150.0
    assert legs[1]["strike"] == 165.0
    assert legs[0]["leg_type"] == "Short"
    assert legs[1]["leg_type"] == "Long"
    assert notice and "diagon" in notice.lower()


def test_calendar_legs_two_expiries_and_strike():
    row = pd.Series(
        {
            "Exp Leg1": "2026-06-18",
            "Exp Leg2": "2026-07-17",
            "Leg1 Strike": "100",
            "Right": "Call",
        }
    )
    legs, err, _notice = m.calendar_legs_from_variant_row(row, spot=100.0, iv=0.25, contracts=1)
    assert err is None
    assert len(legs) == 2
    assert legs[0]["leg_type"] == "Short"
    assert legs[1]["leg_type"] == "Long"
    assert legs[0]["strike"] == legs[1]["strike"] == 100.0
    assert legs[0]["right"] == legs[1]["right"] == "C"


def test_calendar_legs_default_expiries_when_missing():
    row = pd.Series({"Leg1 Strike": "100", "Right": "Call"})
    legs, err, notice = m.calendar_legs_from_variant_row(row, spot=100.0, iv=0.25, contracts=1)
    assert err is None
    assert len(legs) == 2
    assert len(legs[0]["expiry"]) == 8
    assert len(legs[1]["expiry"]) == 8
    assert notice


def test_excel_export_style_row_maps_price_bid_ask_iv_net_greeks():
    """Stĺpce ako z Excelu: Price~, Exp Leg1/2, Bid1, Ask2, Leg1 IV, Leg2 IV, Net Delta."""
    row = pd.Series(
        {
            "Price~": "133,83",
            "Exp Leg1": "2026-05-15",
            "Leg1 Strike": "160",
            "Type": "Call",
            "Bid1": "2,26",
            "Exp Leg2": "2028-06-16",
            "Ask2": "44,25",
            "Leg1 IV": "62,35%",
            "Leg2 IV": "60,06%",
            "Net Delta": "0,436466",
        }
    )
    legs, err, _n = m.calendar_legs_from_variant_row(row, spot=50.0, iv=0.30, contracts=1)
    assert err is None
    assert len(legs) == 2
    assert legs[0]["entry_price"] == 2.26
    assert legs[1]["entry_price"] == 44.25
    assert abs(legs[0]["iv"] - 0.6235) < 1e-9
    assert abs(legs[1]["iv"] - 0.6006) < 1e-9
    assert legs[0]["tws_bid"] == 2.26
    assert legs[1]["tws_ask"] == 44.25
    net_d = float(legs[0]["leg_delta_usd"]) + float(legs[1]["leg_delta_usd"])
    assert abs(net_d - 43.6466) < 0.25


def test_on_flag():
    assert m.csv_row_on_flag({"on": "On"}) is True
    assert m.csv_row_on_flag({"on": "0"}) is False
