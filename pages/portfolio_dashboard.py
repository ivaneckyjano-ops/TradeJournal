"""
TWS Portfolio Dashboard – live stav portfólia z Interactive Brokers.

Zobrazuje (poradie na stránke):
  1. Kontrola dát (zdroje cien, rozpad marketValue z API)
  2. Voliteľné ručné úpravy: P/L, Available Funds, Net Theta, Net Vega, JSON podľa podkladu (uložené v DB)
  3. Celkové výsledky: P/L, podľa podkladu, Margin, Greeks (vč. ručnej Theta/Vega z kroku 2), APTR (Θ) a grafy
  4. Detail podľa skupín: marža, Theta z TWS, na konci tlačidlo **Uložiť snímky trendových grafov** (história grafov, nie pri Načítať z TWS)
"""
import json
import time
from datetime import datetime, timezone
from typing import Optional

import streamlit as st
import pandas as pd

from core import ibkr
from core import database as db
from core.page_context import TWS_DASHBOARD_PAGE, set_tradejournal_page
from core.portfolio_data import (
    PORTFOLIO_FINANCE_OVERRIDES_KEY,
    calc_dte,
    compute_portfolio_theta_aptr,
    compute_theta_annualized_yield_pct,
    dashboard_group_margin_widget_key,
    group_ibkr_positions_for_dashboard,
    ibkr_aggregates_by_underlying,
    journal_group_id,
    merge_ibkr_by_underlying_overrides,
    normalize_expiry,
    parse_portfolio_finance_overrides,
)
from core.steady_yields.engine import traffic_light

db.init_db()
set_tradejournal_page(TWS_DASHBOARD_PAGE)


def _plotly_line_trend(series: pd.Series, *, chart_key: str, height: int = 200) -> None:
    """
    Čiarový trend namiesto ``st.line_chart`` — Vega-Lite graf vo Streamlite často spúšťa
    React ``removeChild`` chyby pri veľkom DOM (expandy, metriky, spinner).
    Plotly + stabilný ``key`` je v praxi spoľahlivejší.
    """
    import plotly.graph_objects as go

    s = series.dropna()
    if len(s) < 2:
        return
    x_axis = s.index
    if hasattr(x_axis, "tz") and getattr(x_axis, "tz", None) is not None:
        try:
            x_axis = x_axis.tz_convert("UTC").tz_localize(None)
        except (TypeError, ValueError):
            x_axis = s.index
    fig = go.Figure(
        data=[go.Scatter(x=x_axis, y=s.values, mode="lines", connectgaps=True)]
    )
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=36),
        showlegend=False,
        xaxis=dict(showgrid=True, title=None),
        yaxis=dict(showgrid=True, title=None),
    )
    st.plotly_chart(fig, use_container_width=True, key=chart_key)


# ─── Fetch job je uložený v ibkr module (perzistentný medzi page reruns) ──────
_JOB = ibkr.DASHBOARD_FETCH_JOB

# Staršie verzie nastavovali "running" + thread; pri obnovení stránky to zostalo visieť.
if _JOB.get("status") == "running":
    _JOB["status"] = "idle"


def _run_dashboard_fetch() -> None:
    """
    Synchronné načítanie v hlavnom vlákne Streamlitu (pod st.spinner).
    Vyhýba sa vláknu + periodickému rerunu / dvojitému st_autorefresh, ktoré
    na fronte spôsobovali NotFoundError removeChild.
    """
    _JOB["error"] = None
    try:
        pos_res = ibkr.fetch_positions(with_greeks=True, use_historical_last=False)
        if pos_res.get("error"):
            _JOB["error"] = pos_res["error"]
            _JOB["status"] = "error"
            return
        _JOB["positions"] = pos_res.get("positions", [])

        ib = ibkr.get_ib()
        acct = ibkr._parse_account_values(ib.accountValues()) if ib else {}
        if not acct and ib and ib.isConnected():
            deadline = time.time() + 6.0
            while time.time() < deadline:
                acct = ibkr._parse_account_values(ib.accountValues())
                if acct:
                    break
                time.sleep(0.5)

        _JOB["account"] = acct or {}

        if ibkr.is_connected():
            try:
                ord_res = ibkr.fetch_open_orders(use_cache=False)
                if ord_res.get("error"):
                    _JOB["orders"] = []
                    _JOB["orders_err"] = ord_res["error"]
                else:
                    _JOB["orders"] = ord_res.get("orders", [])
                    _JOB["orders_err"] = None
                    _JOB["orders_raw"] = ord_res.get("total_raw", 0)
            except Exception as e:
                _JOB["orders"] = []
                _JOB["orders_err"] = str(e)
                _JOB["orders_raw"] = 0
        else:
            _JOB["orders"] = []
            _JOB["orders_err"] = None
            _JOB["orders_raw"] = 0

        _JOB["status"] = "done"
    except Exception as exc:
        _JOB["error"] = str(exc)
        _JOB["status"] = "error"


# ─── UI ───────────────────────────────────────────────────────────────────────

st.title("📊 Portfolio Dashboard")
st.caption(
    "**Návod:** Vyžaduje **pripojený IB** (najprv **Dashboard**). Stlač **Načítať z TWS** — načítajú sa pozície, účet a objednávky. "
    "Potom prejdi sekcie: kontrola dát, voliteľné ručné úpravy súčtov, celkové P/L, marža, Gréky, APTR a detail **podľa skupín** z denníka (tlačidlá na snímky grafov)."
)
st.caption("Live stav portfólia z TWS – P/L, Margin, Greeks.")

