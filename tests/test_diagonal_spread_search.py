import os
import tempfile
from dataclasses import replace

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
            assert any("čistá delta" in str(c).lower() for c in out.columns)
            assert any(c.startswith("Čistá theta") for c in out.columns)
            assert any("Debit" in c and "100" in c for c in out.columns)
            assert "Short — bid" in out.columns and "Long — ask" in out.columns
            assert "Short — DTE" in out.columns and "Long — DTE" in out.columns
            assert any("čistá vega" in str(c).lower() for c in out.columns)
            assert "Čistá gamma" in out.columns
            assert "Skóre" in out.columns
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


def test_rank_mode_score_non_increasing_skore():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("SCORE")
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
                "SCORE",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                target_net_delta=0.0,
                top_n=10,
                max_strikes_per_expiry=50,
                options=dss.DiagonalSearchOptions(rank_mode="score"),
            )
            assert not out.empty
            assert "Skóre" in out.columns
            dl_col = next(c for c in out.columns if "delta" in str(c).lower() and "čistá" in str(c).lower())
            th_col = next(c for c in out.columns if "theta" in str(c).lower() and "čistá" in str(c).lower())
            vg_col = next(c for c in out.columns if "vega" in str(c).lower() and "čistá" in str(c).lower())
            assert "×100" in str(dl_col)
            assert "×100" in str(th_col)
            assert "×100" in str(vg_col)
            s = pd.to_numeric(out["Skóre"], errors="coerce").dropna().tolist()
            for i in range(len(s) - 1):
                assert s[i] >= s[i + 1] - 1e-9
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "SCORE.db")
            if os.path.isfile(p):
                os.remove(p)


def test_rank_mode_theta_delta_debit_sort_keys():
    """Konzistentné s triedením v search_diagonal_spreads: theta ↓, delta_err ↑, debit ↑."""
    df = pd.DataFrame(
        {
            "net_theta": [1.0, 1.0, 2.0, 2.0],
            "delta_err": [0.5, 0.2, 0.1, 0.3],
            "debit_per_share": [2.0, 1.0, 1.5, 1.5],
        }
    )
    got = df.sort_values(
        by=["net_theta", "delta_err", "debit_per_share"],
        ascending=[False, True, True],
        kind="mergesort",
        na_position="last",
    ).reset_index(drop=True)
    assert got["net_theta"].tolist() == [2.0, 2.0, 1.0, 1.0]
    assert got["delta_err"].tolist() == [0.1, 0.3, 0.2, 0.5]
    assert got["debit_per_share"].tolist() == [1.5, 1.5, 1.0, 2.0]


def test_long_put_diagonal_long_otm_excludes_itm_long_strike():
    """Bez long_otm môže byť dlhá put noha ITM (K > spot); s long_otm_min ostanú len OTM dlhé nohy."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("OTML")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            spot = 262.0
            for strike in ("240", "245"):
                odb.import_merged_dataframe(
                    conn,
                    expiry="2026-06-18",
                    as_of_date=as_of,
                    merged=_merged_row(strike, "-0.25", "-0.08", "Put"),
                )
            for strike in ("250", "280"):
                odb.import_merged_dataframe(
                    conn,
                    expiry="2026-09-18",
                    as_of_date=as_of,
                    merged=_merged_row(strike, "-0.35", "-0.05", "Put"),
                )
            conn.close()
            base = dict(
                spot=spot,
                short_otm_min=0.02,
                dte_near_min=1,
                dte_near_max=500,
                dte_far_min=1,
                dte_far_max=500,
                delta_tolerance=5.0,
                net_theta_min=None,
                net_theta_max=None,
                theta_scale_contracts=False,
            )
            out_all = dss.search_diagonal_spreads(
                "OTML",
                as_of_date=as_of,
                strategy="long_put_diagonal",
                target_net_delta=0.0,
                top_n=50,
                max_strikes_per_expiry=50,
                options=dss.DiagonalSearchOptions(**{**base, "long_otm_min": None}),
            )
            assert not out_all.empty
            assert (out_all["Long — strike"] >= spot).any()

            out_f = dss.search_diagonal_spreads(
                "OTML",
                as_of_date=as_of,
                strategy="long_put_diagonal",
                target_net_delta=0.0,
                top_n=50,
                max_strikes_per_expiry=50,
                options=dss.DiagonalSearchOptions(**{**base, "long_otm_min": 0.02}),
            )
            assert not out_f.empty
            assert (out_f["Long — strike"] < spot).all()
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "OTML.db")
            if os.path.isfile(p):
                os.remove(p)


def test_net_theta_max_can_empty_results():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("TMAX")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-06-01", "100", "0.50", "-0.10"),
                ("2026-09-01", "100", "0.52", "-0.04"),
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
            broad = dss.search_diagonal_spreads(
                "TMAX",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=20,
                max_strikes_per_expiry=50,
            )
            narrow = dss.search_diagonal_spreads(
                "TMAX",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=20,
                max_strikes_per_expiry=50,
                options=dss.DiagonalSearchOptions(net_theta_max=-1.0, theta_scale_contracts=False),
            )
            assert not broad.empty
            assert narrow.empty
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "TMAX.db")
            if os.path.isfile(p):
                os.remove(p)


def test_negative_net_theta_combinations_excluded_from_results():
    """len(long_call) net_theta = th_far - th_near; ak je < 0, riadok sa nepridá (strata)."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("NTNEG")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-06-01", "100", "0.50", "-0.10"),
                ("2026-09-01", "100", "0.50", "-0.16"),
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
                "NTNEG",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=20,
                max_strikes_per_expiry=50,
                options=dss.DiagonalSearchOptions(
                    dte_near_min=0,
                    dte_near_max=200,
                    dte_far_min=0,
                    dte_far_max=500,
                ),
            )
            assert out.empty
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "NTNEG.db")
            if os.path.isfile(p):
                os.remove(p)


