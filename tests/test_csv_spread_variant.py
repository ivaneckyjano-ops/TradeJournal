import pandas as pd
import pytest

from core import csv_spread_variant as m


def test_parse_expiry_variants():
    assert m.parse_expiry_to_yyyymmdd("20260717") == "20260717"
    assert m.parse_expiry_to_yyyymmdd("2026-07-17") == "20260717"
    assert m.parse_expiry_to_yyyymmdd("17.07.2026") == "20260717"


def test_calendar_legs_need_strike():
    row = pd.Series({"Exp Leg1": "2026-06-18", "Exp Leg2": "2026-07-17"})
    legs, err = m.calendar_legs_from_variant_row(row, spot=100.0, iv=0.25, contracts=1)
    assert err
    assert legs == []


def test_calendar_legs_two_expiries_and_strike():
    row = pd.Series(
        {
            "Exp Leg1": "2026-06-18",
            "Exp Leg2": "2026-07-17",
            "Leg1 Strike": "100",
            "Right": "Call",
        }
    )
    legs, err = m.calendar_legs_from_variant_row(row, spot=100.0, iv=0.25, contracts=1)
    assert err is None
    assert len(legs) == 2
    assert legs[0]["leg_type"] == "Short"
    assert legs[1]["leg_type"] == "Long"
    assert legs[0]["strike"] == legs[1]["strike"] == 100.0
    assert legs[0]["right"] == legs[1]["right"] == "C"


def test_on_flag():
    assert m.csv_row_on_flag({"on": "On"}) is True
    assert m.csv_row_on_flag({"on": "0"}) is False
