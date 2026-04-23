from __future__ import annotations

import pandas as pd

from core import sector_insights_engine as sie


def test_cosine_similarity_sectors_identity():
    df = pd.DataFrame(
        [
            {"sector": "A", "pct_1d": 1.0, "pct_5d": 0.0, "pct_1m": 0.0, "pct_3m": 0.0, "pct_1y": 0.0},
            {"sector": "B", "pct_1d": 1.0, "pct_5d": 0.0, "pct_1m": 0.0, "pct_3m": 0.0, "pct_1y": 0.0},
        ]
    )
    sim = sie.cosine_similarity_sectors(df)
    assert sim.loc["A", "B"] > 0.99


def test_portfolio_sector_weights():
    trades = [
        {"ticker": "AAA", "contracts": 1, "entry_price": 2.0},
        {"ticker": "AAA", "contracts": 1, "entry_price": -1.0},
        {"ticker": "BBB", "contracts": 1, "entry_price": 1.0},
    ]

    def sec(tk: str):
        return {"AAA": "Tech", "BBB": "Utilities"}[tk]

    w = sie.portfolio_sector_weights(trades, sec)
    # AAA: |2|×100 + |-1|×100 = 300; BBB: 100 → celkom 400
    assert abs(w["Tech"] - 0.75) < 1e-6
    assert abs(w["Utilities"] - 0.25) < 1e-6


def test_evaluate_ticker_empty():
    out = sie.evaluate_ticker_diversification("", pd.DataFrame(), None, {}, lambda tk: None, open_trades=[])
    assert out["error"]


def test_evaluate_ticker_missing_symbol():
    df = pd.DataFrame(
        [{"sector": "Electronic Technology", "pct_1d": 1, "pct_5d": 0, "pct_1m": 0, "pct_3m": 0, "pct_1y": 0}]
    )
    out = sie.evaluate_ticker_diversification("NOTINT", df, None, {}, lambda tk: None, open_trades=[])
    assert out["error"] and "Symboly" in out["error"]


def test_evaluate_ticker_returns_verdict():
    short = pd.DataFrame(
        [
            {"sector": "Electronic Technology", "pct_1d": 1, "pct_5d": 1, "pct_1m": 0, "pct_3m": 0, "pct_1y": 0},
            {"sector": "Retail Trade", "pct_1d": -1, "pct_5d": -1, "pct_1m": 0, "pct_3m": 0, "pct_1y": 0},
        ]
    )

    def sec(tk: str):
        return {"AMZN": "Consumer Discretionary", "MSFT": "Technology"}[tk.upper()]

    out = sie.evaluate_ticker_diversification(
        "AMZN",
        short,
        None,
        {"Technology": 0.7},
        sec,
        open_trades=[{"ticker": "MSFT"}],
    )
    assert out.get("error") is None
    assert out.get("verdict")
    assert out.get("avg_sim_to_portfolio_table") is not None


def test_prepare_sector_df_drops_spurious_snp_and_merges_canonical():
    df = pd.DataFrame(
        [
            {"sector": "S&P", "pct_1d": 9, "pct_5d": 0, "pct_1m": 0, "pct_3m": 0, "pct_1y": 0},
            {"sector": "sp", "pct_1d": 1, "pct_5d": 0, "pct_1m": 0, "pct_3m": 0, "pct_1y": 0},
            {
                "sector": "Electronic Technology",
                "pct_1d": 2,
                "pct_5d": 0,
                "pct_1m": 0,
                "pct_3m": 0,
                "pct_1y": 0,
            },
            {
                "sector": "Informačné technológie indexu S&P 500",
                "pct_1d": 0,
                "pct_5d": 4,
                "pct_1m": 0,
                "pct_3m": 0,
                "pct_1y": 0,
            },
        ]
    )
    out = sie.prepare_sector_df_for_insights(df)
    assert "S&P" not in out["sector"].astype(str).tolist()
    assert "sp" not in out["sector"].astype(str).tolist()
    tech_rows = out[out["sector"].astype(str).str.contains("technol", case=False, na=False)]
    assert len(tech_rows) == 1


def test_build_insight_report_concentration_warning():
    short = pd.DataFrame(
        [
            {"sector": "Tech", "pct_1d": 1, "pct_5d": 1, "pct_1m": 1, "pct_3m": 1, "pct_1y": 1},
            {"sector": "Semi", "pct_1d": 1, "pct_5d": 1, "pct_1m": 1, "pct_3m": 1, "pct_1y": 1},
            {"sector": "Utilities", "pct_1d": -1, "pct_5d": -1, "pct_1m": -1, "pct_3m": -1, "pct_1y": -1},
        ]
    )
    rep = sie.build_insight_report(short, None, {"Tech": 0.5, "Semi": 0.4}, sim_threshold_high=0.99)
    assert any("správaní" in x or "váh" in x.lower() for x in rep.get("warnings", []))
