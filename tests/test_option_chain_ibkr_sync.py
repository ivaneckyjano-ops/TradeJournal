"""Unit testy pre IBKR sync do option_chain_db (bez live IB)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from core import option_chain_db as odb
from core.option_chain_ibkr_sync import (
    available_expiries_from_secdef,
    dte_calendar_days,
    metrics_to_import_row,
    normalize_user_expiry,
    parse_expiry_text,
    partition_expiries,
    pick_strikes_near_spot,
)


def test_normalize_user_expiry():
    assert normalize_user_expiry("2026-05-16") == "2026-05-16"
    assert normalize_user_expiry("20260516") == "2026-05-16"
    assert normalize_user_expiry("2026-5-6") == "2026-05-06"
    assert normalize_user_expiry("") is None


def test_available_expiries_from_secdef():
    chains = [
        {
            "exchange": "MERGED",
            "expirations": ["20260515", "20260619"],
            "strikes": [100.0, 110.0],
        }
    ]
    s = available_expiries_from_secdef(chains)
    assert s == {"2026-05-15", "2026-06-19"}


def test_partition_expiries_order():
    avail = {"2026-05-15", "2026-06-19"}
    valid, missing = partition_expiries(
        ["2026-06-19", "2099-01-01", "2026-05-15", "2026-05-15"],
        avail,
    )
    assert valid == ["2026-06-19", "2026-05-15"]
    assert missing == ["2099-01-01"]


def test_pick_strikes_near_spot():
    strikes = [90.0, 95.0, 100.0, 105.0, 110.0]
    assert pick_strikes_near_spot(strikes, spot=100.0, n=3) == [95.0, 100.0, 105.0]
    assert pick_strikes_near_spot(strikes, spot=100.0, n=10) == strikes


def test_dte_calendar_days():
    assert dte_calendar_days(date(2026, 5, 1), "2026-05-16") == 15


def test_parse_expiry_text():
    assert parse_expiry_text("2026-01-02, 2026-03-04") == ["2026-01-02", "2026-03-04"]
    assert parse_expiry_text("2026-01-02\n2026-03-04") == ["2026-01-02", "2026-03-04"]


def test_metrics_to_import_row_bs_fallback():
    metrics = {
        "strike": 100.0,
        "right": "C",
        "bid": 2.0,
        "ask": 2.2,
        "last": None,
        "mid": 2.1,
        "und_price": 105.0,
        "iv": None,
        "delta": None,
        "gamma": None,
        "theta": None,
        "vega": None,
        "open_interest": 1000,
    }
    row = metrics_to_import_row(metrics, option_type="Call", dte=30, risk_free=0.05)
    assert row["strike"] == 100.0
    assert row["option_type"] == "Call"
    assert row["mid"] == 2.1
    assert row["delta"] is not None
    assert row["iv"] is not None and 0 < float(row["iv"]) < 3.0


def test_import_merged_dataframe_from_ibkr_style_row():
    """Riadok z metrics_to_import_row ide cez import_merged_dataframe (in-memory DB)."""
    metrics = {
        "strike": 100.0,
        "right": "P",
        "bid": 1.0,
        "ask": 1.1,
        "last": None,
        "mid": 1.05,
        "und_price": 95.0,
        "iv": 0.44,
        "delta": -0.35,
        "gamma": 0.02,
        "theta": -0.05,
        "vega": 0.12,
        "open_interest": 500,
    }
    row = metrics_to_import_row(metrics, option_type="Put", dte=20, risk_free=0.05)
    df = pd.DataFrame([row])
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    odb.init_schema(conn)
    n = odb.import_merged_dataframe(
        conn,
        expiry="2026-06-19",
        as_of_date="2026-05-01",
        merged=df,
        source_options_csv="IBKR:test",
        source_greeks_csv="IBKR:test",
    )
    assert n == 1
    cur = conn.execute("SELECT strike, option_type, delta FROM option_rows")
    r = cur.fetchone()
    assert float(r["strike"]) == 100.0
    assert r["option_type"] == "Put"
    conn.close()
