"""
Portfolio Command Center — analýza otvorenej knihy z denníka.

Záložky: skupiny a expozícia, Greky a scenáre, časová os (DTE), história a súhrn (P&L, APR, TWS).
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date
from typing import Optional

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page
from core.portfolio_data import normalize_expiry, compute_simple_apr
from core.portfolio_view import min_short_dte_open, open_groups_count
from core.probability import bs_price, calc_greeks

db.init_db()
set_tradejournal_page("portfolio_cc")

st.title("Portfolio — analýza otvorenej knihy")
st.caption(
    "Zdroj pozícií je **denník (DB)**. **Nerealizovaný P&L** otvorených nôh: pri zhode s TWS **live z IBKR**, "
    "inak **Black–Scholes** po zadaní spotu a IV. **Live účet, margin a súhrnné P/L z brokera** — v menu "
    "**Analýza → TWS Dashboard**."
)
st.page_link("pages/portfolio_dashboard.py", label="Otvoriť TWS Dashboard", icon=":material/monitor_heart:")

# ─── Pomocné funkcie ──────────────────────────────────────────────────────────

def _dte(expiry_str: str) -> int:
    """Počet dní do expirácie."""
    if not expiry_str:
        return 0
    try:
        exp = date.fromisoformat(
            f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:8]}"
            if len(expiry_str) == 8 else expiry_str
        )
        return max(0, (exp - date.today()).days)
    except Exception:
        return 0


def _leg_sign(leg_type: str) -> int:
    return -1 if leg_type == "Short" else 1


def _opt_match_key(ticker: str, strike, expiry, option_type: str, leg_type: str) -> tuple:
    """Rovnaká logika ako pri párovaní Greeks – exp YYYYMMDD aj YYYY-MM-DD."""
    exp = normalize_expiry(str(expiry or ""))
    exp_c = exp.replace("-", "")
    ot = str(option_type or "").strip()
    if ot.lower().startswith("c"):
        ot_norm = "Call"
    elif ot.lower().startswith("p"):
        ot_norm = "Put"
    else:
        ot_norm = ot
    lt = str(leg_type or "").strip().capitalize()
    if lt not in ("Long", "Short"):
        lt = str(leg_type or "")
    return (
        str(ticker).upper(),
        round(float(strike or 0), 4),
        exp_c,
        ot_norm,
        lt,
    )


def _bs_value(t: dict, spot: float, iv: float) -> Optional[float]:
    """Teoretická hodnota jednej nohy (za celý kontrakt)."""
    dte_val = _dte(t.get("expiry", ""))
    if dte_val <= 0:
        return None
    right = "C" if t.get("option_type", "Call") == "Call" else "P"
    price = bs_price(spot, t.get("strike", 0), dte_val, iv, right)
    if price is None:
        return None
    contracts = float(t.get("contracts", 1) or 1)
    sign = _leg_sign(t.get("leg_type", "Long"))
    entry_p = t.get("entry_price", 0) or 0
    unrealized = sign * (entry_p - price) * contracts * 100
    return unrealized


def _exp_value(t: dict, spot_at_exp: float) -> float:
    """P&L pri expirácii nohy pri danom spote."""
    contracts = float(t.get("contracts", 1) or 1)
    entry_p = float(t.get("entry_price", 0) or 0)
    strike = float(t.get("strike", 0) or 0)
    leg = t.get("leg_type", "Long")
    opt = t.get("option_type", "Call")

    if leg == "Short":
        if opt == "Call":
            intrinsic = max(0.0, spot_at_exp - strike)
        else:
            intrinsic = max(0.0, strike - spot_at_exp)
        pnl = (entry_p - intrinsic) * contracts * 100
    else:
        if opt == "Call":
            intrinsic = max(0.0, spot_at_exp - strike)
        else:
            intrinsic = max(0.0, strike - spot_at_exp)
        pnl = (intrinsic - entry_p) * contracts * 100
    return pnl


# ─── Vstupné parametre ────────────────────────────────────────────────────────
with st.expander("Parametre výpočtu (spot, IV, filter)", expanded=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        spot_input = st.number_input(
            "Aktuálna cena podkladu ($)",
            min_value=0.0, step=0.5, value=st.session_state.get("pf_spot", 0.0),
            key="pf_spot",
            help="0 = pri zadanom tickery sa pokúsi načítať z IBKR",
        )
    with c2:
        iv_input = st.number_input(
            "IV (napr. 0.45 = 45%)",
            min_value=0.0, max_value=5.0, step=0.01,
            value=st.session_state.get("pf_iv", 0.45),
            key="pf_iv",
        )
    with c3:
        ticker_filter = st.text_input(
            "Ticker (prázdne = všetky)",
            value=st.session_state.get("pf_ticker", ""),
            key="pf_ticker",
        ).upper().strip()

    if ibkr.is_connected() and spot_input == 0 and ticker_filter:
        with st.spinner(f"Načítavam spot pre {ticker_filter}..."):
            res = ibkr.fetch_underlying(ticker_filter)
        if not res.get("error") and res.get("price"):
            spot_input = res["price"]
            st.session_state["pf_spot"] = spot_input
            st.caption(f"Spot z IBKR: **${spot_input:.2f}**")

spot = spot_input
iv = iv_input

_tick_lbl = ticker_filter if ticker_filter else "všetky tickery"
if spot > 0 and iv > 0:
    _model_lbl = (
        "Black–Scholes; kde sedí párovanie s TWS, použije sa **live Unrealized** z IBKR."
        if ibkr.is_connected()
        else "Black–Scholes (bez IBKR nie je live P&L)."
    )
else:
    _model_lbl = "bez BS — zadaj Spot a IV pre odhad nerealizovaného P&L (inak 0)."
st.caption(f"**Aktívny filter:** {_tick_lbl}. **Odhad otvorených:** {_model_lbl}")

# ─── Dáta ─────────────────────────────────────────────────────────────────────
all_trades = db.get_all_trades()
open_trades = [t for t in all_trades if t["status"] == "Open"]
closed_trades = [t for t in all_trades if t["status"] == "Closed"]

if ticker_filter:
    open_trades = [t for t in open_trades if t["ticker"].upper() == ticker_filter]
    closed_trades = [t for t in closed_trades if t["ticker"].upper() == ticker_filter]

scope_trades = [t for t in all_trades if (not ticker_filter or t["ticker"].upper() == ticker_filter)]

# ─── Výpočet metrík ───────────────────────────────────────────────────────────
realized_pnl = sum(db.compute_pnl(t) or 0 for t in closed_trades)
total_commission = sum(
    (t.get("commission") or 0) for t in all_trades
    if (not ticker_filter or t["ticker"].upper() == ticker_filter)
)

unrealized_pnl = 0.0
unrealized_by_trade: dict[int, float] = {}

ibkr_prices: dict[tuple, float] = {}
if ibkr.is_connected():
    live = ibkr.fetch_positions()
    if not live.get("error"):
        for p in live["positions"]:
            if p["sec_type"] == "OPT":
                key = _opt_match_key(
                    p["ticker"],
                    p.get("strike"),
                    p.get("expiry"),
                    p.get("option_type", ""),
                    p.get("leg_type", ""),
                )
                ibkr_prices[key] = float(p.get("unrealized_pnl", 0) or 0)

for t in open_trades:
    key = _opt_match_key(
        t["ticker"],
        t.get("strike"),
        t.get("expiry"),
        t.get("option_type", ""),
        t.get("leg_type", ""),
    )
    if key in ibkr_prices:
        val = ibkr_prices[key]
    elif spot > 0 and iv > 0:
        val = _bs_value(t, spot, iv) or 0.0
    else:
        val = 0.0
    unrealized_by_trade[int(t["id"])] = val
    unrealized_pnl += val

pf_apr = compute_simple_apr(scope_trades, unrealized_by_trade)

exp_pnl_max = 0.0
for t in open_trades:
    contracts = float(t.get("contracts", 1) or 1)
    entry_p = float(t.get("entry_price", 0) or 0)
    leg = t.get("leg_type", "Long")
    if leg == "Short":
        exp_pnl_max += entry_p * contracts * 100
    else:
        if spot > 0 and iv > 0:
            dte_v = _dte(t.get("expiry", ""))
            right = "C" if t.get("option_type", "Call") == "Call" else "P"
            bs_val = bs_price(spot, t.get("strike", 0), dte_v, iv, right)
            if bs_val:
                exp_pnl_max += (bs_val - entry_p) * contracts * 100

port_delta = 0.0
port_theta = 0.0
port_vega = 0.0
greeks_rows = []

for t in open_trades:
    if spot <= 0 or iv <= 0:
        break
    dte_v = _dte(t.get("expiry", ""))
    if dte_v <= 0:
        continue
    right = "C" if t.get("option_type", "Call") == "Call" else "P"
    g = calc_greeks(spot, t.get("strike", 0), dte_v, iv, right)
    contracts = float(t.get("contracts", 1) or 1)
    sign = _leg_sign(t.get("leg_type", "Long"))

    d_dollar = (g["delta"] or 0) * sign * contracts * 100
    th_dollar = (g["theta"] or 0) * sign * contracts * 100
    vg_dollar = (g["vega"] or 0) * sign * contracts * 100

    port_delta += d_dollar
    port_theta += th_dollar
    port_vega += vg_dollar

    greeks_rows.append({
        "ID": t["id"],
        "Ticker": t["ticker"],
        "Noha": t.get("leg_type", ""),
        "Typ": t.get("option_type", ""),
        "Strike": t.get("strike"),
        "Expiry": t.get("expiry", ""),
        "DTE": dte_v,
        "Group": t.get("group_id", "") or "—",
        "Delta $": round(d_dollar, 0),
        "Theta $/deň": round(th_dollar, 2),
        "Vega $/%IV": round(vg_dollar, 2),
        "Unrealized P&L": round(unrealized_by_trade.get(int(t["id"]), 0), 0),
    })

# ─── Agregácia skupín (zdieľané tabmi) ────────────────────────────────────────
groups_map: dict[str, list] = {}
for t in all_trades:
    if ticker_filter and t["ticker"].upper() != ticker_filter:
        continue
    gid = (t.get("group_id") or "").strip() or "— (bez skupiny)"
    groups_map.setdefault(gid, []).append(t)

group_summary_rows = []
for gid, legs in sorted(groups_map.items()):
    open_l = [t for t in legs if t["status"] == "Open"]
    closed_l = [t for t in legs if t["status"] == "Closed"]
    r_pnl = sum(db.compute_pnl(t) or 0 for t in closed_l)
    u_pnl = sum(unrealized_by_trade.get(int(t["id"]), 0) for t in open_l)
    g_comm = sum(t.get("commission") or 0 for t in legs)

    g_exp = 0.0
    for t in open_l:
        contracts = int(t.get("contracts", 1))
        entry_p = float(t.get("entry_price", 0) or 0)
        leg = t.get("leg_type", "Long")
        if leg == "Short":
            g_exp += entry_p * contracts * 100
        elif spot > 0 and iv > 0:
            dte_v = _dte(t.get("expiry", ""))
            right = "C" if t.get("option_type", "Call") == "Call" else "P"
            bs_val = bs_price(spot, t.get("strike", 0), dte_v, iv, right)
            if bs_val:
                g_exp += (bs_val - entry_p) * contracts * 100

    short_dtes = [_dte(t.get("expiry", "")) for t in open_l if t.get("leg_type") == "Short"]
    min_dte = min(short_dtes) if short_dtes else None

    g_theta = sum(
        (calc_greeks(spot, t.get("strike", 0), _dte(t.get("expiry", "")), iv,
                     "C" if t.get("option_type", "Call") == "Call" else "P")
         .get("theta", 0) or 0)
        * _leg_sign(t.get("leg_type", "Long"))
        * int(t.get("contracts", 1)) * 100
        for t in open_l
        if spot > 0 and iv > 0 and _dte(t.get("expiry", "")) > 0
    )

    g_apr = compute_simple_apr(legs, unrealized_by_trade)
    apr_cell = round(g_apr["apr_pct"], 1) if g_apr is not None else None

    group_summary_rows.append({
        "Skupina": gid,
        "Otvorené": len(open_l),
        "Uzavreté": len(closed_l),
        "Realized P&L $": round(r_pnl, 0),
        "Unrealized P&L $": round(u_pnl, 0),
        "Celkom P&L $": round(r_pnl + u_pnl, 0),
        "APR % (rči.)": apr_cell,
        "Pri exp. max $": round(g_exp, 0),
        "Theta $/deň": round(g_theta, 2) if (spot > 0 and iv > 0) else None,
        "DTE short": min_dte,
        "Komisie $": round(g_comm, 2),
    })

open_groups_n = open_groups_count(groups_map)
min_short_dte_pf = min_short_dte_open(open_trades, _dte)

# ─── Záložky ──────────────────────────────────────────────────────────────────
tab_skupiny, tab_greky, tab_cas, tab_hist = st.tabs(
    ["Skupiny a expozícia", "Greky a scenáre", "Časová os (DTE)", "História a súhrn"]
)

# ─── Tab: Skupiny ─────────────────────────────────────────────────────────────
with tab_skupiny:
    st.subheader("Otvorená kniha — súhrn")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric(
        "Nerealizovaný P&L",
        f"${unrealized_pnl:+,.0f}",
        help="Mark-to-market otvorených nôh (IBKR alebo BS).",
    )
    s2.metric(
        "Skupín s otvorenými",
        f"{open_groups_n}",
        help="Počet group_id, ktoré majú aspoň jednu otvorenú nohu.",
    )
    s3.metric(
        "Min. DTE (short)",
        f"{min_short_dte_pf}" if min_short_dte_pf is not None else "—",
        help="Najkratšia expirácia medzi otvorenými short nohami.",
    )
    s4.metric(
        "Pri exp. shortov (max)",
        f"${exp_pnl_max:+,.0f}",
        help="Scenár: všetky shorty exspirujú bezcenné (orientačné).",
    )

    st.subheader("Prehľad skupín")
    if group_summary_rows:
        df_groups = pd.DataFrame(group_summary_rows)
        st.dataframe(
            df_groups,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Realized P&L $": st.column_config.NumberColumn(format="$%+d"),
                "Unrealized P&L $": st.column_config.NumberColumn(format="$%+d"),
                "Celkom P&L $": st.column_config.NumberColumn(format="$%+d"),
                "APR % (rči.)": st.column_config.NumberColumn(
                    format="%+.1f %%",
                    help="Orientačné APR; rovnaká logika ako v záložke História.",
                ),
                "Pri exp. max $": st.column_config.NumberColumn(format="$%+d"),
                "Theta $/deň": st.column_config.NumberColumn(format="$%.2f"),
                "Komisie $": st.column_config.NumberColumn(format="$%.2f"),
                "DTE short": st.column_config.NumberColumn(
                    help="Dni do expirácie najkratšej short nohy v skupine",
                ),
            },
        )
        st.caption(
            "**Theta** a **Pri exp. max** vyžadujú zadaný spot a IV v parametroch. "
            "**APR %** je orientačné (viac nôh v skupine zväčší menovateľ)."
        )
    else:
        st.info("Žiadne skupiny v aktuálnom filtri.")

    if open_trades:
        st.subheader("Detail otvorených nôh")
        detail_rows = []
        for t in open_trades:
            dte_v = _dte(t.get("expiry", ""))
            u_pnl = unrealized_by_trade.get(int(t["id"]), 0)
            contracts = int(t.get("contracts", 1))
            entry_p = float(t.get("entry_price", 0) or 0)
            if t.get("leg_type") == "Short":
                max_gain = entry_p * contracts * 100
            else:
                max_gain = None
            detail_rows.append({
                "ID": t["id"],
                "Group": t.get("group_id", "") or "—",
                "Ticker": t["ticker"],
                "Noha": t.get("leg_type", ""),
                "Typ": t.get("option_type", ""),
                "Strike": t.get("strike"),
                "Expiry": t.get("expiry", ""),
                "DTE": dte_v,
                "Kontr.": contracts,
                "Entry $": entry_p,
                "Unrealized P&L": round(u_pnl, 0),
                "Max zisk $": round(max_gain, 0) if max_gain is not None else None,
                "Entry dátum": t.get("entry_date", ""),
            })
        st.dataframe(
            pd.DataFrame(detail_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Strike": st.column_config.NumberColumn(format="$%.0f"),
                "Entry $": st.column_config.NumberColumn(format="$%.2f"),
                "Unrealized P&L": st.column_config.NumberColumn(format="$%+.0f"),
                "Max zisk $": st.column_config.NumberColumn(format="$%.0f"),
            },
        )
    else:
        st.info("Žiadne otvorené pozície v tomto filtri.")

# ─── Tab: Greky a scenáre ─────────────────────────────────────────────────────
with tab_greky:
    if ticker_filter:
        st.caption(f"Greky a scenáre sa počítajú pre **otvorené** nohy s filtrom ticker **{ticker_filter}**.")
    else:
        st.caption("Greky a scenáre zahŕňajú **všetky** otvorené nohy v denníku (jeden zadaný spot + IV pre všetky).")

    if spot > 0 and iv > 0 and greeks_rows:
        g1, g2, g3 = st.columns(3)
        g1.metric(
            "Celková Delta ($)",
            f"${port_delta:+,.0f}",
            help="Smerová expozícia pri zadanom spote a IV.",
        )
        g2.metric(
            "Celková Theta ($/deň)",
            f"${port_theta:+,.2f}",
            help="Časový rozpad (BS).",
        )
        g3.metric(
            "Celková Vega ($/1%IV)",
            f"${port_vega:+,.2f}",
            help="Citlivosť na zmenu impl. volatility.",
        )
        with st.expander("Detail grekov po nohách"):
            st.dataframe(
                pd.DataFrame(greeks_rows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Strike": st.column_config.NumberColumn(format="$%.0f"),
                    "Delta $": st.column_config.NumberColumn(format="$%+.0f"),
                    "Theta $/deň": st.column_config.NumberColumn(format="$%+.2f"),
                    "Vega $/%IV": st.column_config.NumberColumn(format="$%+.2f"),
                    "Unrealized P&L": st.column_config.NumberColumn(format="$%+.0f"),
                },
            )
    else:
        st.warning("Zadaj **Spot** a **IV** v parametroch hore, aby sa vypočítali Greky.")

    if spot > 0 and iv > 0 and open_trades:
        st.subheader("Scenárová analýza — P&L pri rôznych cenách podkladu")
        st.caption(
            "Očakávaný P&L otvorených nôh, ak by podklad bol na danej úrovni **v deň expirácie** (intrinsic vs. vstupná prémia)."
        )
        pct_steps = [-20, -15, -10, -7.5, -5, -2.5, 0, +2.5, +5, +7.5, +10, +15, +20]
        spot_levels = [round(spot * (1 + p / 100), 2) for p in pct_steps]
        scenario_rows = []
        for slevel in spot_levels:
            total_exp = sum(_exp_value(t, slevel) for t in open_trades)
            pct = (slevel / spot - 1) * 100
            scenario_rows.append({
                "Cena podkladu $": slevel,
                "Zmena %": round(pct, 1),
                "P&L pri expirácii $": round(total_exp, 0),
            })
        df_scen = pd.DataFrame(scenario_rows)
        fig_scen = go.Figure()
        colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in df_scen["P&L pri expirácii $"]]
        fig_scen.add_trace(go.Bar(
            x=df_scen["Cena podkladu $"],
            y=df_scen["P&L pri expirácii $"],
            marker_color=colors,
            text=[f"${v:+,.0f}" for v in df_scen["P&L pri expirácii $"]],
            textposition="outside",
            hovertemplate="Podklad: $%{x:.2f}<br>P&L: $%{y:+,.0f}<extra></extra>",
        ))
        fig_scen.add_vline(
            x=spot, line_dash="dash", line_color="#f39c12",
            annotation_text=f"Spot ${spot:.0f}", annotation_position="top right",
        )
        fig_scen.add_hline(y=0, line_color="gray", line_width=1)
        fig_scen.update_layout(
            height=380,
            xaxis_title="Cena podkladu pri expirácii",
            yaxis_title="P&L ($)",
            margin=dict(l=10, r=10, t=30, b=40),
            showlegend=False,
        )
        st.plotly_chart(fig_scen, use_container_width=True)
        with st.expander("Tabuľka scenárov"):
            st.dataframe(
                df_scen,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Cena podkladu $": st.column_config.NumberColumn(format="$%.2f"),
                    "Zmena %": st.column_config.NumberColumn(format="%.1f%%"),
                    "P&L pri expirácii $": st.column_config.NumberColumn(format="$%+d"),
                },
            )
    elif open_trades:
        st.info("Pre graf scenárov zadaj Spot a IV.")

# ─── Tab: Časová os ───────────────────────────────────────────────────────────
with tab_cas:
    if open_trades:
        st.subheader("Časová os pozícií (DTE)")
        gantt_rows = []
        for t in open_trades:
            entry_d = t.get("entry_date") or date.today().isoformat()
            expiry_str = t.get("expiry", "")
            if not expiry_str:
                continue
            try:
                exp_d = date.fromisoformat(
                    f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:8]}"
                    if len(expiry_str) == 8 else expiry_str
                ).isoformat()
            except Exception:
                continue
            dte_v = _dte(expiry_str)
            label = (
                f"#{t['id']} {t['ticker']} "
                f"{'▼' if t.get('leg_type') == 'Short' else '▲'}"
                f"{t.get('option_type', '')[0]} ${t.get('strike', 0):.0f} "
                f"({t.get('group_id', '') or '—'})"
            )
            color = "#e74c3c" if t.get("leg_type") == "Short" else "#3498db"
            gantt_rows.append({
                "Pozícia": label,
                "Start": entry_d,
                "End": exp_d,
                "DTE": dte_v,
                "Color": color,
            })
        if gantt_rows:
            df_gantt = pd.DataFrame(gantt_rows).sort_values("End")
            fig_gantt = go.Figure()
            today_str = date.today().isoformat()
            for _, row in df_gantt.iterrows():
                fig_gantt.add_trace(go.Bar(
                    name=row["Pozícia"],
                    x=[row["End"]],
                    y=[row["Pozícia"]],
                    orientation="h",
                    base=[row["Start"]],
                    marker_color=row["Color"],
                    opacity=0.75,
                    hovertemplate=(
                        f"<b>{row['Pozícia']}</b><br>"
                        f"Entry: {row['Start']}<br>"
                        f"Expiry: {row['End']}<br>"
                        f"DTE: {row['DTE']} dní<extra></extra>"
                    ),
                    width=0.6,
                ))
            fig_gantt.add_shape(
                type="line",
                x0=today_str, x1=today_str,
                y0=-0.5, y1=len(gantt_rows) - 0.5,
                line=dict(dash="dash", color="#f39c12", width=2),
            )
            fig_gantt.add_annotation(
                x=today_str, y=len(gantt_rows) - 0.5,
                text="Dnes", showarrow=False,
                font=dict(color="#f39c12", size=11),
                xanchor="left", yanchor="bottom",
            )
            fig_gantt.update_layout(
                barmode="overlay",
                height=max(200, len(gantt_rows) * 55 + 80),
                xaxis_title=None,
                yaxis_title=None,
                showlegend=False,
                margin=dict(l=10, r=10, t=20, b=20),
                xaxis=dict(type="date"),
            )
            st.plotly_chart(fig_gantt, use_container_width=True)
            st.caption("Červená = short noha, modrá = long. Oranžová čiara = dnes.")
        else:
            st.info("Nedajú sa vykresliť expirácie (skontroluj formát expirácie v denníku).")
    else:
        st.info("Žiadne otvorené pozície — časová os je prázdna.")

# ─── Tab: História a súhrn ────────────────────────────────────────────────────
with tab_hist:
    st.subheader("Účtovný súhrn a porovnanie")
    st.caption(
        "Realizovaný P&L, komisie a APR pracujú s **filtrovaným** rozsahom denníka. "
        "Ide o súhrn, nie náhradu za úplné účtovníctvo."
    )
    h1, h2, h3, h4 = st.columns(4)
    h1.metric("Realizovaný P&L", f"${realized_pnl:+,.0f}", help="Uzavreté obchody v rozsahu filtra.")
    h2.metric("Nerealizovaný P&L", f"${unrealized_pnl:+,.0f}", help="Rovnaký ako v záložke Skupiny.")
    h3.metric(
        "Celkový P&L",
        f"${(realized_pnl + unrealized_pnl):+,.0f}",
        delta=f"komisie: -${total_commission:.2f}",
    )
    h4.metric(
        "Pri exp. shortov (max)",
        f"${exp_pnl_max:+,.0f}",
        help="Rovnaký scenár ako v záložke Skupiny.",
    )

    if pf_apr is not None:
        st.metric(
            "APR portfólia (orientačné)",
            f"{pf_apr['apr_pct']:+.1f}%",
            help=(
                "(Realizovaný + nerealizovaný P&L) / súčet |prémia|×100×kontrakty × (365 / dni). "
                "Nie je broker ROC."
            ),
        )
    else:
        st.metric("APR portfólia (orientačné)", "—")

    st.page_link("pages/trade_log.py", label="Otvoriť Trade Log (detail záznamov)", icon=":material/edit_note:")

    if ibkr.is_connected():
        with st.expander("Live pozície z TWS (porovnanie s brokerom)", expanded=False):
            _lr = ibkr.fetch_positions(with_greeks=False, use_mkt_snapshot=True)
            if _lr.get("error"):
                st.warning(_lr["error"])
            elif not _lr.get("positions"):
                st.info("IBKR nevrátil žiadne pozície.")
            else:
                _rows = []
                for _p in _lr["positions"]:
                    _q = float(_p.get("contracts") or 0)
                    _q_s = int(_q) if abs(_q - round(_q)) < 1e-6 else round(_q, 4)
                    _rows.append({
                        "Ticker": _p.get("ticker"),
                        "Typ": _p.get("sec_type"),
                        "L/S": _p.get("leg_type"),
                        "Množstvo": _q_s,
                        "Strike": _p.get("strike"),
                        "Expiry": _p.get("expiry"),
                        "Trh. cena": round(float(_p.get("market_price") or 0), 2),
                        "Unreal. P/L": round(float(_p.get("unrealized_pnl") or 0), 1),
                    })
                st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
                st.caption(
                    "Súčet stĺpca Unreal. P/L by mal sedieť s TWS. P&L v denníku sa priradí len pri zhode kľúča nôh."
                )
    else:
        st.caption("Pre live tabuľku z TWS pripoj IBKR (Dashboard → Pripojenie).")
