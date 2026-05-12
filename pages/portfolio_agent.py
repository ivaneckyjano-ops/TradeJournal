"""
Portfolio Agent – prehľad a AI analýza celého portfólia call diagonalov.
Pokrýva: AMZN, AAPL, GOOGL, MSFT, TSLA, NVDA, META
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from core import database as db
from core import agent as ai_agent
from core import ibkr
from core import portfolio_data as pdata
from core.page_context import render_ai_chat_markdown, set_tradejournal_page

db.init_db()
set_tradejournal_page("portfolio_agent")

# ── Konštanty ────────────────────────────────────────────────────────────────
WATCHED          = ai_agent.WATCHED_TICKERS
BETA             = ai_agent.TICKER_BETA
SHORT_DTE_ALERT  = 14
MIN_THETA_GROUP  = 0.0
# Archív vyhodnotení agenta v DB (približne posledné 3 mesiace)
_AGENT_ARCHIVE_DAYS = 90
MIN_THETA_TOTAL  = 10.0
DELTA_BW_MIN     = 50
DELTA_BW_MAX     = 150
LOW_IV_RANK      = 25
DEFAULT_IV       = 0.30   # fallback IV keď nemáme iné dáta

# ── Pomocné funkcie – delegované na core.portfolio_data ──────────────────────
_calc_dte        = pdata.calc_dte
_normalize_expiry = pdata.normalize_expiry
_greek_for_trade  = pdata.greek_for_trade
_build_group_data = pdata.build_group_data
_build_alerts     = pdata.build_alerts


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────
st.title("🧠 Portfolio Agent")
st.caption(
    "**Návod:** V **sidebar** nastav referenčný **SPY** (beta-weighted delta), model **Claude** a voliteľne **načítaj spoty z IB**. "
    "Hlavná stránka zobrazuje skupiny z denníka (call diagonály), metriky a AI štúdie — analýza je na požiadanie, dáta sú z **Trade Log** a živého IB ak je pripojený."
)

with st.sidebar:
    st.header("⚙️ Nastavenia portfólia")

    spy_price_input = st.number_input(
        "SPY ref. cena (Beta-weighted Delta)",
        min_value=100.0, max_value=2000.0, value=560.0, step=1.0,
    )

    st.markdown("---")

    # ── Výber Claude modelu ─────────────────────────────────────────────────
    st.markdown("**🤖 Claude model**")
    _model_options = list(ai_agent.AVAILABLE_MODELS.keys())
    _model_labels  = [ai_agent.AVAILABLE_MODELS[m]["label"] for m in _model_options]
    _saved_model   = st.session_state.get("selected_claude_model", "claude-sonnet-4-6")
    _saved_idx     = _model_options.index(_saved_model) if _saved_model in _model_options else 1
    _model_sel_idx = st.radio(
        "Model pre analýzu",
        options=range(len(_model_options)),
        format_func=lambda i: _model_labels[i],
        index=_saved_idx,
        label_visibility="collapsed",
    )
    _selected_model = _model_options[_model_sel_idx]
    st.session_state["selected_claude_model"] = _selected_model
    _model_info = ai_agent.AVAILABLE_MODELS[_selected_model]
    st.caption(f"Max tokens: {_model_info['max_tokens']}")

    st.markdown("---")

    # ── Tlačidlo na fetch Spot cien z IBKR (background) ────────────────────
    _spot_job = ibkr.SPOT_FETCH_JOB
    _spot_running = (_spot_job["status"] == "running")

    col_sb1, col_sb2 = st.columns([3, 2])
    with col_sb1:
        _do_spot_fetch = st.button(
            "📡 Načítať Spot z IBKR",
            use_container_width=True,
            disabled=_spot_running,
            help="Stiahne ceny všetkých 7 tickerov na pozadí (nezasekne UI).",
        )
    with col_sb2:
        if _spot_running:
            st.info("⏳ Sťahujem...")
        elif _spot_job["status"] == "done" and _spot_job["result"]:
            st.caption(f"✅ {len(_spot_job['result'])} tickerov")
        elif _spot_job["status"] == "error":
            st.caption(f"❌ {_spot_job['error']}")

    if _do_spot_fetch:
        if ibkr.is_connected():
            # Použijeme tickers zo session_state (dynamické) + WATCHED ako fallback
            _fetch_tickers = list(st.session_state.get("mkt_table", {}).keys()) or WATCHED
            ibkr.fetch_spot_prices_bg(_fetch_tickers)
            st.rerun()
        else:
            st.error("IBKR nie je pripojené.")

    # Spracuj výsledok keď hotovo
    if _spot_job["status"] == "done" and _spot_job["result"]:
        _fetched = _spot_job["result"]
        _tbl = st.session_state.get("mkt_table", {})
        for tk, price in _fetched.items():
            if tk not in _tbl:
                _tbl[tk] = {"Spot $": 0.0, "IV %": 30, "IV Rank": 0}
            _tbl[tk]["Spot $"] = round(price, 2)
        st.session_state["mkt_table"] = _tbl
        _spot_job["status"] = "idle"
        st.rerun()

    if _spot_running:
        st.rerun()  # auto-refresh každý render kým beží

    # ── Trhové vstupy: duplicitné voči Symboly — sidebar len skrátená úprava ──
    st.subheader("📊 Trhové dáta pre agenta")
    st.caption(
        "**Symboly** sú primárny zdroj (Yahoo sync dopĺňa spot/IV/industry, **nie sektor**). Tá istá trojica polí je tu pre rýchlu úpravu "
        "bez opustenia stránky; hlavná tabuľka nižšie (**Otvorené skupiny**) nie je duplicita — je to prehľad pozícií."
    )
    st.page_link("pages/symbols.py", label="Otvoriť Symboly", icon="📌")

    with st.expander("Tabuľka Spot / IV % / IV Rank (voliteľné)", expanded=False):
        st.caption(
            "➕ Pridaj riadok | 🗑 Vymaž riadok (Delete) | **IV Rank** < 25 % = doma. "
            "Po úprave **Uložiť do DB**."
        )

        # ── Zostav počiatočné riadky: DB (spot+iv_pct+iv_rank) + session_state ──
        # Priorita: session_state > DB > default
        _saved_tbl: dict = st.session_state.get("mkt_table", {})
        _all_syms   = db.get_symbols()
        _db_tickers = {s["ticker"]: s for s in _all_syms}

        _base_tickers = list(dict.fromkeys(
            list(_saved_tbl.keys()) +
            WATCHED +
            [s["ticker"] for s in _all_syms]   # všetky z DB
        ))

        _tbl_rows = []
        for tk in _base_tickers:
            tk = tk.strip().upper()
            if not tk:
                continue
            sym_data   = _db_tickers.get(tk, {})
            saved_spot = float(sym_data.get("spot") or 0)
            saved_iv   = float(sym_data.get("iv_pct") or 30)
            saved_ivr  = int(sym_data.get("iv_rank") or 0)
            if tk in _saved_tbl:
                saved_spot = float(_saved_tbl[tk].get("Spot $",  saved_spot))
                saved_iv   = float(_saved_tbl[tk].get("IV %",    saved_iv))
                saved_ivr  = int(_saved_tbl[tk].get("IV Rank", saved_ivr))
            _tbl_rows.append({
                "Ticker":  tk,
                "Spot $":  saved_spot,
                "IV %":    saved_iv,
                "IV Rank": saved_ivr,
            })

        _df_tbl = pd.DataFrame(_tbl_rows) if _tbl_rows else pd.DataFrame(
            columns=["Ticker", "Spot $", "IV %", "IV Rank"]
        )

        _edited = st.data_editor(
            _df_tbl,
            key="mkt_data_editor",
            use_container_width=True,
            hide_index=True,
            num_rows="dynamic",
            column_config={
                "Ticker":  st.column_config.TextColumn(
                    "Ticker", width="small",
                    help="Ticker symbol (napr. AMZN, SPY, QQQ...)",
                ),
                "Spot $":  st.column_config.NumberColumn(
                    "Spot $", min_value=0, max_value=100000, step=0.5, format="$%.2f",
                ),
                "IV %":    st.column_config.NumberColumn(
                    "IV %",   min_value=1, max_value=200,    step=1,   format="%.0f%%",
                ),
                "IV Rank": st.column_config.NumberColumn(
                    "IV Rank", min_value=0, max_value=100,   step=1,   format="%.0f%%",
                    help="IV Rank 0–100. Pod 25% = nevhodné prostredie.",
                ),
            },
        )

        if st.button("💾 Uložiť do DB", use_container_width=True, type="primary", key="mkt_save_db"):
            _new_tbl = {}
            for _, row in _edited.iterrows():
                tk  = str(row.get("Ticker") or "").strip().upper()
                if not tk:
                    continue
                ivr  = float(row.get("IV Rank") or 0)
                iv   = float(row.get("IV %") or 30)
                spot = float(row.get("Spot $") or 0)
                sym  = db.get_symbol(tk)
                if sym:
                    db.update_symbol(
                        sym["id"], sym["ticker"], sym.get("company_name", ""),
                        sym.get("sector", ""), sym.get("asset_type", "Stock"),
                        sym.get("description", ""), sym.get("earnings_date"),
                        ivr,
                        spot=spot, iv_pct=iv,
                        iv_rank_13w=sym.get("iv_rank_13w"),
                        iv_rank_52w=sym.get("iv_rank_52w"),
                    )
                else:
                    db.add_symbol(tk, iv_rank=ivr)
                    sym2 = db.get_symbol(tk)
                    if sym2:
                        db.update_symbol(
                            sym2["id"], tk, "", "", "Stock", "", None,
                            ivr, spot=spot, iv_pct=iv,
                            iv_rank_13w=sym2.get("iv_rank_13w"),
                            iv_rank_52w=sym2.get("iv_rank_52w"),
                        )
                _new_tbl[tk] = {"Spot $": spot, "IV %": iv, "IV Rank": int(ivr)}
            st.session_state["mkt_table"] = _new_tbl
            st.success(f"Uložené ({len(_new_tbl)} tickerov).")
            st.rerun()

    # Extrahuj hodnoty pre zvyšok stránky (vynechaj prázdne riadky); _edited z data_editor vyššie
    _valid_rows = _edited.dropna(subset=["Ticker"])
    _valid_rows = _valid_rows[_valid_rows["Ticker"].astype(str).str.strip() != ""]
    manual_spots: dict = {
        str(r["Ticker"]).upper(): float(r.get("Spot $") or 0)
        for _, r in _valid_rows.iterrows()
    }
    manual_ivs: dict = {
        str(r["Ticker"]).upper(): float(r.get("IV %") or 30) / 100.0
        for _, r in _valid_rows.iterrows()
    }
    iv_ranks: dict = {
        str(r["Ticker"]).upper(): int(r.get("IV Rank") or 0)
        for _, r in _valid_rows.iterrows()
    }

    st.markdown("---")
    st.subheader("🎯 Parametre stratégie")
    st.caption("Tieto hodnoty agent zohľadní pri návrhu nových spreadov.")

    _sp = st.session_state.get("strategy_params", {})

    _max_debet = st.number_input(
        "Max. debet na spread ($)",
        min_value=0, max_value=50000, step=50,
        value=int(_sp.get("max_debet", 1500)),
        help="Maximálny čistý debet (náklad) ktorý si ochotný zaplatiť za jeden diagonal spread.",
    )
    _max_positions = st.number_input(
        "Max. počet nových pozícií naraz",
        min_value=1, max_value=20, step=1,
        value=int(_sp.get("max_positions", 2)),
        help="Koľko nových spreadov smie agent navrhnúť v jednom cykle.",
    )

    from datetime import date as _dt
    _months = [
        "Najbližší štandardný",
        "Apríl 2026", "Máj 2026", "Jún 2026", "Júl 2026",
        "August 2026", "September 2026", "Október 2026",
    ]
    _pref_month = st.selectbox(
        "Preferovaný mesiac expirácie SHORT nohy",
        options=_months,
        index=_months.index(_sp.get("pref_short_month", "Najbližší štandardný"))
               if _sp.get("pref_short_month") in _months else 0,
        help="Štandardné expirácie sú 3. piatok v mesiaci.",
    )
    _pref_long_dte = st.number_input(
        "Cieľové DTE LONG nohy (dni)",
        min_value=30, max_value=365, step=10,
        value=int(_sp.get("pref_long_dte", 90)),
        help="Orientačné DTE pre long leg pri otvorení nového diagonalu.",
    )
    _max_risk_pct = st.number_input(
        "Max. riziko na spread (% portfólia)",
        min_value=1, max_value=50, step=1,
        value=int(_sp.get("max_risk_pct", 5)),
        help="Agent nenavrhandne spread ak by jeho debet prekročil X% čistej hodnoty portfólia.",
    )

    # Uložiť parametre do session_state (automaticky pri každom render)
    st.session_state["strategy_params"] = {
        "max_debet":        _max_debet,
        "max_positions":    _max_positions,
        "pref_short_month": _pref_month,
        "pref_long_dte":    _pref_long_dte,
        "max_risk_pct":     _max_risk_pct,
    }

    with st.expander("📡 IBKR predplatné — informácia pre agenta", expanded=False):
        st.caption(
            "Sem vlož zoznam trhových dát z **Client Portal / Account Management** (alebo skrátený výpis z TWS). "
            "Po úprave vždy klikni **Uložiť** — ten istý text dostane agent pri **Spustiť novú analýzu** aj pri **pokračovaní v chate**. "
            "Užitočné pri otázkach na IV, opčné reťazce, L1/L2."
        )
        if "agent_ibkr_market_data" not in st.session_state:
            st.session_state["agent_ibkr_market_data"] = db.get_setting(
                db.AGENT_IBKR_MARKET_DATA_KEY, ""
            )
        st.text_area(
            "Tvoje predplatné (voľný text)",
            height=200,
            key="agent_ibkr_market_data",
            placeholder=(
                "Napr. US Equity and Options Add-On Streaming Bundle (NP) …\n"
                "US Securities Snapshot and Futures Value Bundle …"
            ),
        )
        if st.button("💾 Uložiť text pre agenta do DB", key="agent_ibkr_save"):
            db.set_setting(
                db.AGENT_IBKR_MARKET_DATA_KEY,
                st.session_state.get("agent_ibkr_market_data") or "",
            )
            st.success("Uložené. Použije sa pri ďalšej analýze a v chate.")
            st.rerun()

    st.markdown("---")
    st.subheader("💳 Margin účtu")
    st.caption("Zadaj hodnoty ručne z TWS (Account → Account Summary).")

    # Načítaj z DB ak session_state je prázdny (po reštarte)
    if "account_summary" not in st.session_state:
        import json as _json
        try:
            _raw = db.get_setting("account_summary")
            st.session_state["account_summary"] = _json.loads(_raw) if _raw else {}
        except Exception:
            st.session_state["account_summary"] = {}
    _saved_acct = st.session_state.get("account_summary", {})

    _nlv = st.number_input(
        "Čistá hodnota portfólia – NLV ($)",
        min_value=0, max_value=10_000_000, step=100,
        value=int(_saved_acct.get("net_liquidation", 0)),
        key="acct_nlv",
    )
    _avail = st.number_input(
        "Voľný margin – Available Funds ($)",
        min_value=0, max_value=10_000_000, step=100,
        value=int(_saved_acct.get("available_funds", 0)),
        key="acct_avail",
    )
    _bp = st.number_input(
        "Kúpna sila – Buying Power ($)",
        min_value=0, max_value=10_000_000, step=100,
        value=int(_saved_acct.get("buying_power", 0)),
        key="acct_bp",
    )

    # Ukladaj do session_state aj do DB pri každej zmene
    _acct_data = {
        "net_liquidation":   float(_nlv),
        "available_funds":   float(_avail),
        "buying_power":      float(_bp),
        "maintenance_margin": 0.0,
    }
    st.session_state["account_summary"] = _acct_data
    import json as _json
    db.set_setting("account_summary", _json.dumps(_acct_data))

    _acct = st.session_state.get("account_summary", {})
    if _acct:
        st.metric("Voľný margin", f"${_acct.get('available_funds', 0):,.0f}")
        st.metric("Čistá hodnota (NLV)", f"${_acct.get('net_liquidation', 0):,.0f}")
        st.caption(
            f"Kúpna sila: ${_acct.get('buying_power', 0):,.0f}  |  "
            f"Maint. margin: ${_acct.get('maintenance_margin', 0):,.0f}"
        )
    else:
        st.caption("Margin nenačítaný – klikni tlačidlo.")

# ── Načítanie a obohatenie dát ────────────────────────────────────────────────
groups     = db.get_groups()
all_trades = db.get_open_trades()

# Zdroj live dát: session_state (naplnený zo stránky Skupiny)
# Fallback: FETCH_JOB["positions"] je dict {"positions": [...], "error": ...}
_ss_positions = ibkr.get_scoped_session_value("live_positions", [])
_job_raw      = ibkr.FETCH_JOB.get("positions")
_job_positions = (
    _job_raw.get("positions", [])
    if isinstance(_job_raw, dict)
    else []
)
pos_cache: list = _ss_positions or _job_positions

# ── Tlačidlo na fetch Greeks priamo z tejto stránky ─────────────────────────
if ibkr.is_connected():
    _job = ibkr.FETCH_JOB
    _running = (_job["status"] == "running")

    col_f1, col_f2, col_f3 = st.columns([2, 1.5, 3])
    with col_f1:
        _do_fetch = st.button(
            "🔄 Načítať Live Greeks z IBKR",
            disabled=_running,
            help="Stiahne Greeks pre všetky opčné pozície z TWS (pozadie).",
        )
    with col_f2:
        _do_stop = st.button(
            "⏹ Zastaviť",
            disabled=not _running,
        )
    with col_f3:
        _n_opts = sum(1 for p in pos_cache if p.get("sec_type") == "OPT")
        _n_g    = sum(1 for p in pos_cache if p.get("sec_type") == "OPT" and p.get("theta") is not None)
        if _running:
            st.info("⏳ Sťahujem Greeks z TWS...")
        elif _n_opts:
            st.caption(f"Cache: {_n_opts} opcií · {_n_g} má Greeks")
        else:
            st.caption("Cache prázdna — klikni pre načítanie")

    if _do_fetch and not _running:
        import threading
        def _fetch_bg(stop_ev):
            try:
                res = ibkr.fetch_positions(with_greeks=True)
                if stop_ev.is_set():
                    _job["status"] = "cancelled"
                    return
                _job["positions"] = res
                if res and not res.get("error"):
                    ibkr.set_scoped_session_value("live_positions", res.get("positions", []))
                _job["status"] = "done"
            except Exception as exc:
                _job["error"] = str(exc)
                _job["status"] = "error"

        _stop_ev = threading.Event()
        _job.update({"status": "running", "stop_event": _stop_ev, "error": None})
        threading.Thread(target=_fetch_bg, args=(_stop_ev,), daemon=True).start()
        st.rerun()

    if _do_stop and _running:
        se = _job.get("stop_event")
        if se:
            se.set()
        _job["status"] = "cancelled"
        st.rerun()

    if _job["status"] == "done":
        res = _job["positions"]
        if res and not res.get("error"):
            pos_cache = res.get("positions", [])
            ibkr.set_scoped_session_value("live_positions", pos_cache)
        _job["status"] = "idle"
        st.rerun()

# Spot ceny: IBKR STK pozícia (market_price) má prednosť pred manuálnym vstupom
spot_prices: dict = {}
for p in pos_cache:
    tk = p.get("ticker", "")
    if p.get("sec_type") == "STK" and tk:
        mp = float(p.get("market_price") or 0)
        if mp > 0 and tk not in spot_prices:
            spot_prices[tk] = mp
# Manuálne zadané spot ceny (pre tickery bez STK v portfóliu)
for tk, sp in manual_spots.items():
    if sp > 0 and tk not in spot_prices:
        spot_prices[tk] = sp

# IV z IBKR cache (uložená priamo ako "iv" pri BS výpočte v ibkr.py)
iv_data: dict = {}
for p in pos_cache:
    tk = p.get("ticker", "")
    iv = p.get("iv")
    if tk and iv and float(iv) > 0 and tk not in iv_data:
        iv_data[tk] = float(iv)
# Doplniť manuálne IV (kde cache nemá)
for tk, iv in manual_ivs.items():
    if tk not in iv_data:
        iv_data[tk] = iv

# Spojiť spot + iv pre BS: uprednostni manuálne IV vstupy
effective_ivs = {**iv_data}
for tk, iv in manual_ivs.items():
    effective_ivs[tk] = iv  # manuálny vstup vždy prepisuje cache

group_data       = _build_group_data(groups, all_trades, pos_cache, spot_prices, effective_ivs)
tickers_with_pos = list({g["ticker"] for g in group_data if g.get("ticker")})

# Sledované tickery = dynamický zoznam z tabuľky (nie hardcoded WATCHED)
_watched_dynamic = list(manual_spots.keys()) or WATCHED
tickers_no_pos   = [t for t in _watched_dynamic if t not in tickers_with_pos]

# ── Dátový zdroj – banner ─────────────────────────────────────────────────────
_n_live_greeks = sum(1 for p in pos_cache if p.get("sec_type") == "OPT" and p.get("theta") is not None)
_n_all_opts    = sum(1 for p in pos_cache if p.get("sec_type") == "OPT")
_has_spots     = any(v > 0 for v in spot_prices.values())

if _n_live_greeks > 0:
    st.success(
        f"🟢 **Live IBKR Greeks** – {_n_live_greeks}/{_n_all_opts} opcií má Greeks z TWS cache."
    )
elif pos_cache and _has_spots:
    st.info(
        f"🟡 **Odhadnuté Greeks (Black-Scholes)** – IBKR cache obsahuje {_n_all_opts} opcií, "
        "ale Greeks chýbajú (pravdepodobne nie je AMZN akcia v portfóliu → `under_price=None`). "
        "Spot ceny zo sidebara sa použijú pre BS výpočet."
    )
elif not pos_cache and _has_spots:
    st.info(
        "🟡 **Odhadnuté Greeks (Black-Scholes)** – IBKR nie je pripojené. "
        "Greeks vypočítané z manuálnych Spot cien a IV."
    )
else:
    st.warning(
        "🔴 **Žiadne dáta** – Klikni **Načítať Live Greeks z IBKR** (ak pripojené) "
        "alebo zadaj **Spot ceny** v sidebari pre BS odhadnuté Greeks."
    )

# ── Portfóliové metriky ───────────────────────────────────────────────────────
total_theta = sum(g["net_theta"] for g in group_data)
total_vega  = sum(g["net_vega"]  for g in group_data)

total_bw_delta_final = 0.0
for g in group_data:
    tk   = g.get("ticker", "")
    beta = BETA.get(tk, 1.0)
    spot = spot_prices.get(tk, 0) or 0
    nd   = g.get("net_delta", 0)
    if spot > 0 and spy_price_input > 0:
        total_bw_delta_final += nd * (spot / spy_price_input) * beta

alerts = _build_alerts(group_data, iv_ranks)

col1, col2, col3, col4 = st.columns(4)
with col1:
    st.metric(
        "Net Theta (portfólio)",
        f"${total_theta:+.2f}/deň",
        delta="✅ OK" if total_theta >= MIN_THETA_TOTAL else "⚠️ Nízka",
        delta_color="normal" if total_theta >= MIN_THETA_TOTAL else "inverse",
    )
with col2:
    bw_ok = DELTA_BW_MIN <= total_bw_delta_final <= DELTA_BW_MAX
    bw_disp = f"${total_bw_delta_final:+.0f}" if total_bw_delta_final != 0 else "—"
    st.metric(
        "Beta-weighted Delta",
        bw_disp,
        delta="✅ V pásme" if bw_ok and total_bw_delta_final != 0 else (
            "⚠️ Mimo pásma" if not bw_ok and total_bw_delta_final != 0 else "Zadaj Spot ceny"),
    )
with col3:
    st.metric("Net Vega", f"${total_vega:+.2f}" if total_vega else "—")
with col4:
    st.metric(
        "Skupiny / Tickery bez pozície",
        f"{len(group_data)} / {len(tickers_no_pos)}",
    )

# ── Alerty ────────────────────────────────────────────────────────────────────
if alerts:
    with st.expander(f"⚠️ Aktívne upozornenia ({len(alerts)})", expanded=True):
        for a in alerts:
            st.warning(a)
else:
    st.success("✅ Žiadne aktívne upozornenia.")

st.divider()

# ── Prehľad skupín ────────────────────────────────────────────────────────────
st.subheader("📋 Otvorené skupiny")

SOURCE_BADGE = {"live": "🟢 Live", "bs": "🟡 BS est.", "none": "🔴 N/A"}

if not group_data:
    st.info("Žiadne skupiny s otvorenými pozíciami. Pridaj skupiny a prirad im obchody.")
else:
    rows = []
    for g in group_data:
        short_leg = next((l for l in g["open_legs"] if l["leg_type"] == "Short"), None)
        long_leg  = next((l for l in g["open_legs"] if l["leg_type"] == "Long"),  None)
        iv_r  = iv_ranks.get(g.get("ticker", ""))
        spot  = spot_prices.get(g.get("ticker", ""), 0)
        rows.append({
            "Skupina":   g["name"],
            "Ticker":    g.get("ticker", ""),
            "Dáta":      SOURCE_BADGE.get(g.get("data_source", "none"), "—"),
            "IV Rank":   f"{iv_r}%" if iv_r is not None else "—",
            "Spot":      f"${spot:.2f}" if spot else "—",
            "Short leg": (f"${short_leg['strike']:.0f} {short_leg['expiry']} (DTE {short_leg['dte']})")
                         if short_leg else "—",
            "Long leg":  (f"${long_leg['strike']:.0f} {long_leg['expiry']} (DTE {long_leg['dte']})")
                         if long_leg else "—",
            "Net Theta": f"${g['net_theta']:+.2f}",
            "Net Delta": f"${g['net_delta']:+.0f}",
            "Net Gamma": f"{g['net_gamma']:+.4f}",
            "Net Vega":  f"${g['net_vega']:+.2f}",
        })
    df = pd.DataFrame(rows)

    def _color_theta(val: str):
        try:
            v = float(str(val).replace("$", "").replace("+", ""))
            return "color: #ff4b4b" if v < 0 else "color: #2ecc71"
        except Exception:
            return ""

    st.dataframe(
        df.astype(str).style.map(_color_theta, subset=["Net Theta"]),
        use_container_width=True, hide_index=True,
    )

    # Detail skupiny
    st.markdown("**Detail skupiny:**")
    selected_group = st.selectbox(
        "Vyber skupinu na detail",
        options=[g["name"] for g in group_data],
        key="portfolio_detail_group",
    )
    sel_g = next((g for g in group_data if g["name"] == selected_group), None)
    if sel_g:
        col_a, col_b = st.columns(2)
        with col_a:
            for leg in sel_g["open_legs"]:
                src_badge = SOURCE_BADGE.get(leg.get("source", "none"), "")
                st.markdown(
                    f"**{leg['leg_type']} {leg['option_type']}** "
                    f"${leg['strike']:.0f} · exp {leg['expiry']} · DTE {leg['dte']} · "
                    f"{leg['contracts']}x · {src_badge}"
                )
                gv = leg["greeks"]
                st.caption(
                    f"Theta {gv['theta']:+.2f} | Delta ${gv['delta']:+.0f} | "
                    f"Gamma {gv['gamma']:+.4f} | Vega {gv['vega']:+.2f}"
                )
        with col_b:
            st.markdown(f"**Stratégia:** {sel_g.get('strategy', '?')}")
            st.markdown(f"**Popis:** {sel_g.get('description') or '—'}")
            st.markdown(f"**Net Theta:** ${sel_g['net_theta']:+.2f}/deň")
            st.markdown(f"**Net Delta:** ${sel_g['net_delta']:+.0f}")
            src_g = SOURCE_BADGE.get(sel_g.get("data_source", "none"), "—")
            st.caption(f"Zdroj Greeks: {src_g}")

st.divider()

# ── Korelácie ─────────────────────────────────────────────────────────────────
with st.expander("🔗 Korelačná matica (aproximácia)", expanded=False):
    st.caption("Historické korelácie medzi sledovanými tickermi (aproximácia pre Tech Big 7).")
    _corr_tickers = _watched_dynamic if _watched_dynamic else WATCHED
    corr_matrix = [
        [ai_agent.get_correlation(t1, t2) for t2 in _corr_tickers]
        for t1 in _corr_tickers
    ]
    fig_corr = go.Figure(data=go.Heatmap(
        z=corr_matrix, x=_corr_tickers, y=_corr_tickers,
        colorscale="RdYlGn", zmin=0, zmax=1,
        text=[[f"{v:.2f}" for v in row] for row in corr_matrix],
        texttemplate="%{text}", showscale=True,
    ))
    fig_corr.update_layout(
        height=400,
        margin=dict(l=10, r=10, t=30, b=10),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="white"),
    )
    st.plotly_chart(fig_corr, use_container_width=True)

st.divider()


def _agent_parse_at(iso_s: str) -> datetime | None:
    if not iso_s:
        return None
    try:
        s = str(iso_s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _agent_load_eval_archive() -> list[dict]:
    raw = db.get_setting(db.PORTFOLIO_AGENT_EVAL_ARCHIVE_KEY, "")
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _agent_save_eval_archive(sessions: list[dict]) -> None:
    try:
        db.set_setting(db.PORTFOLIO_AGENT_EVAL_ARCHIVE_KEY, json.dumps(sessions))
    except Exception:
        pass


def _agent_prune_eval_archive(sessions: list[dict]) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=_AGENT_ARCHIVE_DAYS)
    out: list[dict] = []
    for s in sessions:
        dt = _agent_parse_at(s.get("at") or "")
        if dt is not None and dt >= cutoff:
            out.append(s)
    return out


def _agent_archive_current_session(question: str) -> None:
    hist = st.session_state.get("portfolio_chat") or []
    if not hist:
        return
    arch = _agent_prune_eval_archive(_agent_load_eval_archive())
    arch.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "question": (question or "")[:800],
        "messages": hist,
    })
    _agent_save_eval_archive(_agent_prune_eval_archive(arch))


def _agent_render_chat_messages(messages: list) -> None:
    render_ai_chat_markdown(messages)


# ── AI Portfolio Analýza + Chat ───────────────────────────────────────────────
st.subheader("🤖 AI Portfóliový Agent")
st.caption(
    "Agent analyzuje celé portfólio, alerty, korelácie, Beta-weighted Delta "
    "a navrhne nové call diagonaly tam, kde má zmysel (IV Rank ≥ 25%)."
)

# Inicializuj históriu chatu – načítaj z DB ak session_state je prázdny
if "portfolio_chat" not in st.session_state:
    try:
        _raw_chat = db.get_setting("portfolio_chat")
        st.session_state["portfolio_chat"] = json.loads(_raw_chat) if _raw_chat else []
    except Exception:
        st.session_state["portfolio_chat"] = []


def _save_chat(history: list) -> None:
    """Uloží chat históriu do DB."""
    try:
        db.set_setting("portfolio_chat", json.dumps(history))
    except Exception:
        pass

q_portfolio = st.text_area(
    "Úvodná otázka / kontext (voliteľné)",
    placeholder="Napr. 'Ktorý ticker má najlepší pomer riziko/odmena pre nový diagonal?'",
    height=80,
    key="portfolio_question",
)

col_run, col_clear = st.columns([3, 1])
with col_run:
    _do_analyze = st.button("🚀 Spustiť novú analýzu", type="primary", use_container_width=True)
with col_clear:
    if st.button("🗑 Vymazať chat", use_container_width=True):
        st.session_state["portfolio_chat"] = []
        st.session_state.pop("portfolio_analysis", None)
        _save_chat([])
        st.rerun()

if _do_analyze:
    if not group_data:
        st.warning("Žiadne otvorené skupiny na analýzu.")
    else:
        with st.spinner("Agent analyzuje portfólio..."):
            try:
                _strat  = st.session_state.get("strategy_params", {})
                # Live margin z TWS (ak je dostupný), inak manuálne zadaný
                _tws_acct = ibkr.DASHBOARD_FETCH_JOB.get("account") or {}
                _acct_s   = _tws_acct if _tws_acct else st.session_state.get("account_summary", {})
                # Otvorené objednávky z TWS (s BAG nohami a podmienkami)
                _tws_orders = ibkr.DASHBOARD_FETCH_JOB.get("orders")
                if _tws_orders is None and ibkr.is_connected():
                    _ord_res    = ibkr.fetch_open_orders(use_cache=False)
                    _tws_orders = _ord_res.get("orders", [])
                portfolio_payload = {
                    "groups":                   group_data,
                    "total_theta":              total_theta,
                    "total_delta_bw":           total_bw_delta_final,
                    "total_vega":               total_vega,
                    "alerts":                   alerts,
                    "tickers_without_position": tickers_no_pos,
                    "spot_prices":              spot_prices,
                    "iv_data":                  effective_ivs,
                    "iv_ranks":                 iv_ranks,
                    "account":                  _acct_s,
                    "strategy_params":          _strat,
                    "open_orders":              _tws_orders or [],
                    "ibkr_market_data_notes":   db.get_setting(
                        db.AGENT_IBKR_MARKET_DATA_KEY, ""
                    ),
                }
                result = ai_agent.analyze_portfolio(
                    portfolio_payload,
                    question=q_portfolio,
                    model=st.session_state.get("selected_claude_model"),
                )
                # Pred resetom ulož predchádzajúcu session do archívu (max. ~90 dní)
                _agent_archive_current_session(q_portfolio)
                # Nová analýza = nový chat (resetuj históriu)
                _new_hist = [{"role": "assistant", "content": result}]
                st.session_state["portfolio_chat"] = _new_hist
                st.session_state["portfolio_analysis"] = result
                _save_chat(_new_hist)
            except ValueError as e:
                st.error(f"Chyba konfigurácie: {e}")
            except Exception as e:
                st.error(f"Chyba AI agenta: {e}")

# ── Zobraz históriu chatu + archív vyhodnotení ────────────────────────────────
chat_history = st.session_state.get("portfolio_chat", [])

_arch_raw = _agent_prune_eval_archive(_agent_load_eval_archive())
_arch_sorted = list(reversed(_arch_raw))

if chat_history:
    st.markdown("---")
    with st.expander("📋 Aktuálne vyhodnotenie a chat — rozbaľ / zbaľ", expanded=True):
        _agent_render_chat_messages(chat_history)

    # ── Vstup pre follow-up otázky ────────────────────────────────────────────
    st.markdown("**💬 Pokračuj v diskusii:**")
    _followup = st.chat_input("Napíš doplňujúcu otázku...")

    if _followup:
        # Pridaj správu používateľa do histórie
        chat_history.append({"role": "user", "content": _followup})

        with st.spinner("Agent odpovedá..."):
            try:
                reply = ai_agent.chat_portfolio(
                    chat_history,
                    model=st.session_state.get("selected_claude_model"),
                )
                chat_history.append({"role": "assistant", "content": reply})
                st.session_state["portfolio_chat"] = chat_history
                _save_chat(chat_history)
            except Exception as e:
                st.error(f"Chyba: {e}")
        st.rerun()

if _arch_sorted:
    st.markdown("---")
    st.subheader(f"📚 Archív vyhodnotení (posledných {_AGENT_ARCHIVE_DAYS} dní)")
    st.caption(
        "Pri **Spustiť novú analýzu** sa predchádzajúca diskusia uloží sem. Najnovšie sú hore; záznamy staršie ako tri mesiace sa odstránia."
    )
    for sess in _arch_sorted:
        _at = sess.get("at") or ""
        _at_disp = _at[:19].replace("T", " ") if len(_at) >= 19 else (_at or "—")
        _q = (sess.get("question") or "").strip() or "bez úvodnej otázky"
        _prev = _q[:72] + ("…" if len(_q) > 72 else "")
        with st.expander(f"**{_at_disp}** · {_prev}", expanded=False):
            st.caption(_q)
            _agent_render_chat_messages(sess.get("messages") or [])

st.divider()

# ── Tickery bez pozície ────────────────────────────────────────────────────────
if tickers_no_pos:
    with st.expander(f"📭 Tickery bez otvorenej pozície ({len(tickers_no_pos)})", expanded=True):
        for tk in tickers_no_pos:
            ivr  = iv_ranks.get(tk, 0)
            spot = spot_prices.get(tk, 0)
            iv   = effective_ivs.get(tk, DEFAULT_IV)
            suitable = ivr >= LOW_IV_RANK and ivr > 0
            suit_str  = "✅ Vhodné na nový diagonal" if suitable else (
                f"⛔ IV Rank {ivr}% {'– radšej doma' if 0 < ivr < LOW_IV_RANK else '– nezadaný'}"
            )
            spot_str = f"${spot:.2f}" if spot > 0 else "—"
            st.markdown(
                f"**{tk}** – Spot: {spot_str} | IV: {iv*100:.1f}% | "
                f"IV Rank: {ivr}% → {suit_str}"
            )