# ── Stav IBKR pripojenia ──────────────────────────────────────────────────────
if not ibkr.is_connected():
    st.error("IBKR nie je pripojené. Pripoj sa na TWS / IB Gateway cez Dashboard → Pripojenie.")
    st.stop()

# ── Tlačidlo na načítanie / stav jobu ─────────────────────────────────────────
# Jednoduchý vertikálny blok (bez stĺpcov medzi tlačidlom a spinnerom) — stabilnejší DOM.
do_refresh = st.button(
    "🔄 Načítať z TWS",
    type="primary",
    use_container_width=True,
    key="portfolio_dash_tws_refresh",
)

if do_refresh:
    _JOB["positions"] = None
    _JOB["orders"] = None
    _JOB["account"] = None
    _JOB["error"] = None
    _JOB["orders_err"] = None
    _JOB["orders_raw"] = 0
    with st.spinner("Sťahujem pozície, účet a objednávky z TWS (môže trvať 10–30 s)…"):
        _run_dashboard_fetch()
    # Bez st.rerun(): druhý beh skriptu často spôsobil removeChild v Reacte.

job_status = _JOB["status"]

if job_status == "done":
    n_pos = len(_JOB.get("positions") or [])
    n_ord = len(_JOB.get("orders") or [])
    n_raw = _JOB.get("orders_raw", n_ord)
    oe = _JOB.get("orders_err")
    caption = f"✅ Načítané: {n_pos} pozícií · {n_ord} objednávok"
    if oe:
        caption += f"  ⚠ (chyba objednávok: {oe})"
    elif n_raw != n_ord:
        caption += f"  (TWS vrátil {n_raw} celkovo, zobrazených aktívnych: {n_ord})"
    st.caption(caption)
elif job_status == "error":
    st.error(f"Chyba: {_JOB['error']}")
else:
    st.caption("Klikni **Načítať z TWS** pre aktuálne dáta.")

