import streamlit as st
import pandas as pd
import numpy as np
from datetime import date

from core.probability import (
    calc_sd_lines,
    pop_short_call, pop_short_put,
    pop_long_call, pop_long_put,
    pop_diagonal, pop_strangle,
)
from core.charts import bell_curve_chart, sd_lines_chart
from core import database as db
from core import ibkr

db.init_db()

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

        c1, c2 = st.columns(2)
        with c1:
            load_chain_btn = st.button("Generuj expirácie (1 rok)", type="primary", key="load_chain_btn")
        with c2:
            load_spot_btn = st.button("Načítaj Spot cenu z IBKR", disabled=not ibkr_ok, key="load_spot_btn")

        if load_spot_btn:
            spot_placeholder = st.empty()
            spot_placeholder.info("Hľadám cenu v portfóliu...")
            res = ibkr.fetch_underlying(sel_trade["ticker"], timeout=8.0)
            if res["error"]:
                spot_placeholder.error(f"Chyba: {res['error']}")
            else:
                st.session_state["mod_spot"] = res["price"]
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

        # Výber expirácie a strikeov pre fetch
        if "mod_expirations" in st.session_state:
            exps = st.session_state["mod_expirations"]

            st.markdown("---")
            st.markdown("**Vyber cieľovú expiráciu a strike pre roll:**")

            def _fmt_exp(e):
                try:
                    exp_date = date(int(e[:4]), int(e[4:6]), int(e[6:]))
                    dte_days = (exp_date - date.today()).days
                    return f"{exp_date.strftime('%d.%m.%Y')}  ({dte_days}d)"
                except:
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
                    help="Zadaj strike manuálne alebo uprav podľa potreby",
                )
            with fc3:
                sel_right = st.selectbox("Call / Put", ["C", "P"],
                                          index=0 if st.session_state.get("mod_right", "C") == "C" else 1,
                                          key="mod_sel_right")

            btn_col, manual_col = st.columns([2, 3])
            with btn_col:
                fetch_data_btn = st.button(
                    f"Načítaj z IBKR",
                    type="primary", key="fetch_opt_btn",
                    disabled=not ibkr_ok,
                    help=f"{sel_trade['ticker']} {sel_exp} ${sel_strike:.0f} {'Call' if sel_right=='C' else 'Put'}"
                )
            with manual_col:
                with st.expander("Zadaj cenu manuálne (ak IBKR nefunguje)"):
                    man_c1, man_c2, man_c3 = st.columns(3)
                    man_bid  = man_c1.number_input("Bid ($)", min_value=0.0, step=0.05, key="man_bid")
                    man_ask  = man_c1.number_input("Ask ($)", min_value=0.0, step=0.05, key="man_ask")
                    # mod_spot sa aktualizuje z IBKR — synchronizujeme s man_spot
                    if "man_spot" not in st.session_state:
                        st.session_state["man_spot"] = float(st.session_state.get("mod_spot", 200.0))
                    man_spot = man_c2.number_input("Spot ($)", min_value=1.0, step=0.5, key="man_spot")
                    man_iv   = man_c2.number_input("IV (0.30=30%)", min_value=0.01, max_value=5.0, step=0.01, key="man_iv")
                    man_c3.markdown("&nbsp;")
                    man_btn  = man_c3.button("Vypočítaj Greeks", key="man_calc_btn", type="primary", use_container_width=True)
                    if man_btn:
                        from core.probability import calc_iv_from_price, calc_greeks
                        try:
                            _dte_m = max(1, (date(int(sel_exp[:4]), int(sel_exp[4:6]), int(sel_exp[6:])) - date.today()).days)
                        except Exception:
                            _dte_m = 30
                        man_mid = round((man_bid + man_ask) / 2, 3) if man_ask > 0 else man_bid
                        _iv_m = man_iv if man_iv else calc_iv_from_price(man_mid, man_spot, float(sel_strike), _dte_m, sel_right)
                        _g_m  = calc_greeks(man_spot, float(sel_strike), _dte_m, _iv_m, sel_right) if _iv_m else {}
                        st.session_state["mod_fetched"] = {
                            "ticker": sel_trade["ticker"], "expiry": sel_exp,
                            "strike": float(sel_strike), "right": sel_right,
                            "bid": man_bid, "ask": man_ask, "last": None,
                            "mid": man_mid,
                            "iv": _iv_m, "iv_source": "Manuálny vstup",
                            "delta": _g_m.get("delta"), "gamma": _g_m.get("gamma"),
                            "theta": _g_m.get("theta"), "vega": _g_m.get("vega"),
                            "und_price": man_spot, "error": None,
                        }
                        st.session_state["mod_spot"] = man_spot
                        st.session_state["mod_iv"]   = _iv_m

            if fetch_data_btn:
                with st.spinner("Načítavam opčné dáta z IBKR..."):
                    opt_data = ibkr.fetch_option_data(
                        sel_trade["ticker"], sel_exp, float(sel_strike), sel_right
                    )
                if opt_data.get("error"):
                    st.error(f"IBKR: {opt_data['error']}  — použi manuálny vstup vyššie.")
                else:
                    st.session_state["mod_fetched"] = opt_data
                    st.session_state["mod_spot"] = opt_data.get("und_price") or st.session_state.get("mod_spot", 200.0)
                    if opt_data.get("iv"):
                        st.session_state["mod_iv"] = opt_data["iv"]

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
                        from core import database as _db2
                        _db2.add_note(
                            text=_snap_md,
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
                    # (pri prvom zobrazení nastav z aktuálnej pozície)
                    _roll_key = f"roll_init_{fd.get('ticker')}_{k}"
                    if _roll_key not in st.session_state:
                        st.session_state["roll_strike"] = float(k) + 5.0
                        st.session_state["roll_entry"]  = float(mid_price)
                        st.session_state["roll_iv"]     = float(_iv_fd)
                        st.session_state[_roll_key]     = True  # označí že už bolo init

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
