"""
Portfolio Command Center — analýza otvorenej knihy z denníka.

Záložky: skupiny a expozícia, Greky a APR z Thety, časová os (DTE), história a súhrn (P&L, APR, TWS).
"""
from __future__ import annotations

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import date, timedelta
from typing import Optional

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page
from core.portfolio_data import normalize_expiry, compute_simple_apr, net_open_debit_capital_usd
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


# ─── Vstupné parametre ────────────────────────────────────────────────────────
# Zisti unikátne tickery z otvorených pozícií pre per-ticker Spot/IV
_all_open_for_tickers = [t for t in db.get_all_trades() if t["status"] == "Open"]
_unique_tickers: list[str] = sorted({t["ticker"].upper() for t in _all_open_for_tickers if t.get("ticker")})

with st.expander("Parametre výpočtu (spot, IV, filter)", expanded=True):
    # Ticker filter
    ticker_filter = st.text_input(
        "Filter — Ticker (prázdne = všetky)",
        value=st.session_state.get("pf_ticker", ""),
        key="pf_ticker",
    ).upper().strip()

    _tickers_to_show = [ticker_filter] if ticker_filter else _unique_tickers

    if not _tickers_to_show:
        _tickers_to_show = [""]

    st.caption("Zadaj **Spot** a **IV** pre každý ticker (0 = načítaj z IBKR ak je pripojený).")

    # Per-ticker Spot + IV vstupy
    _spot_iv_map: dict[str, tuple[float, float]] = {}  # ticker -> (spot, iv)
    _cols_per_row = 3
    for _i in range(0, len(_tickers_to_show), _cols_per_row):
        _row_tickers = _tickers_to_show[_i:_i + _cols_per_row]
        _row_cols = st.columns(len(_row_tickers) * 2)
        for _j, _tk in enumerate(_row_tickers):
            _sk_spot = f"pf_spot_{_tk}" if _tk else "pf_spot"
            _sk_iv   = f"pf_iv_{_tk}"   if _tk else "pf_iv"
            with _row_cols[_j * 2]:
                _sp = st.number_input(
                    f"Spot {_tk}" if _tk else "Spot ($)",
                    min_value=0.0, step=0.5,
                    value=float(st.session_state.get(_sk_spot, 0.0)),
                    key=_sk_spot,
                )
            with _row_cols[_j * 2 + 1]:
                _iv = st.number_input(
                    f"IV {_tk} (napr. 0.30)",
                    min_value=0.0, max_value=5.0, step=0.01,
                    value=float(st.session_state.get(_sk_iv, 0.45)),
                    key=_sk_iv,
                )
            # Načítaj Spot z IBKR ak je 0 a ticker je zadaný
            if _sp == 0 and _tk and ibkr.is_connected():
                with st.spinner(f"Načítavam spot pre {_tk}..."):
                    _res = ibkr.fetch_underlying(_tk)
                if not _res.get("error") and _res.get("price"):
                    _sp = _res["price"]
                    st.session_state[_sk_spot] = _sp
            _spot_iv_map[_tk] = (_sp, _iv)

# Spätná kompatibilita — globálny spot/iv pre jednoticker alebo fallback
if ticker_filter and ticker_filter in _spot_iv_map:
    spot, iv = _spot_iv_map[ticker_filter]
elif len(_spot_iv_map) == 1:
    spot, iv = next(iter(_spot_iv_map.values()))
else:
    # Pri viac tickeroch — fallback 0 (BS sa počíta per-trade nižšie)
    spot, iv = 0.0, 0.0

_tick_lbl = ticker_filter if ticker_filter else "všetky tickery"
_any_spot = any(s > 0 for s, _ in _spot_iv_map.values())
if _any_spot:
    _model_lbl = "Black–Scholes per ticker; kde sedí párovanie s TWS, použije sa live Unrealized z IBKR."
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
    live = ibkr.fetch_positions(use_historical_last=False)
    if not live.get("error"):
        _src_counts: dict[str, int] = {}
        for p in live["positions"]:
            _src = str(p.get("price_source") or "?")
            _src_counts[_src] = _src_counts.get(_src, 0) + 1
            if p["sec_type"] == "OPT":
                key = _opt_match_key(
                    p["ticker"],
                    p.get("strike"),
                    p.get("expiry"),
                    p.get("option_type", ""),
                    p.get("leg_type", ""),
                )
                ibkr_prices[key] = float(p.get("unrealized_pnl", 0) or 0)
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
            _src_txt = ", ".join(
                f"{_src_label.get(k, k)}: {v}"
                for k, v in sorted(_src_counts.items(), key=lambda x: (-x[1], x[0]))
            )
            st.caption(f"Zdroj live cien: {_src_txt}")

