import streamlit as st
import pandas as pd
from datetime import date, datetime

from core import database as db
from core import ibkr
from core.probability import pop_short_call, pop_short_put, pop_long_call, pop_long_put, pop_diagonal

db.init_db()


# ─── Helper funkcie ───────────────────────────────────────────────────────────
def _build_df(trades: list[dict], show_pnl: bool = False) -> pd.DataFrame:
    rows = []
    for t in trades:
        pnl = db.compute_pnl(t) if show_pnl else None
        pop_v = t.get("pop_at_entry")
        comm = t.get("commission") or 0.0
        rows.append({
            "ID": t["id"],
            "Group": t.get("group_id", "") or "",
            "Ticker": t["ticker"],
            "Stratégia": t.get("strategy", ""),
            "Noha": t.get("leg_type", ""),
            "Typ": t.get("option_type", ""),
            "Strike": t.get("strike"),
            "Expiry": t.get("expiry", ""),
            "Kontrakty": t.get("contracts", 1),
            "Entry": t.get("entry_price"),
            "Exit": t.get("exit_price") if show_pnl else None,
            "Komisia": comm if show_pnl else None,
            "P&L čistý ($)": pnl,
            "PoP (entry)": f"{pop_v*100:.1f}%" if pop_v else "—",
            "Entry dátum": t.get("entry_date", ""),
            "Exit dátum": t.get("exit_date", "") if show_pnl else None,
        })
    df = pd.DataFrame(rows)
    if show_pnl:
        df = df.sort_values("Entry dátum", na_position="last").reset_index(drop=True)
        df["P&L kumulatív ($)"] = df["P&L čistý ($)"].cumsum()
        df = df.drop(columns=["PoP (entry)"], errors="ignore")
    else:
        df = df.drop(columns=["Exit", "Komisia", "P&L čistý ($)", "Exit dátum"], errors="ignore")
    return df


def _col_config(pnl: bool = False) -> dict:
    cfg = {
        "Strike": st.column_config.NumberColumn(format="$%.2f"),
        "Entry": st.column_config.NumberColumn(format="$%.2f"),
    }
    if pnl:
        cfg["Exit"] = st.column_config.NumberColumn(format="$%.2f")
        cfg["Komisia"] = st.column_config.NumberColumn(format="$%.2f")
        cfg["P&L čistý ($)"] = st.column_config.NumberColumn(format="$%.2f")
        cfg["P&L kumulatív ($)"] = st.column_config.NumberColumn(format="$%.2f")
    return cfg


STRATEGIES = [
    "Long Call", "Long Put", "Short Call", "Short Put",
    "Covered Call", "Cash-Secured Put",
    "Bull Call Spread", "Bear Put Spread", "Bull Put Spread", "Bear Call Spread",
    "Diagonal", "Calendar Spread",
    "Iron Condor", "Straddle", "Strangle", "Butterfly",
    "Iné",
]

st.title("Trade Log")

tab_add, tab_open, tab_edit, tab_closed = st.tabs([
    "Pridať obchod", "Otvorené pozície", "Upraviť / Zoskupiť", "Uzavreté pozície"
])