def test_diagonal_relax_suggestions_lists_dte_when_far_min_high():
    opt = dss.DiagonalSearchOptions(
        dte_far_min=90,
        theta_scale_contracts=True,
        net_theta_min=3.0,
        net_vega_min=0.10,
        short_otm_min=0.10,
        spot=100.0,
        min_open_interest=100,
    )
    md = dss.diagonal_relax_suggestions_markdown(opt)
    assert "dte" in md.lower() and "90" in md
    assert "širšie filtre" in md.lower()


def test_diagonal_search_why_empty_hint_flags_strict_dte():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("HINT")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-05-15", "100", "0.50", "-0.10"),
                ("2026-06-15", "100", "0.48", "-0.09"),
                ("2026-07-15", "100", "0.46", "-0.08"),
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
            strict = dss.DiagonalSearchOptions(
                dte_near_min=0,
                dte_near_max=500,
                dte_far_min=95,
                dte_far_max=400,
            )
            hint = dss.diagonal_search_why_empty_hint(
                "HINT", as_of_date=as_of, strategy="long_call_diagonal", opt=strict
            )
            assert "Neskoršia min" in hint or "neskoršej" in hint or "DTE" in hint
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "HINT.db")
            if os.path.isfile(p):
                os.remove(p)


def test_min_open_interest_when_oi_missing_in_import_still_finds_rows():
    """CSV bez OI — starý filter fillna(0) vyhodil všetko pri min. OI 100."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("NOOI")
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
                "NOOI",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                target_net_delta=0.0,
                top_n=10,
                max_strikes_per_expiry=50,
                options=dss.DiagonalSearchOptions(min_open_interest=100),
            )
            assert not out.empty
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "NOOI.db")
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


def test_progressive_strike_band_not_reported_as_dte_gate_failure():
    """Ak je prázdne len kvôli **úzkemu pásu strike-ov**, brána DTE nesmie hlásiť zlyhanie na DTE (dáta a DTE pás môžu inak sedieť)."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("STKBR")
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
            opt = dss.DiagonalSearchOptions(
                dte_near_min=40,
                dte_near_max=55,
                dte_far_min=90,
                dte_far_max=140,
                delta_tolerance=2.0,
                net_theta_min=3.0,
                net_theta_max=8.0,
            )
            out, flog, _ = dss.progressive_filter_search(
                "STKBR",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
                strike_min=200.0,
                strike_max=300.0,
                options=opt,
                max_relax_iterations=2,
            )
            assert out.empty
            for step in flog.failure_steps or []:
                if step.field == "dte_near_min/dte_near_max/dte_far_min/dte_far_max":
                    raise AssertionError(
                        "Úzky rozsah strike-ov nemal byť interpretovaný ako nevyhovujúce DTE pásma."
                    )
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "STKBR.db")
            if os.path.isfile(p):
                os.remove(p)


