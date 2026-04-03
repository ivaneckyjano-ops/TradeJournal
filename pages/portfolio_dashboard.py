"""
TWS Portfolio Dashboard – live stav portfólia z Interactive Brokers.

Zobrazuje:
  - P/L: Unrealized, Realized (z IBKR portfólia) + rozpad podľa tickeru
  - Pozície podľa skupín: trend Ročný výnos (Theta); voliteľný APR z P&L (unrealized)
  - Margin: NLV, Available Funds, Buying Power, Maintenance Margin (úroveň účtu)
  - Greeks: Net Theta, Vega; portfólio APTR (Θ) = ΣΘ / Σ(net debet + marža) po denníkových skupinách
"""
import time
from datetime import datetime, timezone
from typing import Optional

import streamlit as st
import pandas as pd

from core import ibkr
from core import database as db
from core.page_context import TWS_DASHBOARD_PAGE, set_tradejournal_page
from core.portfolio_data import (
    compute_group_apr_on_maint_margin,
    compute_portfolio_theta_aptr,
    compute_simple_apr,
    compute_theta_annualized_yield_pct,
    dashboard_group_margin_widget_key,
    group_ibkr_positions_for_dashboard,
    ibkr_aggregates_by_underlying,
    journal_group_id,
    unrealized_by_journal_ids_for_ib_legs,
)

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
        pos_res = ibkr.fetch_positions(with_greeks=True)
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

    opts   = [p for p in positions if p.get("sec_type") == "OPT"]
    stocks = [p for p in positions if p.get("sec_type") == "STK"]

    unreal_pnl = sum(float(p.get("unrealized_pnl") or 0) for p in positions)
    real_pnl   = sum(float(p.get("realized_pnl")   or 0) for p in positions)
    mkt_val    = sum(float(p.get("market_value")    or 0) for p in positions)

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

    st.divider()

    # ── P/L sekcia ────────────────────────────────────────────────────────────
    st.subheader("📈 P/L")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric(
            "Unrealized P/L",
            f"${unreal_pnl:+,.2f}",
            delta_color="normal",
        )
    with c2:
        st.metric(
            "Realized P/L (session)",
            f"${real_pnl:+,.2f}",
            delta_color="normal",
        )
    with c3:
        st.metric(
            "Market Value (celé portfólio)",
            f"${mkt_val:,.2f}",
        )

    st.caption(
        "Číslo **Unrealized P/L** vyššie je súčet stĺpca z **všetkých zobrazených riadkov** OPT+STK z API. "
        "Panel v TWS často ukazuje **iný** súčet (iné produkty, viac účtov, FX, alebo iný stĺpec ako *Unrealized*). "
        "Rozdiel 10–20 % pri väčšom portfóliu nie je nezvyčajný — porovnaj rovnaký filter účtu a typ riadkov v TWS."
    )

    st.subheader("Podľa podkladu (IBKR)")
    st.caption(
        "**Σ abs trh. hodn.** = súčet absolútnych trhových hodnôt nôh pod symbolom — ukáže „hmotnosť“ pozície. "
        "**Nie je to marža brokera:** `PortfolioItem` v API neobsahuje maintenance margin po nôhach. "
        "Presné marže po spreadoch: v TWS *Portfolio* → pravý klik na spread/balík → *Margin Impact*, prípadne *Account* okno."
    )
    _by_under = ibkr_aggregates_by_underlying(positions)
    if _by_under:
        st.table(pd.DataFrame(_by_under))
    else:
        st.caption("Žiadne riadky.")

    st.divider()

    # ── Margin / Account sekcia ───────────────────────────────────────────────
    from_tws = bool(account and any(k != "_currency" for k in account))
    st.subheader(f"💳 Margin a hodnota účtu {'(TWS live)' if from_tws else '(manuálne)'}")
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

    # ── Greeks sekcia ─────────────────────────────────────────────────────────
    st.subheader("🔢 Portfolio Greeks (opcie)")
    g1, g2, g3 = st.columns(3)
    with g1:
        st.metric(
            "Net Theta (celé portfólio)",
            f"${total_theta:+.2f}/deň" if total_theta else "—",
            help="Denný čas. rozpad všetkých opčných nôh (Short nogy záporné → kladný príspevok).",
        )
    with g2:
        st.metric(
            "Net Vega",
            f"${total_vega:+.2f}" if total_vega else "—",
            help="Citlivosť portfólia na 1% zmenu IV.",
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

        if do_refresh:
            db.append_group_apr_snapshot(
                db.PORTFOLIO_APTR_SNAPSHOT_GROUP_ID,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "theta",
                float(_pf_theta_aptr["yield_pct"]),
                float(_pf_theta_aptr["theta_per_day"]),
                float(_pf_theta_aptr["capital_basis_usd"]),
                0,
                float(_pf_theta_aptr["theta_per_day"]),
            )
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
            st.caption("Trend **Portfólio APTR (Θ)** — každý bod = jedno **Načítať z TWS** (zmena marží / skupín môže skočiť krivku).")
            _plotly_line_trend(
                _chpf.iloc[:, 0],
                chart_key="tj_plot_portfolio_aptr_theta",
                height=200,
            )
        elif len(_hist_pf) == 1:
            st.caption("Po ďalšom **Načítať z TWS** uvidíš graf vývoja portfólio APTR.")

    st.divider()

    # ── Pozície podľa skupín (denník), marža, APR ──────────────────────────────
    st.subheader("📋 Pozície podľa skupín (denník ↔ TWS)")
    st.caption(
        "Cena opcie z **API** sa môže líšiť od TWS (**mark vs Last**) — **Unreal. P/L** na riadku je z IB, ale môže sa líšiť od toho, čo porovnávaš v okne. "
        "Pre rozhodnutie **či držať** spread je hlavná metrika **Ročný výnos z Θ** a jej **trend**. "
        "Báza nákladu: **vstupný net debet** (Trade Log) **+ udržiavacia marža** (ak ju zadáš) — čo spread reálne stojí. "
        "Vzorec: ``(Θ×365 / (net debet + marža)) × 100``; Θ z IB. "
        "**APR z P&L** (s IB unrealized) je len voliteľné — unrealized vie skresliť, preto je schované nižšie. "
        "**Udržiavacia marža** z TWS ostáva k dispozícii pre ten voliteľný APR."
    )

    def _row_from_ib_position(p: dict) -> dict:
        is_opt = p.get("sec_type") == "OPT"
        return {
            "Ticker": p.get("ticker", ""),
            "Typ": p.get("sec_type", ""),
            "L/S": p.get("leg_type", ""),
            "Kontr.": _fmt_qty(p.get("contracts", 1)),
            "Strike": f"${p['strike']:.0f}" if is_opt and p.get("strike") else "—",
            "Expiry": p.get("expiry", "—") if is_opt else "—",
            "Opt. typ": p.get("option_type", "—") if is_opt else "—",
            "Trhová cena": f"${float(p.get('market_price') or 0):.2f}",
            "Trhová hodnota": f"${float(p.get('market_value') or 0):,.2f}",
            "Unreal. P/L": f"${float(p.get('unrealized_pnl') or 0):+,.2f}",
            "Real. P/L": f"${float(p.get('realized_pnl') or 0):+,.2f}",
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
                f"{_gid} · {len(_plist)} IB · Unreal Σ ${_sum_u:+,.0f} · MV Σ ${_sum_mv:+,.0f}",
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
                _theta_y = compute_theta_annualized_yield_pct(
                    _open_legs_g, _plist, maintenance_margin_usd=_mval
                )

                if _theta_y is not None:
                    st.metric(
                        "Ročný výnos z Θ (náklad: net debet + marža)",
                        f"{_theta_y['yield_pct']:+.1f} %",
                        help="Vzorec: (Θ $/deň × 365 / (vstupný net debet z denníka + udržiavacia marža)) × 100. "
                        "Ak je marža 0, menovateľ je len net debet — doplň maržu z TWS pre plný náklad.",
                    )
                    _nd = float(_theta_y["net_debit_usd"])
                    _mm = float(_theta_y["maintenance_margin_usd"])
                    _cb = float(_theta_y["capital_basis_usd"])
                    if _mm >= 1.0:
                        st.caption(
                            f"Θ **${_theta_y['theta_per_day']:+.2f}**/deň (IB) · náklad **${_nd:,.0f}** net debet + **${_mm:,.0f}** marža "
                            f"= **${_cb:,.0f}** (menovateľ výnosu z Θ)."
                        )
                    else:
                        st.caption(
                            f"Θ **${_theta_y['theta_per_day']:+.2f}**/deň (IB) · menovateľ zatiaľ len **net debet ${_nd:,.0f}** — "
                            f"doplň **udržiavaciu maržu** vyššie pre bázu *net debet + marža*."
                        )
                    if _theta_y.get("incomplete_theta"):
                        st.caption(
                            "⚠️ Aspoň jedna opčná noha v TWS nemá Theta — súčet Θ môže byť neúplný."
                        )
                    if do_refresh:
                        db.append_group_apr_snapshot(
                            _gid,
                            datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "theta",
                            float(_theta_y["yield_pct"]),
                            float(_theta_y["theta_per_day"]),
                            float(_theta_y["capital_basis_usd"]),
                            0,
                            float(_theta_y["theta_per_day"]),
                        )
                    _hist_t = db.get_group_apr_snapshots(_gid, limit=120, basis_kind="theta")
                    if len(_hist_t) >= 2:
                        _ht = pd.DataFrame(_hist_t)
                        _ht["Čas"] = pd.to_datetime(_ht["captured_at"], utc=True)
                        _ht = _ht.sort_values("Čas")
                        _cht = _ht.set_index("Čas")[["apr_pct"]].rename(
                            columns={"apr_pct": "Ročný výnos Θ %"}
                        )
                        st.caption("Trend **Ročný výnos (Theta)** — každý bod = jedno **Načítať z TWS**.")
                        _plotly_line_trend(
                            _cht.iloc[:, 0],
                            chart_key=f"tj_plot_grp_theta_{_wk}",
                            height=200,
                        )
                    elif len(_hist_t) == 1:
                        st.caption("Po ďalšom **Načítať z TWS** uvidíš čiarový graf vývoja Theta výnosu.")
                else:
                    st.caption(
                        "Ročný výnos (Theta) teraz nie je: skontroluj **Open** nohy s **Entry** v Trade Logu, "
                        "zosúladenie s TWS a že IB posiela **Theta** pre opcie."
                    )

                _umap = unrealized_by_journal_ids_for_ib_legs(_legs_g, _plist)
                _apr = compute_group_apr_on_maint_margin(_legs_g, _sum_u, _mval)
                _apr_prem = (
                    compute_simple_apr(_legs_g, _umap) if (_apr is None and _legs_g) else None
                )
                with st.expander(
                    "Nepovinné: APR z P&L (vrátane IB unrealized) — môže zavádzať",
                    expanded=False,
                ):
                    st.caption(
                        "Tento APR používa **nerealizovaný P&L z IB** a realizovaný z denníka. "
                        "Pre úvahu *či držať kvôli času* je spoľahlivejší **Ročný výnos (Theta)** vyššie."
                    )
                    if _apr is not None:
                        st.metric("APR (na udrž. marži)", f"{_apr['apr_pct']:+.1f} %")
                        st.caption(
                            f"Vzorec: **P&L** / marža × 365/dní × 100 — čitateľ je **zisk/strata** (denník + IB unreal.), "
                            f"**nie** Theta. Ak je číslo zhodné s **Ročný výnos z Θ** vyššie, ide o **náhodu**."
                        )
                        st.caption(
                            f"P&L **${_apr['pnl']:+,.0f}** (Rlz denník ${_apr['realized']:+,.0f} + IB unreal. ${_apr['unreal_ib']:+,.0f}) "
                            f"· **{_apr['days']}** dní · marža **${_mval:,.0f}**"
                        )
                    elif _apr_prem is not None:
                        st.metric("APR (orient., báza prémia)", f"{_apr_prem['apr_pct']:+.1f} %")
                        st.caption(
                            f"P&L **${_apr_prem['pnl']:+,.0f}** · báza **${_apr_prem['basis']:,.0f}** · **{_apr_prem['days']}** dní."
                        )
                        if _apr_prem.get("short_horizon"):
                            st.caption("Krátky horizont: annualizácia je hlučná.")
                    elif _mval >= 1:
                        st.caption("APR z P&L: chýbajú dáta (denník / dátum vstupu).")
                    else:
                        st.caption("Zadaj maržu vyššie, ak chceš tento orientačný APR.")

                if _plist:
                    st.table(pd.DataFrame([_row_from_ib_position(p) for p in _plist]))
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
