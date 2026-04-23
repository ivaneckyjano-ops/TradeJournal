"""
Journal — otvorené pozície z denníka: Gréky, IV, Vega v čase, skupiny a net súčty.
Pri pripojenom IBKR: záložka **TWS (živé OPT)** = rovnaký zdroj ako Dashboard; zápis journalu môže
zobrazovať stĺpce **TWS** (model BS z cien IB, rovnaká mierka ako stĺpce Δ/Θ/Vega v denníku).
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page
from core.portfolio_data import ib_opt_greeks_scaled_for_journal, journal_position_key

db.init_db()
set_tradejournal_page("portfolio")

_SHORT_DELTA_WARN_RATIO = 1.5
_SHORT_DELTA_ALERT_RATIO = 2.0

st.title("Journal — Gréky a skupiny")
st.caption(
    "**TWS (živé):** opčné pozície z IB portfólia (OPT) — rovnaký kľúč ako pri kontrole na Dashboarde. "
    "**Journal:** len obchody so stavom *Open*; pri pripojenom IB a OPT v TWS **iba nohy, ktoré sú aj v TWS**. "
    "Zápis **Δ, Θ, Vega, IV** (vstup / aktuál), skupiny, net a história — stĺpce „TWS …“ sú **odhad z BS** z cien IB."
)

_JOURNAL_GUIDE_PATH = Path(__file__).resolve().parents[1] / "docs" / "journal-greky.md"
try:
    _journal_guide_md = _JOURNAL_GUIDE_PATH.read_text(encoding="utf-8")
except OSError:
    _journal_guide_md = ""

with st.expander("Návod na použitie", expanded=False):
    if _journal_guide_md.strip():
        st.markdown(_journal_guide_md)
    else:
        st.warning(
            f"V projekte chýba súbor s návodom: `{_JOURNAL_GUIDE_PATH}`. "
            "Skopíruj z repozitára **docs/journal-greky.md** alebo ho obnov z gitu."
        )


def _dte(expiry_str: str) -> int | None:
    if not expiry_str:
        return None
    try:
        exp = date.fromisoformat(
            f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:8]}"
            if len(expiry_str) == 8
            else expiry_str
        )
        return max(0, (exp - date.today()).days)
    except Exception:
        return None


def _notional_per_leg(t: dict) -> float:
    try:
        c = float(t.get("contracts") or 1)
        e = float(t.get("entry_price") or 0)
        return abs(e) * c * 100.0
    except (TypeError, ValueError):
        return 0.0


def _nan_to_none(v) -> float | None:
    if isinstance(v, (list, tuple)) and len(v) == 1:
        v = v[0]
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (ValueError, TypeError):
        pass
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


def _entry_float_eq(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def _short_delta_abs_ratio(leg_type: str, entry_d: float | None, curr_d: float | None) -> float | None:
    if str(leg_type or "").strip() != "Short":
        return None
    if entry_d is None or curr_d is None:
        return None
    ae = abs(float(entry_d))
    if ae < 1e-12:
        return None
    return abs(float(curr_d)) / ae


def _sum_legs(legs: list[dict], key: str) -> float | None:
    xs = []
    for t in legs:
        v = t.get(key)
        if v is None:
            continue
        try:
            xs.append(float(v))
        except (TypeError, ValueError):
            continue
    return sum(xs) if xs else None


def _avg_legs(legs: list[dict], key: str) -> float | None:
    xs = []
    for t in legs:
        v = t.get(key)
        if v is None:
            continue
        try:
            xs.append(float(v))
        except (TypeError, ValueError):
            continue
    return sum(xs) / len(xs) if xs else None


def _diff_msg(cur: float | None, ent: float | None) -> str:
    if cur is None or ent is None:
        return "—"
    d = cur - ent
    return f"{d:+.4f}" if abs(d) < 50 else f"{d:+.2f}"


def _leg_key(t: dict) -> tuple:
    return journal_position_key(
        t.get("ticker"),
        t.get("strike") or 0,
        t.get("expiry") or "",
        t.get("option_type") or "",
        t.get("leg_type") or "",
    )


with st.expander("Filter", expanded=False):
    _sym_raw = db.get_symbol_tickers()
    _sym_sorted = sorted({str(t).strip().upper() for t in _sym_raw if str(t).strip()})
    _sym_opts = ["— všetky —"] + _sym_sorted
    _sel = st.selectbox(
        "Ticker (zo záložky Symboly)",
        options=_sym_opts,
        index=0,
        key="pf_journal_symbol_filter",
        help="Zoznam berie z tabuľky Symboly.",
    )
    ticker_filter = "" if _sel == "— všetky —" else _sel
    if not _sym_sorted:
        st.info("V **Symboly** zatiaľ nemáš žiadny ticker.")

open_trades_raw = [
    t
    for t in db.get_open_trades()
    if str(t.get("status") or "Open").strip().lower() == "open"
]
if ticker_filter:
    open_trades_raw = [t for t in open_trades_raw if str(t.get("ticker") or "").upper() == ticker_filter]

groups_meta = {g["name"]: g for g in db.get_groups()}

_ib_connected = ibkr.is_connected()
live_pkg: dict | None = None
tws_err: str | None = None
tws_opts: list = []
if _ib_connected:
    live_pkg = ibkr.fetch_positions(with_greeks=True, use_historical_last=False)
    tws_err = live_pkg.get("error")
    if not tws_err:
        tws_opts = [p for p in live_pkg["positions"] if p.get("sec_type") == "OPT"]

tws_by_key: dict = {}
for p in tws_opts:
    k = journal_position_key(
        p.get("ticker"),
        p.get("strike") or 0,
        p.get("expiry") or "",
        p.get("option_type") or "",
        p.get("leg_type") or "",
    )
    tws_by_key[k] = p

tws_cols_active = _ib_connected and bool(tws_opts)

if tws_cols_active:
    open_trades = [t for t in open_trades_raw if _leg_key(t) in tws_by_key]
else:
    open_trades = list(open_trades_raw)

by_group: dict[str, list[dict]] = defaultdict(list)
for t in open_trades:
    gid = (t.get("group_id") or "").strip()
    label = gid if gid else "— bez skupiny"
    by_group[label].append(t)

_sort_keys = sorted(by_group.keys(), key=lambda x: (x == "— bez skupiny", x.lower()))

_extra_gids = {str(t.get("group_id") or "").strip() for t in open_trades if str(t.get("group_id") or "").strip()}
_grp_opts = ["— bez skupiny"] + sorted(set(groups_meta.keys()) | _extra_gids)

n_legs = len(open_trades)
n_groups = len(by_group)
st.divider()
m1, m2, m3 = st.columns(3)
m1.metric("Otvorené nohy", str(n_legs))
m2.metric("Skupín (v zobrazení)", str(n_groups))
if not tws_cols_active:
    st.caption(
        "**Bez živého TWS** (nepripojený IB alebo v účte nie sú OPT): aplikácia **neporovnáva** denník s brokerom — "
        "nezniká zoznam „nepárovaných“ pozícií. Zobrazujú sa **všetky** nohy so stavom *Open* z databázy; stĺpce „TWS …“ sa neukážu."
    )
if tws_cols_active and not open_trades and open_trades_raw and tws_opts:
    st.info(
        "Máš **otvorené** nohy v denníku, ale **žiadna** nezodpovedá OPT v TWS — na tejto stránke sa nezobrazujú. "
        "Skontroluj expiráciu (formát), strike alebo synchronizáciu z Dashboardu."
    )
if open_trades:
    notionals = [_notional_per_leg(t) for t in open_trades]
    m3.metric(
        "Σ |vstupná prémia| × 100",
        f"${sum(notionals):,.0f}",
        help="Súčet |prémia| × kontrakty × 100 z denníka.",
    )
else:
    m3.metric("Σ |vstupná prémia| × 100", "—")

with st.expander("Sektory — insight (Barchart OCR)", expanded=False):
    try:
        from core import sector_insights_engine as _sie
        from core import sector_performance_ocr as _spo

        _sh = db.get_latest_sector_performance_snapshot("short")
        if not _sh:
            st.caption(
                "Zatiaľ nemáš uložený krátkodobý snímok. Stránka **Sektory — insight** v sekcii Analýza."
            )
        else:
            _short_df = _spo.payload_rows_to_dataframe(_sh["payload"])
            _lo = db.get_latest_sector_performance_snapshot("long")
            _long_df = _spo.payload_rows_to_dataframe(_lo["payload"]) if _lo else None

            def _sec_tf(tk: str) -> str | None:
                r = db.get_symbol(tk)
                return (str(r["sector"]).strip() if r and r.get("sector") else None)

            _w = _sie.portfolio_sector_weights(open_trades, _sec_tf)
            _rep = _sie.build_insight_report(_short_df, _long_df, _w)
            st.caption(_rep.get("similarity_note", ""))
            for _x in _rep.get("warnings", [])[:3]:
                st.warning(_x)
            for _x in _rep.get("diversifiers", [])[:2]:
                st.info(_x)
            st.page_link("pages/sector_insights.py", label="Otvoriť Sektory — insight", icon=":material/hub:")
    except Exception as _e:
        st.caption(f"Sektorový prehľad: {_e}")

st.subheader("Otvorené pozície")

if not open_trades and not tws_opts:
    st.info(
        "Nemáš žiadne **otvorené** obchody v denníku ani **opčné (OPT)** pozície v TWS. "
        "Trade Log / import z Dashboardu (IBKR)."
    )
    if not _ib_connected:
        st.page_link("pages/dashboard.py", label="Otvoriť Dashboard (IBKR)", icon=":material/dashboard:")
    st.stop()

tab_tws, tab_legs, tab_net, tab_hist = st.tabs(
    ["TWS (živé OPT)", "Zápis journal", "Net podľa skupiny", "Časový vývoj (graf)"]
)

with tab_tws:
    if not _ib_connected:
        st.info("Pre živý výpis sa **pripoj na IBKR** (panel na Dashboarde).")
        st.page_link("pages/dashboard.py", label="Dashboard — IBKR", icon=":material/dashboard:")
    elif tws_err:
        st.error(str(tws_err))
    elif not tws_opts:
        st.caption("V portfóliu z IB momentálne nie sú žiadne **OPT** pozície.")
    else:
        spot_ref = None
        if live_pkg:
            for rp in live_pkg["positions"]:
                if rp.get("sec_type") == "STK":
                    mp = rp.get("market_price")
                    if mp is not None and not (isinstance(mp, float) and math.isnan(mp)) and float(mp) > 0:
                        spot_ref = float(mp)
                        break
        if spot_ref:
            st.caption(
                "Rovnaké polia ako z ``ib.portfolio()`` + **Gréky** dopočítané v aplikácii (BS, podkladové **spot** z prvého STK v portfóliu). "
                f"Referenčný spot: **{spot_ref:.2f}**."
            )
        else:
            st.caption(
                "Rovnaké polia ako z ``ib.portfolio()`` + **Gréky** dopočítané v aplikácii (BS) **len ak** je v portfóliu STK s kladnou trhovou cenou. "
                "Bez toho ostanú stĺpce Δ/Θ/Vega/IV prázdne."
            )
        tw_rows = []
        for p in sorted(
            tws_opts,
            key=lambda x: (
                str(x.get("ticker") or ""),
                str(x.get("expiry") or ""),
                float(x.get("strike") or 0),
                str(x.get("option_type") or ""),
                str(x.get("leg_type") or ""),
            ),
        ):
            g = ib_opt_greeks_scaled_for_journal(p)
            exp = str(p.get("expiry") or "")
            exp_disp = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}" if len(exp) >= 8 and "-" not in exp else exp
            tw_rows.append(
                {
                    "Ticker": p.get("ticker") or "",
                    "Expirácia": exp_disp,
                    "Strike": float(p.get("strike") or 0),
                    "Typ": p.get("option_type") or "",
                    "Noha": p.get("leg_type") or "",
                    "Kontr.": int(float(p.get("contracts") or 1)),
                    "Trh. cena": p.get("market_price"),
                    "Mkt hodnota": p.get("market_value"),
                    "U P&L": p.get("unrealized_pnl"),
                    "Zdroj ceny": p.get("price_source") or "",
                    "TWS Δ (odhad)": g["delta"],
                    "TWS Θ $/deň": g["theta_usd"],
                    "TWS Vega": g["vega"],
                    "TWS IV": g["iv"],
                }
            )
        df_tw = pd.DataFrame(tw_rows)
        for c in ("TWS Δ (odhad)", "TWS Θ $/deň", "TWS Vega", "TWS IV", "Trh. cena", "Mkt hodnota", "U P&L"):
            if c in df_tw.columns:
                df_tw[c] = df_tw[c].astype("Float64")
        st.dataframe(
            df_tw,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Strike": st.column_config.NumberColumn(format="$%.2f"),
                "Trh. cena": st.column_config.NumberColumn(format="$%.4f"),
                "Mkt hodnota": st.column_config.NumberColumn(format="$%.2f"),
                "U P&L": st.column_config.NumberColumn(format="$%.2f"),
                "TWS Δ (odhad)": st.column_config.NumberColumn(format="%.4f"),
                "TWS Θ $/deň": st.column_config.NumberColumn(format="$%.3f"),
                "TWS Vega": st.column_config.NumberColumn(format="%.2f"),
                "TWS IV": st.column_config.NumberColumn(format="%.4f"),
            },
        )

with tab_legs:
    if not open_trades:
        if tws_cols_active:
            st.info(
                "Žiadna **otvorená** noha so stavom *Open* v denníku nezodpovedá OPT v TWS, alebo ešte nemáš otvorené záznamy. "
                "Skontroluj zhodu (expirácia, strike) alebo Trade Log."
            )
        else:
            st.info(
                "V denníku nemáš **otvorené** obchody (stav *Open*) — zápis journalu je po importe z IB alebo v Trade Log. "
                "Po pripojení TWS sa tu zobrazia len nohy, ktoré sú aj v brokerovi."
            )
    else:
        if tws_cols_active:
            st.caption(
                "Zobrazujú sa len nohy z denníka, ktoré majú **rovnakú OPT** v TWS (kľúč ako na Dashboarde). "
                "Stĺpce **TWS …** a **TWS kontr.** sú len na čítanie (BS z cien IB)."
            )
        for gname in _sort_keys:
            legs = by_group[gname]
            meta = groups_meta.get(gname) if gname != "— bez skupiny" else None
            _gkey = hashlib.sha256(gname.encode("utf-8")).hexdigest()[:16]
            legs_edit = sorted(legs, key=lambda x: (str(x.get("ticker") or ""), int(x.get("id") or 0)))
            if not legs_edit:
                continue

            with st.container():
                st.markdown(f"#### {gname}")
                if meta:
                    _tk = meta.get("ticker") or ""
                    _st = meta.get("strategy") or ""
                    if _tk or _st:
                        st.caption(f"Skupina v DB: **{_tk}** · {_st}")
                rows = []
                orig_by_id: dict[int, dict] = {}
                for t in legs_edit:
                    tid = int(t["id"])
                    orig_by_id[tid] = t
                    exp = t.get("expiry") or ""
                    dte_v = _dte(str(exp))
                    iv_e = t.get("iv_at_entry")
                    iv_c = t.get("iv_current")
                    dlt_e = t.get("delta_at_entry")
                    th_e = t.get("theta_at_entry")
                    th_c = t.get("theta_current")
                    dlt_c = t.get("delta_current")
                    v_e = t.get("vega_at_entry")
                    v_c = t.get("vega_current")
                    gid_disp = (t.get("group_id") or "").strip() or "— bez skupiny"
                    r = {
                        "ID": tid,
                        "Skupina": gid_disp if gid_disp in _grp_opts else "— bez skupiny",
                        "Stratégia": t.get("strategy") or "",
                        "Ticker": t.get("ticker") or "",
                        "Noha": t.get("leg_type") or "",
                        "Typ": t.get("option_type") or "",
                        "Strike": float(t.get("strike") or 0),
                        "Expirácia": exp,
                        "DTE": int(dte_v) if dte_v is not None else None,
                        "Kontr.": int(t.get("contracts") or 1),
                        "Entry $": float(t.get("entry_price") or 0),
                        "Entry dátum": t.get("entry_date") or "",
                        "Θ vstup ($/deň)": pd.NA if th_e is None else float(th_e),
                        "Θ aktuálna ($/deň)": pd.NA if th_c is None else float(th_c),
                        "Δ vstup": pd.NA if dlt_e is None else float(dlt_e),
                        "Δ aktuálna": pd.NA if dlt_c is None else float(dlt_c),
                        "Vega vstup": pd.NA if v_e is None else float(v_e),
                        "Vega aktuálna": pd.NA if v_c is None else float(v_c),
                        "IV vstup": pd.NA if iv_e is None else float(iv_e),
                        "IV aktuálna": pd.NA if iv_c is None else float(iv_c),
                    }
                    if tws_cols_active:
                        twp = tws_by_key.get(_leg_key(t))
                        if twp:
                            gtw = ib_opt_greeks_scaled_for_journal(twp)
                            r["TWS kontr."] = int(float(twp.get("contracts") or 1))
                            r["TWS Δ"] = pd.NA if gtw["delta"] is None else float(gtw["delta"])
                            r["TWS Θ $/deň"] = pd.NA if gtw["theta_usd"] is None else float(gtw["theta_usd"])
                            r["TWS Vega"] = pd.NA if gtw["vega"] is None else float(gtw["vega"])
                            r["TWS IV"] = pd.NA if gtw["iv"] is None else float(gtw["iv"])
                        else:
                            r["TWS kontr."] = pd.NA
                            r["TWS Δ"] = pd.NA
                            r["TWS Θ $/deň"] = pd.NA
                            r["TWS Vega"] = pd.NA
                            r["TWS IV"] = pd.NA
                    rows.append(r)
                df = pd.DataFrame(rows)
                _float_cols = [
                    "Θ vstup ($/deň)",
                    "Θ aktuálna ($/deň)",
                    "Δ vstup",
                    "Δ aktuálna",
                    "Vega vstup",
                    "Vega aktuálna",
                    "IV vstup",
                    "IV aktuálna",
                ]
                if tws_cols_active:
                    _float_cols.extend(["TWS Δ", "TWS Θ $/deň", "TWS Vega", "TWS IV"])
                for _c in _float_cols:
                    if _c in df.columns:
                        df[_c] = df[_c].astype("Float64")
                if tws_cols_active and "TWS kontr." in df.columns:
                    df["TWS kontr."] = df["TWS kontr."].astype("Int64")
                st.caption(
                    "**IV** ako zlomok (0,35 = 35 %). **Θ** = USD/deň za celú nohu. **Vega** = za pozíciu (× kontrakty × 100, znamienko podľa nohy). "
                    "Po **Uložiť journal** sa z aktuálnych hodnôt uloží aj **bod do histórie** (graf v záložke Časový vývoj)."
                )
                _disabled = [
                    "ID",
                    "Stratégia",
                    "Ticker",
                    "Noha",
                    "Typ",
                    "Strike",
                    "Expirácia",
                    "DTE",
                    "Kontr.",
                    "Entry $",
                    "Entry dátum",
                ]
                if tws_cols_active:
                    _disabled.extend(["TWS kontr.", "TWS Δ", "TWS Θ $/deň", "TWS Vega", "TWS IV"])
                _col_cfg = {
                    "Skupina": st.column_config.SelectboxColumn(
                        "Skupina",
                        options=_grp_opts,
                        required=True,
                        help="Rovnaké mená ako v záložke **Skupiny** / Trade Log.",
                    ),
                    "Strike": st.column_config.NumberColumn(format="$%.2f"),
                    "Entry $": st.column_config.NumberColumn(format="$%.2f"),
                    "DTE": st.column_config.NumberColumn(format="%d dní"),
                    "Θ vstup ($/deň)": st.column_config.NumberColumn(format="$%.3f", step=0.001),
                    "Θ aktuálna ($/deň)": st.column_config.NumberColumn(
                        format="$%.3f", step=0.001, help="Aktuálna theta pozície ($/deň)."
                    ),
                    "Δ vstup": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                    "Δ aktuálna": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                    "Vega vstup": st.column_config.NumberColumn(format="%.2f", step=0.01),
                    "Vega aktuálna": st.column_config.NumberColumn(format="%.2f", step=0.01),
                    "IV vstup": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                    "IV aktuálna": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                }
                if tws_cols_active:
                    _col_cfg["TWS kontr."] = st.column_config.NumberColumn(format="%d", help="Počet kontraktov z IB.")
                    _col_cfg["TWS Δ"] = st.column_config.NumberColumn(format="%.4f")
                    _col_cfg["TWS Θ $/deň"] = st.column_config.NumberColumn(format="$%.3f")
                    _col_cfg["TWS Vega"] = st.column_config.NumberColumn(format="%.2f")
                    _col_cfg["TWS IV"] = st.column_config.NumberColumn(format="%.4f")
                edited = st.data_editor(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    disabled=_disabled,
                    column_config=_col_cfg,
                    key=f"pf_ed_{_gkey}",
                )
                _watch_rows: list[dict] = []
                for _, row in edited.iterrows():
                    lt = str(row.get("Noha") or "")
                    de = _nan_to_none(row["Δ vstup"])
                    dc = _nan_to_none(row["Δ aktuálna"])
                    ratio = _short_delta_abs_ratio(lt, de, dc)
                    if lt != "Short":
                        p_str, st_lbl = "—", "—"
                    elif ratio is None:
                        p_str, st_lbl = "—", "doplň Δ vstup + Δ aktuálnu"
                    elif ratio >= _SHORT_DELTA_ALERT_RATIO:
                        p_str, st_lbl = f"{ratio:.2f}×", "⛔ |Δ| ≥ 2× oproti vstupu"
                    elif ratio >= _SHORT_DELTA_WARN_RATIO:
                        p_str, st_lbl = f"{ratio:.2f}×", "⚠ blíži sa k 2×"
                    else:
                        p_str, st_lbl = f"{ratio:.2f}×", "OK"
                    _watch_rows.append(
                        {
                            "ID": int(row["ID"]),
                            "Ticker": row.get("Ticker") or "",
                            "Noha": lt,
                            "|Δ aktuál| / |Δ vstup|": p_str,
                            "Stav": st_lbl,
                        }
                    )
                if any(str(r.get("Noha")) == "Short" for r in _watch_rows):
                    st.markdown("##### Sledovanie delty (shortové nohy)")
                    st.caption(
                        f"Pomer |Δ aktuálna| ÷ |Δ vstup|. Varovanie od **{_SHORT_DELTA_WARN_RATIO}×**, "
                        f"silné od **{_SHORT_DELTA_ALERT_RATIO}×**."
                    )
                    st.dataframe(pd.DataFrame(_watch_rows), use_container_width=True, hide_index=True)

                if st.button("Uložiť journal (Gréky, IV, Vega, skupina)", key=f"pf_sv_{_gkey}", type="primary"):
                    nchg = 0
                    nsnap = 0
                    for _, row in edited.iterrows():
                        tid = int(row["ID"])
                        orig = orig_by_id.get(tid, {})
                        sk = row.get("Skupina")
                        new_gid = None if sk in (None, "", "— bez skupiny") else str(sk).strip()
                        old_gid = (orig.get("group_id") or "").strip() or None
                        if (new_gid or "") != (old_gid or ""):
                            db.update_trade(tid, group_id="" if not new_gid else new_gid)
                            nchg += 1

                        new_iv = _nan_to_none(row["IV vstup"])
                        new_d = _nan_to_none(row["Δ vstup"])
                        new_th = _nan_to_none(row["Θ vstup ($/deň)"])
                        new_dc = _nan_to_none(row["Δ aktuálna"])
                        new_tc = _nan_to_none(row["Θ aktuálna ($/deň)"])
                        new_ve = _nan_to_none(row["Vega vstup"])
                        new_vc = _nan_to_none(row["Vega aktuálna"])
                        new_ivc = _nan_to_none(row["IV aktuálna"])

                        greek_changed = (
                            not _entry_float_eq(orig.get("iv_at_entry"), new_iv)
                            or not _entry_float_eq(orig.get("delta_at_entry"), new_d)
                            or not _entry_float_eq(orig.get("theta_at_entry"), new_th)
                            or not _entry_float_eq(orig.get("delta_current"), new_dc)
                            or not _entry_float_eq(orig.get("vega_at_entry"), new_ve)
                            or not _entry_float_eq(orig.get("vega_current"), new_vc)
                            or not _entry_float_eq(orig.get("iv_current"), new_ivc)
                            or not _entry_float_eq(orig.get("theta_current"), new_tc)
                        )
                        if greek_changed:
                            db.set_trade_portfolio_greeks(
                                tid,
                                new_iv,
                                new_d,
                                new_th,
                                new_dc,
                                vega_at_entry=new_ve,
                                vega_current=new_vc,
                                iv_current=new_ivc,
                                theta_current=new_tc,
                            )
                            nchg += 1
                        if greek_changed and any(
                            x is not None for x in (new_dc, new_tc, new_vc, new_ivc)
                        ):
                            db.insert_trade_greek_snapshot(
                                tid,
                                delta=new_dc,
                                theta_usd=new_tc,
                                vega=new_vc,
                                iv=new_ivc,
                            )
                            nsnap += 1
                    if nchg or nsnap:
                        st.success(f"Uložené — zmenených záznamov: **{nchg}**, nových bodov histórie: **{nsnap}**.")
                        st.rerun()
                    else:
                        st.info("Žiadna zmena.")
            st.divider()

with tab_net:
    st.caption("Súčty **po nohách** v skupine (jednoduchý súčet hodnôt v denníku — rovnaká konvencia ako pri zápise z TWS).")
    net_rows = []
    for gname in _sort_keys:
        legs = by_group[gname]
        s_de = _sum_legs(legs, "delta_at_entry")
        s_dc = _sum_legs(legs, "delta_current")
        s_the = _sum_legs(legs, "theta_at_entry")
        s_thc = _sum_legs(legs, "theta_current")
        s_vee = _sum_legs(legs, "vega_at_entry")
        s_vec = _sum_legs(legs, "vega_current")
        a_ive = _avg_legs(legs, "iv_at_entry")
        a_ivc = _avg_legs(legs, "iv_current")
        net_rows.append(
            {
                "Skupina": gname,
                "Σ Δ vstup": s_de,
                "Σ Δ aktuál": s_dc,
                "Δ zmena": _diff_msg(s_dc, s_de),
                "Σ Θ vstup $/deň": s_the,
                "Σ Θ aktuál $/deň": s_thc,
                "Θ zmena $/deň": _diff_msg(s_thc, s_the),
                "Σ Vega vstup": s_vee,
                "Σ Vega aktuál": s_vec,
                "Vega zmena": _diff_msg(s_vec, s_vee),
                "Priemer IV vstup": a_ive,
                "Priemer IV aktuál": a_ivc,
                "IV zmena (priemer)": _diff_msg(a_ivc, a_ive) if a_ive is not None and a_ivc is not None else "—",
            }
        )
    df_net = pd.DataFrame(net_rows)
    st.dataframe(
        df_net,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Σ Δ vstup": st.column_config.NumberColumn(format="%.4f"),
            "Σ Δ aktuál": st.column_config.NumberColumn(format="%.4f"),
            "Σ Θ vstup $/deň": st.column_config.NumberColumn(format="$%.2f"),
            "Σ Θ aktuál $/deň": st.column_config.NumberColumn(format="$%.2f"),
            "Σ Vega vstup": st.column_config.NumberColumn(format="%.2f"),
            "Σ Vega aktuál": st.column_config.NumberColumn(format="%.2f"),
            "Priemer IV vstup": st.column_config.NumberColumn(format="%.4f"),
            "Priemer IV aktuál": st.column_config.NumberColumn(format="%.4f"),
        },
    )

with tab_hist:
    st.caption("Body pridávaš uložením journalu v záložke **Zápis journal** (aspoň jedna z **aktuálnych** hodnôt Δ, Θ, Vega, IV).")
    opts = {
        f"#{t['id']} {t.get('ticker','')} {t.get('option_type','')} {t.get('strike','')}": int(t["id"])
        for t in sorted(open_trades, key=lambda x: int(x.get("id") or 0))
    }
    if not opts:
        st.info("Žiadne pozície.")
    else:
        pick = st.selectbox("Noha", options=list(opts.keys()), key="pf_hist_pick")
        tid = opts[pick]
        snaps = db.list_trade_greek_snapshots(tid)
        if not snaps:
            st.info("Pre túto nohu zatiaľ nie sú žiadne uložené snímky — ulož journal s vyplnenými aktuálnymi hodnotami.")
        else:
            dfp = pd.DataFrame(snaps)
            dfp["recorded_at"] = pd.to_datetime(dfp["recorded_at"], utc=True, errors="coerce")
            fig = make_subplots(
                rows=4,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.06,
                subplot_titles=("Δ aktuálna", "Θ $/deň", "Vega", "IV"),
            )
            fig.add_trace(go.Scatter(x=dfp["recorded_at"], y=dfp["delta"], name="Δ", mode="lines+markers"), row=1, col=1)
            fig.add_trace(
                go.Scatter(x=dfp["recorded_at"], y=dfp["theta_usd"], name="Θ", mode="lines+markers"), row=2, col=1
            )
            fig.add_trace(go.Scatter(x=dfp["recorded_at"], y=dfp["vega"], name="Vega", mode="lines+markers"), row=3, col=1)
            fig.add_trace(go.Scatter(x=dfp["recorded_at"], y=dfp["iv"], name="IV", mode="lines+markers"), row=4, col=1)
            fig.update_layout(height=780, showlegend=False, margin=dict(l=8, r=8, t=40, b=8))
            fig.update_yaxes(title_text="Δ", row=1, col=1)
            fig.update_yaxes(title_text="Θ $", row=2, col=1)
            fig.update_yaxes(title_text="Vega", row=3, col=1)
            fig.update_yaxes(title_text="IV", row=4, col=1)
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(dfp, use_container_width=True, hide_index=True)
