import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import date

from core.probability import (
    calc_sd_lines,
    pop_short_call, pop_short_put,
    pop_long_call, pop_long_put,
    pop_diagonal, pop_strangle,
    calc_greeks, bs_price,
)
from core.charts import bell_curve_chart, sd_lines_chart
from core import database as db
from core import ibkr

db.init_db()


# ─── Pomocné funkcie pre skupinový kontext ────────────────────────────────────

def _dte_from_str(expiry_str: str) -> int:
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


def _greeks_for_leg(t: dict, spot: float, iv: float) -> dict:
    """Vypočíta BS greky a unrealized P&L pre jednu nohu."""
    dte_v = _dte_from_str(t.get("expiry", ""))
    if dte_v <= 0 or spot <= 0 or iv <= 0:
        return {"delta": 0.0, "theta": 0.0, "vega": 0.0, "unrealized": 0.0}
    right = "C" if t.get("option_type", "Call") == "Call" else "P"
    contracts = int(t.get("contracts", 1))
    sign = _leg_sign(t.get("leg_type", "Long"))
    g = calc_greeks(spot, t.get("strike", 0), dte_v, iv, right)
    entry_p = float(t.get("entry_price", 0) or 0)
    curr_p = bs_price(spot, t.get("strike", 0), dte_v, iv, right) or 0.0
    unrealized = sign * (entry_p - curr_p) * contracts * 100
    return {
        "delta":      (g["delta"] or 0) * sign * contracts * 100,
        "theta":      (g["theta"] or 0) * sign * contracts * 100,
        "vega":       (g["vega"]  or 0) * sign * contracts * 100,
        "unrealized": unrealized,
    }


def _exp_pnl_leg(t: dict, spot_at_exp: float) -> float:
    """P&L nohy pri expirácii (intrinsic)."""
    contracts = int(t.get("contracts", 1))
    entry_p   = float(t.get("entry_price", 0) or 0)
    strike    = float(t.get("strike", 0) or 0)
    leg       = t.get("leg_type", "Long")
    opt       = t.get("option_type", "Call")
    if leg == "Short":
        intrinsic = max(0.0, spot_at_exp - strike) if opt == "Call" else max(0.0, strike - spot_at_exp)
        return (entry_p - intrinsic) * contracts * 100
    else:
        intrinsic = max(0.0, spot_at_exp - strike) if opt == "Call" else max(0.0, strike - spot_at_exp)
        return (intrinsic - entry_p) * contracts * 100


