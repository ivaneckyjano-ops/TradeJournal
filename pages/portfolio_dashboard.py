"""
TWS Portfolio Dashboard – live stav portfólia z Interactive Brokers.

Zobrazuje:
  - P/L: Unrealized, Realized, Daily (z IBKR portfólia)
  - Margin: NLV, Available Funds, Buying Power, Maintenance Margin
  - Greeks: Celková Theta, Vega (agregované za všetky opčné pozície)
  - Pozície: tabuľka všetkých aktuálnych pozícií
"""
import time
import threading

import streamlit as st
import pandas as pd

from core import ibkr
from core import database as db

db.init_db()

# ─── Fetch job je uložený v ibkr module (perzistentný medzi page reruns) ──────
_JOB = ibkr.DASHBOARD_FETCH_JOB


def _run_dashboard_fetch():
    """Beží v background vlákne – stiahne pozície aj account summary."""
    try:
        # ── Pozície + Greeks ──────────────────────────────────────────────────
        pos_res = ibkr.fetch_positions(with_greeks=True)
        if pos_res.get("error"):
            _JOB["error"]  = pos_res["error"]
            _JOB["status"] = "error"
            return
        _JOB["positions"] = pos_res.get("positions", [])

        # ── Account summary ───────────────────────────────────────────────────
        # reqAccountUpdates(True) je spustené pri connect() → cache by mala byť naplnená.
        # Ak nie, počkáme krátko a skúsime znova.
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

        # Objednávky načítame v hlavnom vlákne (kvôli qualifyContracts pre BAG nohy)
        _JOB["orders"]      = None
        _JOB["orders_err"]  = None
        _JOB["orders_raw"]  = 0

        _JOB["status"]  = "done"
    except Exception as exc:
        _JOB["error"]  = str(exc)
        _JOB["status"] = "error"


# ─── UI ───────────────────────────────────────────────────────────────────────

st.title("📊 Portfolio Dashboard")
st.caption("Live stav portfólia z TWS – P/L, Margin, Greeks.")

# ── Stav IBKR pripojenia ──────────────────────────────────────────────────────
if not ibkr.is_connected():
    st.error("IBKR nie je pripojené. Pripoj sa na TWS / IB Gateway cez Dashboard → Pripojenie.")
    st.stop()

# ── Tlačidlo na načítanie / stav jobu ─────────────────────────────────────────
job_status = _JOB["status"]
is_running = (job_status == "running")

col_btn, col_status = st.columns([2, 4])
with col_btn:
    do_refresh = st.button(
        "🔄 Načítať z TWS",
        disabled=is_running,
        type="primary",
        use_container_width=True,
    )
with col_status:
    if is_running:
        st.info("⏳ Sťahujem dáta z TWS...")
    elif job_status == "done":
        n_pos  = len(_JOB.get("positions") or [])
        n_ord  = len(_JOB.get("orders") or [])
        n_raw  = _JOB.get("orders_raw", n_ord)
        oe     = _JOB.get("orders_err")
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

if do_refresh and not is_running:
    _JOB["status"]    = "running"
    _JOB["positions"] = None
    _JOB["orders"]    = None
    _JOB["account"]   = None
    _JOB["error"]     = None
    threading.Thread(target=_run_dashboard_fetch, daemon=True).start()
    st.rerun()

if is_running:
    time.sleep(1)
    st.rerun()

if job_status == "done":
    positions: list = _JOB.get("positions") or []
    account:   dict = _JOB.get("account")  or {}

    # Načítaj objednávky v hlavnom vlákne (BAG nohy vyžadujú qualifyContracts)
    if _JOB.get("orders") is None and ibkr.is_connected():
        try:
            ord_res = ibkr.fetch_open_orders(use_cache=False)
            # DEBUG vypnutý
            # st.toast(f"DEBUG orders: err={ord_res.get('error')}, count={len(ord_res.get('orders', []))}, raw={ord_res.get('total_raw')}, statuses={ord_res.get('_debug_statuses')}")
            if ord_res.get("error"):
                _JOB["orders"]      = []
                _JOB["orders_err"]  = ord_res["error"]
            else:
                _JOB["orders"]      = ord_res.get("orders", [])
                _JOB["orders_err"]  = None
                _JOB["orders_raw"]  = ord_res.get("total_raw", 0)
        except Exception as e:
            st.error(f"Chyba pri načítaní objednávok: {e}")
            _JOB["orders"]     = []
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

    total_theta = sum(
        float(p.get("theta") or 0) * int(p.get("contracts") or 1) * 100 *
        (-1 if p.get("leg_type") == "Short" else 1)
        for p in opts if p.get("theta")
    )
    total_vega = sum(
        float(p.get("vega") or 0) * int(p.get("contracts") or 1) * 100 *
        (-1 if p.get("leg_type") == "Short" else 1)
        for p in opts if p.get("vega")
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

    st.divider()

    # ── Tabuľka pozícií ───────────────────────────────────────────────────────
    st.subheader("📋 Pozície")

    if not positions:
        st.info("Žiadne pozície v portfóliu.")
    else:
        rows = []
        for p in positions:
            is_opt = p.get("sec_type") == "OPT"
            rows.append({
                "Ticker":        p.get("ticker", ""),
                "Typ":           p.get("sec_type", ""),
                "L/S":           p.get("leg_type", ""),
                "Kontr.":        p.get("contracts", 1),
                "Strike":        f"${p['strike']:.0f}" if is_opt and p.get("strike") else "—",
                "Expiry":        p.get("expiry", "—") if is_opt else "—",
                "Opt. typ":      p.get("option_type", "—") if is_opt else "—",
                "Trhová cena":   f"${float(p.get('market_price') or 0):.2f}",
                "Trhová hodnota":f"${float(p.get('market_value') or 0):,.2f}",
                "Unreal. P/L":   f"${float(p.get('unrealized_pnl') or 0):+,.2f}",
                "Real. P/L":     f"${float(p.get('realized_pnl') or 0):+,.2f}",
                "Delta":         f"{p['delta']:+.3f}" if p.get("delta") else "—",
                "Theta":         f"{p['theta']:+.4f}" if p.get("theta") else "—",
                "Vega":          f"{p['vega']:+.4f}"  if p.get("vega")  else "—",
                "IV":            f"{float(p['iv'])*100:.1f}%" if p.get("iv") else "—",
            })

        df = pd.DataFrame(rows)

        def _color_pnl(val: str):
            try:
                num = float(val.replace("$", "").replace(",", "").replace("+", ""))
                if num > 0:
                    return "color: #22c55e"
                if num < 0:
                    return "color: #ef4444"
            except Exception:
                pass
            return ""

        styled = df.style.map(_color_pnl, subset=["Unreal. P/L", "Real. P/L"])
        st.dataframe(styled, use_container_width=True, hide_index=True)

        # Export
        csv = df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "⬇️ Exportovať CSV",
            data=csv,
            file_name="portfolio_pozicie.csv",
            mime="text/csv",
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

        def _color_action(val: str):
            if val == "BUY":
                return "color: #22c55e; font-weight: bold"
            if val == "SELL":
                return "color: #ef4444; font-weight: bold"
            return ""

        df_ord = pd.DataFrame(ord_rows)
        styled_ord = df_ord.style.map(_color_action, subset=["Akcia"])
        st.dataframe(styled_ord, use_container_width=True, hide_index=True)

else:
    st.info("Klikni **Načítať z TWS** pre zobrazenie live dát.")