for t in open_trades:
    key = _opt_match_key(
        t["ticker"],
        t.get("strike"),
        t.get("expiry"),
        t.get("option_type", ""),
        t.get("leg_type", ""),
    )
    _tk = t["ticker"].upper()
    _t_spot, _t_iv = _spot_iv_map.get(_tk, _spot_iv_map.get("", (0.0, 0.45)))
    if key in ibkr_prices:
        val = ibkr_prices[key]
    elif _t_spot > 0 and _t_iv > 0:
        val = _bs_value(t, _t_spot, _t_iv) or 0.0
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
    _tk = t["ticker"].upper()
    _t_spot, _t_iv = _spot_iv_map.get(_tk, _spot_iv_map.get("", (0.0, 0.45)))
    if leg == "Short":
        exp_pnl_max += entry_p * contracts * 100
    else:
        if _t_spot > 0 and _t_iv > 0:
            dte_v = _dte(t.get("expiry", ""))
            right = "C" if t.get("option_type", "Call") == "Call" else "P"
            bs_val = bs_price(_t_spot, t.get("strike", 0), dte_v, _t_iv, right)
            if bs_val:
                exp_pnl_max += (bs_val - entry_p) * contracts * 100

port_delta = 0.0
port_theta = 0.0
port_vega = 0.0
greeks_rows = []

