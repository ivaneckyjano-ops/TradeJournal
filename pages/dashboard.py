import streamlit as st
import pandas as pd
from datetime import datetime

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page
db.init_db()
set_tradejournal_page("dashboard")

# Nastav správne defaulty pre IBKR pripojenie ak ešte nie sú nastavené
if "ib_port" not in st.session_state:
    st.session_state["ib_port"] = 7496
if "ib_host" not in st.session_state:
    st.session_state["ib_host"] = "127.0.0.1"
if "ib_cid" not in st.session_state:
    st.session_state["ib_cid"] = 10

# Auto-refresh odkaz na session_state nastavené v streamlit_app.py
auto_on = st.session_state.get("auto_refresh_on", False)

# Stav pripojenia — raz vyhodnotený pre celú stránku
_ib_connected = ibkr.is_connected()

st.title("Dashboard")
st.caption(
    "**Návod:** (1) V expandéri **IBKR Pripojenie** zadaj host/port a **Pripojiť**. (2) Stlač **Importuj pozície z IBKR** a prípadne import **Fills**. "
    "(3) Skontroluj zhodu s denníkom v tabuľkách nižšie. Podrobný zápis Grékov a skupín je v **Casopise — Gréky**."
)
st.info(
    "**TWS:** pripojenie, import pozícií a fills, kontrola zhody s denníkom. "
    "**Casopis (čo TWS nedáva dlhodobo):** zápis a história **Δ, Θ, Vega, IV** po otvorení, skupiny a net súčty — stránka **Casopis — Gréky**."
)

# ─── IBKR Panel ───────────────────────────────────────────────────────────────
with st.expander("IBKR Pripojenie", expanded=not _ib_connected):
    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        host = st.text_input("Host", value="127.0.0.1", key="ib_host")
    with col2:
        port = st.number_input("Port", value=7496, step=1, key="ib_port")
    with col3:
        client_id = st.number_input("Client ID", value=10, step=1, key="ib_cid")

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Pripojiť", type="primary", use_container_width=True):
            with st.spinner("Pripájam..."):
                ok, msg = ibkr.connect(host, int(port), int(client_id))
            if ok:
                st.session_state.pop("ib_last_err", None)
                st.success(msg)
                st.rerun()
            else:
                st.session_state["ib_last_err"] = msg
                st.error(msg)
    with c2:
        if st.button("Odpojiť", use_container_width=True):
            ibkr.disconnect()
            st.session_state.pop("ib_last_err", None)
            st.info("Odpojený.")
            st.rerun()

    if st.session_state.get("ib_last_err"):
        st.caption(f"Posledná chyba: `{st.session_state['ib_last_err']}`")

if _ib_connected:
    st.success("IBKR: Pripojený")
else:
    st.warning("IBKR: Nie je pripojenie. Použi panel vyššie na pripojenie.")

st.divider()

# ─── Sync pozícií z IBKR ──────────────────────────────────────────────────────
st.subheader("Synchronizácia pozícií z IBKR")

col_sync1, col_sync2 = st.columns([1, 3])
with col_sync1:
    sync_btn = st.button(
        "Importuj pozície z IBKR",
        type="primary",
        disabled=not _ib_connected,
        use_container_width=True,
    )
with col_sync2:
    show_ibkr_raw = st.checkbox("Zobraziť live portfólio z IBKR", value=False)

# Auto-sync prebieha globálne v streamlit_app.py — tu len zobrazíme stav

if sync_btn:
    with st.spinner("Načítavam portfólio z IBKR..."):
        res = ibkr.fetch_positions(use_historical_last=False)
    if res["error"]:
        st.error(res["error"])
    else:
        sync_result = ibkr.sync_positions_to_db(res["positions"], db, close_missing=True)
        st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
        st.success(
            f"Synchronizácia hotová — "
            f"pridané: **{sync_result['added']}** &nbsp;·&nbsp; "
            f"aktualizované: **{sync_result.get('updated', 0)}** &nbsp;·&nbsp; "
            f"nezmenené: **{sync_result['skipped']}** &nbsp;·&nbsp; "
            f"uzavreté: **{sync_result.get('closed', 0)}**"
        )
        st.rerun()

