import io
import os
import tempfile

import pandas as pd
import pytest

from core import option_chain_db as m


class _FakeUpload:
    def __init__(self, name: str, text: str):
        self.name = name
        self._bio = io.BytesIO(text.encode("utf-8"))

    def read(self, n: int = -1):
        return self._bio.read(n)

    def seek(self, pos: int, whence: int = 0) -> int:
        return self._bio.seek(pos, whence)


def test_parse_barchart_filename():
    p = "amzn-options-exp-2026-05-15-monthly-near-the-money-stacked-04-16-2026.csv"
    meta = m.parse_barchart_option_filename(p)
    assert meta is not None
    assert meta.ticker == "AMZN"
    assert meta.kind == "options"
    assert meta.expiry == "2026-05-15"
    assert meta.as_of_date == "2026-04-16"

    g = "amzn-volatility-greeks-exp-2026-05-15-monthly-near-the-money-04-16-2026.csv"
    mg = m.parse_barchart_option_filename(g)
    assert mg is not None
    assert mg.kind == "greeks"


def test_parse_float_eu():
    assert abs(m._parse_float("50,15") - 50.15) < 1e-9
    assert abs(m._parse_float("0,9387") - 0.9387) < 1e-9
    assert m._parse_float("52.28%") == 52.28
    assert m._parse_float("17,610") == 17610.0


def test_merge_options_and_greeks():
    opts = pd.DataFrame(
        {
            "Strike": ["200", "205"],
            "Bid": ["100,1", "95,0"],
            "Mid": ["101,0", "96,0"],
            "Ask": ["102,0", "97,0"],
            "Latest": ["101,5", "96,5"],
            "IV": ["50%", "48%"],
            "Delta": ["0,90", "0,85"],
            "Type": ["Call", "Call"],
        }
    )
    greeks = pd.DataFrame(
        {
            "Strike": ["200", "205"],
            "Theta": ["-0,09", "-0,08"],
            "Gamma": ["0,01", "0,02"],
            "IV": ["50%", "48%"],
            "Delta": ["0,90", "0,85"],
            "Type": ["Call", "Call"],
        }
    )
    merged = m.merge_options_and_greeks(opts, greeks)
    assert len(merged) == 2
    assert merged["theta"].tolist() == ["-0,09", "-0,08"]
    assert merged["bid"].tolist() == ["100,1", "95,0"]


def test_import_roundtrip():
    opts = pd.DataFrame(
        {
            "Strike": ["250"],
            "Bid": ["10"],
            "Mid": ["10,5"],
            "Ask": ["11"],
            "Latest": ["10,2"],
            "Moneyness": ["0%"],
            "IV": ["40%"],
            "Delta": ["0,5"],
            "Type": ["Call"],
        }
    )
    greeks = pd.DataFrame(
        {
            "Strike": ["250"],
            "Theta": ["-0,05"],
            "Gamma": ["0,1"],
            "IV": ["40%"],
            "Delta": ["0,5"],
            "Type": ["Call"],
        }
    )
    merged = m.merge_options_and_greeks(opts, greeks)
    with tempfile.TemporaryDirectory() as td:
        old = m.OPTION_CHAINS_DIR
        m.OPTION_CHAINS_DIR = td
        try:
            conn = m.get_connection("TEST")
            m.init_schema(conn)
            m.import_merged_dataframe(
                conn,
                expiry="2026-06-01",
                as_of_date="2026-04-12",
                merged=merged,
                source_options_csv="x.csv",
                source_greeks_csv="y.csv",
            )
            conn.close()
            df = m.read_chain("TEST", expiry="2026-06-01", as_of_date="2026-04-12")
            assert len(df) == 1
            assert float(df.iloc[0]["strike"]) == 250.0
            assert abs(float(df.iloc[0]["iv"]) - 0.40) < 1e-6
            assert df.iloc[0]["theta"] is not None
        finally:
            m.OPTION_CHAINS_DIR = old
            dbp = os.path.join(td, "TEST.db")
            if os.path.isfile(dbp):
                os.remove(dbp)