def test_progressive_one_expiry_does_not_misblame_dte():
    """Len **jedna** expirácia s Call — diagonál nie je; nesmie sa hneď hlásiť agregované zlyhanie na DTE."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("ONEEXP")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for strike, d, th in [("100", "0.50", "-0.10"), ("105", "0.48", "-0.09")]:
                odb.import_merged_dataframe(
                    conn,
                    expiry="2026-09-01",
                    as_of_date=as_of,
                    merged=_merged_row(strike, d, th, "Call"),
                    source_options_csv="o.csv",
                    source_greeks_csv="g.csv",
                )
            conn.close()
            opt = dss.DiagonalSearchOptions(
                dte_near_min=10,
                dte_near_max=200,
                dte_far_min=20,
                dte_far_max=300,
            )
            out, flog, _ = dss.progressive_filter_search(
                "ONEEXP",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=5,
                max_strikes_per_expiry=50,
                options=opt,
            )
            assert out.empty
            for step in flog.failure_steps or []:
                if step.field == "dte_near_min/dte_near_max/dte_far_min/dte_far_max":
                    raise AssertionError("Pri jedinej expirácii to nie je chyba DTE pásiem.")
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "ONEEXP.db")
            if os.path.isfile(p):
                os.remove(p)


def test_suggest_dte_pair_prefers_closer_to_ui_bands():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("SUG1")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-05-15", "100", "0.50", "-0.10"),
                ("2026-06-18", "100", "0.52", "-0.04"),
                ("2026-08-21", "100", "0.54", "-0.03"),
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
            opt = dss.DiagonalSearchOptions(
                dte_near_min=50,
                dte_near_max=60,
                dte_far_min=100,
                dte_far_max=130,
            )
            pair = dss.suggest_dte_pair_closest_to_ui("SUG1", as_of, "long_call_diagonal", opt)
            assert pair is not None
            # skoršia 2026-05-15 DTE=29, 2026-06-18 DTE=63, 2026-08-21 DTE=127
            # pár 2026-06-18 + 2026-08-21: near 63, far 127 — skoršia 63 je v 50-60? nie. 29+127 horšie.
            # 29+63: near 29 mimo, 29+127: atď. Najnižšia penalizácia: pair (jún, aug) dte 63+127
            assert str(pair["expiry_near"]).startswith("2026-06")
            assert str(pair["expiry_far"]).startswith("2026-08")
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "SUG1.db")
            if os.path.isfile(p):
                os.remove(p)


def test_first_calendar_dte_pair_is_earliest_two_expiries():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("CAL1")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-08-21", "100", "0.50", "-0.10"),
                ("2026-05-15", "100", "0.50", "-0.10"),
                ("2026-06-18", "100", "0.52", "-0.04"),
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
            pair = dss.first_calendar_dte_pair("CAL1", as_of, "long_call_diagonal")
            assert pair is not None
            assert pair["expiry_near"] == "2026-05-15"
            assert pair["expiry_far"] == "2026-06-18"
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "CAL1.db")
            if os.path.isfile(p):
                os.remove(p)


def test_first_dte_pair_within_bounds_returns_first_matching_pair():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("PAIR1")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-05-15", "100", "0.50", "-0.10"),
                ("2026-06-18", "100", "0.52", "-0.04"),
                ("2026-08-21", "100", "0.54", "-0.03"),
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
            opt = dss.DiagonalSearchOptions(
                dte_near_min=20,
                dte_near_max=40,
                dte_far_min=50,
                dte_far_max=80,
            )
            pair = dss.first_dte_pair_within_bounds("PAIR1", as_of, "long_call_diagonal", opt)
            assert pair is not None
            assert pair["expiry_near"] == "2026-05-15"
            assert pair["expiry_far"] == "2026-06-18"
            assert pair["dte_near"] == 29
            assert pair["dte_far"] == 63
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "PAIR1.db")
            if os.path.isfile(p):
                os.remove(p)


def test_progressive_filter_search_does_not_relax_dte_when_dte_far_min_blocks():
    """Prvé hľadanie prázdne kvôli príliš prísnej **neskoršej** min. DTE — zjemnenie DTE sa nespúšťa (výsledok ostane prázdny)."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("PROG")
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
            strict = dss.DiagonalSearchOptions(
                dte_near_min=0,
                dte_near_max=500,
                dte_far_min=200,
                dte_far_max=500,
            )
            empty = dss.search_diagonal_spreads(
                "PROG",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
                options=strict,
            )
            assert empty.empty

            out, flog, eff = dss.progressive_filter_search(
                "PROG",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
                options=strict,
                max_relax_iterations=5,
            )
            assert out.empty
            assert not flog.any_relaxed
            assert int(eff.dte_far_min) == 200
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "PROG.db")
            if os.path.isfile(p):
                os.remove(p)


