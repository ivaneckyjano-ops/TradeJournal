import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import date, timedelta

from core.probability import calc_greeks, bs_price, calc_iv_from_price, calc_sd_lines
from core import database as db
from core import ibkr
from core import agent as ai_agent

db.init_db()

st.title("Spread Builder")
st.caption("Poskladaj opčný spread z ľubovoľných nôh a okamžite vidíš P&L, Greeks, max profit/loss a breakeveny.")

# ─── Session state init ────────────────────────────────────────────────────────
if "sb_legs" not in st.session_state:
    st.session_state["sb_legs"] = []   # list of leg dicts
if "sb_spot" not in st.session_state:
    st.session_state["sb_spot"] = 200.0
if "sb_iv" not in st.session_state:
    st.session_state["sb_iv"] = 0.30

# ─── Pomocné funkcie ───────────────────────────────────────────────────────────

def _dte(expiry_str: str) -> int:
    try:
        e = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
        return max(0, (e - date.today()).days)
    except Exception:
        return 0


def _leg_greeks(leg: dict, spot: float) -> dict:
    iv   = leg.get("iv") or st.session_state["sb_iv"]
    dte  = _dte(leg["expiry"])
    sign = -1 if leg["leg_type"] == "Short" else 1
    n    = int(leg.get("contracts", 1))
    if dte <= 0 or spot <= 0 or iv <= 0:
        return {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    g = calc_greeks(spot, leg["strike"], dte, iv, leg["right"])
    return {
        "delta": (g.get("delta") or 0) * sign * n * 100,
        "theta": (g.get("theta") or 0) * sign * n * 100,
        "vega":  (g.get("vega")  or 0) * sign * n * 100,
        "gamma": (g.get("gamma") or 0) * sign * n * 100,
    }


def _pnl_at_exp(leg: dict, spot_val: float) -> float:
    n       = int(leg.get("contracts", 1))
    entry   = float(leg.get("entry_price", 0))
    strike  = float(leg["strike"])
    right   = leg["right"]
    lt      = leg["leg_type"]
    intrinsic = max(0.0, spot_val - strike) if right == "C" else max(0.0, strike - spot_val)
    if lt == "Short":
        return (entry - intrinsic) * n * 100
    else:
        return (intrinsic - entry) * n * 100


def _pnl_at_dte(leg: dict, spot_val: float, dte_v: int) -> float:
    n      = int(leg.get("contracts", 1))
    entry  = float(leg.get("entry_price", 0))
    strike = float(leg["strike"])
    right  = leg["right"]
    lt     = leg["leg_type"]
    iv     = leg.get("iv") or st.session_state["sb_iv"]
    if dte_v <= 0:
        theo = max(0.0, spot_val - strike) if right == "C" else max(0.0, strike - spot_val)
    else:
        theo = bs_price(spot_val, strike, dte_v, iv, right) or 0.0
    if lt == "Short":
        return (entry - theo) * n * 100
    else:
        return (theo - entry) * n * 100


# ─── Panel: Spot + globálne IV ─────────────────────────────────────────────────
with st.container():
    hc1, hc2, hc3 = st.columns([2, 2, 2])
    _ticker_input = hc1.text_input("Ticker", value=st.session_state.get("sb_ticker", "AMZN"),
                                    key="sb_ticker_inp").upper()
    st.session_state["sb_ticker"] = _ticker_input

    _spot_val = hc2.number_input(
        "Spot ($)", min_value=1.0, step=0.5,
        value=float(st.session_state["sb_spot"]), key="sb_spot_inp",
    )
    st.session_state["sb_spot"] = _spot_val

    _iv_val = hc3.number_input(
        "Globálna IV (0.30 = 30%)", min_value=0.01, max_value=5.0, step=0.01,
        value=float(st.session_state["sb_iv"]), key="sb_iv_inp",
        help="Použije sa pre nohy bez vlastnej IV",
    )
    st.session_state["sb_iv"] = _iv_val

    if ibkr.is_connected():
        if st.button("📡 Načítať Spot z IBKR", key="sb_load_spot"):
            with st.spinner(f"Načítavam spot pre {_ticker_input}..."):
                _res = ibkr.fetch_underlying(_ticker_input, timeout=6.0)
            if not _res.get("error") and _res.get("price"):
                st.session_state["sb_spot"] = _res["price"]
                st.rerun()
            else:
                st.warning(_res.get("error", "Spot nenájdený"))

st.divider()

# ─── Panel: Pridanie nohy ──────────────────────────────────────────────────────
with st.expander("➕ Pridať nohu", expanded=len(st.session_state["sb_legs"]) == 0):
    lc1, lc2, lc3, lc4 = st.columns(4)
    _add_lt     = lc1.selectbox("Long / Short", ["Long", "Short"], key="sb_add_lt")
    _add_right  = lc2.selectbox("Call / Put",   ["C", "P"],        key="sb_add_right",
                                 format_func=lambda x: "Call" if x=="C" else "Put")
    _add_strike = lc3.number_input("Strike ($)", min_value=0.5, step=0.5,
                                    value=float(st.session_state["sb_spot"]), key="sb_add_strike")
    _add_contr  = lc4.number_input("Kontrakty", min_value=1, step=1, value=1, key="sb_add_contr")

    lc5, lc6, lc7 = st.columns(3)
    # Expirácia – výber z lokálnych alebo manuálne
    _exps = ibkr.generate_expirations_local(months=12)["expirations"]
    _exp_fmt = {}
    for _e in _exps:
        try:
            _ed = date(int(_e[:4]), int(_e[4:6]), int(_e[6:]))
            _exp_fmt[f"{_ed.strftime('%d.%m.%Y')} ({(_ed-date.today()).days}d)"] = _e
        except Exception:
            pass
    _sel_exp_lbl = lc5.selectbox("Expirácia", list(_exp_fmt.keys()), key="sb_add_exp_sel")
    _add_exp = _exp_fmt.get(_sel_exp_lbl, _exps[0] if _exps else "")

    _add_entry = lc6.number_input(
        "Vstupná cena ($)", min_value=0.01, step=0.05,
        value=round(max(0.01,
            bs_price(_spot_val, _add_strike, max(1, _dte(_add_exp)), _iv_val,
                     _add_right) or 0.5), 2),
        key="sb_add_entry",
        help="BS odhad je predvyplnený – uprav podľa trhu",
    )
    _add_leg_iv = lc7.number_input(
        "IV pre túto nohu", min_value=0.01, max_value=5.0, step=0.01,
        value=_iv_val, key="sb_add_leg_iv",
        help="Nechaj rovnakú ako globálna IV, alebo uprav pre konkrétnu nohu",
    )

    if st.button("✅ Pridať nohu", type="primary", key="sb_btn_add"):
        st.session_state["sb_legs"].append({
            "id":         len(st.session_state["sb_legs"]) + 1,
            "leg_type":   _add_lt,
            "right":      _add_right,
            "strike":     _add_strike,
            "expiry":     _add_exp,
            "contracts":  int(_add_contr),
            "entry_price": _add_entry,
            "iv":          _add_leg_iv,
        })
        st.rerun()

# ─── Tabuľka nôh ──────────────────────────────────────────────────────────────
legs = st.session_state["sb_legs"]

if not legs:
    st.info("Žiadne nohy. Pridaj aspoň jednu nohu spreadu vyššie.")
    st.stop()

st.markdown(f"### Nohy spreadu  ({len(legs)})")

# Riadky tabuľky + Greeks
_spot = st.session_state["sb_spot"]
rows  = []
tot   = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
for i, leg in enumerate(legs):
    g = _leg_greeks(leg, _spot)
    for k in tot:
        tot[k] += g[k]
    _bs_est = bs_price(_spot, leg["strike"], max(1, _dte(leg["expiry"])),
                       leg.get("iv") or _iv_val, leg["right"]) or 0.0
    rows.append({
        "#":             i + 1,
        "L/S":           leg["leg_type"],
        "C/P":           "Call" if leg["right"] == "C" else "Put",
        "Strike":        leg["strike"],
        "Expiry":        leg["expiry"],
        "DTE":           _dte(leg["expiry"]),
        "Kontr.":        leg["contracts"],
        "Vstup $":       leg["entry_price"],
        "BS odhad $":    round(_bs_est, 2),
        "IV":            f"{leg.get('iv', _iv_val)*100:.1f}%",
        "Theta $/deň":   round(g["theta"], 2),
        "Delta $":       round(g["delta"], 0),
        "Vega $":        round(g["vega"], 2),
        "Gamma":         round(g["gamma"], 4),
    })

df_legs = pd.DataFrame(rows)
st.dataframe(
    df_legs, use_container_width=True, hide_index=True,
    column_config={
        "Strike":       st.column_config.NumberColumn(format="$%.2f"),
        "Vstup $":      st.column_config.NumberColumn(format="$%.2f"),
        "BS odhad $":   st.column_config.NumberColumn(format="$%.2f"),
        "Theta $/deň":  st.column_config.NumberColumn(format="$%+.2f"),
        "Delta $":      st.column_config.NumberColumn(format="$%+.0f"),
        "Vega $":       st.column_config.NumberColumn(format="$%+.2f"),
    },
)

# Tlačidlá na mazanie nôh
_del_cols = st.columns(min(len(legs), 6))
for i, leg in enumerate(legs):
    _lbl = f"{'Call' if leg['right']=='C' else 'Put'} ${leg['strike']:.0f} {leg['leg_type'][0]}"
    if _del_cols[i % 6].button(f"🗑 #{i+1} {_lbl}", key=f"sb_del_{i}"):
        st.session_state["sb_legs"].pop(i)
        # Prečísluj ID
        for j, l in enumerate(st.session_state["sb_legs"]):
            l["id"] = j + 1
        st.rerun()

if st.button("🗑 Vymazať všetky nohy", key="sb_clear_all"):
    st.session_state["sb_legs"] = []
    st.rerun()

st.divider()

# ─── Net Greeks + súhrn ───────────────────────────────────────────────────────
st.markdown("### Net Greeks celého spreadu")
_gc1, _gc2, _gc3, _gc4 = st.columns(4)
_gc1.metric("Net Delta $",      f"${tot['delta']:+.0f}",
            help="O koľko sa zmení hodnota spreadu pri pohybe spotu o $1")
_gc2.metric("Net Theta $/deň",  f"${tot['theta']:+.2f}",
            help="Denný časový rozpad celého spreadu")
_gc3.metric("Net Vega $",       f"${tot['vega']:+.2f}",
            help="Zmena hodnoty pri 1% pohybe IV")
_gc4.metric("Net Gamma",        f"{tot['gamma']:+.4f}",
            help="Rýchlosť zmeny delty")

# Net kredit / debet
_net_flow = sum(
    (-leg["entry_price"] if leg["leg_type"] == "Long" else leg["entry_price"])
    * leg["contracts"] * 100
    for leg in legs
)
_flow_lbl = "Čistý kredit" if _net_flow >= 0 else "Čistý debet"
st.metric(_flow_lbl, f"${abs(_net_flow):,.0f}",
          help="Suma prijatého prémia mínus zaplatené prémium za celý spread")

st.divider()

# ─── P&L diagram ──────────────────────────────────────────────────────────────
st.markdown("### P&L diagram")

# Najkratšia expirácia (referenčná pre DTE slider a SD pásma)
_min_dte = min((_dte(l["expiry"]) for l in legs), default=30)
_min_dte = max(_min_dte, 1)

_show_dte = st.slider(
    "Zobraziť P&L k tomuto DTE",
    min_value=0, max_value=_min_dte,
    value=min(_min_dte, max(1, _min_dte // 2)),
    step=1, key="sb_dte_slider",
)

_price_range = np.linspace(_spot * 0.65, _spot * 1.35, 500)

def _combined_pnl(price_arr, dte_v):
    result = np.zeros(len(price_arr))
    for leg in legs:
        # Pre nohy s dlhším DTE ako slider: vypočítaj zostatok
        _leg_dte_now = _dte(leg["expiry"])
        _elapsed = _min_dte - dte_v
        _leg_dte_at = max(0, _leg_dte_now - _elapsed)
        for j, s in enumerate(price_arr):
            result[j] += _pnl_at_dte(leg, float(s), _leg_dte_at)
    return np.round(result, 0)

_pnl_exp     = _combined_pnl(_price_range, 0)
_pnl_now     = _combined_pnl(_price_range, _min_dte)
_pnl_slider  = _combined_pnl(_price_range, _show_dte)

fig = go.Figure()

# Farebná plocha pre slider P&L
fig.add_trace(go.Scatter(
    x=_price_range, y=np.where(_pnl_slider >= 0, _pnl_slider, 0),
    fill="tozeroy", fillcolor="rgba(46,204,113,0.07)",
    line=dict(width=0), showlegend=False, hoverinfo="skip",
))
fig.add_trace(go.Scatter(
    x=_price_range, y=np.where(_pnl_slider < 0, _pnl_slider, 0),
    fill="tozeroy", fillcolor="rgba(231,76,60,0.06)",
    line=dict(width=0), showlegend=False, hoverinfo="skip",
))

# Časové rezy
_time_slices = [
    (_min_dte,               "#60a5fa", f"Teraz ({_min_dte}d)", 2.0),
    (max(1, _min_dte * 2//3), "#a78bfa", f"{_min_dte*2//3}d",  1.5),
    (max(1, _min_dte // 3),   "#fb923c", f"{_min_dte//3}d",    1.5),
    (0,                      "#f43f5e", "Expirácia (0d)",       2.5),
]
for d_v, col, lbl, lw in _time_slices:
    _y = _combined_pnl(_price_range, d_v)
    fig.add_trace(go.Scatter(
        x=_price_range, y=_y, mode="lines",
        line=dict(color=col, width=lw), name=lbl,
        hovertemplate=f"{lbl} — Spot: $%{{x:.1f}}  P&L: $%{{y:+.0f}}<extra></extra>",
    ))

# Slider rez (žltá prerušovaná) – ak nie je duplikát
if not any(abs(d - _show_dte) <= 1 for d, *_ in _time_slices):
    fig.add_trace(go.Scatter(
        x=_price_range, y=_pnl_slider, mode="lines",
        line=dict(color="#facc15", width=3, dash="dash"),
        name=f"Slider {_show_dte}d",
        hovertemplate=f"DTE {_show_dte}d — $%{{x:.1f}} → $%{{y:+.0f}}<extra></extra>",
    ))

# Vertikálne línie
fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
fig.add_vline(x=_spot, line_color="#fbbf24", line_width=2, line_dash="dash",
              annotation_text=f"Spot ${_spot:.0f}", annotation_font_color="#fbbf24",
              annotation_position="top right")

# Strikeы každej nohy
_colors_strikes = ["#34d399", "#fb7185", "#a78bfa", "#fdba74", "#67e8f9", "#f9a8d4"]
for i, leg in enumerate(legs):
    _col_s = _colors_strikes[i % len(_colors_strikes)]
    _lbl_s = f"{'C' if leg['right']=='C' else 'P'} ${leg['strike']:.0f} {'S' if leg['leg_type']=='Short' else 'L'}"
    fig.add_vline(x=leg["strike"], line_color=_col_s, line_width=1, line_dash="dot",
                  annotation_text=_lbl_s, annotation_font_color=_col_s,
                  annotation_font_size=10)

# SD pásma
try:
    _sd = calc_sd_lines(_spot, _iv_val, max(1, _show_dte))
    for _lvl, _lbl_sd, _col_sd in [
        (_sd.upper_1sd, "1SD+", "rgba(96,165,250,0.5)"),
        (_sd.lower_1sd, "1SD−", "rgba(96,165,250,0.5)"),
        (_sd.upper_2sd, "2SD+", "rgba(167,139,250,0.4)"),
        (_sd.lower_2sd, "2SD−", "rgba(167,139,250,0.4)"),
    ]:
        fig.add_vline(x=_lvl, line_color=_col_sd, line_width=1, line_dash="dot",
                      annotation_text=_lbl_sd, annotation_font_size=9,
                      annotation_font_color=_col_sd)
except Exception:
    pass

fig.update_layout(
    title=f"Spread P&L — {st.session_state.get('sb_ticker','?')}  ·  Spot ${_spot:.0f}  ·  IV {_iv_val*100:.0f}%",
    xaxis_title="Cena podkladu ($)",
    yaxis_title="P&L ($)",
    height=460, showlegend=True,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    margin=dict(l=60, r=60, t=80, b=50),
    plot_bgcolor="rgba(20,20,30,0.97)",
    paper_bgcolor="rgba(20,20,30,0.0)",
    font_color="#e0e0e0",
    hovermode="x unified",
    yaxis=dict(tickformat="$,.0f"),
)
_big = st.session_state.get("sb_pnl_big", False)
fig.update_layout(height=750 if _big else 460)
_z1, _ = st.columns([1, 5])
if _z1.button("🔲 " + ("Zmenši" if _big else "Zväčši"), key="sb_zoom"):
    st.session_state["sb_pnl_big"] = not _big
    st.rerun()
st.plotly_chart(fig, use_container_width=True, key="sb_main_chart")
st.caption(
    "Čiary: modrá=teraz → fialová → oranžová → červená=expirácia. "
    "Žltá prerušovaná = slider DTE. Modré pásma = 1SD/2SD pre slider DTE."
)

st.divider()

# ─── Max profit / Max loss / Breakeven ────────────────────────────────────────
st.markdown("### Max profit · Max loss · Breakeven")

_pnl_at_exp_arr = _combined_pnl(_price_range, 0)
_max_profit = float(np.max(_pnl_at_exp_arr))
_max_loss   = float(np.min(_pnl_at_exp_arr))

# Breakeven body (prechody cez 0 pri expirácii)
_be_points = []
for _i in range(len(_pnl_at_exp_arr) - 1):
    if _pnl_at_exp_arr[_i] * _pnl_at_exp_arr[_i + 1] < 0:
        _be = _price_range[_i] + (_price_range[_i+1] - _price_range[_i]) * \
              abs(_pnl_at_exp_arr[_i]) / (abs(_pnl_at_exp_arr[_i]) + abs(_pnl_at_exp_arr[_i+1]))
        _be_points.append(round(_be, 2))

_ma1, _ma2, _ma3 = st.columns(3)
_ma1.metric(
    "Max Profit (pri expirácii)",
    f"${_max_profit:+,.0f}" if _max_profit < 50_000 else "Neohraničený",
    help="Maximálny P&L v rozsahu ±35% od spotu"
)
_ma2.metric(
    "Max Loss (pri expirácii)",
    f"${_max_loss:+,.0f}" if _max_loss > -50_000 else "Neohraničená",
    help="Maximálna strata v rozsahu ±35% od spotu"
)
if _be_points:
    _ma3.metric("Breakeven body", "  /  ".join(f"${b:.2f}" for b in _be_points))
else:
    _ma3.metric("Breakeven", "—")

# Risk/Reward
if _max_loss < 0 and _max_profit > 0:
    _rr = _max_profit / abs(_max_loss)
    st.caption(f"**Risk/Reward: {_rr:.2f}×** — za každé $1 rizika potenciálny zisk ${_rr:.2f}")

st.divider()

# ─── Scenárová analýza ────────────────────────────────────────────────────────
st.markdown("### Scenárová analýza pri expirácii")
_pct_steps  = [-15, -10, -7.5, -5, -2.5, 0, +2.5, +5, +7.5, +10, +15]
_spot_lvls  = [round(_spot * (1 + p / 100), 2) for p in _pct_steps]
_scen_pnls  = [round(sum(_pnl_at_exp(l, s) for l in legs), 0) for s in _spot_lvls]

fig_scen = go.Figure()
fig_scen.add_trace(go.Bar(
    x=[f"{p:+.1f}%" for p in _pct_steps],
    y=_scen_pnls,
    marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in _scen_pnls],
    hovertemplate="Zmena: %{x}<br>P&L: $%{y:+,.0f}<extra></extra>",
    showlegend=False,
))
fig_scen.add_hline(y=0, line_color="gray", line_width=1)
fig_scen.update_layout(
    height=300,
    xaxis_title="Zmena ceny podkladu pri expirácii",
    yaxis_title="P&L ($)", yaxis=dict(tickformat="$,.0f"),
    margin=dict(l=10, r=10, t=20, b=40),
    plot_bgcolor="rgba(20,20,30,0.97)",
    paper_bgcolor="rgba(20,20,30,0.0)",
    font_color="#e0e0e0",
)
st.plotly_chart(fig_scen, use_container_width=True, key="sb_scen_chart")

st.divider()

# ─── Uložiť do denníka ────────────────────────────────────────────────────────
st.markdown("### Uložiť analýzu do denníka")

_snap_note = st.text_input(
    "Poznámka (voliteľné)",
    placeholder="napr. Zvažujem Bear Put Spread na hedge...",
    key="sb_snap_note",
)
_snap_group = st.selectbox(
    "Priradiť ku skupine (voliteľné)",
    ["—"] + [g["name"] for g in db.get_groups()],
    key="sb_snap_group",
)

if st.button("📝 Uložiť snapshot do denníka", type="primary", key="sb_save_btn"):
    _ticker = st.session_state.get("sb_ticker", "?")
    _legs_md = "\n".join(
        f"| {'Long' if l['leg_type']=='Long' else 'Short'} | "
        f"{'Call' if l['right']=='C' else 'Put'} | "
        f"${l['strike']:.0f} | {l['expiry']} (DTE {_dte(l['expiry'])}d) | "
        f"${l['entry_price']:.2f} | {l['contracts']}× |"
        for l in legs
    )
    _be_str = "  /  ".join(f"${b:.2f}" for b in _be_points) if _be_points else "—"
    _note_md = f"""## Spread Builder — {_ticker}
**Dátum:** {date.today().strftime('%d.%m.%Y')}  ·  Spot: ${_spot:.2f}  ·  IV: {_iv_val*100:.1f}%

### Nohy
| L/S | C/P | Strike | Expiry | Vstup | Kontrakty |
|-----|-----|--------|--------|-------|----------|
{_legs_md}

### Greeks celého spreadu
| Net Delta $ | Net Theta $/deň | Net Vega $ | Net Gamma |
|------------|-----------------|-----------|----------|
| ${tot['delta']:+.0f} | ${tot['theta']:+.2f} | ${tot['vega']:+.2f} | {tot['gamma']:+.4f} |

### Výsledky
| Metrika | Hodnota |
|---------|---------|
| Čistý kredit/debet | ${_net_flow:+,.0f} |
| Max profit | {"${:+,.0f}".format(_max_profit) if _max_profit < 50000 else "Neohraničený"} |
| Max loss | {"${:+,.0f}".format(_max_loss) if _max_loss > -50000 else "Neohraničená"} |
| Breakeven | {_be_str} |

{("**Poznámka:** " + _snap_note) if _snap_note else ""}
"""
    _gid = _snap_group if _snap_group != "—" else None
    _nid = db.add_note(
        title=f"Spread Builder — {_ticker} [{date.today().strftime('%d.%m.%Y')}]",
        content=_note_md,
        group_id=_gid,
    )
    st.success(f"✅ Poznámka #{_nid} uložená do denníka{' (skupina ' + _gid + ')' if _gid else ''}.")

st.divider()

# ─── AI Analýza spreadu ────────────────────────────────────────────────────────
st.markdown("### 🤖 AI Analýza spreadu")

if not legs:
    st.caption("Pridaj aspoň jednu nohu aby bola dostupná AI analýza.")
else:
    _ticker    = st.session_state.get("sb_ticker", "?")
    _model_opt = list(ai_agent.AVAILABLE_MODELS.keys())
    _model_lbl = [ai_agent.AVAILABLE_MODELS[m]["label"] for m in _model_opt]
    _saved_m   = st.session_state.get("selected_claude_model", "claude-sonnet-4-6")
    _saved_idx = _model_opt.index(_saved_m) if _saved_m in _model_opt else 1

    ai_c1, ai_c2, ai_c3 = st.columns([3, 2, 1])
    with ai_c1:
        _ai_question = st.text_input(
            "Otázka (voliteľné)",
            placeholder="napr. Je teraz vhodný čas? Aká podmienka vstupu?",
            key="sb_ai_question",
            label_visibility="collapsed",
        )
    with ai_c2:
        _sel_idx = st.selectbox(
            "Model",
            options=range(len(_model_opt)),
            format_func=lambda i: _model_lbl[i],
            index=_saved_idx,
            key="sb_model_sel",
            label_visibility="collapsed",
        )
        _selected_model = _model_opt[_sel_idx]
        st.session_state["selected_claude_model"] = _selected_model
    with ai_c3:
        _run_ai = st.button("Analyzovať", type="primary", key="sb_ai_btn", use_container_width=True)

    if _run_ai:
        with st.spinner("Claude analyzuje spread..."):
            try:
                # Zostav popis nôh pre prompt
                _legs_lines = []
                for l in legs:
                    _g = _leg_greeks(l, _spot)
                    _ls  = "Long" if l["leg_type"] == "Long" else "Short"
                    _cp  = "Call" if l["right"] == "C" else "Put"
                    _dte_v = _dte(l["expiry"])
                    _legs_lines.append(
                        f"  {_ls} {l.get('contracts',1)}× {_cp} ${l['strike']:.0f} exp {l['expiry']} (DTE {_dte_v})"
                        f" | Entry ${l['entry_price']:.2f}"
                        f" | Delta ${_g['delta']:+.0f} | Theta ${_g['theta']:+.2f}/deň"
                    )

                # Otvorené objednávky z TWS pre tento ticker
                _tws_ord_lines = []
                _tws_orders = ibkr.DASHBOARD_FETCH_JOB.get("orders") or []
                for o in _tws_orders:
                    if o.get("ticker", "").upper() == _ticker.upper():
                        sec = o.get("sec_type", "")
                        if sec in ("OPT", "FOP"):
                            detail = f"{o.get('option_type')} ${o.get('strike',0):.0f} exp {o.get('expiry')}"
                        elif sec == "BAG":
                            detail = f"Combo: {o.get('legs_descr') or '?'}"
                        else:
                            detail = sec
                        conds = "; ".join(
                            f"Cena {'>' if c.get('isMore') else '<'} {c.get('price')} USD"
                            for c in (o.get("conditions") or [])
                            if c.get("type") == "PriceCondition"
                        )
                        cond_s = f" ⟦{conds}⟧" if conds else ""
                        _tws_ord_lines.append(
                            f"  - {o.get('action')} {o.get('total_qty')}× {detail}"
                            f" | {o.get('order_type')} | {o.get('status')}{cond_s}"
                        )
                tws_text = ("\n## Súvisiace objednávky v TWS:\n" + "\n".join(_tws_ord_lines)) if _tws_ord_lines else ""
                q_text   = f"\n## Otázka obchodníka:\n{_ai_question}" if _ai_question else ""

                _be_str  = "  /  ".join(f"${b:.2f}" for b in _be_points) if _be_points else "—"
                mp_str   = f"${_max_profit:+,.0f}" if _max_profit < 50000 else "Neohraničený"
                ml_str   = f"${_max_loss:+,.0f}" if _max_loss > -50000 else "Neohraničená"

                prompt = f"""Si skúsený obchodník s opciami. Analyzuj nasledujúci spread.

PRAVIDLÁ:
- Píš v slovenčine, ceny ako "190 USD", bez LaTeX
- Buď konkrétny a číselný, max 350 slov

## Spread: {_ticker}
- Dátum: {date.today().strftime('%d.%m.%Y')}
- Spot: ${_spot:.2f} | IV: {_iv_val*100:.1f}%

## Nohy:
{chr(10).join(_legs_lines)}

## Výsledky spreadu:
- Čistý kredit/debet: ${_net_flow:+,.0f}
- Max profit: {mp_str}
- Max loss: {ml_str}
- Breakeven: {_be_str}
- Net Delta: ${tot['delta']:+.0f} | Net Theta: ${tot['theta']:+.2f}/deň | Net Vega: ${tot['vega']:+.2f}
{tws_text}
{q_text}
---
Odpovedaj v tomto formáte:

## Hodnotenie spreadu
(silné stránky, slabiny, vhodnosť pre súčasné trhové podmienky)

## Riziká a podmienky vstupu
- Podmienka vstupu: (napr. len ak IV > 30%, alebo spot > X USD)
- Stop-loss úroveň: (konkrétna cena alebo % pohyb)
- Čo sledovať: (kľúčové úrovne a udalosti)

## Návrh úpravy (ak relevantný)
(alternatívny strike/expiry alebo iný typ spreadu pre lepší pomer rizika)

## Záver
(vstúpiť teraz / počkať / zamietnuť)
"""
                client    = ai_agent._load_client()
                m_info    = ai_agent.AVAILABLE_MODELS.get(_selected_model, {})
                max_tok   = m_info.get("max_tokens", 1200)
                msg       = client.messages.create(
                    model=_selected_model,
                    max_tokens=max_tok,
                    messages=[{"role": "user", "content": prompt}],
                )
                _ai_result = msg.content[0].text

                # Uložiť do denníka
                _gid2 = _snap_group if _snap_group != "—" else None
                db.add_note(
                    title=f"🤖 AI Spread: {_ticker} [{date.today().strftime('%d.%m.%Y')}]",
                    content=_ai_result,
                    group_id=_gid2,
                )
                st.success("Analýza uložená do Konzultácií!")
                with st.container(border=True):
                    st.markdown(_ai_result)

            except Exception as e:
                st.error(f"Chyba: {e}")
