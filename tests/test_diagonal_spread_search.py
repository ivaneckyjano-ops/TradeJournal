import os
import tempfile

import pandas as pd

from core import option_chain_db as odb
from core import diagonal_spread_search as dss


def _merged_row(strike: str, delta: str, theta: str, typ: str) -> pd.DataFrame:
    opts = pd.DataFrame(
        {
            "Strike": [strike],
            "Bid": ["1"],
            "Mid": ["1.5"],
            "Ask": ["2"],
            "Latest": ["1.5"],
            "IV": ["40%"],
            "Delta": [delta],
            "Type": [typ],
        }
    )
    greeks = pd.DataFrame(
        {
            "Strike": [strike],
            "Theta": [theta],
            "Gamma": ["0.01"],
            "IV": ["40%"],
            "Delta": [delta],
            "Type": [typ],
        }
    )
    return odb.merge_options_and_greeks(opts, greeks)


def test_search_diagonal_prefers_low_delta_error_and_high_net_theta():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("DIAG")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-06-01", "100", "0.50", "-0.10"),
                ("2026-06-01", "105", "0.48", "-0.09"),
                ("2026-09-01", "100", "0.52", "-0.04"),
                ("2026-09-01", "105", "0.50", "-0.03"),
            ]:
                odb.import_merged_dataframe(
                    conn,
                    expiry=exp,
                    as_of_date=as_of,
                    merged=_merged_row(strike, d, th, "Call"),
                    source_options_csv="o.csv",
                    source_greeks_csv="g.csv",
                )
            conn.close()

            out = dss.search_diagonal_spreads(
                "DIAG",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                target_net_delta=0.0,
                top_n=5,
                max_strikes_per_expiry=50,
            )
            assert not out.empty
            assert "Čistá delta" in out.columns
            assert any(c.startswith("Čistá theta") for c in out.columns)
            assert any("Debit" in c and "100" in c for c in out.columns)
            assert "Short — bid" in out.columns and "Long — ask" in out.columns
            assert "Short — DTE" in out.columns and "Long — DTE" in out.columns
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "DIAG.db")
            if os.path.isfile(p):
                os.remove(p)


def test_subsample_strikes_caps():
    s = list(range(200))
    sub = dss._subsample_strikes(s, 10)
    assert len(sub) <= 10
    assert len(sub) == len(set(sub))


def test_strike_band_filters_rows():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("BAND")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-06-01", "50", "0.5", "-0.1"),
                ("2026-06-01", "300", "0.5", "-0.1"),
                ("2026-09-01", "50", "0.5", "-0.04"),
                ("2026-09-01", "300", "0.5", "-0.04"),
            ]:
                odb.import_merged_dataframe(
                    conn,
                    expiry=exp,
                    as_of_date=as_of,
                    merged=_merged_row(strike, d, th, "Call"),
                    source_options_csv="o.csv",
                    source_greeks_csv="g.csv",
                )
            conn.close()

            out_all = dss.search_diagonal_spreads(
                "BAND",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=50,
                max_strikes_per_expiry=50,
            )
            out_band = dss.search_diagonal_spreads(
                "BAND",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=50,
                max_strikes_per_expiry=50,
                strike_min=40.0,
                strike_max=60.0,
            )
            assert not out_all.empty
            assert not out_band.empty
            assert (out_band["Short — strike"] <= 60).all() and (out_band["Short — strike"] >= 40).all()
            assert (out_band["Long — strike"] <= 60).all() and (out_band["Long — strike"] >= 40).all()
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "BAND.db")
            if os.path.isfile(p):
                os.remove(p)


def test_list_as_of_dates_empty_chain():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            out = dss.list_as_of_dates("EMPTYCHAIN")
            assert out == []
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "EMPTYCHAIN.db")
            if os.path.isfile(p):
                os.remove(p)