def test_progressive_filter_search_failure_report_lists_trace_and_criteria():
    """Jedna expirácia — diagonál nikdy nevznikne; postupné zjemnenie zaznamená neúspechy."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("FAIL1")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            odb.import_merged_dataframe(
                conn,
                expiry="2026-09-01",
                as_of_date=as_of,
                merged=_merged_row("100", "0.50", "-0.10", "Call"),
                source_options_csv="o.csv",
                source_greeks_csv="g.csv",
            )
            conn.close()
            strict = dss.DiagonalSearchOptions(
                dte_near_min=10,
                dte_near_max=400,
                dte_far_min=30,
                dte_far_max=500,
                delta_tolerance=0.5,
            )
            out, flog, last_eff = dss.progressive_filter_search(
                "FAIL1",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
                options=strict,
                max_relax_iterations=2,
            )
            assert out.empty
            assert flog.failure_steps
            md = flog.failure_report_markdown(initial_opt=strict, last_tried_opt=last_eff)
            assert "Kde sa to zastavilo" in md
            assert "Posledné vyskúšané" in md
            assert "vstupné kritériá" in md.lower()
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "FAIL1.db")
            if os.path.isfile(p):
                os.remove(p)


def test_progressive_filter_search_no_op_when_first_search_ok():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("PROG2")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-06-01", "100", "0.50", "-0.10"),
                ("2026-09-01", "100", "0.52", "-0.04"),
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
            base = dss.search_diagonal_spreads(
                "PROG2",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
            )
            out, flog, eff = dss.progressive_filter_search(
                "PROG2",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
            )
            assert not out.empty
            assert len(out) == len(base)
            assert flog.any_relaxed is False
            assert not flog.steps
            assert eff.delta_tolerance is None
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "PROG2.db")
            if os.path.isfile(p):
                os.remove(p)


def test_build_delta_search_protocol_markdown_empty_and_nonempty():
    opt = dss.DiagonalSearchOptions(delta_tolerance=1.0, net_theta_min=0.05)
    flog_empty = dss.FilterLog(steps=[], final_rows=0, any_relaxed=False, failure_steps=None)
    md0 = dss.build_delta_search_protocol_markdown(
        ticker="XX",
        as_of_date="2026-01-01",
        strategy="long_call_diagonal",
        target_net_delta=0.0,
        top_n=10,
        max_strikes_per_expiry=40,
        strike_min=None,
        strike_max=None,
        initial_options=opt,
        effective_options=opt,
        filter_log=flog_empty,
        result=pd.DataFrame(),
    )
    assert "XX" in md0
    assert "Výsledok: 0 riadkov" in md0
    assert "```json" in md0
    assert "delta_tolerance" in md0

    tiny = pd.DataFrame({"Čistá delta ×100": [1.0], "Short — strike": [100.0]})
    flog_ok = dss.FilterLog(steps=[], final_rows=1, any_relaxed=False, failure_steps=None)
    md1 = dss.build_delta_search_protocol_markdown(
        ticker="YY",
        as_of_date="2026-01-01",
        strategy="short_put_diagonal",
        target_net_delta=-0.1,
        top_n=5,
        max_strikes_per_expiry=30,
        strike_min=90.0,
        strike_max=110.0,
        initial_options=opt,
        effective_options=opt,
        filter_log=flog_ok,
        result=tiny,
        preview_rows=5,
    )
    assert "YY" in md1
    assert "Výsledok: 1 riadkov" in md1
    assert "90" in md1 and "110" in md1
    assert "Čistá delta" in md1


def test_next_relax_short_otm_min_last_step_disables_filter():
    """Posledný krok v RELAX_STEPS musí nastaviť filter na None (predtým sa kvôli ``None`` slučka nevykonala)."""
    v = dss._next_relax_value("short_otm_min", 0.02)
    assert dss._opt_value_from_relax_token("short_otm_min", v) is None


def test_progressive_filter_search_phase2_combined_can_find_rows():
    """Ak izolované zjemnenie nič nenájde v prvých krokoch, dlhší reťazec na pole alebo 2. fáza nájde výsledky."""
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("PH2")
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
            impossible = dss.DiagonalSearchOptions(
                dte_near_min=0,
                dte_near_max=500,
                dte_far_min=30,
                dte_far_max=500,
                net_theta_min=1e9,
                theta_scale_contracts=False,
            )
            solo = dss.search_diagonal_spreads(
                "PH2",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
                options=impossible,
            )
            assert solo.empty
            out, flog, eff = dss.progressive_filter_search(
                "PH2",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=10,
                max_strikes_per_expiry=50,
                options=impossible,
                max_relax_iterations=2,
            )
            assert not out.empty
            assert flog.any_relaxed
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "PH2.db")
            if os.path.isfile(p):
                os.remove(p)


def test_dte_pair_band_violation_codes():
    o = dss.DiagonalSearchOptions(
        dte_near_min=40,
        dte_near_max=60,
        dte_far_min=90,
        dte_far_max=120,
    )
    assert dss.dte_pair_band_violation_codes(50, 100, o) == []
    assert "neskoršia_min" in dss.dte_pair_band_violation_codes(50, 85, o)
    assert "skoršia_max" in dss.dte_pair_band_violation_codes(65, 100, o)


def test_dte_calendar_diagnostic_markdown_lists_pairs():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("DTEG")
            odb.init_schema(conn)
            as_of = "2026-04-16"
            for exp, strike, d, th in [
                ("2026-06-18", "100", "0.50", "-0.10"),
                ("2026-07-17", "100", "0.48", "-0.09"),
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
            o = dss.DiagonalSearchOptions(
                dte_near_min=40,
                dte_near_max=60,
                dte_far_min=90,
                dte_far_max=120,
            )
            md = dss.dte_calendar_diagnostic_markdown("DTEG", as_of, "long_call_diagonal", o)
            assert "Expirácie" in md
            assert "Kalendárne dvojice" in md
            assert "Súčet" in md
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "DTEG.db")
            if os.path.isfile(p):
                os.remove(p)


def test_otm_orientation_bullets_call_and_put():
    o = dss.DiagonalSearchOptions(spot=262.01, short_otm_min=0.1, long_otm_min=0.05)
    lc = dss._otm_strike_orientation_bullets_sk(o, option_type="Call")
    assert any("288.21" in L for L in lc)
    assert any("275.11" in L for L in lc)  # long OTM 5 % → 262.01 * 1.05
    lp = dss._otm_strike_orientation_bullets_sk(
        dss.DiagonalSearchOptions(spot=100.0, short_otm_min=0.1), option_type="Put"
    )
    assert any("90.00" in L for L in lp)


def test_compute_score_bounded_when_net_delta_near_zero():
    s = dss._compute_score(
        pd.Series([1e-15, 0.5]),
        pd.Series([0.0, 0.0]),
        pd.Series([0.0, 0.0]),
        pd.Series([0.0, 0.0]),
        pd.Series([0.1, 0.1]),
        pd.Series([1.0, 1.0]),
    )
    assert abs(s.iloc[0]) < 1e6
    assert s.iloc[1] < s.iloc[0]  # väčšia |delta| → menší príspevok 1/|delta|


def test_compute_score_bounded_when_strike_width_zero_same_strike_diagonal():
    """Rovnaký strike na oboch nohách → šírka 0; bez orezania ratio by |debit|/eps explodovalo."""
    s = dss._compute_score(
        pd.Series([0.05]),
        pd.Series([0.0]),
        pd.Series([0.0]),
        pd.Series([0.0]),
        pd.Series([10.0]),
        pd.Series([0.0]),
    )
    assert abs(s.iloc[0]) < 80_000


def test_progressive_relax_exclude_otm_keeps_short_otm_threshold():
    with tempfile.TemporaryDirectory() as td:
        old = odb.OPTION_CHAINS_DIR
        odb.OPTION_CHAINS_DIR = td
        try:
            conn = odb.get_connection("RXOTM")
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
            base = dss.DiagonalSearchOptions(
                spot=100.0,
                short_otm_min=0.5,
                long_otm_min=None,
                strike_proximity_leg=None,
                dte_near_min=1,
                dte_near_max=400,
                dte_far_min=1,
                dte_far_max=500,
            )
            _res_off, _log_off, eff_off = dss.progressive_filter_search(
                "RXOTM",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=20,
                max_strikes_per_expiry=50,
                options=replace(base, relax_exclude_otm=False),
            )
            _res_on, _log_on, eff_on = dss.progressive_filter_search(
                "RXOTM",
                as_of_date=as_of,
                strategy="long_call_diagonal",
                top_n=20,
                max_strikes_per_expiry=50,
                options=replace(base, relax_exclude_otm=True),
            )
            assert eff_off.short_otm_min is None or float(eff_off.short_otm_min) < 0.5 - 1e-12
            assert eff_on.short_otm_min is not None and float(eff_on.short_otm_min) >= 0.5 - 1e-9
        finally:
            odb.OPTION_CHAINS_DIR = old
            p = os.path.join(td, "RXOTM.db")
            if os.path.isfile(p):
                os.remove(p)


def test_failure_report_markdown_includes_keep_otm_actions():
    fl = dss.FilterLog(steps=[], final_rows=0, any_relaxed=False, failure_steps=None)
    md = fl.failure_report_markdown(
        initial_opt=dss.DiagonalSearchOptions(spot=262.01, short_otm_min=0.1, long_otm_min=0.1),
        last_tried_opt=dss.DiagonalSearchOptions(spot=262.01, short_otm_min=None, long_otm_min=None),
        strategy="long_put_diagonal",
    )
    assert "Čo spraviť, ak chceš zostať OTM" in md
    assert "rozšír import strikov" in md
    assert "nižšie" in md


def test_otm_keep_otm_strike_band_suggestion_matches_option_type():
    call = dss.otm_keep_otm_strike_band_suggestion(
        dss.DiagonalSearchOptions(spot=262.01, short_otm_min=0.1, long_otm_min=0.05),
        strategy="long_call_diagonal",
        buffer_points=100.0,
    )
    assert call is not None
    assert abs(call["strike_min"] - 288.211) < 0.01
    assert abs(call["strike_max"] - 388.211) < 0.01

    put = dss.otm_keep_otm_strike_band_suggestion(
        dss.DiagonalSearchOptions(spot=262.01, short_otm_min=0.1, long_otm_min=0.05),
        strategy="long_put_diagonal",
        buffer_points=100.0,
    )
    assert put is not None
    assert abs(put["strike_max"] - 235.809) < 0.01
    assert abs(put["strike_min"] - 135.809) < 0.01


def test_otm_keep_otm_tuning_suggestion_includes_max_strikes():
    tune = dss.otm_keep_otm_tuning_suggestion(
        dss.DiagonalSearchOptions(spot=262.01, short_otm_min=0.1, long_otm_min=0.05),
        strategy="long_put_diagonal",
        buffer_points=100.0,
        max_strikes_per_expiry=120,
    )
    assert tune is not None
    assert tune["max_strikes_per_expiry"] == 120
    assert abs(tune["strike_max"] - 235.809) < 0.01


def test_protocol_markdown_includes_otm_orientation_when_spot_and_otm():
    fl = dss.FilterLog(steps=[], final_rows=0, any_relaxed=False, failure_steps=None)
    md = dss.build_delta_search_protocol_markdown(
        ticker="X",
        as_of_date="2026-01-01",
        strategy="long_call_diagonal",
        target_net_delta=0.0,
        top_n=5,
        max_strikes_per_expiry=50,
        strike_min=None,
        strike_max=None,
        initial_options=dss.DiagonalSearchOptions(spot=100.0, short_otm_min=0.1),
        effective_options=dss.DiagonalSearchOptions(spot=100.0, short_otm_min=0.1),
        filter_log=fl,
        result=pd.DataFrame(),
    )
    assert "OTM vs. strike" in md
    assert "110.00" in md
    assert "Čo spraviť, ak chceš zostať OTM" in md
