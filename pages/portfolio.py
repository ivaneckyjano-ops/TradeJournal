"""
Portfolio Command Center — celkový prehľad portfólia.

Zobrazuje:
- Realizovaný P&L (uzavreté obchody, po skupinách)
- Nerealizovaný P&L (otvorené pozície, live z IBKR alebo BS odhad)
- Scenár pri expirácii shortov (max profit scenario)
- Portfóliové greky (Delta, Theta/deň, Vega)
- Scenárová analýza (čo ak sa podklad pohne o ±X%)
- DTE vizualizácia (Gantt bar pre každú pozíciu)
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
from datetime import date, datetime
from typing import Optional

from core import database as db
from core import ibkr
from core.probability import bs_price, calc_greeks, calc_sd_lines

db.init_db()

st.title("Portfolio Command Center")

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


def _bs_value(t: dict, spot: float, iv: float) -> Optional[float]:
    """Teoretická hodnota jednej nohy (za celý kontrakt)."""
    dte_val = _dte(t.get("expiry", ""))
    if dte_val <= 0:
        return None
    right = "C" if t.get("option_type", "Call") == "Call" else "P"
    price = bs_price(spot, t.get("strike", 0), dte_val, iv, right)
    if price is None:
        return None
    contracts = int(t.get("contracts", 1))
    sign = _leg_sign(t.get("leg_type", "Long"))
    entry_p = t.get("entry_price", 0) or 0
    # Nerealizovaný P&L = (aktuálna_hodnota - entry) × kontrakt × 100 × sign
    unrealized = sign * (entry_p - price) * contracts * 100
    return unrealized


def _exp_value(t: dict, spot_at_exp: float) -> float:
    """
    P&L pri expirácii short nohy.
    Short Call: max profit ak spot < strike → zarobíš celú prémiu
    Short Put:  max profit ak spot > strike
    Long: hodnota závisí od spotu
    """
    contracts = int(t.get("contracts", 1))
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
    else:  # Long
        if opt == "Call":
            intrinsic = max(0.0, spot_at_exp - strike)
        else:
            intrinsic = max(0.0, strike - spot_at_exp)
        pnl = (intrinsic - entry_p) * contracts * 100
    return pnl


# ─── Vstupné parametre ────────────────────────────────────────────────────────
with st.expander("Parametre výpočtu", expanded=True):
    c1, c2, c3 = st.columns(3)
    with c1:
        spot_input = st.number_input(
            "Aktuálna cena podkladu ($)",
            min_value=0.0, step=0.5, value=st.session_state.get("pf_spot", 0.0),
            key="pf_spot",
            help="0 = pokúsi sa načítať z IBKR"
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
iv   = iv_input

# ─── Dáta ─────────────────────────────────────────────────────────────────────
all_trades    = db.get_all_trades()
open_trades   = [t for t in all_trades if t["status"] == "Open"]
closed_trades = [t for t in all_trades if t["status"] == "Closed"]

if ticker_filter:
    open_trades   = [t for t in open_trades   if t["ticker"].upper() == ticker_filter]
    closed_trades = [t for t in closed_trades if t["ticker"].upper() == ticker_filter]

# ─── Výpočet metrík ───────────────────────────────────────────────────────────
realized_pnl = sum(db.compute_pnl(t) or 0 for t in closed_trades)
total_commission = sum((t.get("commission") or 0) for t in all_trades
                       if (not ticker_filter or t["ticker"].upper() == ticker_filter))

# Nerealizovaný P&L (BS alebo IBKR live)
unrealized_pnl = 0.0
unrealized_by_trade: dict[int, float] = {}

# Skús IBKR live ceny
ibkr_prices: dict[tuple, float] = {}
if ibkr.is_connected():
    live = ibkr.fetch_positions()
    if not live.get("error"):
        for p in live["positions"]:
            if p["sec_type"] == "OPT":
                key = (
                    p["ticker"].upper(),
                    str(p.get("strike", "")),
                    str(p.get("expiry", "")),
                    p.get("option_type", ""),
                    p.get("leg_type", ""),
                )
                ibkr_prices[key] = float(p.get("unrealized_pnl", 0) or 0)

for t in open_trades:
    key = (
        t["ticker"].upper(),
        str(t.get("strike", "")),
        str(t.get("expiry", "")),
        t.get("option_type", ""),
        t.get("leg_type", ""),
    )
    if key in ibkr_prices:
        val = ibkr_prices[key]
    elif spot > 0 and iv > 0:
        val = _bs_value(t, spot, iv) or 0.0
    else:
        val = 0.0
    unrealized_by_trade[t["id"]] = val
    unrealized_pnl += val

# P&L pri expirácii všetkých shortov (max profit scenár)
exp_pnl_max = 0.0  # shorty exspirujú bezcenné, longy udržíme
exp_pnl_spot = 0.0  # pri aktuálnom spote

for t in open_trades:
    # Max profit: short exspiruje bezcenný (intrinsic=0), long predáme za BS
    contracts  = int(t.get("contracts", 1))
    entry_p    = float(t.get("entry_price", 0) or 0)
    leg        = t.get("leg_type", "Long")
    if leg == "Short":
        exp_pnl_max += entry_p * contracts * 100
    else:  # Long: pri max profit scenári shortu predpokladáme long stále má BS hodnotu
        if spot > 0 and iv > 0:
            dte_v = _dte(t.get("expiry", ""))
            right = "C" if t.get("option_type", "Call") == "Call" else "P"
            bs_val = bs_price(spot, t.get("strike", 0), dte_v, iv, right)
            if bs_val:
                exp_pnl_max += (bs_val - entry_p) * contracts * 100
    # Pri aktuálnom spote
    exp_pnl_spot += _exp_value(t, spot) if spot > 0 else 0.0

# Greky portfólia
port_delta = 0.0
port_theta = 0.0
port_vega  = 0.0
greeks_rows = []

for t in open_trades:
    if spot <= 0 or iv <= 0:
        break
    dte_v = _dte(t.get("expiry", ""))
    if dte_v <= 0:
        continue
    right = "C" if t.get("option_type", "Call") == "Call" else "P"
    g = calc_greeks(spot, t.get("strike", 0), dte_v, iv, right)
    contracts = int(t.get("contracts", 1))
    sign = _leg_sign(t.get("leg_type", "Long"))

    d_dollar = (g["delta"] or 0) * sign * contracts * 100
    th_dollar = (g["theta"] or 0) * sign * contracts * 100
    vg_dollar = (g["vega"] or 0) * sign * contracts * 100

    port_delta += d_dollar
    port_theta += th_dollar
    port_vega  += vg_dollar

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
        "Unrealized P&L": round(unrealized_by_trade.get(t["id"], 0), 0),
    })

st.divider()

# ─── Blok 1: TOP METRIKY ──────────────────────────────────────────────────────
st.subheader("Celkový stav portfólia")

m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric(
    "Realizovaný P&L",
    f"${realized_pnl:+,.0f}",
    help="Čistý P&L uzavretých obchodov (po komisiách)",
)
m2.metric(
    "Nerealizovaný P&L",
    f"${unrealized_pnl:+,.0f}",
    help="Aktuálna mark-to-market hodnota otvorených pozícií (IBKR live alebo BS odhad)",
)
m3.metric(
    "Celkový P&L",
    f"${(realized_pnl + unrealized_pnl):+,.0f}",
    delta=f"komisie: -${total_commission:.2f}",
    help="Realizovaný + nerealizovaný",
)
m4.metric(
    "Pri exp. shortov (max)",
    f"${exp_pnl_max:+,.0f}",
    help="Ak všetky shorty exspirujú bezcenné — maximálny zisk z otvorených pozícií",
)
if spot > 0 and iv > 0:
    m5.metric(
        "Theta / deň ($)",
        f"${port_theta:+,.2f}",
        help="Denný zisk/strata z plynutia času (celé portfólio)",
    )
    m6.metric(
        "Delta portfólia ($)",
        f"${port_delta:+,.0f}",
        help="Smerová expozícia — koľko zarobíš/stratíš ak podklad +$1",
    )
else:
    m5.metric("Theta / deň ($)", "—", help="Zadaj Spot a IV pre výpočet")
    m6.metric("Delta portfólia ($)", "—", help="Zadaj Spot a IV pre výpočet")

st.divider()

# ─── Blok 2: SKUPINY — detailný prehľad ──────────────────────────────────────
st.subheader("Prehľad skupín")

groups_map: dict[str, list] = {}
for t in all_trades:
    if ticker_filter and t["ticker"].upper() != ticker_filter:
        continue
    gid = (t.get("group_id") or "").strip() or "— (bez skupiny)"
    groups_map.setdefault(gid, []).append(t)

group_summary_rows = []
for gid, legs in sorted(groups_map.items()):
    open_l   = [t for t in legs if t["status"] == "Open"]
    closed_l = [t for t in legs if t["status"] == "Closed"]
    r_pnl    = sum(db.compute_pnl(t) or 0 for t in closed_l)
    u_pnl    = sum(unrealized_by_trade.get(t["id"], 0) for t in open_l)
    g_comm   = sum(t.get("commission") or 0 for t in legs)

    # Max exp P&L pre skupinu
    g_exp = 0.0
    for t in open_l:
        contracts = int(t.get("contracts", 1))
        entry_p   = float(t.get("entry_price", 0) or 0)
        leg       = t.get("leg_type", "Long")
        if leg == "Short":
            g_exp += entry_p * contracts * 100
        elif spot > 0 and iv > 0:
            dte_v = _dte(t.get("expiry", ""))
            right = "C" if t.get("option_type", "Call") == "Call" else "P"
            bs_val = bs_price(spot, t.get("strike", 0), dte_v, iv, right)
            if bs_val:
                g_exp += (bs_val - entry_p) * contracts * 100

    # DTE najkratšej (short) nohy
    short_dtes = [_dte(t.get("expiry", "")) for t in open_l if t.get("leg_type") == "Short"]
    min_dte = min(short_dtes) if short_dtes else None

    # Theta skupiny
    g_theta = sum(
        (calc_greeks(spot, t.get("strike", 0), _dte(t.get("expiry", "")), iv,
                     "C" if t.get("option_type", "Call") == "Call" else "P")
         .get("theta", 0) or 0)
        * _leg_sign(t.get("leg_type", "Long"))
        * int(t.get("contracts", 1)) * 100
        for t in open_l
        if spot > 0 and iv > 0 and _dte(t.get("expiry", "")) > 0
    )

    group_summary_rows.append({
        "Skupina": gid,
        "Otvorené": len(open_l),
        "Uzavreté": len(closed_l),
        "Realized P&L $": round(r_pnl, 0),
        "Unrealized P&L $": round(u_pnl, 0),
        "Celkom P&L $": round(r_pnl + u_pnl, 0),
        "Pri exp. max $": round(g_exp, 0),
        "Theta $/deň": round(g_theta, 2) if (spot > 0 and iv > 0) else None,
        "DTE short": min_dte,
        "Komisie $": round(g_comm, 2),
    })

if group_summary_rows:
    df_groups = pd.DataFrame(group_summary_rows)
    st.dataframe(
        df_groups,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Realized P&L $":    st.column_config.NumberColumn(format="$%+d"),
            "Unrealized P&L $":  st.column_config.NumberColumn(format="$%+d"),
            "Celkom P&L $":      st.column_config.NumberColumn(format="$%+d"),
            "Pri exp. max $":    st.column_config.NumberColumn(format="$%+d"),
            "Theta $/deň":       st.column_config.NumberColumn(format="$%.2f"),
            "Komisie $":         st.column_config.NumberColumn(format="$%.2f"),
            "DTE short":         st.column_config.NumberColumn(help="Počet dní do expirácie najkratšej short nohy"),
        },
    )

st.divider()

# ─── Blok 3: GREKY PORTFÓLIA (iba ak máme spot+IV) ───────────────────────────
if spot > 0 and iv > 0 and greeks_rows:
    st.subheader("Greky portfólia — otvorené pozície")

    g1, g2, g3 = st.columns(3)
    g1.metric("Celková Delta ($)", f"${port_delta:+,.0f}",
              help="Smerová expozícia. Ak +$100 → pri pohybe podkladu +$1 zarobíš $100.")
    g2.metric("Celková Theta ($/deň)", f"${port_theta:+,.2f}",
              help="Časový rozpad. Každý deň ti portfólio zmení hodnotu o túto sumu.")
    g3.metric("Celková Vega ($/1%IV)", f"${port_vega:+,.2f}",
              help="Citlivosť na zmenu IV. Ak IV +1% → portfólio zmení hodnotu o túto sumu.")

    with st.expander("Detail grekov po nohách"):
        st.dataframe(
            pd.DataFrame(greeks_rows),
            use_container_width=True,
            hide_index=True,
            column_config={
                "Strike":          st.column_config.NumberColumn(format="$%.0f"),
                "Delta $":         st.column_config.NumberColumn(format="$%+.0f"),
                "Theta $/deň":     st.column_config.NumberColumn(format="$%+.2f"),
                "Vega $/%IV":      st.column_config.NumberColumn(format="$%+.2f"),
                "Unrealized P&L":  st.column_config.NumberColumn(format="$%+.0f"),
            },
        )
    st.divider()

# ─── Blok 4: SCENÁROVÁ ANALÝZA ────────────────────────────────────────────────
if spot > 0 and iv > 0 and open_trades:
    st.subheader("Scenárová analýza — P&L pri rôznych cenách podkladu")

    st.caption(
        "Tabuľka ukazuje očakávaný P&L otvorených pozícií "
        "ak podklad bude na danej úrovni v deň expirácie každého shortu."
    )

    # Generuj ceny podkladu ±20% od spotu v krokoch po 2.5%
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

    # Farby: kladné = zelené, záporné = červené
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
    # Vertikálna čiara pre aktuálny spot
    fig_scen.add_vline(x=spot, line_dash="dash", line_color="#f39c12",
                       annotation_text=f"Spot ${spot:.0f}", annotation_position="top right")
    fig_scen.add_hline(y=0, line_color="gray", line_width=1)
    fig_scen.update_layout(
        height=380,
        xaxis_title="Cena podkladu pri expirácii",
        yaxis_title="P&L ($)",
        margin=dict(l=10, r=10, t=30, b=40),
        showlegend=False,
    )
    st.plotly_chart(fig_scen, use_container_width=True)

    with st.expander("Zobraziť tabuľku scenárov"):
        st.dataframe(
            df_scen,
            use_container_width=True,
            hide_index=True,
            column_config={
                "Cena podkladu $":     st.column_config.NumberColumn(format="$%.2f"),
                "Zmena %":             st.column_config.NumberColumn(format="%.1f%%"),
                "P&L pri expirácii $": st.column_config.NumberColumn(format="$%+d"),
            },
        )
    st.divider()

# ─── Blok 5: DTE VIZUALIZÁCIA (Gantt) ────────────────────────────────────────
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
            f"{t.get('option_type','')[0]} ${t.get('strike',0):.0f} "
            f"({t.get('group_id','') or '—'})"
        )
        color = "#e74c3c" if t.get("leg_type") == "Short" else "#3498db"
        gantt_rows.append({
            "Pozícia": label,
            "Start": entry_d,
            "End": exp_d,
            "DTE": dte_v,
            "Color": color,
            "Leg": t.get("leg_type", ""),
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
        st.caption("🔴 Short nohy &nbsp;|&nbsp; 🔵 Long nohy &nbsp;|&nbsp; 🟠 Dnes")
    st.divider()

# ─── Blok 6: DETAIL OTVORENÝCH POZÍCIÍ ───────────────────────────────────────
if open_trades:
    st.subheader("Detail otvorených pozícií")

    detail_rows = []
    for t in open_trades:
        dte_v = _dte(t.get("expiry", ""))
        u_pnl = unrealized_by_trade.get(t["id"], 0)
        contracts = int(t.get("contracts", 1))
        entry_p = float(t.get("entry_price", 0) or 0)
        # Maximálny zisk z tejto nohy
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
            "Strike":         st.column_config.NumberColumn(format="$%.0f"),
            "Entry $":        st.column_config.NumberColumn(format="$%.2f"),
            "Unrealized P&L": st.column_config.NumberColumn(format="$%+.0f"),
            "Max zisk $":     st.column_config.NumberColumn(format="$%.0f"),
        },
    )