fills_btn = st.button(
    "Importuj Fills + Uzavri pozície (BOT/SLD)",
    disabled=not _ib_connected,
    type="primary",
    help="Načíta vykonané obchody z TWS. Automaticky uzavrie Short pozície (BOT) a Long pozície (SLD).",
)
if fills_btn:
    with st.spinner("Načítavam fills z IBKR (reqExecutions)..."):
        fills_res = ibkr.fetch_fills()
    if fills_res["error"]:
        st.error(fills_res["error"])
    elif not fills_res["fills"]:
        st.warning(
            "IBKR nevrátil žiadne opčné výplne. Skús znova po obchode v TWS, "
            "alebo skontroluj účet / typ klienta (Paper vs Live). Pri prvom pripojení často pomôže druhý klik."
        )
    else:
        n_in = len(fills_res["fills"])
        sync_f = ibkr.sync_fills_to_db(fills_res["fills"], db)
        msg = (
            f"Z IBKR prišlo **{n_in}** výplní (OPT). Spracovanie: "
            f"uzavreté **{sync_f.get('closed', 0)}** · pridané **{sync_f['added']}** · preskočené **{sync_f['skipped']}**."
        )
        if sync_f.get("closed", 0) > 0 or sync_f.get("added", 0) > 0:
            st.success(msg)
        else:
            st.info(msg + " Ak čakáš uzavretie nohy, skontroluj v denníku rovnaký ticker, strike, expiráciu (YYYYMMDD) a typ nohy (Short+BOT / Long+SLD).")
        st.rerun()

if show_ibkr_raw and _ib_connected:
    with st.spinner("Načítavam..."):
        live_res = ibkr.fetch_positions(use_historical_last=False)
    if live_res["error"]:
        st.error(live_res["error"])
    elif not live_res["positions"]:
        st.info("IBKR nevrátil žiadne pozície.")
    else:
        st.caption(f"Načítaných pozícií z IBKR: **{len(live_res['positions'])}** (OPT + STK + ostatné typy v zdroji).")
        opts = [p for p in live_res["positions"] if p["sec_type"] == "OPT"]
        stks = [p for p in live_res["positions"] if p["sec_type"] == "STK"]

        opt_upnl = sum(float(p.get("unrealized_pnl") or 0) for p in opts)
        stk_upnl = sum(float(p.get("unrealized_pnl") or 0) for p in stks)
        total_upnl = opt_upnl + stk_upnl

        if not opts and not stks:
            st.warning(
                "V odpovedi nie sú riadky typu OPT ani STK (napr. len futures, cash alebo iný typ). "
                "Kontrola zhody nižšie pracuje len s **opciami**."
            )

        if opts:
            st.markdown("**Opcie v portfóliu:**")
            df_live = pd.DataFrame(opts)[[
                "ticker", "leg_type", "option_type", "strike", "expiry",
                "contracts", "avg_cost", "market_price", "price_source", "unrealized_pnl"
            ]].copy()
            _src_map = {
                "settlement_close": "Settle (=TWS)",
                "hist_trades": "Last (hist)",
                "hist_midpoint": "Midpoint (hist)",
                "hist_last": "Last (hist)",
                "portfolio_mark": "Mark (portfólia)",
                "last": "Last",
                "mid": "Mid",
                "mark": "Mark",
                "close": "Close",
            }
            df_live["price_source"] = df_live["price_source"].map(lambda x: _src_map.get(str(x), str(x)))
            df_live.columns = [
                "Ticker", "Noha", "Typ", "Strike", "Expiry",
                "Kontr.", "Avg Cost", "Trh. cena", "Zdroj ceny", "Unrealized P&L"
            ]
            # Súčtový riadok pre opcie
            total_row_opt = pd.DataFrame([{
                "Ticker": "SPOLU",
                "Noha": "",
                "Typ": "",
                "Strike": None,
                "Expiry": "",
                "Kontr.": int(sum(abs(float(p.get("contracts") or 0)) for p in opts)),
                "Avg Cost": None,
                "Trh. cena": None,
                "Unrealized P&L": opt_upnl,
            }])
            df_live_total = pd.concat([df_live, total_row_opt], ignore_index=True)
            st.dataframe(df_live_total, use_container_width=True, hide_index=True,
                         column_config={
                             "Strike": st.column_config.NumberColumn(format="$%.2f"),
                             "Avg Cost": st.column_config.NumberColumn(format="$%.2f"),
                             "Trh. cena": st.column_config.NumberColumn(format="$%.2f"),
                             "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
                         })

        if stks:
            st.markdown("**Akcie v portfóliu:**")
            df_stk = pd.DataFrame(stks)[["ticker", "leg_type", "contracts", "avg_cost", "market_price", "price_source", "unrealized_pnl"]].copy()
            _src_map = {
                "settlement_close": "Settle (=TWS)",
                "hist_trades": "Last (hist)",
                "hist_midpoint": "Midpoint (hist)",
                "hist_last": "Last (hist)",
                "portfolio_mark": "Mark (portfólia)",
                "last": "Last",
                "mid": "Mid",
                "mark": "Mark",
                "close": "Close",
            }
            df_stk["price_source"] = df_stk["price_source"].map(lambda x: _src_map.get(str(x), str(x)))
            df_stk.columns = ["Ticker", "Noha", "Kontr.", "Avg Cost", "Trh. cena", "Zdroj ceny", "Unrealized P&L"]
            total_row_stk = pd.DataFrame([{
                "Ticker": "SPOLU",
                "Noha": "",
                "Kontr.": None,
                "Avg Cost": None,
                "Trh. cena": None,
                "Unrealized P&L": stk_upnl,
            }])
            df_stk_total = pd.concat([df_stk, total_row_stk], ignore_index=True)
            st.dataframe(df_stk_total, use_container_width=True, hide_index=True,
                         column_config={
                             "Avg Cost": st.column_config.NumberColumn(format="$%.4f"),
                             "Trh. cena": st.column_config.NumberColumn(format="$%.4f"),
                             "Unrealized P&L": st.column_config.NumberColumn(format="$%.2f"),
                         })

        # Celkový súčet portfólia
        st.markdown("**Celé portfólio:**")
        delta_color = "normal" if total_upnl >= 0 else "inverse"
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric("Unrealized P&L — Opcie", f"${opt_upnl:+,.2f}")
        pc2.metric("Unrealized P&L — Akcie", f"${stk_upnl:+,.2f}")
        pc3.metric("Unrealized P&L — CELKOM", f"${total_upnl:+,.2f}", delta_color=delta_color)

