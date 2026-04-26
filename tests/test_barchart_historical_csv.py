import pandas as pd

from core import barchart_historical_csv as bhc


def test_read_barchart_sk_headers_and_correlation():
    csv_a = """Čas,OTVORENÉ,Vysoká,Nízka,Najnovšie,Zmena,% Zmena,Objem
04/23/2026,54.0,55.0,53.0,54.5,+0.5,+0.9%,1000000
04/22/2026,53.5,54.5,52.5,54.0,-0.5,-0.9%,950000
04/21/2026,53.0,54.0,52.0,54.5,+0.5,+0.9%,900000
04/18/2026,52.5,53.5,51.5,54.0,+0.5,+0.9%,920000
04/17/2026,53.0,54.0,52.0,53.5,-0.5,-0.9%,900000
04/16/2026,54.0,55.0,53.5,54.0,+0.2,+0.4%,800000
"""
    csv_b = """Čas,Najnovšie
04/23/2026,109.0
04/22/2026,108.5
04/21/2026,109.0
04/18/2026,108.0
04/17/2026,109.0
04/16/2026,110.0
"""
    da = bhc.read_barchart_history_csv(csv_a)
    db = bhc.read_barchart_history_csv(csv_b)
    assert list(da.columns)[:2] == ["date", "close"]
    ca, cb, m = bhc.align_close_series(da, db, max_trading_days=None)
    assert len(m) == 6
    corr, n, _, _ = bhc.correlation_from_closes(ca, cb, method="pearson")
    assert n == 5
    assert -1.0 <= corr <= 1.0


def test_read_barchart_english_time_latest_headers():
    csv_en = """Time,Open,High,Low,Latest,Change,%Change,Volume
04/20/2026,55.10,55.72,54.80,55.07,+0.05,+0.09%,35150000
04/17/2026,56.50,56.80,55.00,55.02,-1.56,-2.76%,40000000
"""
    d = bhc.read_barchart_history_csv(csv_en)
    assert len(d) == 2
    assert "close" in d.columns
    # zoradené vzostupne podľa dátumu
    assert float(d.loc[d["date"] == pd.Timestamp("2026-04-20"), "close"].iloc[0]) == 55.07


def test_hist_json_roundtrip_and_pairwise_matrix():
    d1 = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06", "2026-01-07", "2026-01-08"]
            ),
            "close": [100.0, 101.0, 100.5, 102.0, 101.5, 102.5],
        }
    )
    js, f0, f1 = bhc.hist_dataframe_to_series_json(d1)
    assert f0 == "2026-01-01" and f1 == "2026-01-08"
    d1b = bhc.hist_series_json_to_dataframe(js)
    assert len(d1b) == len(d1)

    d2 = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06", "2026-01-07", "2026-01-08"]
            ),
            "close": [50.0, 50.5, 50.2, 51.0, 50.8, 51.2],
        }
    )
    d3 = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06", "2026-01-07", "2026-01-08"]
            ),
            "close": [200.0, 198.0, 199.0, 197.0, 198.5, 199.0],
        }
    )
    tickers, mat, nobs = bhc.correlation_matrix_pairwise(
        {"ZZZ": d3, "AAA": d1, "BBB": d2}, max_trading_days=None, method="pearson"
    )
    assert tickers == ["AAA", "BBB", "ZZZ"]
    assert mat[0][0] == 1.0 and mat[1][1] == 1.0 and mat[2][2] == 1.0
    assert mat[0][1] is not None and -1.0 <= float(mat[0][1]) <= 1.0


def test_extend_correlation_matrix_preserves_top_left_block():
    dates = pd.to_datetime(
        ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06", "2026-01-07", "2026-01-08"]
    )
    d1 = pd.DataFrame({"date": dates, "close": [100.0, 101.0, 100.5, 102.0, 101.5, 102.5]})
    d2 = pd.DataFrame({"date": dates, "close": [50.0, 50.5, 50.2, 51.0, 50.8, 51.2]})
    d3 = pd.DataFrame({"date": dates, "close": [10.0, 10.2, 10.1, 10.4, 10.3, 10.5]})
    old_mat = [[1.0, 0.33], [0.33, 1.0]]
    old_n = [[10, 8], [8, 9]]
    t, m, nmat = bhc.extend_correlation_matrix(
        ["AAA", "BBB"],
        old_mat,
        old_n,
        ["ZZZ"],
        {"AAA": d1, "BBB": d2, "ZZZ": d3},
        max_trading_days=None,
    )
    assert t == ["AAA", "BBB", "ZZZ"]
    assert m[0][0] == 1.0 and m[0][1] == 0.33 and m[1][0] == 0.33 and m[1][1] == 1.0
    assert m[0][2] is not None and m[1][2] is not None and m[2][0] is not None
    assert m[2][2] == 1.0
    assert nmat[0][0] == 10 and nmat[0][1] == 8