def test_import_pair_from_uploads_roundtrip():
    csv_o = "Strike,Bid,Mid,Ask,Latest,IV,Delta,Type\n200,1,2,3,2,50%,0.9,Call\n"
    csv_g = "Strike,Latest,Theor,IV,Delta,Gamma,Theta,Type\n200,2,2,50%,0.9,0.01,-0.05,Call\n"
    name_o = "amzn-options-exp-2026-05-15-monthly-stacked-04-16-2026.csv"
    name_g = "amzn-volatility-greeks-exp-2026-05-15-monthly-04-16-2026.csv"
    with tempfile.TemporaryDirectory() as td:
        old = m.OPTION_CHAINS_DIR
        m.OPTION_CHAINS_DIR = td
        try:
            t, n = m.import_pair_from_uploads(_FakeUpload(name_o, csv_o), _FakeUpload(name_g, csv_g))
            assert t == "AMZN"
            assert n == 1
            df = m.read_chain("AMZN", expiry="2026-05-15", as_of_date="2026-04-16")
            assert len(df) == 1
        finally:
            m.OPTION_CHAINS_DIR = old
            for fn in ("AMZN.db",):
                p = os.path.join(td, fn)
                if os.path.isfile(p):
                    os.remove(p)


def test_import_pair_core_ticker_mismatch():
    meta = m.ParsedFilename("AMZN", "options", "2026-05-15", "2026-04-16")
    with pytest.raises(ValueError, match="Ticker musí sedieť"):
        m.import_pair_core(
            "MSFT",
            pd.DataFrame({"Strike": ["200"], "Type": ["Call"]}),
            pd.DataFrame(),
            meta_o=meta,
            meta_g=None,
            source_options="x.csv",
            source_greeks=None,
        )


def test_side_import_preserves_existing_fields():
    with tempfile.TemporaryDirectory() as td:
        old = m.OPTION_CHAINS_DIR
        m.OPTION_CHAINS_DIR = td
        try:
            ticker, expiry, as_of = "AMZN", "2026-07-17", "2026-04-16"
            # 1) najprv import options-only
            df_opt = pd.DataFrame(
                {
                    "Strike": ["200"],
                    "Bid": ["10.0"],
                    "Mid": ["10.5"],
                    "Ask": ["11.0"],
                    "Latest": ["10.6"],
                    "IV": ["40%"],
                    "Delta": ["0.50"],
                    "Type": ["Call"],
                }
            )
            n1 = m.import_snapshot_side_only(
                ticker,
                expiry,
                as_of,
                "options",
                source_name="opt.csv",
                df=df_opt,
            )
            assert n1 == 1
            # 2) potom dopln greeks-only
            df_gk = pd.DataFrame(
                {
                    "Strike": ["200"],
                    "Latest": ["10.6"],
                    "Theor": ["10.4"],
                    "IV": ["41%"],
                    "Delta": ["0.49"],
                    "Gamma": ["0.01"],
                    "Theta": ["-0.05"],
                    "Vega": ["0.10"],
                    "Rho": ["0.02"],
                    "Type": ["Call"],
                }
            )
            n2 = m.import_snapshot_side_only(
                ticker,
                expiry,
                as_of,
                "greeks",
                source_name="gk.csv",
                df=df_gk,
            )
            assert n2 == 1
            out = m.read_chain(ticker, expiry=expiry, as_of_date=as_of)
            assert len(out) == 1
            row = out.iloc[0]
            # options údaje ostali
            assert abs(float(row["bid"]) - 10.0) < 1e-9
            assert abs(float(row["ask"]) - 11.0) < 1e-9
            assert row["source_options_csv"] == "opt.csv"
            # greeks sa doplnili
            assert row["source_greeks_csv"] == "gk.csv"
            assert row["theta"] is not None
        finally:
            m.OPTION_CHAINS_DIR = old