st.divider()

# ─── Otvorené pozície ─────────────────────────────────────────────────────────
st.subheader("Aktuálny stav v TWS")

if not _ib_connected:
    st.info("Pripoj sa na IBKR, aby sa zobrazil aktuálny stav z TWS.")
else:
    live_tbl = ibkr.fetch_positions(use_historical_last=False)
    if live_tbl.get("error"):
        st.error(live_tbl["error"])
    else:
        pos_list = live_tbl.get("positions") or []
        tws_opts = [p for p in pos_list if p.get("sec_type") == "OPT"]
        st.caption(
            f"Z TWS načítané pozície: **{len(pos_list)}**, z toho opčné (**OPT**): **{len(tws_opts)}**. "
            "Tento blok je čisto live stav brokera, bez denníka."
        )

        if not tws_opts:
            st.info("TWS momentálne nevrátil žiadne opčné pozície (OPT).")
        else:
            rows = []
            for p in sorted(
                tws_opts,
                key=lambda x: (
                    str(x.get("ticker") or ""),
                    str(x.get("expiry") or ""),
                    float(x.get("strike") or 0),
                    str(x.get("option_type") or ""),
                    str(x.get("leg_type") or ""),
                ),
            ):
                exp = str(p.get("expiry") or "")
                exp_disp = f"{exp[:4]}-{exp[4:6]}-{exp[6:8]}" if len(exp) >= 8 and "-" not in exp else exp
                rows.append(
                    {
                        "Ticker": p.get("ticker") or "",
                        "Expirácia": exp_disp,
                        "Strike": p.get("strike"),
                        "Typ": p.get("option_type") or "",
                        "Noha": p.get("leg_type") or "",
                        "Kontrakty": int(float(p.get("contracts") or 1)),
                        "Trh. cena": p.get("market_price"),
                        "Mkt hodnota": p.get("market_value"),
                        "U P&L": p.get("unrealized_pnl"),
                        "Zdroj ceny": p.get("price_source") or "",
                        "TWS Δ (odhad)": p.get("delta"),
                        "TWS Θ $/deň": p.get("theta"),
                        "TWS Vega": p.get("vega"),
                        "TWS IV": p.get("iv"),
                    }
                )

            df = pd.DataFrame(rows)
            for c in ("TWS Δ (odhad)", "TWS Θ $/deň", "TWS Vega", "TWS IV", "Trh. cena", "Mkt hodnota", "U P&L"):
                if c in df.columns:
                    df[c] = df[c].astype("Float64")
            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Strike": st.column_config.NumberColumn(format="$%.2f"),
                    "Trh. cena": st.column_config.NumberColumn(format="$%.4f"),
                    "Mkt hodnota": st.column_config.NumberColumn(format="$%.2f"),
                    "U P&L": st.column_config.NumberColumn(format="$%.2f"),
                    "TWS Δ (odhad)": st.column_config.NumberColumn(format="%.4f"),
                    "TWS Θ $/deň": st.column_config.NumberColumn(format="$%.3f"),
                    "TWS Vega": st.column_config.NumberColumn(format="%.2f"),
                    "TWS IV": st.column_config.NumberColumn(format="%.4f"),
                },
            )