# ─── Tab: Pridať obchod ───────────────────────────────────────────────────────
with tab_add:
    st.subheader("Zadanie nového obchodu")

    with st.form("add_trade_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            _sym_tickers = db.get_symbol_tickers()
            if _sym_tickers:
                _sym_opts = _sym_tickers + ["— vlastný ticker —"]
                _sym_sel = st.selectbox("Ticker *", _sym_opts, help="Symboly spravuješ v záložke Symboly")
                if _sym_sel == "— vlastný ticker —":
                    ticker = st.text_input("Zadaj ticker", value="").upper().strip()
                else:
                    ticker = _sym_sel
            else:
                ticker = st.text_input("Ticker *", value="", placeholder="napr. AMZN").upper().strip()
                st.caption("💡 Pridaj symboly v záložke **Symboly** pre rýchly výber.")
        with c2:
            strategy = st.selectbox("Stratégia *", STRATEGIES)
        with c3:
            group_names = ["— (bez skupiny) —"] + db.get_group_names()
            group_sel = st.selectbox("Skupina (Group ID)", group_names,
                                     help="Skupiny spravuješ v záložke Skupiny")
            group_id = group_sel if group_sel != "— (bez skupiny) —" else ""

        st.markdown("**Noha (Leg)**")
        c4, c5, c6, c7 = st.columns(4)
        with c4:
            leg_type = st.selectbox("Typ nohy", ["Short", "Long"])
        with c5:
            option_type = st.selectbox("Opcia", ["Call", "Put"])
        with c6:
            strike = st.number_input("Strike ($) *", min_value=0.0, step=0.5)
        with c7:
            expiry_date = st.date_input("Expiry *", value=date.today(), min_value=date.today())

        c8, c9, c10, c10b = st.columns([1, 1, 1, 1])
        with c8:
            contracts = st.number_input("Kontrakty", min_value=1, value=1, step=1)
        with c9:
            entry_price = st.number_input("Entry cena (prémia) *", min_value=0.0, step=0.01)
        with c10:
            entry_date = st.date_input("Dátum vstupu", value=date.today())
        with c10b:
            commission_input = st.number_input(
                "Komisia ($)", min_value=0.0, step=0.01, value=0.0,
                help="Celková komisia brokera za otvorenie (napr. 0.65 × počet kontraktov)"
            )

        st.markdown("**Voliteľné — IV a PoP**")
        c11, c12 = st.columns(2)
        with c11:
            iv_input = st.number_input("IV pri vstupe (napr. 0.30)", min_value=0.0, max_value=5.0,
                                       step=0.01, value=0.0)
        with c12:
            spot_input = st.number_input("Spot cena pri vstupe ($)", min_value=0.0, step=0.5, value=0.0)

        submitted = st.form_submit_button("Uložiť obchod", type="primary", use_container_width=True)

    if submitted:
        if not ticker or strike <= 0 or entry_price <= 0:
            st.error("Vyplň: Ticker, Strike a Entry cenu.")
        else:
            expiry_str = expiry_date.strftime("%Y%m%d")
            dte = (expiry_date - date.today()).days

            pop_val = None
            if iv_input > 0 and spot_input > 0 and dte > 0:
                if leg_type == "Short" and option_type == "Call":
                    pop_val = pop_short_call(spot_input, strike, dte, iv_input)
                elif leg_type == "Short" and option_type == "Put":
                    pop_val = pop_short_put(spot_input, strike, dte, iv_input)
                elif leg_type == "Long" and option_type == "Call":
                    pop_val = pop_long_call(spot_input, strike, dte, iv_input)
                else:
                    pop_val = pop_long_put(spot_input, strike, dte, iv_input)

            trade_id = db.add_trade(
                ticker=ticker,
                strategy=strategy,
                leg_type=leg_type,
                option_type=option_type,
                strike=strike,
                expiry=expiry_str,
                contracts=int(contracts),
                entry_price=entry_price,
                entry_date=entry_date.isoformat(),
                group_id=group_id if group_id else None,
                iv_at_entry=iv_input if iv_input > 0 else None,
                pop_at_entry=pop_val,
                commission=commission_input if commission_input > 0 else None,
            )
            st.success(f"Obchod #{trade_id} uložený! {ticker} {leg_type} {option_type} ${strike:.0f}  |  PoP: {pop_val*100:.1f}%" if pop_val else f"Obchod #{trade_id} uložený!")
            st.rerun()


# ─── Tab: Otvorené pozície ────────────────────────────────────────────────────
with tab_open:
    st.subheader("Otvorené pozície")
    open_trades = db.get_open_trades()

    if not open_trades:
        st.info("Žiadne otvorené pozície.")
    else:
        df_open = _build_df(open_trades, show_pnl=False)
        st.dataframe(df_open, use_container_width=True, hide_index=True,
                     column_config=_col_config())

        st.markdown("---")
        st.markdown("**Uzavrieť pozíciu**")
        with st.form("close_trade_form"):
            trade_options = {f"#{t['id']} — {t['ticker']} {t['leg_type']} {t['option_type']} ${t['strike']:.0f} exp {t['expiry']}": t["id"]
                             for t in open_trades}
            selected_label = st.selectbox("Vyber obchod na uzavretie", list(trade_options.keys()))
            c1, c2 = st.columns(2)
            with c1:
                exit_price = st.number_input("Exit cena *", min_value=0.0, step=0.01)
            with c2:
                exit_date = st.date_input("Dátum výstupu", value=date.today())
            close_btn = st.form_submit_button("Uzavrieť", type="primary")

        if close_btn:
            trade_id = trade_options[selected_label]
            db.close_trade(trade_id, exit_price, exit_date.isoformat())
            st.success(f"Obchod #{trade_id} uzavretý za ${exit_price:.2f}")
            st.rerun()

        st.markdown("---")
        st.markdown("**Zmazať pozíciu**")
        with st.form("delete_trade_form"):
            del_options = {f"#{t['id']} — {t['ticker']} {t['leg_type']} {t['option_type']} ${t['strike']:.0f}": t["id"]
                           for t in open_trades}
            del_label = st.selectbox("Vyber obchod na zmazanie", list(del_options.keys()))
            del_btn = st.form_submit_button("Zmazať", type="secondary")
        if del_btn:
            db.delete_trade(del_options[del_label])
            st.warning("Obchod zmazaný.")
            st.rerun()


# ─── Tab: Upraviť / Zoskupiť ─────────────────────────────────────────────────
with tab_edit:
    all_edit_trades = db.get_all_trades()

    if not all_edit_trades:
        st.info("Žiadne obchody.")
    else:
        # ── 1. Priama editácia tabuľky ─────────────────────────────────────
        st.subheader("Priama editácia (Všetky polia)")
        st.caption("Tu môžeš opraviť čokoľvek — Ticker, Strike, Expiráciu, Status aj ceny. Pre uloženie klikni **Uložiť zmeny**.")

        edit_rows = []
        for t in all_edit_trades:
            edit_rows.append({
                "ID": t["id"],
                "Ticker": t["ticker"],
                "Status": t.get("status", "Open"),
                "Noha": t.get("leg_type", "Short"),
                "Typ": t.get("option_type", "Call"),
                "Strike": float(t.get("strike", 0.0)),
                "Expiry": t.get("expiry", ""),
                "Kontrakty": int(t.get("contracts", 1)),
                "Entry $": float(t.get("entry_price", 0.0)),
                "Exit $": float(t.get("exit_price", 0.0)) if t.get("exit_price") else 0.0,
                "Komisia $": float(t.get("commission") or 0.0),
                "Exit Date": t.get("exit_date", ""),
                "Group ID": t.get("group_id") or "",
                "Stratégia": t.get("strategy") or "",
            })

        edited_df = st.data_editor(
            pd.DataFrame(edit_rows),
            use_container_width=True,
            hide_index=True,
            disabled=["ID"],
            column_config={
                "Status": st.column_config.SelectboxColumn("Status", options=["Open", "Closed"]),
                "Noha": st.column_config.SelectboxColumn("Noha", options=["Short", "Long"]),
                "Typ": st.column_config.SelectboxColumn("Typ", options=["Call", "Put"]),
                "Stratégia": st.column_config.SelectboxColumn("Stratégia", options=STRATEGIES),
                "Strike": st.column_config.NumberColumn("Strike", format="$%.2f"),
                "Entry $": st.column_config.NumberColumn("Entry $", format="$%.2f"),
                "Exit $": st.column_config.NumberColumn("Exit $", format="$%.2f"),
                "Komisia $": st.column_config.NumberColumn("Komisia $", format="$%.2f",
                                                            help="Celková komisia brokera (entry + exit)"),
                "Expiry": st.column_config.TextColumn("Expiry", help="Formát: YYYYMMDD"),
                "Exit Date": st.column_config.TextColumn("Exit Date", help="Formát: YYYY-MM-DD"),
            },
            key="edit_table_v2",
        )

        if st.button("Uložiť zmeny", type="primary", key="save_edit_btn_v2"):
            changed = 0
            orig_map = {t["id"]: t for t in all_edit_trades}
            for _, row in edited_df.iterrows():
                tid = int(row["ID"])
                orig = orig_map.get(tid, {})
                
                # Sledujeme zmeny
                updates = {}
                if row["Ticker"] != orig.get("ticker"): updates["ticker"] = row["Ticker"]
                if row["Status"] != orig.get("status"): updates["status"] = row["Status"]
                if row["Noha"] != orig.get("leg_type"): updates["leg_type"] = row["Noha"]
                if row["Typ"] != orig.get("option_type"): updates["option_type"] = row["Typ"]
                if float(row["Strike"]) != float(orig.get("strike", 0)): updates["strike"] = float(row["Strike"])
                if row["Expiry"] != orig.get("expiry"): updates["expiry"] = row["Expiry"]
                if int(row["Kontrakty"]) != int(orig.get("contracts", 1)): updates["contracts"] = int(row["Kontrakty"])
                if float(row["Entry $"]) != float(orig.get("entry_price", 0)): updates["entry_price"] = float(row["Entry $"])
                
                # Exit cena a dátum
                new_exit_p = float(row["Exit $"])
                if new_exit_p != float(orig.get("exit_price") or 0.0): 
                    updates["exit_price"] = new_exit_p if new_exit_p > 0 else None
                
                new_exit_d = (row["Exit Date"] or "").strip() or None
                if new_exit_d != orig.get("exit_date"): updates["exit_date"] = new_exit_d
                
                new_group = (row["Group ID"] or "").strip() or None
                if new_group != (orig.get("group_id") or None): updates["group_id"] = new_group
                
                if row["Stratégia"] != orig.get("strategy"): updates["strategy"] = row["Stratégia"]

                new_comm = float(row.get("Komisia $") or 0.0)
                if new_comm != float(orig.get("commission") or 0.0):
                    updates["commission"] = new_comm if new_comm > 0 else None

                if updates:
                    db.update_trade(trade_id=tid, **updates)
                    changed += 1
            
            if changed:
                st.success(f"Uložené — zmenených {changed} záznam(ov).")
                st.rerun()
            else:
                st.info("Žiadne zmeny.")

        st.divider()

        # ── 1b. Rozdeliť pozíciu ────────────────────────────────────────────
        st.subheader("Rozdeliť pozíciu na samostatné nohy")
        st.caption("Napr. Long 205 Call ×2 → noha A (Diagonal skupina 1) + noha B (skupina 2)")

        multi_trades = [t for t in all_edit_trades if int(t.get("contracts", 1)) > 1]
        if not multi_trades:
            st.info("Žiadna pozícia s viac ako 1 kontraktom.")
        else:
            split_options = {
                f"#{t['id']} | {t['ticker']} {t.get('leg_type','')} {t.get('option_type','')} "
                f"${t.get('strike',0):.0f} ×{t.get('contracts',1)} kontr.": t["id"]
                for t in multi_trades
            }
            split_label = st.selectbox("Vyber pozíciu na rozdelenie", list(split_options.keys()), key="split_sel")
            split_id = split_options[split_label]
            split_trade_obj = next((t for t in multi_trades if t["id"] == split_id), None)
            n_contracts = int(split_trade_obj.get("contracts", 2)) if split_trade_obj else 2

            st.markdown(f"Rozdelí sa na **{n_contracts}** nôh po 1 kontrakte. Zadaj Group ID pre každú:")
            split_group_inputs = []
            for i in range(n_contracts):
                default_gid = split_trade_obj.get("group_id") or ""
                g = st.text_input(
                    f"Group ID pre nohu {i+1}",
                    value=default_gid,
                    key=f"split_gid_{i}",
                    placeholder=f"napr. AMZN_DIA_{i+1}",
                )
                split_group_inputs.append(g)

            if st.button("Rozdeliť pozíciu", type="primary", key="split_btn"):
                new_ids = db.split_trade(split_id, split_group_inputs)
                st.success(f"Pozícia #{split_id} rozdelená na nohy: {new_ids}")
                st.rerun()

        st.divider()

        # ── 2. Rýchle hromadné Group ID ────────────────────────────────────
        st.subheader("Rýchle hromadné priradenie Group ID")
        st.caption("Zadaj ID nôh oddelené čiarkou a Group ID → uloží naraz.")

        rc1, rc2 = st.columns([2, 3])
        with rc1:
            bulk_ids_input = st.text_input("ID nôh (napr. 1,2,3)", placeholder="1,2,3")
        with rc2:
            bulk_group_input = st.text_input("Group ID", placeholder="napr. AMZN_DIA_MAR26")

        if st.button("Priradiť", type="primary", key="quick_group_btn"):
            try:
                ids_list = [int(x.strip()) for x in bulk_ids_input.split(",") if x.strip()]
                if not ids_list:
                    st.warning("Zadaj aspoň jedno ID.")
                else:
                    db.bulk_set_group_id(ids_list, bulk_group_input.strip())
                    st.success(f"Group ID **{bulk_group_input}** priradené nohám: {ids_list}")
                    st.rerun()
            except ValueError:
                st.error("Neplatné ID — zadaj čísla oddelené čiarkou.")

        st.divider()

        # ── 3. Prehľad skupín ───────────────────────────────────────────────
        st.subheader("Prehľad skupín — finančný stav")
        groups_map: dict[str, list] = {}
        for t in all_edit_trades:
            # Oprava duplikátov: strip() a jednotné zaobchádzanie s None
            gid = (t.get("group_id") or "").strip() or "— (bez skupiny)"
            groups_map.setdefault(gid, []).append(t)

        for gid, legs in sorted(groups_map.items()):
            open_legs   = [t for t in legs if t.get("status") == "Open"]
            closed_legs = [t for t in legs if t.get("status") == "Closed"]
            real_pnl    = sum(db.compute_pnl(t) or 0 for t in closed_legs)
            total_comm  = sum(t.get("commission") or 0 for t in legs)
            # Entry cost: súčet entry_price * contracts * 100 pre otvorené nohy
            entry_cost  = sum((t.get("entry_price") or 0) * (t.get("contracts") or 1) * 100
                              for t in open_legs)
            
            # Priebežná finančná stopa (Cumulative P&L v rámci skupiny)
            legs_sorted = sorted(legs, key=lambda x: (x.get("exit_date") or x.get("entry_date") or ""))
            
            # Formátovanie hlavičky
            pnl_sign = "🟢" if real_pnl >= 0 else "🔴"
            header = (
                f"**{gid}** &nbsp;·&nbsp; "
                f"{len(open_legs)} otvorené / {len(closed_legs)} uzavreté &nbsp;·&nbsp; "
                f"Realizovaný P&L: {pnl_sign} **${real_pnl:+,.0f}**"
            )
            
            with st.expander(header, expanded=(gid != "— (bez skupiny)")):
                if gid != "— (bez skupiny)":
                    # Finančná stopa (malý graf alebo metriky)
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Realizovaný zisk/strata (čistý)", f"${real_pnl:+,.0f}")
                    m2.metric("Aktuálny capital", f"${entry_cost:,.0f}")
                    m4.metric("Zaplatené komisie", f"${total_comm:,.2f}" if total_comm > 0 else "—")
                    
                    # Dátumová stopa
                    all_dates = sorted([d for d in [t.get("entry_date") for t in legs] + [t.get("exit_date") for t in legs] if d])
                    if all_dates:
                        duration = (datetime.now().date() - datetime.fromisoformat(all_dates[0]).date()).days
                        m3.metric("Vek stratégie", f"{duration} dní", help=f"Od {all_dates[0]}")

                    # Malý P&L graf pre skupinu
                    if closed_legs:
                        import plotly.express as px
                        cdf = pd.DataFrame([
                            {"date": t["exit_date"], "pnl": db.compute_pnl(t)} 
                            for t in sorted(closed_legs, key=lambda x: x["exit_date"])
                        ])
                        cdf["cum_pnl"] = cdf["pnl"].cumsum()
                        fig_mini = px.line(cdf, x="date", y="cum_pnl", title="Vývoj P&L skupiny")
                        fig_mini.update_layout(height=200, margin=dict(l=0,r=0,t=30,b=0), xaxis_title=None, yaxis_title=None)
                        st.plotly_chart(fig_mini, use_container_width=True, config={'displayModeBar': False})

                grows = []
                for t in legs:
                    pnl_v = db.compute_pnl(t)
                    grows.append({
                        "ID": t["id"],
                        "Status": t.get("status", ""),
                        "Ticker": t["ticker"],
                        "Noha": t.get("leg_type", ""),
                        "Typ": t.get("option_type", ""),
                        "Strike": t.get("strike"),
                        "Expiry": t.get("expiry", ""),
                        "Kontr.": t.get("contracts", 1),
                        "Entry $": t.get("entry_price"),
                        "Exit $": t.get("exit_price"),
                        "Komisia $": t.get("commission") or 0.0,
                        "P&L čistý $": round(pnl_v) if pnl_v is not None else None,
                        "Dátum": t.get("exit_date") or t.get("entry_date", ""),
                        "Stratégia": t.get("strategy", ""),
                    })
                st.dataframe(
                    pd.DataFrame(grows),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Strike":      st.column_config.NumberColumn(format="$%.2f"),
                        "Entry $":     st.column_config.NumberColumn(format="$%.2f"),
                        "Exit $":      st.column_config.NumberColumn(format="$%.2f"),
                        "Komisia $":   st.column_config.NumberColumn(format="$%.2f"),
                        "P&L čistý $": st.column_config.NumberColumn(format="$%d"),
                    },
                )


# ─── Tab: Uzavreté pozície ────────────────────────────────────────────────────
with tab_closed:
    st.subheader("Uzavreté pozície")
    closed_trades = db.get_closed_trades()

    if not closed_trades:
        st.info("Žiadne uzavreté obchody.")
    else:
        df_closed = _build_df(closed_trades, show_pnl=True)
        st.dataframe(df_closed, use_container_width=True, hide_index=True,
                     column_config=_col_config(pnl=True))

        total_pnl = sum(db.compute_pnl(t) or 0 for t in closed_trades)
        wins = sum(1 for t in closed_trades if (db.compute_pnl(t) or 0) > 0)
        losses = len(closed_trades) - wins
        wr = wins / len(closed_trades) * 100 if closed_trades else 0

        st.divider()
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Celkový P&L", f"${total_pnl:.2f}", delta=f"{'▲' if total_pnl >= 0 else '▼'}")
        m2.metric("Počet obchodov", len(closed_trades))
        m3.metric("Win Rate", f"{wr:.1f}%")
        m4.metric("Wins / Losses", f"{wins} / {losses}")