def _show_group_context(sel_trade: dict, spot: float, iv: float,
                        roll_strike: float, roll_entry: float,
                        roll_iv: float, roll_right: str, roll_dte_str: str):
    """Zobrazí kontext skupiny pred a po rolle."""
    gid = sel_trade.get("group_id", "")
    if not gid:
        st.info("Vybraná pozícia nemá priradenú skupinu — kontext skupiny nie je dostupný.")
        return

    all_open = db.get_open_trades()
    group_legs = [t for t in all_open if (t.get("group_id") or "").strip() == gid.strip()]

    if not group_legs:
        return

    with st.expander(f"📊 Kontext skupiny **{gid}** — pred / po rolle", expanded=True):

        # ── Tabuľka: aktuálny stav ────────────────────────────────────────
        st.markdown("**Aktuálny stav skupiny**")
        rows_cur = []
        totals = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "unrealized": 0.0}
        for t in group_legs:
            g = _greeks_for_leg(t, spot, iv)
            for k in totals:
                totals[k] += g[k]
            rows_cur.append({
                "ID": t["id"],
                "Noha": t.get("leg_type", ""),
                "Typ": t.get("option_type", ""),
                "Strike": t.get("strike"),
                "DTE": _dte_from_str(t.get("expiry", "")),
                "Delta $": round(g["delta"], 0),
                "Theta $/deň": round(g["theta"], 2),
                "Vega $": round(g["vega"], 2),
                "Unrealized P&L": round(g["unrealized"], 0),
                "Rolujem": "✏️" if t["id"] == sel_trade["id"] else "",
            })
        rows_cur.append({
            "ID": "—",
            "Noha": "SPOLU",
            "Typ": "",
            "Strike": None,
            "DTE": None,
            "Delta $": round(totals["delta"], 0),
            "Theta $/deň": round(totals["theta"], 2),
            "Vega $": round(totals["vega"], 2),
            "Unrealized P&L": round(totals["unrealized"], 0),
            "Rolujem": "",
        })

        c1, c2, c3 = st.columns(3)
        c1.metric("Theta skupiny / deň", f"${totals['theta']:+.2f}")
        c2.metric("Delta skupiny $", f"${totals['delta']:+.0f}")
        c3.metric("Unrealized P&L skupiny", f"${totals['unrealized']:+.0f}")

        st.dataframe(
            pd.DataFrame(rows_cur),
            use_container_width=True, hide_index=True,
            column_config={
                "Strike":          st.column_config.NumberColumn(format="$%.0f"),
                "Delta $":         st.column_config.NumberColumn(format="$%+.0f"),
                "Theta $/deň":     st.column_config.NumberColumn(format="$%+.2f"),
                "Vega $":          st.column_config.NumberColumn(format="$%+.2f"),
                "Unrealized P&L":  st.column_config.NumberColumn(format="$%+.0f"),
            },
        )

        # ── Tabuľka: po rolle ─────────────────────────────────────────────
        if roll_entry > 0 and roll_strike > 0:
            st.markdown("**Po rolle** *(rolovaná noha nahradená novými parametrami)*")

            # Syntetická noha po rolle
            roll_dte_v = _dte_from_str(roll_dte_str) if roll_dte_str else _dte_from_str(sel_trade.get("expiry", ""))
            roll_leg_type = sel_trade.get("leg_type", "Short")
            roll_contracts = int(sel_trade.get("contracts", 1))
            roll_sign = _leg_sign(roll_leg_type)
            roll_g = calc_greeks(spot, roll_strike, max(1, roll_dte_v), roll_iv, roll_right) if roll_dte_v > 0 else {}
            roll_curr_p = bs_price(spot, roll_strike, max(1, roll_dte_v), roll_iv, roll_right) or 0.0
            roll_unreal = roll_sign * (roll_entry - roll_curr_p) * roll_contracts * 100

            roll_greeks = {
                "delta":      (roll_g.get("delta") or 0) * roll_sign * roll_contracts * 100,
                "theta":      (roll_g.get("theta") or 0) * roll_sign * roll_contracts * 100,
                "vega":       (roll_g.get("vega")  or 0) * roll_sign * roll_contracts * 100,
                "unrealized": roll_unreal,
            }

            totals_new = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "unrealized": 0.0}
            rows_new = []
            for t in group_legs:
                if t["id"] == sel_trade["id"]:
                    g = roll_greeks
                    rows_new.append({
                        "ID": f"→{t['id']}",
                        "Noha": roll_leg_type,
                        "Typ": "Call" if roll_right == "C" else "Put",
                        "Strike": roll_strike,
                        "DTE": roll_dte_v,
                        "Delta $": round(g["delta"], 0),
                        "Theta $/deň": round(g["theta"], 2),
                        "Vega $": round(g["vega"], 2),
                        "Unrealized P&L": round(g["unrealized"], 0),
                        "Rolujem": "✅",
                    })
                else:
                    g = _greeks_for_leg(t, spot, iv)
                    rows_new.append({
                        "ID": t["id"],
                        "Noha": t.get("leg_type", ""),
                        "Typ": t.get("option_type", ""),
                        "Strike": t.get("strike"),
                        "DTE": _dte_from_str(t.get("expiry", "")),
                        "Delta $": round(g["delta"], 0),
                        "Theta $/deň": round(g["theta"], 2),
                        "Vega $": round(g["vega"], 2),
                        "Unrealized P&L": round(g["unrealized"], 0),
                        "Rolujem": "",
                    })
                for k in totals_new:
                    totals_new[k] += g[k]

            rows_new.append({
                "ID": "—",
                "Noha": "SPOLU",
                "Typ": "",
                "Strike": None,
                "DTE": None,
                "Delta $": round(totals_new["delta"], 0),
                "Theta $/deň": round(totals_new["theta"], 2),
                "Vega $": round(totals_new["vega"], 2),
                "Unrealized P&L": round(totals_new["unrealized"], 0),
                "Rolujem": "",
            })

            # Δ zmeny
            delta_d = totals_new["delta"] - totals["delta"]
            delta_th = totals_new["theta"] - totals["theta"]
            delta_u  = totals_new["unrealized"] - totals["unrealized"]
            d1, d2, d3 = st.columns(3)
            d1.metric("Theta po rolle / deň", f"${totals_new['theta']:+.2f}",
                      delta=f"{delta_th:+.2f} vs. teraz")
            d2.metric("Delta po rolle $", f"${totals_new['delta']:+.0f}",
                      delta=f"{delta_d:+.0f} vs. teraz")
            d3.metric("Unrealized P&L po rolle", f"${totals_new['unrealized']:+.0f}",
                      delta=f"{delta_u:+.0f} vs. teraz")

            st.dataframe(
                pd.DataFrame(rows_new),
                use_container_width=True, hide_index=True,
                column_config={
                    "Strike":          st.column_config.NumberColumn(format="$%.0f"),
                    "Delta $":         st.column_config.NumberColumn(format="$%+.0f"),
                    "Theta $/deň":     st.column_config.NumberColumn(format="$%+.2f"),
                    "Vega $":          st.column_config.NumberColumn(format="$%+.2f"),
                    "Unrealized P&L":  st.column_config.NumberColumn(format="$%+.0f"),
                },
            )

            # ── Scenárová analýza skupiny (bar chart) ─────────────────────
            st.markdown("**Scenárová analýza skupiny pri expirácii**")
            pct_steps = [-15, -10, -7.5, -5, -2.5, 0, +2.5, +5, +7.5, +10, +15]
            spot_levels = [round(spot * (1 + p / 100), 2) for p in pct_steps]

            cur_pnls  = []
            roll_pnls = []
            for slevel in spot_levels:
                cur_total = sum(_exp_pnl_leg(t, slevel) for t in group_legs)
                roll_total = 0.0
                for t in group_legs:
                    if t["id"] == sel_trade["id"]:
                        # Rolovaná noha
                        contracts_r = int(t.get("contracts", 1))
                        entry_r     = roll_entry
                        strike_r    = roll_strike
                        right_r_str = "Call" if roll_right == "C" else "Put"
                        leg_r       = t.get("leg_type", "Short")
                        if leg_r == "Short":
                            intrinsic_r = max(0.0, slevel - strike_r) if right_r_str == "Call" else max(0.0, strike_r - slevel)
                            roll_total += (entry_r - intrinsic_r) * contracts_r * 100
                        else:
                            intrinsic_r = max(0.0, slevel - strike_r) if right_r_str == "Call" else max(0.0, strike_r - slevel)
                            roll_total += (intrinsic_r - entry_r) * contracts_r * 100
                    else:
                        roll_total += _exp_pnl_leg(t, slevel)
                cur_pnls.append(round(cur_total, 0))
                roll_pnls.append(round(roll_total, 0))

            fig_scen = go.Figure()
            fig_scen.add_trace(go.Bar(
                x=[f"{p:+.1f}%" for p in pct_steps],
                y=cur_pnls,
                name="Aktuálna skupina",
                marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in cur_pnls],
                opacity=0.6,
                hovertemplate="Zmena: %{x}<br>P&L aktuál: $%{y:+,.0f}<extra></extra>",
            ))
            fig_scen.add_trace(go.Scatter(
                x=[f"{p:+.1f}%" for p in pct_steps],
                y=roll_pnls,
                name="Po rolle",
                mode="lines+markers",
                line=dict(color="#f39c12", width=2, dash="dash"),
                marker=dict(size=7),
                hovertemplate="Zmena: %{x}<br>P&L po rolle: $%{y:+,.0f}<extra></extra>",
            ))
            fig_scen.add_hline(y=0, line_color="gray", line_width=1)
            fig_scen.update_layout(
                height=320,
                xaxis_title="Zmena ceny podkladu pri expirácii",
                yaxis_title="P&L ($)",
                margin=dict(l=10, r=10, t=20, b=40),
                legend=dict(orientation="h", yanchor="bottom", y=1.02),
                barmode="overlay",
                yaxis=dict(tickformat="$,.0f"),
            )
            st.plotly_chart(fig_scen, use_container_width=True)
            st.caption("Stĺpce = aktuálna skupina pri expirácii · Prerušovaná = po rolle")

            # ── Roll odporúčanie ───────────────────────────────────────────
            st.markdown("---")
            st.markdown("#### 🤖 Roll odporúčanie")

            cur_dte_val  = _dte_from_str(sel_trade.get("expiry", ""))
            roll_dte_val = _dte_from_str(roll_dte_str) if roll_dte_str else cur_dte_val
            cur_mid_p    = float((st.session_state.get("mod_fetched") or {}).get("mid") or 0)
            entry_p_orig = float(sel_trade.get("entry_price") or 0)
            is_short     = sel_trade.get("leg_type", "Short") == "Short"

            # Čistý kredit / debet za rollovanú nohu
            contracts_r  = int(sel_trade.get("contracts", 1))
            if is_short:
                # Short: otvorili sme za entry_p_orig (inkaso), roll = uzavrieme za cur_mid a otvoríme za roll_entry
                net_flow = (roll_entry - cur_mid_p) * contracts_r * 100
            else:
                # Long: zaplatili sme entry_p_orig, roll = predáme za cur_mid a kúpime za roll_entry
                net_flow = (cur_mid_p - roll_entry) * contracts_r * 100

            delta_theta  = totals_new["theta"] - totals["theta"]
            delta_abs    = abs(totals_new["delta"]) - abs(totals["delta"])
            unreal_chg   = totals_new["unrealized"] - totals["unrealized"]

            pros  = []
            cons  = []
            score = 0  # >0 = roll; <0 = nerolovať

            # --- DTE ---
            if cur_dte_val <= 7:
                pros.append(f"🔴 Urgentné — DTE = **{cur_dte_val}** dní, pozícia blízko expirácie")
                score += 2
            elif cur_dte_val <= 21:
                pros.append(f"🟡 DTE = **{cur_dte_val}** dní — klasická zóna pre roll (< 21 dní)")
                score += 1
            else:
                cons.append(f"⏳ DTE = **{cur_dte_val}** dní — ešte dostatok času, roll nie je nutný")
                score -= 1

            if roll_dte_val < 30:
                cons.append(f"⚠️ Cieľová expirácia len **{roll_dte_val}** dní — príliš blízko, zvaž dlhší DTE")
                score -= 1
            elif roll_dte_val >= 45:
                pros.append(f"✅ Nový DTE = **{roll_dte_val}** dní — dobré časové pásmo (45+ dní)")
                score += 1

            # --- Kredit / Debet ---
            if net_flow > 0:
                pros.append(f"✅ Roll za čistý kredit **${net_flow:+.0f}** — zbieraš ďalšie prémium")
                score += 2
            elif net_flow < -50:
                cons.append(f"⛔ Roll za debet **${net_flow:+.0f}** — platíš za predĺženie")
                score -= 2
            else:
                cons.append(f"ℹ️ Roll takmer break-even (**${net_flow:+.0f}**)")

            # --- Theta ---
            if delta_theta > 0.5:
                pros.append(f"✅ Theta sa zlepší o **${delta_theta:+.2f}/deň** (viac pasívneho výnosu)")
                score += 1
            elif delta_theta < -1:
                cons.append(f"⚠️ Theta sa zhorší o **${delta_theta:+.2f}/deň**")
                score -= 1

            # --- Delta ---
            if delta_abs < -10:
                pros.append(f"✅ Delta risk skupiny sa zníži o **${abs(delta_abs):.0f}** (menej smerového rizika)")
                score += 1
            elif delta_abs > 20:
                cons.append(f"⚠️ Delta risk skupiny vzrastie o **${delta_abs:.0f}** — pozícia bude smerovanejšia")
                score -= 1

            # --- IV prostredie ---
            if iv > 0.40:
                pros.append(f"✅ IV = **{iv*100:.1f}%** — vysoká volatilita, ideálne prostredie pre predaj prémia")
                score += 1
            elif iv > 0.25:
                pros.append(f"🟡 IV = **{iv*100:.1f}%** — priemerná volatilita, prémia sú slušné")
            else:
                cons.append(f"⚠️ IV = **{iv*100:.1f}%** — nízka volatilita, prémia sú lacné — zvýšené riziko")
                score -= 1

            # --- Unrealized P&L ---
            if totals["unrealized"] < -500:
                cons.append(
                    f"⚠️ Skupina je v strate **${totals['unrealized']:+.0f}** — "
                    f"roll predĺži expozíciu, nie je záruka zotavenia"
                )
                score -= 1
            elif totals["unrealized"] > 0:
                pros.append(f"✅ Skupina je v zisku **${totals['unrealized']:+.0f}** — roll z pozície sily")
                score += 1

            # --- Verdict ---
            if score >= 3:
                verdict_label = "✅ ROLL ODPORÚČANÝ"
                verdict_color = "#1a7a1a"
                verdict_bg    = "#d4edda"
            elif score <= -2:
                verdict_label = "⛔ ROLL NEODPORÚČANÝ"
                verdict_color = "#721c24"
                verdict_bg    = "#f8d7da"
            else:
                verdict_label = "🟡 NEUTRÁLNE — závisí od tvojej stratégie"
                verdict_color = "#856404"
                verdict_bg    = "#fff3cd"

            st.markdown(
                f"<div style='background:{verdict_bg};border-radius:8px;padding:12px 18px;"
                f"margin-bottom:12px;'>"
                f"<span style='font-size:1.3em;font-weight:bold;color:{verdict_color};'>"
                f"{verdict_label}</span>"
                f"<span style='color:#555;font-size:0.9em;margin-left:12px;'>"
                f"(skóre: {score:+d})</span>"
                f"</div>",
                unsafe_allow_html=True,
            )

            ra_col, rb_col = st.columns(2)
            with ra_col:
                if pros:
                    st.markdown("**Za roll:**")
                    for p in pros:
                        st.markdown(f"- {p}")
            with rb_col:
                if cons:
                    st.markdown("**Proti rollu:**")
                    for c_item in cons:
                        st.markdown(f"- {c_item}")

            # ── Uložiť do denníka ──────────────────────────────────────────
            st.markdown("")
            _save_key = f"roll_rec_saved_{sel_trade['id']}_{roll_strike}_{roll_dte_str}"
            if st.button("📝 Zapísať odporúčanie do denníka", key="btn_save_roll_rec",
                         use_container_width=False):
                _ticker  = sel_trade.get("ticker", "")
                _opt_str = (
                    f"{sel_trade.get('leg_type','')} {sel_trade.get('option_type','')} "
                    f"${sel_trade.get('strike',0):.0f} exp {sel_trade.get('expiry','')}"
                )
                _roll_str = (
                    f"{'Call' if roll_right == 'C' else 'Put'} "
                    f"${roll_strike:.0f} exp {roll_dte_str}"
                )
                _note_title = (
                    f"Roll analýza {_ticker} — {verdict_label.split()[-1]} "
                    f"[{date.today().strftime('%d.%m.%Y')}]"
                )
                _pros_md  = "\n".join(f"- {p}" for p in pros)  if pros  else "—"
                _cons_md  = "\n".join(f"- {c}" for c in cons)  if cons  else "—"
                _note_body = f"""## Roll analýza — {_ticker}
**Dátum:** {date.today().strftime('%d.%m.%Y')}
**Skupina:** {gid}

### Pozícia
- Aktuálna noha: {_opt_str} (DTE {cur_dte_val}d)
- Roll target: {_roll_str} (DTE {roll_dte_val}d)
- Spot: ${spot:.2f} · IV: {iv*100:.1f}%

### Kľúčové čísla
| Metrika | Pred rollom | Po rolle | Zmena |
|---------|-------------|----------|-------|
| Theta/deň | ${totals['theta']:+.2f} | ${totals_new['theta']:+.2f} | ${delta_theta:+.2f} |
| Delta $ | ${totals['delta']:+.0f} | ${totals_new['delta']:+.0f} | ${delta_abs:+.0f} |
| Unrealized P&L | ${totals['unrealized']:+.0f} | ${totals_new['unrealized']:+.0f} | ${unreal_chg:+.0f} |
| Net flow rollo | | ${net_flow:+.0f} | |

### Verdict: {verdict_label}  (skóre: {score:+d})

### Za roll
{_pros_md}

### Proti rollu
{_cons_md}
"""
                _nid = db.add_note(
                    title=_note_title,
                    content=_note_body,
                    trade_id=int(sel_trade["id"]),
                    group_id=gid or None,
                )
                st.session_state[_save_key] = _nid
                st.success(f"✅ Poznámka #{_nid} uložená do denníka (priradená k obchodu #{sel_trade['id']}, skupina {gid})")

            if _save_key in st.session_state:
                st.caption(f"Posledný zápis: poznámka #{st.session_state[_save_key]}")

