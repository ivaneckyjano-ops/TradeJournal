import streamlit as st
import pandas as pd
from datetime import date, datetime

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page
from core.portfolio_data import normalize_expiry

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
        sync_result = ibkr.sync_positions_to_db(res["positions"], db)
        st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
        st.session_state["possibly_closed"] = sync_result.get("possibly_closed", [])
        st.success(
            f"Synchronizácia hotová — "
            f"pridané: **{sync_result['added']}** &nbsp;·&nbsp; "
            f"aktualizované: **{sync_result.get('updated', 0)}** &nbsp;·&nbsp; "
            f"nezmenené: **{sync_result['skipped']}**"
        )
        if sync_result.get("possibly_closed"):
            st.warning(
                f"⚠️ **{len(sync_result['possibly_closed'])} pozícií** je v denníku ako *Open*, "
                f"ale v IBKR portfóliu ich nenašiel. Môžu byť uzavreté. Pozri nižšie."
            )
        st.rerun()

fills_btn = st.button(
    "Importuj Fills + Uzavri pozície (BOT/SLD)",
    disabled=not _ib_connected,
    type="primary",
    help="Načíta vykonané obchody z TWS. Automaticky uzavrie Short pozície (BOT) a Long pozície (SLD).",
)
if fills_btn:
    with st.spinner("Načítavam fills z IBKR..."):
        fills_res = ibkr.fetch_fills()
    if fills_res["error"]:
        st.error(fills_res["error"])
    elif not fills_res["fills"]:
        st.info("Žiadne fills v aktuálnej TWS session.")
    else:
        sync_f = ibkr.sync_fills_to_db(fills_res["fills"], db)
        msg = (
            f"Fills spracované — "
            f"uzavreté: **{sync_f.get('closed', 0)}** &nbsp;·&nbsp; "
            f"pridané: **{sync_f['added']}** &nbsp;·&nbsp; "
            f"preskočené: **{sync_f['skipped']}**"
        )
        if sync_f.get("closed", 0) > 0:
            st.success(msg)
        else:
            st.info(msg)
        st.rerun()

# ─── Possibly Closed Alert ────────────────────────────────────────────────────
possibly_closed = st.session_state.get("possibly_closed", [])
if possibly_closed:
    with st.container(border=True):
        st.markdown("### ⚠️ Pozície chýbajúce v IBKR portfóliu")
        st.caption(
            "Tieto obchody sú v denníku ako **Open**, ale IBKR ich neukazuje. "
            "Môžu byť uzavreté. Zadaj exit cenu a uzavri ich, alebo ignoruj ak sú IBKR dáta oneskorené."
        )
        for pc in possibly_closed:
            c1, c2, c3 = st.columns([3, 1, 1])
            with c1:
                st.markdown(
                    f"**#{pc['id']}** &nbsp; {pc['ticker']} "
                    f"{pc['leg_type']} {pc['option_type']} "
                    f"${pc['strike']:.0f} &nbsp; exp {pc['expiry']}"
                )
            with c2:
                close_price = st.number_input(
                    "Exit $", min_value=0.0, step=0.01,
                    key=f"pc_price_{pc['id']}", label_visibility="collapsed",
                    placeholder="0.00"
                )
            with c3:
                if st.button("Uzavrieť", key=f"pc_close_{pc['id']}", type="secondary"):
                    db.close_trade(pc["id"], close_price, date.today().isoformat())
                    st.success(f"Trade #{pc['id']} uzavretý za ${close_price:.2f}")
                    st.session_state["possibly_closed"] = [
                        x for x in possibly_closed if x["id"] != pc["id"]
                    ]
                    st.rerun()

if show_ibkr_raw and _ib_connected:
    with st.spinner("Načítavam..."):
        live_res = ibkr.fetch_positions(use_historical_last=False)
    if live_res["error"]:
        st.error(live_res["error"])
    elif not live_res["positions"]:
        st.info("IBKR portfólio je prázdne alebo žiadne opčné pozície.")
    else:
        opts = [p for p in live_res["positions"] if p["sec_type"] == "OPT"]
        stks = [p for p in live_res["positions"] if p["sec_type"] == "STK"]

        opt_upnl = sum(float(p.get("unrealized_pnl") or 0) for p in opts)
        stk_upnl = sum(float(p.get("unrealized_pnl") or 0) for p in stks)
        total_upnl = opt_upnl + stk_upnl

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

# ─── Kontrola: Porovnanie Denník ↔ TWS ────────────────────────────────────────
st.subheader("Kontrola zhody Denník ↔ TWS")

if not _ib_connected:
    st.info("Pripoj sa na IBKR pre živé porovnanie.")