if job_status == "done":
    positions: list = _JOB.get("positions") or []
    account: dict = _JOB.get("account") or {}

    _src_counts: dict[str, int] = {}
    for _p in positions:
        _src = str(_p.get("price_source") or "?")
        _src_counts[_src] = _src_counts.get(_src, 0) + 1
    if _src_counts:
        _src_label = {
            "settlement_close": "Settlement Close (=TWS)",
            "hist_trades": "Last (historické)",
            "hist_midpoint": "Midpoint (historické)",
            "hist_last": "Last (historické)",
            "portfolio_mark": "Mark (portfólio)",
            "last": "Last",
            "mid": "Mid",
            "mark": "Mark",
            "close": "Close",
        }
        _price_source_summary = ", ".join(
            f"{_src_label.get(k, k)}: {v}"
            for k, v in sorted(_src_counts.items(), key=lambda x: (-x[1], x[0]))
        )
    else:
        _price_source_summary = ""

    # Záloha: staršie sedenia mali objednávky None a načítali sa pri ďalšom rendri
    if _JOB.get("orders") is None and ibkr.is_connected():
        try:
            ord_res = ibkr.fetch_open_orders(use_cache=False)
            if ord_res.get("error"):
                _JOB["orders"] = []
                _JOB["orders_err"] = ord_res["error"]
            else:
                _JOB["orders"] = ord_res.get("orders", [])
                _JOB["orders_err"] = None
                _JOB["orders_raw"] = ord_res.get("total_raw", 0)
        except Exception as e:
            st.error(f"Chyba pri načítaní objednávok: {e}")
            _JOB["orders"] = []
            _JOB["orders_err"] = str(e)

    # Prečítaj aj manuálny margin z DB (ako zálohu keď IBKR nevráti dáta)
    _db_margin_raw = db.get_setting("portfolio_margin", "{}")
    try:
        import json
        _db_margin = json.loads(_db_margin_raw)
    except Exception:
        _db_margin = {}

    acct_currency = account.get("_currency", "")
    cur_sym = acct_currency if acct_currency and acct_currency not in ("USD", "BASE") else "$"

    nlv        = account.get("net_liquidation")    or _db_margin.get("nlv", 0)
    avail      = account.get("available_funds")    or _db_margin.get("available_funds", 0)
    buying_pwr = account.get("buying_power")       or _db_margin.get("buying_power", 0)
    maint_mrg  = account.get("maintenance_margin") or 0

    opts   = [p for p in positions if p.get("sec_type") in ("OPT", "FOP")]
    stocks = [p for p in positions if p.get("sec_type") == "STK"]
    futs   = [p for p in positions if p.get("sec_type") == "FUT"]
    other  = [p for p in positions if p.get("sec_type") not in ("OPT", "FOP", "STK", "FUT")]

    unreal_pnl = sum(float(p.get("unrealized_pnl") or 0) for p in positions)
    real_pnl   = sum(float(p.get("realized_pnl")   or 0) for p in positions)

    try:
        _avail_base = float(avail) if avail not in (None, "") else 0.0
    except (TypeError, ValueError):
        _avail_base = 0.0

    _fin_ov = parse_portfolio_finance_overrides(db.get_setting(PORTFOLIO_FINANCE_OVERRIDES_KEY, "{}"))
    _disp_unreal = unreal_pnl
    _disp_real = real_pnl
    _disp_avail = _avail_base
    if _fin_ov["enabled"]:
        if _fin_ov["unrealized_pnl"] is not None:
            _disp_unreal = float(_fin_ov["unrealized_pnl"])
        if _fin_ov["realized_pnl"] is not None:
            _disp_real = float(_fin_ov["realized_pnl"])
        if _fin_ov["available_funds"] is not None:
            _disp_avail = float(_fin_ov["available_funds"])

    def _qty(p):
        return float(p.get("contracts") or 1)

    def _fmt_qty(x) -> float | int:
        q = float(x or 0)
        return int(q) if abs(q - round(q)) < 1e-6 else round(q, 4)

    total_theta = sum(
        float(p.get("theta") or 0) * _qty(p) * 100 *
        (-1 if p.get("leg_type") == "Short" else 1)
        for p in opts if p.get("theta")
    )
    total_vega = sum(
        float(p.get("vega") or 0) * _qty(p) * 100 *
        (-1 if p.get("leg_type") == "Short" else 1)
        for p in opts if p.get("vega")
    )

    _disp_theta = total_theta
    _disp_vega = total_vega
    if _fin_ov["enabled"]:
        if _fin_ov["net_theta_per_day"] is not None:
            _disp_theta = float(_fin_ov["net_theta_per_day"])
        if _fin_ov["net_vega"] is not None:
            _disp_vega = float(_fin_ov["net_vega"])

    _open_tr = db.get_open_trades()
    _all_tr = db.get_all_trades()
    _margins = db.get_group_maint_margins()
    _ordered: list[tuple[str, list]] = []
    _unmatched: list = []
    _pf_theta_aptr: Optional[dict] = None
    if positions:
        _ordered, _unmatched = group_ibkr_positions_for_dashboard(_open_tr, positions)
        if do_refresh:
            for _g0, _ in _ordered:
                _wk0 = dashboard_group_margin_widget_key(_g0)
                st.session_state[_wk0] = float(_margins.get(_g0, 0.0) or 0.0)
        for _g0, _ in _ordered:
            _wk0 = dashboard_group_margin_widget_key(_g0)
            if _wk0 not in st.session_state:
                st.session_state[_wk0] = float(_margins.get(_g0, 0.0) or 0.0)
        _eff_grp_margins = {
            g: float(st.session_state[dashboard_group_margin_widget_key(g)])
            for g, _ in _ordered
        }
        _pf_theta_aptr = compute_portfolio_theta_aptr(
            _ordered, _all_tr, _eff_grp_margins, _unmatched
        )

    _by_sec_rows: list[dict] = []
    _sec_acc: dict[str, list] = {}
    for _p in positions:
        _st = str(_p.get("sec_type") or "?")
        if _st not in _sec_acc:
            _sec_acc[_st] = [0.0, 0]
        _sec_acc[_st][0] += float(_p.get("market_value") or 0)
        _sec_acc[_st][1] += 1
    for _st in sorted(_sec_acc.keys()):
        _v, _n = _sec_acc[_st]
        _by_sec_rows.append({"Typ (secType)": _st, "Súčet trh. hodnoty $": round(_v, 2), "Počet riadkov": _n})

    _sec_types_in = sorted(set(p.get("sec_type", "?") for p in positions))

    st.divider()
    st.caption(
        "**Postup:** **1** kontrola dát z API · **2** voliteľné ručné úpravy · "
        "**3** súhrnné metriky a trendové grafy · **4** detail podľa skupín z denníka."
    )

    # ── 1 · Kontrola dát ─────────────────────────────────────────────────────
    st.subheader("1 · Kontrola dát")
    st.caption(
        "Over, či zdroje cien a súčty z IB zodpovedajú tomu, čo vidíš v TWS — predtým, ako doplníš ručné hodnoty."
    )
    if _price_source_summary:
        st.info(f"Zdroj cien pri poslednom načítaní: {_price_source_summary}")
    elif positions:
        st.caption("Žiadne pole `price_source` pri pozíciách — skontroluj načítanie z TWS.")

    with st.expander("Rozpad súčtu trhovej hodnoty (marketValue) podľa typu nástroja (IB API)", expanded=False):
        st.caption(
            "Ak sa číslo nezhoduje s očakávaním, skontroluj, či TWS neukazuje **NLV** alebo **Equity with Loan Value** — appka tu sčítava **iba** `PortfolioItem.marketValue`."
        )
        if _by_sec_rows:
            st.dataframe(pd.DataFrame(_by_sec_rows), use_container_width=True, hide_index=True)
        else:
            st.caption("Žiadne pozície.")

    st.divider()

    # ── 2 · Ručné úpravy ──────────────────────────────────────────────────────
    st.subheader("2 · Ručné úpravy (voliteľné)")
    st.caption(
        "Po **Uložiť** sa zmeny prejavia v **metrikách P/L a tabuľke „Podľa podkladu“** v kroku **3**. "
        "**Portfólio APTR** ostáva z IB + denníka + marží (krok **4**) — polia P/L a Available Funds ho nemenia. "
        "**Net Theta / Net Vega** v tomto formulári (po zapnutí a uložení) menia **iba dve metriky** v kroku **3**, nie výpočet APTR. "
        "**Marža skupiny** a **Theta z TWS** v kroku **4** ovplyvňujú ročný výnos Θ a **grafy**; snímky grafov uložíš tlačidlom na konci kroku **4**."
    )
    with st.expander("✏️ Ručné súčty P/L a podľa podkladu (verný obraz oproti TWS)", expanded=_fin_ov.get("enabled", False)):
        st.caption(
            "API sa môže líšiť od TWS (filter účtu, zdroj ceny, Mark vs. Unrealized). "
            "Po zapnutí a **Uložiť** sa zobrazia v kroku **3** metriky P/L, Available Funds, **Net Theta**, **Net Vega** a tabuľka podľa podkladu. "
            "**Portfólio APTR** a výnos Θ po skupinách ostávajú na IB + denníku + maržách (krok **4**)."
        )
        _by_sym_default = _fin_ov.get("by_symbol") or {}
        _by_sym_txt = json.dumps(_by_sym_default, indent=2, ensure_ascii=False) if _by_sym_default else (
            '{\n  "AMZN": {\n    "unreal": -157.74,\n    "mkt": 2250.5,\n    "abs_mkt": 3228.24\n  }\n}'
        )
        with st.form("portfolio_finance_overrides_form"):
            _f_en = st.checkbox(
                "Použiť uložené ručné hodnoty namiesto súčtov z API (P/L, Available Funds, Theta, Vega + tabuľka podľa podkladu)",
                value=bool(_fin_ov.get("enabled")),
            )
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                _f_ue = st.number_input(
                    "Unrealized P/L ($) — celkom",
                    value=float(_fin_ov["unrealized_pnl"] if _fin_ov["unrealized_pnl"] is not None else unreal_pnl),
                    step=0.01,
                    format="%.2f",
                    help="Nechaj zodpovedať súčtu z TWS pre tento účet.",
                )
            with fc2:
                _f_re = st.number_input(
                    "Realized P/L session ($) — celkom",
                    value=float(_fin_ov["realized_pnl"] if _fin_ov["realized_pnl"] is not None else real_pnl),
                    step=0.01,
                    format="%.2f",
                )
            with fc3:
                _f_af = st.number_input(
                    "Volný kapitál (Available Funds) — celkom",
                    value=float(
                        _fin_ov["available_funds"]
                        if _fin_ov["available_funds"] is not None
                        else _avail_base
                    ),
                    step=0.01,
                    format="%.2f",
                    help="Hodnota z TWS / Account — Available Funds (nie súčet trhovej hodnoty pozícií).",
                )
            fth1, fth2 = st.columns(2)
            with fth1:
                _f_th = st.number_input(
                    "Net Theta ($/deň) — celé portfólio",
                    value=float(
                        _fin_ov["net_theta_per_day"]
                        if _fin_ov["net_theta_per_day"] is not None
                        else total_theta
                    ),
                    step=0.25,
                    format="%.2f",
                    help="Súčet z TWS (Portfolio → Theta) alebo vlastná korekcia. Zobrazí sa v kroku 3 pri zapnutých ručných hodnotách.",
                )
            with fth2:
                _f_vg = st.number_input(
                    "Net Vega ($ na 1 % IV)",
                    value=float(
                        _fin_ov["net_vega"] if _fin_ov["net_vega"] is not None else total_vega
                    ),
                    step=0.25,
                    format="%.2f",
                    help="Súčet Vegy z TWS alebo korekcia — rovnaká škála ako metrika v kroku 3.",
                )
            _f_json = st.text_area(
                "Úpravy podľa podkladu (JSON, voliteľné)",
                value=_by_sym_txt,
                height=220,
                help='Pre každý ticker: "unreal", "mkt", "abs_mkt" (všetko voliteľné). Kľúč = symbol ako v stĺpci Podklad.',
            )
            _sub_fin = st.form_submit_button("💾 Uložiť ručné súčty do databázy")
        if _sub_fin:
            _parsed_sym: dict = {}
            try:
                _pj = json.loads(_f_json or "{}")
                if isinstance(_pj, dict):
                    _parsed_sym = _pj
            except json.JSONDecodeError as e:
                st.error(f"Neplatný JSON v tabuľke podľa podkladu: {e}")
            else:
                _payload = {
                    "enabled": bool(_f_en),
                    "unrealized_pnl": float(_f_ue),
                    "realized_pnl": float(_f_re),
                    "available_funds": float(_f_af),
                    "net_theta_per_day": float(_f_th),
                    "net_vega": float(_f_vg),
                    "by_symbol": _parsed_sym,
                }
                db.set_setting(PORTFOLIO_FINANCE_OVERRIDES_KEY, json.dumps(_payload, ensure_ascii=False))
                st.success("Uložené. Obnovenie stránky aplikuje hodnoty.")
                st.rerun()

    st.divider()

    # ── 3 · Celkové výsledky (súhrn + graf trendu) ──────────────────────────────
    st.subheader("3 · Celkové výsledky")
    st.caption(
        "Súhrn po načítaní z TWS a po prípadných úpravách z kroku **2**. "
        "**História grafov** (APTR a výnos Θ po skupinách) sa **neukladá** pri obnovení z TWS — po úprave marží a Thety v kroku **4** použi tlačidlo **Uložiť snímky trendových grafov** (na konci kroku **4**)."
    )

    st.markdown("##### 📈 P/L")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric(
            "Unrealized P/L",
            f"${_disp_unreal:+,.2f}",
            delta_color="normal",
        )
    with c2:
        st.metric(
            "Realized P/L (session)",
            f"${_disp_real:+,.2f}",
            delta_color="normal",
        )
    with c3:
        st.metric(
            "Volný kapitál (Available Funds)",
            f"{cur_sym} {_disp_avail:,.2f}",
            help="Available Funds z účtu IBKR (ako v TWS) — voľný kapitál na obchodovanie, nie súčet trhovej hodnoty pozícií.",
        )

    if _fin_ov.get("enabled"):
        st.caption(
            "ℹ️ **Ručné súčty zapnuté** — metriky P/L, Available Funds, Net Theta, Net Vega a „Podľa podkladu“ môžu byť z **uložených úprav**. "
            f"Z API (orientačné): Unreal **${unreal_pnl:+,.2f}** · Avail. **{cur_sym} {_avail_base:,.2f}** · "
            f"Theta **${total_theta:+.2f}**/deň · Vega **${total_vega:+.2f}**."
        )
    else:
        st.caption(
            f"Unrealized P/L je súčet zo **všetkých** pozícií z API (typy: {', '.join(_sec_types_in) or '—'}). "
            "Ak TWS ukazuje iný súčet, skontroluj filter účtu a stĺpec *Unrealized P&L* (nie Mark alebo P&L%), alebo použi **Ručné súčty** v kroku **2**."
        )

    if not _fin_ov.get("enabled"):
        st.caption(
            "**Volný kapitál** vyššie = **Available Funds** z účtu (ako v TWS Account). "
            "**NLV** je v bloku Margin nižšie. Rozpad súčtu `marketValue` z API podľa typu nástroja je v kroku **1** (expander)."
        )

    st.markdown("##### Podľa podkladu (IBKR)")
    st.caption(
        "**Σ abs trh. hodn.** = súčet absolútnych trhových hodnôt nôh pod symbolom — ukáže „hmotnosť“ pozície. "
        "**Nie je to marža brokera:** `PortfolioItem` v API neobsahuje maintenance margin po nôhach. "
        "Presné marže po spreadoch: v TWS *Portfolio* → pravý klik na spread/balík → *Margin Impact*, prípadne *Account* okno."
    )
    _by_under = ibkr_aggregates_by_underlying(positions)
    if _fin_ov.get("enabled") and _fin_ov.get("by_symbol"):
        _by_under = merge_ibkr_by_underlying_overrides(_by_under, _fin_ov["by_symbol"])
    if _by_under:
        st.table(pd.DataFrame(_by_under))
    else:
        st.caption("Žiadne riadky.")

    from_tws = bool(account and any(k != "_currency" for k in account))
    st.markdown(f"##### 💳 Margin a hodnota účtu {'(TWS live)' if from_tws else '(manuálne)'}")
    if acct_currency:
        st.caption(f"Mena účtu: **{acct_currency}**")
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("NLV – Net Liquidation", f"{cur_sym} {nlv:,.0f}" if nlv else "—")
    with m2:
        st.metric("Available Funds", f"{cur_sym} {avail:,.0f}" if avail else "—")
    with m3:
        st.metric("Buying Power", f"{cur_sym} {buying_pwr:,.0f}" if buying_pwr else "—")
    with m4:
        st.metric("Maintenance Margin", f"{cur_sym} {maint_mrg:,.0f}" if maint_mrg else "—")

    if not from_tws:
        st.caption("ℹ️ Margin z TWS sa nepodarilo načítať. Zobrazené sú manuálne hodnoty z Portfolio Agent.")

    st.divider()

    # ── Greeks (súčasť kroku 3) ───────────────────────────────────────────────
    st.markdown("##### 🔢 Portfolio Greeks (opcie)")
    g1, g2, g3 = st.columns(3)
    with g1:
        st.metric(
            "Net Theta (celé portfólio)",
            f"${_disp_theta:+.2f}/deň" if (opts or _fin_ov.get("enabled")) else "—",
            help="Denný čas. rozpad opčných nôh z IB (Short záporné → kladný príspevok), alebo ručná hodnota z kroku 2.",
        )
    with g2:
        st.metric(
            "Net Vega",
            f"${_disp_vega:+.2f}" if (opts or _fin_ov.get("enabled")) else "—",
            help="Citlivosť portfólia na 1 % zmenu IV z IB, alebo ručná hodnota z kroku 2.",
        )
    with g3:
        st.metric(
            "Počet opčných nôh",
            f"{len(opts)} opcií / {len(stocks)} akcií",
        )

    no_greeks = [p.get("ticker") for p in opts if not p.get("theta")]
    if no_greeks:
        st.caption(
            f"⚠️ Greeks chýbajú pre: {', '.join(dict.fromkeys(no_greeks))} "
            "– z TWS neprišli (pravdepodobne chýba STK pozícia pre BS výpočet)."
        )

    with st.expander("Steady Yields — semafor (short z denníka + IBKR)", expanded=False):
        st.caption(
            "Páruje **otvorené Short** z Trade Log (s Group ID) s opčnými riadkami z práve načítaného portfólia. "
            "Detail a roll odhad: stránka **Steady Yields**."
        )
        _sy_shorts = [
            t
            for t in db.get_open_trades()
            if t.get("leg_type") == "Short" and (t.get("group_id") or "").strip()
        ]
        if not _sy_shorts:
            st.caption("Žiadne otvorené Short s vyplneným Group ID.")
        else:
            _sy_rows = []
            for _t in _sy_shorts:
                _exp = normalize_expiry(str(_t.get("expiry") or ""))
                _k = float(_t.get("strike") or 0)
                _tk = (_t.get("ticker") or "").upper()
                _dte = calc_dte(_exp) or 0
                _pos = None
                for _p in opts:
                    if (_p.get("ticker") or "").upper() != _tk:
                        continue
                    if abs(float(_p.get("strike") or -1) - _k) > 0.01:
                        continue
                    if normalize_expiry(str(_p.get("expiry") or "")) != _exp:
                        continue
                    _pos = _p
                    break
                _ad = None
                if _pos and _pos.get("delta") is not None:
                    _ad = abs(float(_pos.get("delta")))
                _tl = traffic_light(abs_delta=_ad, dte=_dte)
                _emoji = {"green": "🟢", "orange": "🟠", "red": "🔴"}.get(_tl.level, "⚪")
                _sy_rows.append(
                    {
                        "Group": (_t.get("group_id") or "")[:24],
                        "Ticker": _tk,
                        "Short": f"K{_k:g} {_exp}",
                        "DTE": _dte,
                        "Stav": _emoji,
                        "Úroveň": _tl.level,
                        "Δ live": _pos.get("delta") if _pos else None,
                    }
                )
            st.dataframe(pd.DataFrame(_sy_rows), use_container_width=True, hide_index=True)

    if _pf_theta_aptr is not None:
        st.metric(
            "Portfólio APTR (Θ)",
            f"{_pf_theta_aptr['yield_pct']:+.1f} %",
            help="Súčet denného Θ zo skupín (denník ↔ IB) deleno súčet (vstupný net debet + marža) "
            "týchto skupín, × 365 × 100 — rovnaká logika ako pri jednej skupine.",
        )
        st.caption(
            f"Θ v tejto báze **${_pf_theta_aptr['theta_per_day']:+.2f}**/deň · "
            f"súčet nákladov **${_pf_theta_aptr['capital_basis_usd']:,.0f}** · "
            f"**{_pf_theta_aptr['groups_in_basis']}** skupín s opciami a platným net debetom."
        )
        if _pf_theta_aptr.get("unmatched_opt_count", 0) > 0:
            _ut = float(_pf_theta_aptr.get("unmatched_theta_per_day") or 0)
            st.caption(
                f"ℹ️ **{_pf_theta_aptr['unmatched_opt_count']}** opčných riadkov IB **bez** otvorenej nohy v denníku "
                f"má Θ spolu **${_ut:+.2f}**/deň — **nie sú** v čitateľovi; **Net Theta** vyššie ich zahŕňa."
            )
        if _pf_theta_aptr.get("incomplete_theta"):
            st.caption("⚠️ Niektorým opciám chýba Theta z IB — APTR môže byť neúplný.")

        _hist_pf = db.get_group_apr_snapshots(
            db.PORTFOLIO_APTR_SNAPSHOT_GROUP_ID,
            limit=120,
            basis_kind="theta",
        )
        if len(_hist_pf) >= 2:
            _hpf = pd.DataFrame(_hist_pf)
            _hpf["Čas"] = pd.to_datetime(_hpf["captured_at"], utc=True)
            _hpf = _hpf.sort_values("Čas")
            _chpf = _hpf.set_index("Čas")[["apr_pct"]].rename(
                columns={"apr_pct": "Portfólio APTR Θ %"}
            )
            st.caption(
                "**Graf:** každý bod = jedno uloženie tlačidlom **Uložiť snímky trendových grafov** v kroku **4** (po maržiach a Thete z TWS). "
                "Zmena marží alebo zloženia skupín môže krivku posunúť."
            )
            _plotly_line_trend(
                _chpf.iloc[:, 0],
                chart_key="tj_plot_portfolio_aptr_theta",
                height=200,
            )
        elif len(_hist_pf) == 1:
            st.caption(
                "Zatiaľ jeden záznam — po ďalšom **uložení snímok** v kroku **4** uvidíš čiarový graf."
            )

    st.divider()

    # ── 4 · Detail podľa skupín ────────────────────────────────────────────────
    st.subheader("4 · Detail podľa skupín (denník ↔ TWS)")
    st.info(
        "**Hlavná metrika: Ročný výnos z Θ** — z IB Theta a vstupného net debetu z denníka. "
        "Trhové ceny v tabuľke sú **IB mark** (orientačné). "
        "**Grafy trendu** doplníš tlačidlom **Uložiť snímky trendových grafov** pod expandermi (až po úprave marží a Thety z TWS), nie pri **Načítať z TWS**."
    )

    def _row_from_ib_position(p: dict) -> dict:
        is_opt = p.get("sec_type") in ("OPT", "FOP")
        src = p.get("price_source", "")
        src_label = {"settlement_close": "Settle (=TWS)", "hist_trades": "Last (hist)", "hist_midpoint": "Midpoint (hist)", "hist_last": "Last (hist)", "portfolio_mark": "Mark (portfólio)", "last": "Last", "mid": "Mid", "mark": "Mark", "close": "Close"}.get(src, src)
        return {
            "Ticker": p.get("ticker", ""),
            "Typ": p.get("sec_type", ""),
            "L/S": p.get("leg_type", ""),
            "Kontr.": _fmt_qty(p.get("contracts", 1)),
            "Strike": f"${p['strike']:.0f}" if is_opt and p.get("strike") else "—",
            "Expiry": p.get("expiry", "—") if is_opt else "—",
            "Opt. typ": p.get("option_type", "—") if is_opt else "—",
            "Trhová cena": f"${float(p.get('market_price') or 0):.2f}",
            "Zdroj ceny": src_label,
            "Trhová hodnota": f"${float(p.get('market_value') or 0):,.2f}",
            "Neskutoč. P/L": f"${float(p.get('unrealized_pnl') or 0):+,.2f}",
            "Skutočný zisk/strata": f"${float(p.get('realized_pnl') or 0):+,.2f}",
            "Delta": f"{p['delta']:+.3f}" if p.get("delta") else "—",
            "Theta": f"{p['theta']:+.4f}" if p.get("theta") else "—",
            "Vega": f"{p['vega']:+.4f}" if p.get("vega") else "—",
            "IV": f"{float(p['iv'])*100:.1f}%" if p.get("iv") else "—",
        }

    if not positions:
        st.info("Žiadne pozície v portfóliu.")
    else:
        for _idx, (_gid, _plist) in enumerate(_ordered):
            _sum_u = sum(float(x.get("unrealized_pnl") or 0) for x in _plist)
            _sum_mv = sum(float(x.get("market_value") or 0) for x in _plist)
            _legs_g = [t for t in _all_tr if journal_group_id(t) == _gid]
            _wk = dashboard_group_margin_widget_key(_gid)
            if _wk not in st.session_state:
                st.session_state[_wk] = float(_margins.get(_gid, 0.0) or 0.0)

            with st.expander(
                f"{_gid} · {len(_plist)} pozíc. v TWS",
                expanded=(_idx == 0),
            ):
                st.number_input(
                    "Udržiavacia marža skupiny ($)",
                    min_value=0.0,
                    step=50.0,
                    format="%.0f",
                    key=_wk,
                    help="Z TWS (Margin Impact). Ulož tlačidlom nižšie; po **Načítať z TWS** sa hodnota znovu načíta z databázy. APR z P&L je voliteľný.",
                )
                _mval = float(st.session_state.get(_wk, 0) or 0)
                _open_legs_g = [t for t in _legs_g if t.get("status") == "Open"]

                # Ručná korekcia Theta z TWS
                _twk = f"pf_dash_theta_override_{_gid}"
                _theta_ov = st.number_input(
                    "Theta z TWS ($/deň) — 0 = použi IB API",
                    value=float(st.session_state.get(_twk, 0.0)),
                    step=0.5,
                    format="%.2f",
                    key=_twk,
                    help="Zadaj súčet Theta z TWS pre túto skupinu ak sa líši od IB API (napr. +8.50). "
                         "Nechaj 0 pre automatickú hodnotu z IB.",
                )

                _theta_y = compute_theta_annualized_yield_pct(
                    _open_legs_g, _plist,
                    maintenance_margin_usd=_mval,
                    theta_override_usd=_theta_ov,
                )

                if _theta_y is not None:
                    _th_src_label = "manuál TWS" if _theta_y.get("theta_source") == "manual" else "IB API"
                    st.metric(
                        "Ročný výnos z Θ (náklad: net debet + marža)",
                        f"{_theta_y['yield_pct']:+.1f} %",
                        help="Vzorec: (Θ $/deň × 365 / (vstupný net debet z denníka + udržiavacia marža)) × 100. "
                        "Ak je marža 0, menovateľ je len net debet — doplň maržu z TWS pre plný náklad.",
                    )
                    _nd = float(_theta_y["net_debit_usd"])
                    _mm = float(_theta_y["maintenance_margin_usd"])
                    _cb = float(_theta_y["capital_basis_usd"])
                    _th_ib = float(_theta_y.get("theta_per_day_ib") or 0)
                    if _mm >= 1.0:
                        st.caption(
                            f"Θ **${_theta_y['theta_per_day']:+.2f}**/deň ({_th_src_label})"
                            + (f" · IB API: ${_th_ib:+.2f}" if _theta_y.get("theta_source") == "manual" else "")
                            + f" · náklad **${_nd:,.0f}** net debet + **${_mm:,.0f}** marža = **${_cb:,.0f}**"
                        )
                    else:
                        st.caption(
                            f"Θ **${_theta_y['theta_per_day']:+.2f}**/deň ({_th_src_label})"
                            + (f" · IB API: ${_th_ib:+.2f}" if _theta_y.get("theta_source") == "manual" else "")
                            + f" · menovateľ zatiaľ len **net debet ${_nd:,.0f}** — doplň **maržu** vyššie."
                        )
                    if _theta_y.get("incomplete_theta") and _theta_y.get("theta_source") != "manual":
                        st.caption(
                            "⚠️ Aspoň jedna opčná noha v TWS nemá Theta — zadaj hodnotu manuálne vyššie."
                        )
                    _hist_t = db.get_group_apr_snapshots(_gid, limit=120, basis_kind="theta")
                    if len(_hist_t) >= 2:
                        _ht = pd.DataFrame(_hist_t)
                        _ht["Čas"] = pd.to_datetime(_ht["captured_at"], utc=True)
                        _ht = _ht.sort_values("Čas")
                        _cht = _ht.set_index("Čas")[["apr_pct"]].rename(
                            columns={"apr_pct": "Ročný výnos Θ %"}
                        )
                        st.caption(
                            "**Graf skupiny:** nový bod pridáš tlačidlom **Uložiť snímky trendových grafov** pod expandermi skupín."
                        )
                        _plotly_line_trend(
                            _cht.iloc[:, 0],
                            chart_key=f"tj_plot_grp_theta_{_wk}",
                            height=200,
                        )
                    elif len(_hist_t) == 1:
                        st.caption(
                            "Jeden záznam — po ďalšom **uložení snímok** (tlačidlo pod skupinami) uvidíš graf."
                        )
                else:
                    st.caption(
                        "Ročný výnos (Theta) teraz nie je: skontroluj **Open** nohy s **Entry** v Trade Logu, "
                        "zosúladenie s TWS a že IB posiela **Theta** pre opcie."
                    )

                if _plist:
                    _df_pos = pd.DataFrame([_row_from_ib_position(p) for p in _plist])
                    # Skry stĺpce s P/L — sú IB mark a môžu zavádzať
                    _cols_show = [c for c in _df_pos.columns if c not in ("Neskutoč. P/L", "Skutočný zisk/strata", "Trhová hodnota", "Zdroj ceny")]
                    st.table(_df_pos[_cols_show])
                    with st.expander("Orientačné: IB mark ceny a P/L (môžu sa líšiť od TWS)", expanded=False):
                        st.caption("Trhová cena = **IB mark** (mid bid/ask alebo model). Unrealized P/L je vypočítaný IB z tejto ceny — **nie** z TWS Last.")
                        st.table(_df_pos[["Ticker", "Trhová cena", "Trhová hodnota", "Neskutoč. P/L"]])
                else:
                    st.caption("Žiadne IB riadky priradené k tejto skupine.")

        if _unmatched:
            st.subheader("— IB bez riadku v denníku")
            st.caption("Pozície z TWS bez zodpovedajúcej **otvorenej** nohy v Trade Logu.")
            st.table(pd.DataFrame([_row_from_ib_position(p) for p in _unmatched]))

        if st.button("💾 Uložiť udržiavacie marže skupín", key="pf_save_grp_margins"):
            _mnew: dict[str, float] = {}
            for _gid2, _ in _ordered:
                _w = dashboard_group_margin_widget_key(_gid2)
                if _w in st.session_state:
                    _mnew[_gid2] = float(st.session_state[_w])
            db.set_group_maint_margins(_mnew)
            st.success("Marže uložené do databázy (zlúčené; 0 = zruší uloženú maržu pre danú skupinu).")

        st.caption(
            "Keď sú **marže** a **Theta z TWS** v skupinách nastavené, ulož **jeden spoločný bod** do všetkých grafov (portfólio APTR + každá skupina s výpočtom Θ). "
            "Tým sa vyhneš uloženiu nesprávnych hodnôt hneď po surovom načítaní z API."
        )
        if st.button(
            "💾 Uložiť snímky trendových grafov (portfólio APTR + skupiny)",
            type="primary",
            use_container_width=True,
            key="pf_save_apr_snapshots",
        ):
            _ts_snap = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _n_snap = 0
            if _pf_theta_aptr is not None:
                db.append_group_apr_snapshot(
                    db.PORTFOLIO_APTR_SNAPSHOT_GROUP_ID,
                    _ts_snap,
                    "theta",
                    float(_pf_theta_aptr["yield_pct"]),
                    float(_pf_theta_aptr["theta_per_day"]),
                    float(_pf_theta_aptr["capital_basis_usd"]),
                    0,
                    float(_pf_theta_aptr["theta_per_day"]),
                )
                _n_snap += 1
            for _gid_s, _plist_s in _ordered:
                _legs_s = [t for t in _all_tr if journal_group_id(t) == _gid_s]
                _open_s = [t for t in _legs_s if t.get("status") == "Open"]
                _wk_s = dashboard_group_margin_widget_key(_gid_s)
                _mm_s = float(st.session_state.get(_wk_s, 0) or 0)
                _twk_s = f"pf_dash_theta_override_{_gid_s}"
                _tov_s = float(st.session_state.get(_twk_s, 0.0) or 0.0)
                _ty_s = compute_theta_annualized_yield_pct(
                    _open_s,
                    _plist_s,
                    maintenance_margin_usd=_mm_s,
                    theta_override_usd=_tov_s,
                )
                if _ty_s is not None:
                    db.append_group_apr_snapshot(
                        _gid_s,
                        _ts_snap,
                        "theta",
                        float(_ty_s["yield_pct"]),
                        float(_ty_s["theta_per_day"]),
                        float(_ty_s["capital_basis_usd"]),
                        0,
                        float(_ty_s["theta_per_day"]),
                    )
                    _n_snap += 1
            if _n_snap == 0:
                st.warning("Neuložená žiadna snímka — chýba portfólio APTR aj výpočet výnosu Θ pre skupiny.")
            else:
                st.success(f"Uložené **{_n_snap}** snímok do histórie grafov (časová pečiatka: jedna pre celú dávku).")
            st.rerun()

        _csv_rows = [_row_from_ib_position(p) for p in positions]
        _df_all = pd.DataFrame(_csv_rows)
        st.download_button(
            "⬇️ Exportovať všetky pozície (CSV)",
            data=_df_all.to_csv(index=False).encode("utf-8"),
            file_name="portfolio_pozicie.csv",
            mime="text/csv",
            key="pf_dash_csv_export",
        )

    st.divider()

    # ── Otvorené objednávky ────────────────────────────────────────────────────
    orders: list = _JOB.get("orders") or []

    st.subheader(f"📋 Otvorené objednávky TWS ({len(orders)})")

    if not orders:
        st.info("Žiadne čakajúce objednávky v TWS.")
    else:
        ord_rows = []
        for o in orders:
            sec = o.get("sec_type", "")
            is_opt = sec in ("OPT", "FOP")
            is_bag = sec == "BAG"
            lmt = o.get("limit_price")
            aux = o.get("aux_price")
            price_str = f"${lmt:.2f}" if lmt else "—"
            if aux:
                price_str += f" / stop ${aux:.2f}"
            if is_opt:
                detail = f"{o.get('option_type','?')} ${o.get('strike',0):.0f} exp {o.get('expiry','')}"
            elif is_bag:
                detail = o.get("legs_descr") or "—"
            else:
                detail = "—"
            ord_rows.append({
                "Ticker":     o.get("ticker", ""),
                "Typ":        sec,
                "Akcia":      o.get("action", ""),
                "Množstvo":   o.get("total_qty", ""),
                "Typ ord.":   o.get("order_type", ""),
                "Limit cena": price_str,
                "Detail":     detail,
                "Stav":       o.get("status", ""),
            })

        df_ord = pd.DataFrame(ord_rows)
        st.table(df_ord)

else:
    st.info("Klikni **Načítať z TWS** pre zobrazenie live dát.")