for t in open_trades:
    _tk = t["ticker"].upper()
    _t_spot, _t_iv = _spot_iv_map.get(_tk, _spot_iv_map.get("", (0.0, 0.45)))
    if _t_spot <= 0 or _t_iv <= 0:
        continue
    dte_v = _dte(t.get("expiry", ""))
    if dte_v <= 0:
        continue
    right = "C" if t.get("option_type", "Call") == "Call" else "P"
    g = calc_greeks(_t_spot, t.get("strike", 0), dte_v, _t_iv, right)
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
        else:
            _tk2 = t["ticker"].upper()
            _ts2, _ti2 = _spot_iv_map.get(_tk2, _spot_iv_map.get("", (0.0, 0.45)))
            if _ts2 > 0 and _ti2 > 0:
                dte_v = _dte(t.get("expiry", ""))
                right = "C" if t.get("option_type", "Call") == "Call" else "P"
                bs_val = bs_price(_ts2, t.get("strike", 0), dte_v, _ti2, right)
                if bs_val:
                    g_exp += (bs_val - entry_p) * contracts * 100

    short_dtes = [_dte(t.get("expiry", "")) for t in open_l if t.get("leg_type") == "Short"]
    min_dte = min(short_dtes) if short_dtes else None

    g_theta = sum(
        (calc_greeks(
            _spot_iv_map.get(t["ticker"].upper(), _spot_iv_map.get("", (0.0, 0.45)))[0],
            t.get("strike", 0),
            _dte(t.get("expiry", "")),
            _spot_iv_map.get(t["ticker"].upper(), _spot_iv_map.get("", (0.0, 0.45)))[1],
            "C" if t.get("option_type", "Call") == "Call" else "P",
        ).get("theta", 0) or 0)
        * _leg_sign(t.get("leg_type", "Long"))
        * int(t.get("contracts", 1)) * 100
        for t in open_l
        if _spot_iv_map.get(t["ticker"].upper(), (0.0, 0.0))[0] > 0
        and _dte(t.get("expiry", "")) > 0
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
    ["Skupiny a expozícia", "Greky a APR z Thety", "Časová os (DTE)", "História a súhrn"]
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

# ─── Tab: Greky a APR z Thety ─────────────────────────────────────────────────
with tab_greky:
    if ticker_filter:
        st.caption(f"Greky a APR z Thety pre **otvorené** nohy s filtrom ticker **{ticker_filter}**.")
    else:
        st.caption("Greky a APR z Thety pre **všetky** otvorené nohy v denníku (Spot + IV podľa tickera vyššie).")

    # ── Korekcia Theta / Vega z TWS ──────────────────────────────────────────
    st.caption("Zadaj hodnoty z TWS ak sa líšia od Black-Scholes (0 = použije BS výpočet).")
    ov_col1, ov_col2, ov_col3 = st.columns([2, 2, 1])
    with ov_col1:
        tws_theta_override = st.number_input(
            "Theta z TWS ($/deň)",
            value=float(st.session_state.get("pf_tws_theta_override", 0.0)),
            step=0.5,
            format="%.2f",
            key="pf_tws_theta_override",
            help="Súčet Theta z TWS pre všetky otvorené pozície (napr. +15.30). "
                 "Short opcii = + Theta (zbierajú čas. hodnotu).",
        )
    with ov_col2:
        tws_vega_override = st.number_input(
            "Vega z TWS ($/1%IV)",
            value=float(st.session_state.get("pf_tws_vega_override", 0.0)),
            step=1.0,
            format="%.2f",
            key="pf_tws_vega_override",
            help="Súčet Vega z TWS pre všetky otvorené pozície.",
        )
    with ov_col3:
        if st.button("Vymazať", key="pf_tws_greek_reset", help="Vráti na BS hodnoty"):
            st.session_state["pf_tws_theta_override"] = 0.0
            st.session_state["pf_tws_vega_override"] = 0.0
            st.rerun()

    # Použi TWS hodnoty ak sú zadané, inak BS
    has_tws_theta = tws_theta_override != 0.0
    has_tws_vega  = tws_vega_override  != 0.0
    display_theta = tws_theta_override if has_tws_theta else port_theta
    display_vega  = tws_vega_override  if has_tws_vega  else port_vega
    theta_src = "TWS" if has_tws_theta else "BS"
    vega_src  = "TWS" if has_tws_vega  else "BS"

    # Metriky — BS riadky môžu byť aj pri viacerých tickeroch (globálny spot môže byť 0)
    has_bs    = bool(greeks_rows)
    has_any   = has_bs or has_tws_theta or has_tws_vega

    if has_any:
        g1, g2, g3 = st.columns(3)
        if has_bs:
            g1.metric(
                "Celková Delta ($)",
                f"${port_delta:+,.0f}",
                help="Smerová expozícia pri zadanom spote a IV (BS).",
            )
        else:
            g1.caption("Delta: zadaj Spot a IV")

        g2.metric(
            f"Celková Theta ($/deň) [{theta_src}]",
            f"${display_theta:+,.2f}",
            delta=f"BS: ${port_theta:+.2f}" if (has_tws_theta and has_bs) else None,
            help="Časový rozpad. TWS hodnota má prednosť pred BS ak je zadaná.",
        )
        g3.metric(
            f"Celková Vega ($/1%IV) [{vega_src}]",
            f"${display_vega:+,.2f}",
            delta=f"BS: ${port_vega:+.2f}" if (has_tws_vega and has_bs) else None,
            help="Citlivosť na zmenu impl. volatility. TWS hodnota má prednosť pred BS ak je zadaná.",
        )
        if has_bs:
            with st.expander("Detail grekov po nohách (Black-Scholes)"):
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
        if not has_bs:
            st.caption("Pre Delta a detail po nohách zadaj Spot a IV v parametroch hore.")
    else:
        st.info("Zadaj hodnoty z TWS vyššie, alebo Spot + IV pre Black-Scholes výpočet.")

    st.divider()
    st.subheader("APR z Thety (ročný výnos)")
    st.caption(
        "Vzorec: (Θ USD/deň × 365 / (net debet z denníka + marža)) × 100. "
        "Θ je hodnota z metrík vyššie (TWS alebo Black–Scholes). Rovnaká logika ako na **TWS Dashboarde**."
    )
    _mm_apr_th = st.number_input(
        "Udržiavacia marža (USD, voliteľné)",
        min_value=0.0,
        step=50.0,
        format="%.0f",
        key="pf_apr_theta_margin",
        help="Z TWS (Margin Impact). Pripočíta sa k net debetu z Trade Logu ako celková báza nákladu.",
    )
    _net_debit_pf = net_open_debit_capital_usd(open_trades)
    if not open_trades:
        st.caption("Žiadne otvorené pozície v aktívnom filtri.")
    elif not has_any:
        st.info("Najprv zadaj Theta (TWS alebo Spot+IV pre BS), aby bolo z čoho počítať APR.")
    elif _net_debit_pf is None:
        st.warning(
            "Net debet z denníka nie je spočítateľný — všetkým **otvoreným** nohám doplň **vstupnú cenu** v Trade Logu."
        )
    elif _net_debit_pf <= 0:
        st.info(
            "Tento APR z Thety je určený pre **net debetové** štruktúry (kladný net debet). "
            "Pri net kredite použij iné metriky alebo TWS Dashboard."
        )
    else:
        _basis_th = float(_net_debit_pf) + max(0.0, float(_mm_apr_th or 0.0))
        _apr_theta_pct = (display_theta * 365.0 / _basis_th) * 100.0
        ac1, ac2, ac3 = st.columns(3)
        ac1.metric("Ročný výnos z Θ", f"{_apr_theta_pct:+.1f} %")
        ac2.metric("Θ (použitá)", f"${display_theta:+,.2f}/deň", help=f"Zdroj: {theta_src}")
        ac3.metric("Báza nákladu", f"${_basis_th:,.0f}")
        st.caption(
            f"Net debet z denníka: **{float(_net_debit_pf):,.0f}** USD"
            + (f" · marža **{float(_mm_apr_th):,.0f}** USD" if float(_mm_apr_th or 0) >= 1.0 else " · marža 0 (menovateľ = len net debet)")
        )

    # ── História grafov (APR, Θ, Δ, Vega) ───────────────────────────────────
    _apr_for_history: Optional[float] = None
    if (
        open_trades
        and has_any
        and _net_debit_pf is not None
        and float(_net_debit_pf) > 0
    ):
        _b_hist = float(_net_debit_pf) + max(0.0, float(_mm_apr_th or 0))
        if _b_hist > 0:
            _apr_for_history = (display_theta * 365.0 / _b_hist) * 100.0

    st.divider()
    st.subheader("Vývoj v čase — APR, Theta, Delta, Vega")
    st.caption(
        "Grafy zobrazujú posledných **120 dní** (~4 mesiace). **Jeden záznam na kalendárny deň** "
        "pre aktívny filter — opakované uloženie v ten istý deň **prepíše** predchádzajúci bod."
    )
    _scope_key_hist = (ticker_filter or "").strip().upper()
    _scope_lbl_hist = ticker_filter if ticker_filter else "všetky tickery"
    _today_hist = date.today().isoformat()
    _since_hist = (date.today() - timedelta(days=120)).isoformat()
    _hist_rows = db.list_portfolio_greek_history(_scope_key_hist, since_date=_since_hist, limit=500)

    hc1, hc2 = st.columns([1, 2])
    with hc1:
        _do_snap = st.button(
            "Zapísať dnešný údaj do grafu",
            type="primary",
            key="pf_greek_history_snap",
            disabled=not has_any,
            help="Uloží aktuálne APR z Thety (ak ide spočítať), Theta, Delta, Vega. Rovnaký deň = prepis.",
        )
    with hc2:
        st.caption(
            f"Dnešný dátum: **{_today_hist}** · filter: **{_scope_lbl_hist}** · bodov v okne: **{len(_hist_rows)}**"
        )

    if _do_snap and has_any:
        db.upsert_portfolio_greek_history(
            _scope_key_hist,
            _today_hist,
            _apr_for_history,
            float(display_theta),
            float(port_delta) if has_bs else None,
            float(display_vega),
        )
        st.success(f"Uložené na {_today_hist} (scope: {_scope_lbl_hist}). Predchádzajúci záznam z toho dňa bol nahradený.")
        st.rerun()

    if _hist_rows:
        _hx = [r["snapshot_date"] for r in _hist_rows]
        _y_apr = [r.get("apr_theta_pct") for r in _hist_rows]
        _y_th = [r.get("theta_usd") for r in _hist_rows]
        _y_dl = [r.get("delta_usd") for r in _hist_rows]
        _y_vg = [r.get("vega_usd") for r in _hist_rows]

        _fig_h = make_subplots(
            rows=4,
            cols=1,
            shared_xaxes=True,
            vertical_spacing=0.06,
            subplot_titles=(
                "1. APR z Thety (%)",
                "2. Theta ($/deň)",
                "3. Delta ($)",
                "4. Vega ($/1%IV)",
            ),
        )
        _fig_h.add_trace(
            go.Scatter(x=_hx, y=_y_apr, mode="lines+markers", name="APR", line=dict(color="#2ecc71")),
            row=1,
            col=1,
        )
        _fig_h.add_trace(
            go.Scatter(x=_hx, y=_y_th, mode="lines+markers", name="Theta", line=dict(color="#3498db")),
            row=2,
            col=1,
        )
        _fig_h.add_trace(
            go.Scatter(x=_hx, y=_y_dl, mode="lines+markers", name="Delta", line=dict(color="#9b59b6")),
            row=3,
            col=1,
        )
        _fig_h.add_trace(
            go.Scatter(x=_hx, y=_y_vg, mode="lines+markers", name="Vega", line=dict(color="#e67e22")),
            row=4,
            col=1,
        )
        _fig_h.update_xaxes(title_text="Dátum", row=4, col=1)
        _fig_h.update_layout(
            height=920,
            showlegend=False,
            margin=dict(l=40, r=20, t=48, b=40),
            hovermode="x unified",
        )
        st.plotly_chart(_fig_h, use_container_width=True)
        with st.expander("Tabuľka uložených bodov"):
            st.dataframe(
                pd.DataFrame(_hist_rows)[
                    ["snapshot_date", "apr_theta_pct", "theta_usd", "delta_usd", "vega_usd", "saved_at"]
                ],
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.info(
            "Zatiaľ žiadne uložené body. Nastav Spot/IV alebo TWS Greky a klikni **Zapísať dnešný údaj do grafu**."
        )

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
            _lr = ibkr.fetch_positions(with_greeks=False, use_historical_last=False)
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
