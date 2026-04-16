"""
Hľadanie diagonálnych spreadov z DB Grékov: čistá delta ~ cieľ, max. čistá theta (jednotky Barchart).
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core import diagonal_spread_search as dss
from core import option_chain_db as odb
from core import saved_diagonals_db as sdiag
from core.page_context import set_tradejournal_page

set_tradejournal_page("delta_search_diagonal")

if "dsd_last_results" not in st.session_state:
    st.session_state["dsd_last_results"] = None
if "dsd_last_meta" not in st.session_state:
    st.session_state["dsd_last_meta"] = {}

# Tabuľkové číslice — rovnaká šírka číslic v stĺpci (lepšie zarovnanie v tabuľke výsledkov)
_DSD_TABLE_STYLE = """
<style>
div[data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }
</style>
"""


def _spread_table_column_config(df: pd.DataFrame) -> dict:
    """Formát a šírky stĺpcov (výsledky hľadania aj uložené)."""
    cfg: dict = {}
    for c in df.columns:
        if c in ("Uložiť", "Zmazať"):
            cfg[c] = st.column_config.CheckboxColumn(
                c,
                default=False,
                help="Označ riadky" + (" na uloženie" if c == "Uložiť" else " na zmazanie z DB"),
            )
        elif c == "ID":
            cfg[c] = st.column_config.NumberColumn(c, format="%d", disabled=True)
        elif c in ("Uložené", "Ticker uloženia", "Snímka uloženia", "Stratégia ID"):
            cfg[c] = st.column_config.TextColumn(c, width="small")
        elif c == "Stratégia":
            cfg[c] = st.column_config.TextColumn(c, width="large")
        elif c == "Typ":
            cfg[c] = st.column_config.TextColumn(c, width="small")
        elif "DTE" in c:
            cfg[c] = st.column_config.NumberColumn(c, format="%d", width="small")
        elif "expirácia" in c.lower():
            cfg[c] = st.column_config.TextColumn(c, width="small")
        elif "strike" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.1f", width="small")
        elif "bid" in c.lower() or "ask" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.2f", width="small")
        elif "Debit" in c or "kredit" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.2f", width="small")
        elif "delta" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.4f", width="small")
        elif "theta" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.4f", width="medium")
        else:
            cfg[c] = st.column_config.TextColumn(c, width="medium")
    return cfg


st.title("Hľadanie delty — diagonály")
st.caption(
    "Dáta z **DB Grékov** (`data/option_chains/*.db`). Dva kontrakty rovnakého typu (Call alebo Put): "
    "**blízka expirácia** = skorší dátum, **ďaleká expirácia** = neskorší. Gréky sú za long 1 kontrakt; váhy zodpovedajú long/short nohám."
)

tickers = odb.list_chain_tickers()
if not tickers:
    st.info("Najprv importuj reťazce v **DB Grékov**.")
    st.stop()

c1, c2, c3 = st.columns([1, 1, 1])
with c1:
    ticker = st.selectbox("Ticker", options=tickers, key="dsd_ticker")
with c2:
    dates = dss.list_as_of_dates(ticker)
    if not dates:
        st.warning("Pre tento ticker nie sú v DB žiadne snímky.")
        st.stop()
    as_of = st.selectbox("Dátum snímky (as-of)", options=dates, key="dsd_asof")
with c3:
    strat_labels = {
        "long_call_diagonal": "Long call diagonál",
        "short_call_diagonal": "Short call diagonál",
        "long_put_diagonal": "Long put diagonál",
        "short_put_diagonal": "Short put diagonál",
    }
    strategy = st.selectbox(
        "Stratégia",
        options=list(strat_labels.keys()),
        format_func=lambda k: strat_labels[k],
        key="dsd_strat",
    )

st.markdown(
    dss.STRATEGIES[strategy].label_sk
    + " — **blízka** expirácia má skorší dátum ako **ďaleká**."
)

c4, c5, c6 = st.columns([1, 1, 1])
with c4:
    target_d = st.number_input(
        "Cieľová čistá delta",
        value=0.0,
        step=0.01,
        format="%.4f",
        help="Triedenie: najprv najmenšia odchýlka |čistá delta − cieľ|, potom najväčšia čistá theta (+ = príjem z decay, − = strata).",
        key="dsd_target",
    )
with c5:
    top_n = st.number_input("Max. počet výsledkov", min_value=5, max_value=200, value=40, step=5, key="dsd_topn")
with c6:
    max_k = st.number_input(
        "Max. strike-ov na expiráciu (výkon)",
        min_value=15,
        max_value=120,
        value=55,
        step=5,
        key="dsd_maxk",
    )

use_strike_band = st.checkbox(
    "Obmedziť rozsah strike (obidve nohy musia byť v intervale)",
    value=False,
    key="dsd_strike_band",
)
c7, c8 = st.columns(2)
with c7:
    strike_od = st.number_input(
        "Strike od",
        min_value=0.0,
        value=100.0,
        step=1.0,
        format="%.1f",
        disabled=not use_strike_band,
        key="dsd_strike_min",
    )
with c8:
    strike_do = st.number_input(
        "Strike do",
        min_value=0.0,
        value=500.0,
        step=1.0,
        format="%.1f",
        disabled=not use_strike_band,
        key="dsd_strike_max",
    )

if st.button("Hľadať", type="primary", key="dsd_run"):
    with st.spinner("Počítam kombinácie expirácií a strike-ov…"):
        try:
            smin = float(strike_od) if use_strike_band else None
            smax = float(strike_do) if use_strike_band else None
            res = dss.search_diagonal_spreads(
                ticker,
                as_of_date=as_of,
                strategy=strategy,
                target_net_delta=float(target_d),
                top_n=int(top_n),
                max_strikes_per_expiry=int(max_k),
                strike_min=smin,
                strike_max=smax,
            )
        except Exception as exc:
            st.error(f"Chyba: {type(exc).__name__}: {exc}")
            st.stop()
    if res.empty:
        st.session_state["dsd_last_results"] = None
        st.session_state["dsd_last_meta"] = {}
        st.warning(
            "Žiadny výsledok — potrebujú sa aspoň **dve rôzne expirácie** v DB pre túto snímku "
            "a riadky s vyplnenou **delta** aj **theta** pre zvolený typ opcie."
        )
    else:
        st.session_state["dsd_last_results"] = res
        st.session_state["dsd_last_meta"] = {
            "ticker": ticker,
            "as_of": as_of,
            "strategy": strategy,
        }
        st.success(f"Nájdených **{len(res)}** najlepších kombinácií (zoradené podľa delty, potom theta).")

res = st.session_state.get("dsd_last_results")
meta = st.session_state.get("dsd_last_meta") or {}
if res is not None and not res.empty:
    st.caption(
        f"_Posledné hľadanie: **{meta.get('ticker', '')}** · snímka **{meta.get('as_of', '')}** · "
        f"`{meta.get('strategy', '')}` — označ **Uložiť** pri riadkoch a potvrď tlačidlom._"
    )
    st.caption(
        "**Debit/kredit ($/1 lot ×100)** = (Long ask − Short bid) × **100** pri otvorení jedného kontraktu "
        "(kladné = platíš debit, záporné = berieš kredit v USD za lot)."
    )
    st.caption(
        "**Čistá theta:** **+** = decay v tvoj prospech (zjednodušene ako *denný príjem* z theta v týchto jednotkách), "
        "**−** = decay proti tebe (*denná strata*). Ide o model z reťazca, nie hotovosť."
    )
    st.markdown(_DSD_TABLE_STYLE, unsafe_allow_html=True)
    edit_df = res.copy()
    edit_df.insert(0, "Uložiť", False)
    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in edit_df.columns if c != "Uložiť"],
        column_config=_spread_table_column_config(edit_df),
        key="dsd_results_editor",
    )
    if st.button("Uložiť označené riadky do lokálnej DB", type="secondary", key="dsd_save_rows"):
        picked = edited.loc[edited["Uložiť"] == True].drop(columns=["Uložiť"], errors="ignore")
        if picked.empty:
            st.warning("Nie je označený žiadny riadok (stĺpec **Uložiť**).")
        else:
            n = sdiag.save_rows(
                meta.get("ticker", ticker),
                meta.get("as_of", as_of),
                meta.get("strategy", strategy),
                picked,
            )
            st.success(f"Uložených **{n}** riadkov do `{sdiag.db_path()}`.")
            st.rerun()

st.divider()
st.markdown("##### Uložené diagonály")
st.caption(f"Súbor: `{sdiag.db_path()}` — mimo `journal.db`.")
saved_df = sdiag.list_saved()
if saved_df.empty:
    st.info("Zatiaľ nemáš uložené žiadne riadky z tejto stránky.")
else:
    st.markdown(_DSD_TABLE_STYLE, unsafe_allow_html=True)
    del_df = saved_df.copy()
    del_df.insert(0, "Zmazať", False)
    edited_saved = st.data_editor(
        del_df,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in del_df.columns if c != "Zmazať"],
        column_config=_spread_table_column_config(del_df),
        key="dsd_saved_editor",
    )
    if st.button("Odstrániť označené z DB", key="dsd_delete_saved"):
        ids = edited_saved.loc[edited_saved["Zmazať"] == True, "ID"].dropna().astype(int).tolist()
        if not ids:
            st.warning("Označ aspoň jeden riadok v stĺpci **Zmazať**.")
        else:
            k = sdiag.delete_by_ids(ids)
            st.success(f"Odstránených záznamov: **{k}**.")
            st.rerun()

st.divider()
st.markdown("##### Poznámky")
st.markdown(
    "- **Čistá theta** = vážený súčet z reťazca (Barchart). **Kladné číslo** = theta v tvoj prospech (zjednodušene *denný príjem* z decay), "
    "**záporné** = proti tebe (*denná strata*). Nie je to účtovný PnL ani hotovosť.\n"
    "- Diagonál vyžaduje **dve expirácie**; ak máš len jednu, importuj ďalší reťazec.\n"
    "- **Short bid** a **Long ask** môžu byť prázdne, ak v DB chýba strana **options** CSV — vtedy je prázdny aj **Debit/kredit** (za lot).\n"
    "- **Uloženie:** označ stĺpec **Uložiť** pri riadkoch výsledkov a klikni na uloženie; v sekcii **Uložené diagonály** môžeš záznamy zmazať stĺpcom **Zmazať**.\n"
    "- **DTE** = dni do expirácie od zvolenej snímky (as-of).\n"
    "- **Obmedzenie strike:** započítavajú sa len kontrakty, kde **obidve** nohy (short aj long) majú strike v zadanom intervale."
)