else:
    check_btn = st.button("Skontroluj zhodu s TWS", type="secondary", use_container_width=False)
    if check_btn or st.session_state.get("show_check"):
        st.session_state["show_check"] = True
        with st.spinner("Porovnávam..."):
            live_chk = ibkr.fetch_positions(use_historical_last=False)

        if live_chk["error"]:
            st.error(live_chk["error"])
        else:
            tws_opts = [p for p in live_chk["positions"] if p["sec_type"] == "OPT"]
            db_open  = db.get_open_trades()

            def _pos_key(ticker, strike, expiry, opt_type, leg_type):
                """Normalizovaný kľúč pre porovnanie (exp vždy YYYYMMDD)."""
                exp_c = normalize_expiry(str(expiry or "")).replace("-", "")
                return (
                    str(ticker).upper(),
                    f"{float(strike or 0):.2f}",
                    exp_c,
                    str(opt_type).capitalize(),
                    str(leg_type).capitalize(),
                )

            tws_keys  = {_pos_key(p["ticker"], p["strike"], p["expiry"],
                                   p["option_type"], p["leg_type"]): p
                         for p in tws_opts}
            db_keys   = {_pos_key(t["ticker"], t["strike"] or 0, t["expiry"] or "",
                                   t["option_type"] or "", t["leg_type"] or ""): t
                         for t in db_open}

            rows_cmp = []
            all_keys = set(tws_keys) | set(db_keys)

            for k in sorted(all_keys):
                tws_p = tws_keys.get(k)
                db_p  = db_keys.get(k)

                if tws_p and db_p:
                    tws_c = float(abs(tws_p.get("contracts", 1)))
                    db_c  = float(db_p.get("contracts", 1))
                    if abs(tws_c - db_c) < 1e-6:
                        status = "✅ OK"
                    else:
                        status = f"⚠️ Kontrakt: TWS={tws_c} / Denník={db_c}"
                    rows_cmp.append({
                        "Stav": status,
                        "ID": db_p["id"],
                        "Ticker": k[0],
                        "Noha": k[4],
                        "Typ": k[3],
                        "Strike": float(k[1]),
                        "Expiry": k[2],
                        "Kontr. TWS": tws_c if tws_p else "—",
                        "Kontr. Denník": db_c if db_p else "—",
                        "Group": db_p.get("group_id") or "—",
                    })
                elif tws_p and not db_p:
                    rows_cmp.append({
                        "Stav": "❌ Chýba v denníku",
                        "ID": "—",
                        "Ticker": k[0],
                        "Noha": k[4],
                        "Typ": k[3],
                        "Strike": float(k[1]),
                        "Expiry": k[2],
                        "Kontr. TWS": float(abs(tws_p.get("contracts", 1))),
                        "Kontr. Denník": "—",
                        "Group": "—",
                    })
                elif db_p and not tws_p:
                    rows_cmp.append({
                        "Stav": "⚠️ Chýba v TWS",
                        "ID": db_p["id"],
                        "Ticker": k[0],
                        "Noha": k[4],
                        "Typ": k[3],
                        "Strike": float(k[1]),
                        "Expiry": k[2],
                        "Kontr. TWS": "—",
                        "Kontr. Denník": float(db_p.get("contracts", 1)),
                        "Group": db_p.get("group_id") or "—",
                    })

            if not rows_cmp:
                st.success("Denník aj TWS sú prázdne — žiadne otvorené pozície.")
            else:
                ok_count    = sum(1 for r in rows_cmp if r["Stav"].startswith("✅"))
                warn_count  = sum(1 for r in rows_cmp if r["Stav"].startswith("⚠️"))
                err_count   = sum(1 for r in rows_cmp if r["Stav"].startswith("❌"))

                col_s1, col_s2, col_s3 = st.columns(3)
                col_s1.metric("✅ Zhoduje sa", ok_count)
                col_s2.metric("⚠️ Rozdiel / Chýba v TWS", warn_count)
                col_s3.metric("❌ Chýba v denníku", err_count)

                if warn_count == 0 and err_count == 0:
                    st.success("Denník je v plnej zhode s TWS portfóliom.")
                else:
                    st.warning("Nájdené rozdiely — pozri tabuľku nižšie.")

                df_cmp = pd.DataFrame(rows_cmp)
                st.dataframe(
                    df_cmp,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Strike": st.column_config.NumberColumn(format="$%.2f"),
                        "Stav": st.column_config.TextColumn(width="medium"),
                    },
                )

                # Rýchla oprava: ak niečo chýba v denníku, ponúkni import
                missing_in_db = [r for r in rows_cmp if r["Stav"] == "❌ Chýba v denníku"]
                if missing_in_db:
                    st.caption(
                        "Pozície označené ❌ sú v TWS ale nie v denníku. "
                        "Klikni na **Importuj pozície z IBKR** vyššie."
                    )
                missing_in_tws = [r for r in rows_cmp if "Chýba v TWS" in r["Stav"]]
                if missing_in_tws:
                    st.caption(
                        "Pozície označené ⚠️ Chýba v TWS môžu byť uzavreté. "
                        "Klikni na **Importuj Fills** alebo ich uzavri manuálne v sekcii vyššie."
                    )

st.divider()

# ─── Otvorené pozície ─────────────────────────────────────────────────────────
st.subheader("Otvorené pozície")

open_trades = db.get_open_trades()

if not open_trades:
    st.info("Žiadne otvorené pozície. Použi **Importuj pozície z IBKR** alebo pridaj manuálne v **Trade Log**.")
else:
    rows = []
    for t in open_trades:
        dte_val = None
        pop_val = t.get("pop_at_entry")

        if t.get("expiry"):
            try:
                exp_str = t["expiry"]
                exp_date = date.fromisoformat(
                    f"{exp_str[:4]}-{exp_str[4:6]}-{exp_str[6:8]}"
                    if len(exp_str) == 8 else exp_str
                )
                dte_val = (exp_date - date.today()).days
            except Exception:
                pass

        rows.append({
            "ID": t["id"],
            "Group": t.get("group_id", "") or "",
            "Ticker": t["ticker"],
            "Stratégia": t.get("strategy", ""),
            "Noha": t.get("leg_type", ""),
            "Typ": t.get("option_type", ""),
            "Strike": t.get("strike"),
            "Expiry": t.get("expiry", ""),
            "DTE": dte_val,
            "Kontrakty": t.get("contracts", 1),
            "Entry cena": t.get("entry_price"),
            "PoP (entry)": f"{pop_val*100:.1f}%" if pop_val else "—",
            "Entry dátum": t.get("entry_date", ""),
        })

    df = pd.DataFrame(rows)
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Strike": st.column_config.NumberColumn(format="$%.2f"),
            "Entry cena": st.column_config.NumberColumn(format="$%.2f"),
        },
    )