# ─── Inicializácia session_state — raz pri prvom načítaní ─────────────────────
# Hodnoty sa zachovajú pri prepínaní stránok a nezresetujú sa pri re-renderi.
_ss_defaults = {
    "man_bid": 4.80,
    "man_ask": 5.20,
    "man_iv": 0.30,
    "roll_entry": 5.0,
    "roll_iv": 0.30,
    "pnl_big": False,
    "cmp_big": False,
}
for _k, _v in _ss_defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

st.title("Strategy Modeler — Roll Simulátor")

# ─── Sekcia: Načítať z pozície ────────────────────────────────────────────────
with st.expander("📡  Načítať údaje z otvorenej pozície (IBKR)", expanded=True):
    if not ibkr.is_connected():
        st.warning("IBKR nie je pripojené. Pripoj sa cez Dashboard alebo zadaj hodnoty manuálne.")
        ibkr_ok = False
    else:
        st.success("IBKR: Pripojený")
        ibkr_ok = True

    open_trades = db.get_open_trades()
    if not open_trades:
        st.info("Žiadne otvorené pozície v denníku.")
    else:
        trade_opts = {
            f"#{t['id']} | {t['ticker']} {t.get('leg_type','')} {t.get('option_type','')} "
            f"${t.get('strike',0):.0f} exp {t.get('expiry','')} [{t.get('group_id','—')}]": t
            for t in open_trades
        }
        sel_trade_lbl = st.selectbox("Vyber otvorenú pozíciu", list(trade_opts.keys()), key="mod_trade_sel")
        sel_trade = trade_opts[sel_trade_lbl]

        # Auto-fetch spot pri výbere pozície (raz za ticker, ak je IBKR pripojený)
        _spot_auto_key = f"mod_spot_auto_{sel_trade['ticker']}"
        if ibkr_ok and _spot_auto_key not in st.session_state:
            with st.spinner(f"Načítavam spot pre {sel_trade['ticker']}..."):
                _auto_res = ibkr.fetch_underlying(sel_trade["ticker"], timeout=6.0)
            if not _auto_res.get("error") and _auto_res.get("price"):
                st.session_state["mod_spot"] = _auto_res["price"]
                st.session_state[_spot_auto_key] = _auto_res["price"]
                st.caption(f"Spot auto-načítaný z IBKR: **${_auto_res['price']:.2f}**")

        c1, c2 = st.columns(2)
        with c1:
            load_chain_btn = st.button("Generuj expirácie (1 rok)", type="primary", key="load_chain_btn")
        with c2:
            load_spot_btn = st.button("Obnov Spot z IBKR", disabled=not ibkr_ok, key="load_spot_btn",
                                      help="Manuálne obnoví spot cenu (auto-refresh prebehne pri prvom výbere)")

        if load_spot_btn:
            spot_placeholder = st.empty()
            spot_placeholder.info("Hľadám cenu v portfóliu...")
            res = ibkr.fetch_underlying(sel_trade["ticker"], timeout=8.0)
            if res["error"]:
                spot_placeholder.error(f"Chyba: {res['error']}")
            else:
                st.session_state["mod_spot"] = res["price"]
                # Reset auto-cache aby sa pri ďalšom prepnutí znova stiahol
                st.session_state[_spot_auto_key] = res["price"]
                src = res.get("source", "")
                spot_placeholder.success(f"Spot: **${res['price']:.2f}** {'(z portfólia)' if src=='portfolio' else '(market data)'}")

        # Cache kľúč — nepotrebujeme sťahovať znova ak sme to urobili pre rovnaký ticker
        chain_cache_key = f"mod_chain_{sel_trade['ticker']}"
        already_cached = chain_cache_key in st.session_state

        col_chain1, col_chain2 = st.columns([3, 2])
        with col_chain1:
            if load_chain_btn:
                chain = ibkr.generate_expirations_local(months=12)
                st.session_state[chain_cache_key] = chain
                st.session_state["mod_expirations"] = chain["expirations"]
                st.session_state["mod_ticker"] = sel_trade["ticker"]
                st.session_state["mod_right"] = "C" if sel_trade.get("option_type") == "Call" else "P"
            elif already_cached:
                cached = st.session_state[chain_cache_key]
                st.session_state["mod_expirations"] = cached["expirations"]
        with col_chain2:
            if already_cached:
                if st.button("Resetovať", key="refresh_chain_btn"):
                    del st.session_state[chain_cache_key]
                    st.rerun()

        # ── Auto-načítanie z TWS portfólia ──────────────────────────────────
        # Dáta berieme priamo z toho čo TWS zobrazuje v portfóliu —
        # market_price → BS výpočet IV → greky. Žiadne nespoľahlivé chain queries.
        _pf_cache_key = f"mod_pf_{sel_trade['ticker']}_{sel_trade.get('expiry')}"
        if ibkr_ok and _pf_cache_key not in st.session_state:
            _pf_res = ibkr.fetch_positions()
            if not _pf_res.get("error"):
                _pf_map = {}
                for _p in _pf_res["positions"]:
                    if _p["sec_type"] == "OPT":
                        _pk = (
                            _p["ticker"].upper(),
                            str(_p.get("strike", "")),
                            str(_p.get("expiry", "")),
                            _p.get("option_type", ""),
                            _p.get("leg_type", ""),
                        )
                        _pf_map[_pk] = _p
                st.session_state[_pf_cache_key] = _pf_map

        _pf_map = st.session_state.get(_pf_cache_key, {})
        _pf_key = (
            sel_trade["ticker"].upper(),
            str(sel_trade.get("strike", "")),
            str(sel_trade.get("expiry", "")),
            sel_trade.get("option_type", ""),
            sel_trade.get("leg_type", ""),
        )
        _pf_match = _pf_map.get(_pf_key)

        # Aktuálny spot (z auto-fetch)
        _cur_spot = float(st.session_state.get("mod_spot", 0.0))

        # Vypočítaj IV a mod_fetched z portfolio market_price
        _sel_expiry = sel_trade.get("expiry", "")
        _sel_right_c = "C" if sel_trade.get("option_type", "Call") == "Call" else "P"
        _sel_strike_c = float(sel_trade.get("strike", 0))

        if _pf_match and _cur_spot > 0:
            from core.probability import calc_iv_from_price as _calc_iv
            _mkt_p = float(_pf_match.get("market_price") or 0)
            _avg_c = float(_pf_match.get("avg_cost") or 0)
            try:
                _dte_c = max(1, (date(int(_sel_expiry[:4]), int(_sel_expiry[4:6]), int(_sel_expiry[6:])) - date.today()).days)
            except Exception:
                _dte_c = 30

            # IV priamo z TWS (modelGreeks) — fallback BS výpočet
            _iv_tws = _pf_match.get("iv")
            _iv_c   = _iv_tws if _iv_tws else (
                _calc_iv(_mkt_p, _cur_spot, _sel_strike_c, _dte_c, _sel_right_c) if _mkt_p > 0 else None
            )
            _iv_source = "TWS modelGreeks" if _iv_tws else "BS (z trhovej ceny)"

            # Greky priamo z TWS — fallback BS
            _d_tws = _pf_match.get("delta")
            _g_tws = _pf_match.get("gamma")
            _t_tws = _pf_match.get("theta")
            _v_tws = _pf_match.get("vega")
            _has_tws_greeks = any(x is not None for x in [_d_tws, _t_tws, _v_tws])

            if _iv_c:
                if _has_tws_greeks:
                    _delta_c = _d_tws
                    _gamma_c = _g_tws
                    _theta_c = _t_tws
                    _vega_c  = _v_tws
                    _iv_source += " · greky z TWS"
                else:
                    _g_c     = calc_greeks(_cur_spot, _sel_strike_c, _dte_c, _iv_c, _sel_right_c)
                    _delta_c = _g_c.get("delta")
                    _gamma_c = _g_c.get("gamma")
                    _theta_c = _g_c.get("theta")
                    _vega_c  = _g_c.get("vega")

                _fetched_now = {
                    "ticker": sel_trade["ticker"],
                    "expiry": _sel_expiry,
                    "strike": _sel_strike_c,
                    "right": _sel_right_c,
                    "bid": None, "ask": None, "last": None,
                    "mid": round(_mkt_p, 3),
                    "iv": _iv_c, "iv_source": _iv_source,
                    "delta": _delta_c, "gamma": _gamma_c,
                    "theta": _theta_c, "vega": _vega_c,
                    "und_price": _cur_spot, "error": None,
                    "avg_cost": _avg_c,
                }
                _fd_prev = st.session_state.get("mod_fetched", {})
                if (_fd_prev.get("strike") != _sel_strike_c or
                        _fd_prev.get("expiry") != _sel_expiry or
                        _fd_prev.get("iv_source", "").startswith("Manuálny") is False):
                    st.session_state["mod_fetched"] = _fetched_now
                    st.session_state["mod_iv"] = _iv_c
                    _iv_disp = f"{_iv_c*100:.1f}%" if _iv_c else "—"
                    st.success(
                        f"📊 Načítané z TWS portfólia — "
                        f"Cena: **${_mkt_p:.2f}** · IV: **{_iv_disp}** "
                        f"({'priamo z TWS' if _iv_tws else 'BS výpočet'}) · Spot: **${_cur_spot:.2f}**"
                    )
        elif _cur_spot > 0 and not _pf_match:
            st.info(
                "Pozícia nenájdená v TWS portfóliu (možno už uzavretá). "
                "Zadaj trhové parametre manuálne nižšie."
            )

        # Manuálne zadanie (záloha keď TWS nemá dáta)
        with st.expander("✏️ Prepísať dáta manuálne (Bid/Ask alebo vlastná IV)"):
            man_c1, man_c2 = st.columns(2)
            _def_spot = float(st.session_state.get("mod_spot", 200.0))
            _def_iv   = float(st.session_state.get("mod_iv", 0.30))
            _def_mid  = float((st.session_state.get("mod_fetched") or {}).get("mid") or 0.0)
            man_spot = man_c1.number_input("Spot ($)", min_value=1.0, step=0.5,
                                            value=_def_spot, key="man_spot")
            man_mid  = man_c1.number_input("Trhová cena opcie ($)", min_value=0.01,
                                            step=0.05, value=max(0.01, _def_mid), key="man_mid")
            man_iv   = man_c2.number_input("IV (0.30=30%)", min_value=0.01, max_value=5.0,
                                            step=0.01, value=_def_iv, key="man_iv")
            man_btn  = man_c2.button("Použiť tieto hodnoty", key="man_calc_btn",
                                      type="primary", use_container_width=True)
            if man_btn:
                try:
                    _dte_m = max(1, (date(int(_sel_expiry[:4]), int(_sel_expiry[4:6]), int(_sel_expiry[6:])) - date.today()).days)
                except Exception:
                    _dte_m = 30
                _iv_m = man_iv
                _g_m  = calc_greeks(man_spot, _sel_strike_c, _dte_m, _iv_m, _sel_right_c)
                st.session_state["mod_fetched"] = {
                    "ticker": sel_trade["ticker"], "expiry": _sel_expiry,
                    "strike": _sel_strike_c, "right": _sel_right_c,
                    "bid": None, "ask": None, "last": None,
                    "mid": man_mid,
                    "iv": _iv_m, "iv_source": "Manuálny vstup",
                    "delta": _g_m.get("delta"), "gamma": _g_m.get("gamma"),
                    "theta": _g_m.get("theta"), "vega": _g_m.get("vega"),
                    "und_price": man_spot, "error": None,
                }
                st.session_state["mod_spot"] = man_spot
                st.session_state["mod_iv"]   = _iv_m
                st.rerun()

        # Výber cieľovej expirácie a strikeov pre roll
        if "mod_expirations" in st.session_state:
            exps = st.session_state["mod_expirations"]

            st.markdown("---")
            st.markdown("**Cieľová expirácia a strike pre roll:**")

            def _fmt_exp(e):
                try:
                    exp_date = date(int(e[:4]), int(e[4:6]), int(e[6:]))
                    dte_days = (exp_date - date.today()).days
                    return f"{exp_date.strftime('%d.%m.%Y')}  ({dte_days}d)"
                except Exception:
                    return e

            exp_fmt = {_fmt_exp(e): e for e in exps}
            fc1, fc2, fc3 = st.columns(3)
            with fc1:
                sel_exp_fmt = st.selectbox("Cieľová expirácia", list(exp_fmt.keys()), key="mod_sel_exp")
                sel_exp = exp_fmt[sel_exp_fmt]
            with fc2:
                current_strike = float(sel_trade.get("strike", 200.0))
                sel_strike = st.number_input(
                    "Strike ($)",
                    value=current_strike,
                    min_value=1.0,
                    step=0.5,
                    key="mod_sel_strike",
                )
            with fc3:
                sel_right = st.selectbox("Call / Put", ["C", "P"],
                                          index=0 if st.session_state.get("mod_right", "C") == "C" else 1,
                                          key="mod_sel_right")
        else:
            sel_exp    = _sel_expiry
            sel_strike = _sel_strike_c
            sel_right  = _sel_right_c

        if "mod_fetched" in st.session_state:
                fd = st.session_state["mod_fetched"]
                st.markdown("---")
                iv_src = fd.get("iv_source") or "—"
                n_contracts = int(sel_trade.get("contracts", 1))
                is_short = sel_trade.get("leg_type", "Long") == "Short"
                sign = -1 if is_short else 1  # short = predal si, ti greky sa otočia

                # ── Základné ceny ──
                mp1, mp2, mp3, mp4 = st.columns(4)
                mp1.metric("Bid", f"${fd.get('bid') or 0:.2f}")
                mp2.metric("Ask", f"${fd.get('ask') or 0:.2f}")
                mp3.metric("Mid", f"${fd.get('mid') or 0:.2f}")
                mp4.metric("IV", f"{fd.get('iv')*100:.1f}%" if fd.get('iv') else "—",
                           help=f"Zdroj: {iv_src}")

                st.markdown("---")
                st.markdown(f"##### Greeks pre tvoju pozíciu  "
                            f"({'Short' if is_short else 'Long'} · {n_contracts} kontrakt{'y' if n_contracts>1 else ''})")
                st.caption("1 kontrakt = 100 akcií podkladu")

                theta_raw = fd.get("theta")  # $ per deň, per 1 akciu (zo BS)
                delta_raw = fd.get("delta")  # $ per $1 pohyb, per 1 akciu
                gamma_raw = fd.get("gamma")
                vega_raw  = fd.get("vega")   # $ per 1% IV, per 1 akciu (z BS: vega/100)
                und_p     = fd.get("und_price") or 0.0

                # Dollar Greeks za CELÚ pozíciu
                theta_day  = theta_raw * 100 * n_contracts * sign if theta_raw else None
                delta_dol  = delta_raw * 100 * n_contracts * sign if delta_raw else None
                delta_pct  = delta_dol * (und_p / 100) if (delta_dol and und_p) else None  # $ per 1% pohyb
                vega_dol   = vega_raw  * 100 * n_contracts * sign if vega_raw  else None
                gamma_dol  = gamma_raw * 100 * n_contracts * sign if gamma_raw else None

                g1, g2, g3, g4 = st.columns(4)
                g1.metric(
                    "Theta (Čas. rozpad / deň)",
                    f"${theta_day:+.2f}" if theta_day is not None else "—",
                    help="Koľko $$ získaš / stratíš každý deň len plynutím času. "
                         "Short pozícia: + = časový rozpad ti ide DO VRECKA. "
                         "Long: - = platíš rozpad."
                )
                g2.metric(
                    "Delta (na $1 pohyb spotu)",
                    f"${delta_dol:+.2f}" if delta_dol is not None else "—",
                    help="O koľko sa zmení hodnota pozície ak cena podkladu ide o $1. "
                         "+ = profituješ z rastu, - = profituješ z poklesu."
                )
                g3.metric(
                    "Delta (na 1% pohyb spotu)",
                    f"${delta_pct:+.2f}" if delta_pct is not None else "—",
                    help=f"Spot ${und_p:.0f} × 1% = ${und_p*0.01:.1f} pohyb → táto suma × delta"
                )
                g4.metric(
                    "Vega (na 1% zmenu IV)",
                    f"${vega_dol:+.2f}" if vega_dol is not None else "—",
                    help="O koľko sa zmení hodnota ak IV vzrastie o 1 percentný bod. "
                         "Long: + (IV volatilita ti pomáha). Short: - (IV ti škodí)."
                )

                # ── Delta / Theta ratio ──
                st.markdown("---")
                if theta_day is not None and delta_dol is not None and theta_day != 0:
                    ratio = abs(delta_dol) / abs(theta_day)
                    if is_short:
                        ratio_msg = (
                            f"**Delta/Theta = {ratio:.1f}×** — "
                            f"aby $1 pohyb spotu vymazal 1 deň časového rozpadu, "
                            f"spot musí ísť o **${abs(theta_day)/abs(delta_dol) if delta_dol else 0:.2f}**. "
                            f"Čím väčšie číslo, tým viac ťaháš z theta voči delta riziku."
                        )
                    else:
                        ratio_msg = (
                            f"**Delta/Theta = {ratio:.1f}×** — "
                            f"potrebuješ pohyb **${abs(theta_day)/abs(delta_dol) if delta_dol else 0:.2f} / deň** "
                            f"len aby si pokryl dennú ztrátu z theta (časového rozpadu)."
                        )
                    st.info(ratio_msg)

                # ── P&L diagram — teoretický priebeh v čase ──
                st.markdown("---")
                st.markdown("##### P&L diagram — teoretický priebeh v čase")

                mid_price = fd.get("mid") or 0.0
                k         = fd.get("strike", 0.0)
                right_str = fd.get("right", "C")
                _fd_exp   = fd.get("expiry", "")
                try:
                    _fd_dte = max(1, (date(int(_fd_exp[:4]), int(_fd_exp[4:6]), int(_fd_exp[6:])) - date.today()).days)
                except Exception:
                    _fd_dte = 30
                _iv_fd = fd.get("iv") or 0.30

                if und_p and k and mid_price:
                    import plotly.graph_objects as _go
                    import numpy as _np
                    from core.probability import bs_price as _bs_price, calc_sd_lines as _csd

                    _show_dte = st.slider(
                        "Zobraz P&L k tomuto DTE (dní do expirácie)",
                        min_value=0, max_value=_fd_dte,
                        value=min(_fd_dte, max(1, _fd_dte // 2)),
                        step=1, key="pnl_dte_slider",
                        help="Posuň vľavo = bližšie k expirácii, vpravo = dnes"
                    )

                    price_range = _np.linspace(und_p * 0.55, und_p * 1.45, 400)

                    def _pnl_at_dte(s_arr, d):
                        if d <= 0:
                            theo = _np.maximum(0.0, s_arr - k) if right_str == "C" else _np.maximum(0.0, k - s_arr)
                        else:
                            theo = _np.array([_bs_price(float(s), k, d, _iv_fd, right_str) or 0.0 for s in s_arr])
                        raw = (mid_price - theo) * 100 * n_contracts if is_short else (theo - mid_price) * 100 * n_contracts
                        return _np.round(raw, 0)

                    time_slices = [
                        (_fd_dte,               "#60a5fa", f"Teraz ({_fd_dte}d)",      2.0),
                        (max(1, _fd_dte * 2//3), "#a78bfa", f"{_fd_dte*2//3}d",        1.5),
                        (max(1, _fd_dte // 3),   "#fb923c", f"{_fd_dte//3}d",          1.5),
                        (0,                     "#f43f5e", "Expirácia (0d)",           2.5),
                    ]

                    fig_pnl = _go.Figure()
                    pnl_slider = _pnl_at_dte(price_range, _show_dte)
                    fig_pnl.add_trace(_go.Scatter(
                        x=price_range, y=_np.where(pnl_slider >= 0, pnl_slider, 0),
                        fill="tozeroy", fillcolor="rgba(46,204,113,0.08)",
                        line=dict(width=0), showlegend=False, hoverinfo="skip",
                    ))
                    fig_pnl.add_trace(_go.Scatter(
                        x=price_range, y=_np.where(pnl_slider < 0, pnl_slider, 0),
                        fill="tozeroy", fillcolor="rgba(231,76,60,0.07)",
                        line=dict(width=0), showlegend=False, hoverinfo="skip",
                    ))
                    for d_val, col, lbl, lw in time_slices:
                        pnl_t = _pnl_at_dte(price_range, d_val)
                        fig_pnl.add_trace(_go.Scatter(
                            x=price_range, y=pnl_t, mode="lines",
                            line=dict(color=col, width=lw), name=lbl,
                            hovertemplate=f"{lbl} — Spot: $%{{x:.1f}}  P&L: $%{{y:+.0f}}<extra></extra>",
                        ))
                    slider_is_dup = any(abs(d - _show_dte) <= 1 for d, *_ in time_slices)
                    if not slider_is_dup:
                        pnl_s = _pnl_at_dte(price_range, _show_dte)
                        fig_pnl.add_trace(_go.Scatter(
                            x=price_range, y=pnl_s, mode="lines",
                            line=dict(color="#facc15", width=3, dash="dash"),
                            name=f"Slider {_show_dte}d",
                            hovertemplate=f"DTE {_show_dte}d — Spot: $%{{x:.1f}}  P&L: $%{{y:+.0f}}<extra></extra>",
                        ))

                    fig_pnl.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
                    fig_pnl.add_vline(x=und_p, line_color="#fbbf24", line_width=2, line_dash="dash",
                                      annotation_text=f"Spot ${und_p:.0f}",
                                      annotation_position="top right", annotation_font_color="#fbbf24")
                    be = (k + mid_price if right_str == "C" else k - mid_price) if not is_short else \
                         (k - mid_price if right_str == "C" else k + mid_price)
                    fig_pnl.add_vline(x=be, line_color="#f97316", line_width=1, line_dash="dot",
                                      annotation_text=f"BE ${be:.1f}",
                                      annotation_position="top left", annotation_font_color="#f97316")

                    _sd = _csd(und_p, _iv_fd, max(1, _show_dte))
                    for lvl, lbl, col in [
                        (_sd.upper_1sd, "1SD+", "rgba(96,165,250,0.6)"),
                        (_sd.lower_1sd, "1SD−", "rgba(96,165,250,0.6)"),
                        (_sd.upper_2sd, "2SD+", "rgba(167,139,250,0.5)"),
                        (_sd.lower_2sd, "2SD−", "rgba(167,139,250,0.5)"),
                    ]:
                        fig_pnl.add_vline(x=lvl, line_color=col, line_width=1, line_dash="dot",
                                          annotation_text=lbl, annotation_font_size=10, annotation_font_color=col)

                    _pos_lbl = f"{'Short' if is_short else 'Long'} {'Call' if right_str=='C' else 'Put'}"
                    fig_pnl.update_layout(
                        title=f"P&L — {fd.get('ticker')} {_pos_lbl} ${k:.0f}  ·  vstup ${mid_price:.2f}  ·  {n_contracts}× kontrakt",
                        xaxis_title="Cena podkladu ($)",
                        yaxis_title="P&L ($)",
                        height=440, showlegend=True,
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                        margin=dict(l=60, r=60, t=80, b=50),
                        plot_bgcolor="rgba(20,20,30,0.97)",
                        paper_bgcolor="rgba(20,20,30,0.0)",
                        font_color="#e0e0e0",
                        hovermode="x unified",
                    )
                    _big = st.session_state.get("pnl_big", False)
                    fig_pnl.update_layout(
                        height=750 if _big else 440,
                        yaxis=dict(tickformat="$,.0f"),
                    )
                    _zoom_col, _ = st.columns([1, 5])
                    if _zoom_col.button("🔲 " + ("Zmenši" if _big else "Zväčši"), key="pnl_zoom_btn"):
                        st.session_state["pnl_big"] = not _big
                        st.rerun()
                    st.plotly_chart(fig_pnl, width="stretch", key="main_pnl_chart")
                    st.caption(
                        "**Modré SD pásma** = pravdepodobnostný rozsah pre DTE zo slidera. "
                        "**Žltá prerušovaná** = slider DTE. "
                        "Čiary: modrá=teraz → fialová → oranžová → červená=expirácia. "
                        "Ikona 🔍 (hore vpravo grafu) = zoom / fullscreen."
                    )

                    # ── Ulož snapshot do denníka ──────────────────────────────
                    st.markdown("---")
                    snap_c1, snap_c2 = st.columns([3, 1])
                    snap_note = snap_c1.text_input(
                        "Poznámka k snapshotu (voliteľné)",
                        placeholder="napr. Zvažujem roll na 210, theta dobrá...",
                        key="snap_note_input",
                    )
                    if snap_c2.button("📸 Ulož snapshot do denníka", key="save_snapshot_btn", use_container_width=True):
                        _pnl_now = float(_pnl_at_dte(_np.array([und_p]), _fd_dte)[0])
                        _snap_md = f"""## 📸 Snapshot — {fd.get('ticker')} {'Short' if is_short else 'Long'} {'Call' if right_str=='C' else 'Put'} ${k:.0f}

**Dátum:** {date.today()}  ·  **Expiry:** {_fd_exp}  ·  **DTE:** {_fd_dte}d  ·  **Kontrakty:** {n_contracts}×

### Cena opcie
| Bid | Ask | Mid (vstup) | IV | Spot |
|-----|-----|------------|-----|------|
| ${fd.get('bid') or '—'} | ${fd.get('ask') or '—'} | **${mid_price:.2f}** | **{_iv_fd*100:.1f}%** | **${und_p:.2f}** |

### Greeks (celá pozícia, v $)
| Theta/deň | Delta ($1 pohyb) | Delta (1% pohyb) | Vega (1% IV) |
|-----------|-----------------|-----------------|-------------|
| **${theta_day:+.2f}** | **${delta_dol:+.2f}** | **${delta_pct:+.2f}** | **${vega_dol:+.2f}** |

**Teor. P&L pri aktuálnom spote:** ${_pnl_now:+.0f}

{('**Poznámka:** ' + snap_note) if snap_note else ''}
"""
                        _snap_title = (
                            f"P&L Snapshot — {fd.get('ticker','')} "
                            f"${fd.get('strike',0):.0f} exp {fd.get('expiry','')} "
                            f"[{date.today().strftime('%d.%m.%Y')}]"
                        )
                        db.add_note(
                            title=_snap_title,
                            content=_snap_md,
                            trade_id=sel_trade.get("id"),
                            group_id=sel_trade.get("group_id"),
                        )
                        st.success(f"Snapshot uložený do Poznámok (priradený k Trade #{sel_trade.get('id')}).")

                    # ── Roll porovnanie ────────────────────────────────────────
                    st.markdown("---")
                    st.markdown("##### Roll porovnanie — aktuálna vs. nová pozícia")
                    st.caption("Porovnaj P&L krivky: čo sa stane ak zrolluješ na iný strike alebo expiráciu.")

                    roll_c1, roll_c2, roll_c3, roll_c4 = st.columns(4)
                    # Inicializuj roll hodnoty len ak ešte nie sú nastavené
                    _roll_key = f"roll_init_{fd.get('ticker')}_{k}"
                    if _roll_key not in st.session_state:
                        st.session_state["roll_strike"] = float(k) + 5.0
                        st.session_state["roll_entry"]  = float(mid_price)
                        st.session_state["roll_iv"]     = float(_iv_fd)
                        st.session_state[_roll_key]     = True

                    roll_strike = roll_c1.number_input(
                        "Roll Strike ($)", min_value=0.5, step=0.5, key="roll_strike"
                    )
                    roll_entry = roll_c2.number_input(
                        "Roll vstupná cena ($)", min_value=0.01, step=0.1, key="roll_entry",
                        help="Cena za ktorú by si otvoril novú pozíciu po rolle"
                    )
                    roll_right = roll_c3.selectbox(
                        "Roll Call/Put", ["C", "P"],
                        index=0 if right_str == "C" else 1, key="roll_right"
                    )
                    roll_iv = roll_c4.number_input(
                        "Roll IV", min_value=0.01, max_value=5.0, step=0.01,
                        key="roll_iv", help="IV pre roll pozíciu (môže byť odlišná)"
                    )

                    # BS odhad ceny pre roll target (z aktuálnej IV)
                    try:
                        _roll_dte_est = max(1, (date(int(sel_exp[:4]), int(sel_exp[4:6]), int(sel_exp[6:])) - date.today()).days)
                    except Exception:
                        _roll_dte_est = 30
                    _roll_bs_est = bs_price(und_p, roll_strike, _roll_dte_est, roll_iv, roll_right)
                    if _roll_bs_est and _roll_bs_est > 0:
                        st.caption(
                            f"BS odhad pre ${roll_strike:.0f} {sel_exp} "
                            f"(IV {roll_iv*100:.1f}%): **${_roll_bs_est:.2f}**  "
                            f"— použi ako referenčnú cenu pri zadávaní roll_entry"
                        )

                    def _pnl_roll(s_arr, d):
                        if d <= 0:
                            theo = _np.maximum(0.0, s_arr - roll_strike) if roll_right == "C" else _np.maximum(0.0, roll_strike - s_arr)
                        else:
                            theo = _np.array([_bs_price(float(s), roll_strike, d, roll_iv, roll_right) or 0.0 for s in s_arr])
                        raw = (roll_entry - theo) * 100 * n_contracts if is_short else (theo - roll_entry) * 100 * n_contracts
                        return _np.round(raw, 0)

                    fig_cmp = _go.Figure()
                    cmp_slices = [
                        (_fd_dte,               True,  "#60a5fa", "#22d3ee",  f"Teraz ({_fd_dte}d)"),
                        (max(1, _fd_dte // 2),  True,  "#a78bfa", "#34d399",  f"{_fd_dte//2}d"),
                        (0,                     True,  "#f43f5e", "#86efac",  "Expirácia"),
                    ]
                    for d_v, _, col_cur, col_roll, lbl in cmp_slices:
                        cur = _pnl_at_dte(price_range, d_v)
                        rol = _pnl_roll(price_range, d_v)
                        fig_cmp.add_trace(_go.Scatter(
                            x=price_range, y=cur, mode="lines",
                            line=dict(color=col_cur, width=2), name=f"Aktuál {lbl}",
                            hovertemplate=f"Aktuál {lbl} — $%{{x:.1f}} → $%{{y:+.0f}}<extra></extra>",
                        ))
                        fig_cmp.add_trace(_go.Scatter(
                            x=price_range, y=rol, mode="lines",
                            line=dict(color=col_roll, width=2, dash="dash"), name=f"Roll ${roll_strike:.0f} {lbl}",
                            hovertemplate=f"Roll {lbl} — $%{{x:.1f}} → $%{{y:+.0f}}<extra></extra>",
                        ))

                    fig_cmp.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
                    fig_cmp.add_vline(x=und_p, line_color="#fbbf24", line_width=2, line_dash="dash",
                                      annotation_text=f"Spot ${und_p:.0f}", annotation_font_color="#fbbf24")
                    fig_cmp.add_vline(x=float(k), line_color="rgba(96,165,250,0.6)", line_width=1,
                                      annotation_text=f"K_aktuál ${k:.0f}", annotation_font_color="rgba(96,165,250,0.8)")
                    fig_cmp.add_vline(x=roll_strike, line_color="rgba(52,211,153,0.6)", line_width=1, line_dash="dot",
                                      annotation_text=f"K_roll ${roll_strike:.0f}", annotation_font_color="rgba(52,211,153,0.8)")

                    fig_cmp.update_layout(
                        title=f"Roll porovnanie — ${k:.0f} vs. ${roll_strike:.0f}  ·  vstup ${mid_price:.2f} → ${roll_entry:.2f}",
                        xaxis_title="Cena podkladu ($)", yaxis_title="P&L ($)",
                        height=460, showlegend=True,
                        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
                        margin=dict(l=60, r=60, t=80, b=50),
                        plot_bgcolor="rgba(20,20,30,0.97)",
                        paper_bgcolor="rgba(20,20,30,0.0)",
                        font_color="#e0e0e0",
                        hovermode="x unified",
                    )
                    _big_cmp = st.session_state.get("cmp_big", False)
                    fig_cmp.update_layout(
                        height=750 if _big_cmp else 460,
                        yaxis=dict(tickformat="$,.0f"),
                    )
                    _zoom_cmp, _ = st.columns([1, 5])
                    if _zoom_cmp.button("🔲 " + ("Zmenši" if _big_cmp else "Zväčši"), key="cmp_zoom_btn"):
                        st.session_state["cmp_big"] = not _big_cmp
                        st.rerun()
                    st.plotly_chart(fig_cmp, width="stretch", key="roll_cmp_chart")
                    st.caption(
                        "**Plné čiary** = aktuálna pozícia. **Prerušované** = roll pozícia. "
                        "Ak prerušovaná je vyššie → roll je lepší pri tej cene podkladu."
                    )

                else:
                    st.info("Zadaj cenu opcie, spot a IV aby sa zobrazil P&L diagram.")

                if iv_src in ("BS kalkulácia", "Manuálny vstup"):
                    st.caption("Greeks a IV vypočítané z BS modelu.")

# ─── Skupinový kontext — vždy viditeľný po výbere pozície ───────────────────
_ctx_spot = float(st.session_state.get("mod_spot", 0.0))
_ctx_iv   = float(st.session_state.get("mod_iv", 0.0))
_ctx_trade = None

# Znovu načítame sel_trade mimo expandera (môže byť z iného scope)
if "mod_trade_sel" in st.session_state:
    _all_open_ctx = db.get_open_trades()
    _trade_opts_ctx = {
        f"#{t['id']} | {t['ticker']} {t.get('leg_type','')} {t.get('option_type','')} "
        f"${t.get('strike',0):.0f} exp {t.get('expiry','')} [{t.get('group_id','—')}]": t
        for t in _all_open_ctx
    }
    _ctx_trade = _trade_opts_ctx.get(st.session_state["mod_trade_sel"])

if _ctx_trade and _ctx_spot > 0 and _ctx_iv > 0:
    _ctx_roll_s  = float(st.session_state.get("roll_strike", float(_ctx_trade.get("strike", 0) or 0) + 5.0))
    _ctx_roll_e  = float(st.session_state.get("roll_entry",  0.0))
    _ctx_roll_iv = float(st.session_state.get("roll_iv",     _ctx_iv))
    _ctx_roll_r  = st.session_state.get("roll_right", "C" if _ctx_trade.get("option_type") == "Call" else "P")
    _ctx_exp     = st.session_state.get("mod_sel_exp_raw", _ctx_trade.get("expiry", ""))
    # Zisti raw expiráciu z mod_expirations + sel index
    if "mod_expirations" in st.session_state:
        _exps_list = st.session_state["mod_expirations"]
        if _exps_list:
            _ctx_exp = _exps_list[0]   # default = prvá expirácia
            # Skús nájsť podľa session_state kľúča selectboxu
            _sel_exp_label = st.session_state.get("mod_sel_exp", "")
            for _e in _exps_list:
                try:
                    _ed = date(int(_e[:4]), int(_e[4:6]), int(_e[6:]))
                    _dte_d = (_ed - date.today()).days
                    if f"{_ed.strftime('%d.%m.%Y')}  ({_dte_d}d)" == _sel_exp_label:
                        _ctx_exp = _e
                        break
                except Exception:
                    pass

    _show_group_context(
        _ctx_trade, _ctx_spot, _ctx_iv,
        _ctx_roll_s, _ctx_roll_e, _ctx_roll_iv, _ctx_roll_r, _ctx_exp,
    )
elif _ctx_trade and _ctx_trade.get("group_id"):
    if _ctx_spot <= 0 or _ctx_iv <= 0:
        st.info(
            f"Pre skupinový kontext (**{_ctx_trade.get('group_id')}**) zadaj "
            f"**Spot** a **IV** — načítaj ich z IBKR alebo vlož manuálne v sekcii vyššie."
        )

st.divider()

# ─── PoP kalkulačka (jednoduchá, bez pozície) ──────────────────────────────────
with st.expander("PoP kalkulačka — rýchly výpočet pravdepodobnosti (bez pozície z IBKR)"):
    st.caption("Zadaj parametre ručne ak nechceš načítavať pozíciu z IBKR.")
    _default_spot = float(st.session_state.get("mod_spot", 200.0))
    _default_iv = float(st.session_state.get("mod_iv", 0.30))

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        ticker = st.text_input("Ticker", value=st.session_state.get("mod_ticker", "AMZN"), key="pop_ticker").upper()
    with col2:
        spot = st.number_input("Spot cena ($)", value=_default_spot, min_value=1.0, step=0.5, key="pop_spot")
    with col3:
        iv = st.number_input("IV (napr. 0.30 = 30%)", value=_default_iv, min_value=0.01, max_value=5.0, step=0.01, key="pop_iv")
    with col4:
        r = st.number_input("Risk-free rate", value=0.05, min_value=0.0, max_value=0.2, step=0.005, key="pop_r")

    strategy_type = st.selectbox(
        "Typ stratégie",
        ["Short Call", "Short Put", "Long Call", "Long Put", "Diagonal (Call)", "Diagonal (Put)", "Short Strangle"],
        key="pop_strategy",
    )

    # ─── Parametre podľa stratégie ─────────────────────────────────────────
    if strategy_type == "Short Strangle":
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            dte = st.number_input("DTE (dni)", value=30, min_value=1, max_value=730, step=1, key="pop_dte")
        with col_b:
            put_strike_orig = st.number_input("Pôvodný Put Strike ($)", value=round(spot * 0.90, 0), step=0.5, key="pop_put_orig")
        with col_c:
            call_strike_orig = st.number_input("Pôvodný Call Strike ($)", value=round(spot * 1.10, 0), step=0.5, key="pop_call_orig")
        col_s1, col_s2 = st.columns(2)
        with col_s1:
            new_put = st.slider("Nový Put Strike", min_value=float(spot * 0.5), max_value=float(spot * 0.99), value=float(put_strike_orig), step=0.5, key="pop_new_put")
        with col_s2:
            new_call = st.slider("Nový Call Strike", min_value=float(spot * 1.01), max_value=float(spot * 1.5), value=float(call_strike_orig), step=0.5, key="pop_new_call")
        orig_pop = pop_strangle(spot, put_strike_orig, call_strike_orig, int(dte), iv, r)
        new_pop  = pop_strangle(spot, new_put, new_call, int(dte), iv, r)
        sd = calc_sd_lines(spot, iv, int(dte))
        orig_strikes = [put_strike_orig, call_strike_orig]
        orig_labels  = [f"Orig Put ${put_strike_orig:.0f}", f"Orig Call ${call_strike_orig:.0f}"]
        new_strikes  = [new_put, new_call]
        new_labels   = [f"Roll Put ${new_put:.0f}", f"Roll Call ${new_call:.0f}"]
    else:
        col_a, col_b = st.columns(2)
        with col_a:
            dte = st.number_input("DTE short nohy (dni)", value=30, min_value=1, max_value=730, step=1, key="pop_dte2")
        with col_b:
            orig_strike = st.number_input("Pôvodný Strike ($)", value=round(spot * 1.05, 0) if "Call" in strategy_type else round(spot * 0.95, 0), step=0.5, key="pop_orig_strike")
        if "Diagonal" in strategy_type:
            col_c, col_d = st.columns(2)
            with col_c:
                long_strike = st.number_input("Long Strike ($)", value=round(spot * 0.95, 0) if "Call" in strategy_type else round(spot * 1.05, 0), step=0.5, key="pop_long_strike")
            with col_d:
                long_dte = st.number_input("DTE long nohy (dni)", value=90, min_value=1, max_value=730, step=1, key="pop_long_dte")
        strike_min = spot * 0.6 if "Put" in strategy_type else spot * 1.0
        strike_max = spot * 1.0 if "Put" in strategy_type else spot * 1.6
        new_strike = st.slider(f"Nový Strike — Roll target ({strategy_type})", min_value=float(strike_min), max_value=float(strike_max), value=float(orig_strike), step=0.5, key="pop_new_strike")
        def _calc_pop(s):
            if strategy_type == "Short Call":   return pop_short_call(spot, s, int(dte), iv, r)
            elif strategy_type == "Short Put":  return pop_short_put(spot, s, int(dte), iv, r)
            elif strategy_type == "Long Call":  return pop_long_call(spot, s, int(dte), iv, r)
            elif strategy_type == "Long Put":   return pop_long_put(spot, s, int(dte), iv, r)
            elif strategy_type == "Diagonal (Call)": return pop_diagonal(spot, s, int(dte), iv, "call", r)
            elif strategy_type == "Diagonal (Put)":  return pop_diagonal(spot, s, int(dte), iv, "put", r)
            return None
        orig_pop = _calc_pop(orig_strike)
        new_pop  = _calc_pop(new_strike)
        sd = calc_sd_lines(spot, iv, int(dte))
        orig_strikes = [orig_strike]
        orig_labels  = [f"Orig ${orig_strike:.0f}"]
        new_strikes  = [new_strike]
        new_labels   = [f"Roll ${new_strike:.0f}"]

    # ─── Výsledky ──────────────────────────────────────────────────────────
    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spot", f"${spot:.2f}")
    m2.metric("IV", f"{iv*100:.1f}%")
    m3.metric("PoP — PÔVODNÝ", f"{orig_pop*100:.1f}%" if orig_pop else "—")
    delta_pop = (new_pop - orig_pop) * 100 if (orig_pop and new_pop) else None
    m4.metric("PoP — ROLLED", f"{new_pop*100:.1f}%" if new_pop else "—",
              delta=f"{delta_pop:+.1f}%" if delta_pop is not None else None)

    sd_df = pd.DataFrame({
        "": ["1SD (~68%)", "2SD (~95%)"],
        "Upper": [f"${sd.upper_1sd:.0f}", f"${sd.upper_2sd:.0f}"],
        "Lower": [f"${sd.lower_1sd:.0f}", f"${sd.lower_2sd:.0f}"],
        "Pohyb ±": [f"${sd.sd_move:.0f}", f"${sd.sd_move*2:.0f}"],
    })
    st.dataframe(sd_df, use_container_width=True, hide_index=True)

    compare_rows = []
    for strike_val, label, pop_v in zip(orig_strikes + new_strikes, orig_labels + new_labels,
                                         [orig_pop]*len(orig_strikes) + [new_pop]*len(new_strikes)):
        sd_v = calc_sd_lines(spot, iv, int(dte))
        dist_pct = (strike_val - spot) / spot * 100
        compare_rows.append({
            "Popis": label, "Strike ($)": strike_val,
            "Vzdial. od spot": f"{dist_pct:+.1f}%",
            "V 1SD?": "Áno" if sd_v.lower_1sd <= strike_val <= sd_v.upper_1sd else "Nie",
            "V 2SD?": "Áno" if sd_v.lower_2sd <= strike_val <= sd_v.upper_2sd else "Nie",
            "PoP": f"{pop_v*100:.1f}%" if pop_v else "—",
        })
    st.dataframe(pd.DataFrame(compare_rows), use_container_width=True, hide_index=True)

    tab_bell, tab_sd = st.tabs(["Bell Curve", "SD Línie"])
    all_strikes = orig_strikes + new_strikes
    all_labels  = orig_labels  + new_labels
    with tab_bell:
        st.plotly_chart(bell_curve_chart(spot, iv, int(dte), ticker, all_strikes, all_labels), width="stretch")
    with tab_sd:
        st.plotly_chart(sd_lines_chart(sd, ticker, all_strikes, all_labels), width="stretch")

    import plotly.graph_objects as go
    sweep_strikes = np.linspace(spot * (0.9 if "Call" in strategy_type else 0.7),
                                spot * (1.3 if "Call" in strategy_type else 1.1), 80)
    pop_values = []
    for s_val in sweep_strikes:
        p = _calc_pop(s_val) if strategy_type != "Short Strangle" else None
        pop_values.append(p * 100 if p is not None else None)
    fig_sweep = go.Figure()
    fig_sweep.add_trace(go.Scatter(x=sweep_strikes, y=pop_values, mode="lines",
                                   line=dict(color="#2ecc71", width=2), name="PoP (%)",
                                   hovertemplate="Strike: $%{x:.1f}<br>PoP: %{y:.1f}%<extra></extra>"))
    if strategy_type != "Short Strangle":
        fig_sweep.add_vline(x=orig_strike, line_color="rgba(241,196,15,0.9)", line_dash="dash",
                            annotation_text=f"Orig ${orig_strike:.0f}", annotation_position="top right")
        fig_sweep.add_vline(x=new_strike, line_color="rgba(231,76,60,0.85)", line_dash="dashdot",
                            annotation_text=f"Roll ${new_strike:.0f}", annotation_position="top left")
    fig_sweep.add_vline(x=spot, line_color="rgba(200,200,200,0.5)", line_width=1,
                        annotation_text="Spot", annotation_position="top")
    fig_sweep.update_layout(
        title=f"{strategy_type} — PoP podľa strike  |  DTE: {dte}  |  IV: {iv*100:.1f}%",
        xaxis_title="Strike ($)", yaxis_title="PoP (%)", height=350,
        margin=dict(l=60, r=60, t=60, b=40),
        plot_bgcolor="rgba(20,20,30,0.95)", paper_bgcolor="rgba(20,20,30,0.0)", font_color="#e0e0e0",
    )
    st.plotly_chart(fig_sweep, width="stretch")
