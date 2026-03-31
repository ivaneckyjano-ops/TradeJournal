import streamlit as st
import pandas as pd
import threading
import time

from core import database as db
from core import agent as ai_agent
from core import ibkr

db.init_db()


def _run_fetch_job(stop_event: threading.Event):
    """Beží v separátnom vlákne. Stiahne iba pozície s Greeks (čistá matematika)."""
    job = ibkr.FETCH_JOB
    try:
        res = ibkr.fetch_positions(with_greeks=True)
        if stop_event.is_set():
            job["status"] = "cancelled"
            return
        job["positions"] = res
        job["status"] = "done"
    except Exception as exc:
        job["error"] = str(exc)
        job["status"] = "error"

st.title("Správa skupín (Group ID)")
st.caption("Vytvor skupiny tu — potom ich priradíš obchodom aj poznámkam z dropdownu.")

tab_create, tab_manage, tab_assign = st.tabs([
    "Vytvoriť skupinu", "Prehľad a úprava", "Priradiť obchodom"
])

STRATEGIES = [
    "Diagonal", "Calendar Spread", "Iron Condor", "Straddle", "Strangle",
    "Butterfly", "Bull Call Spread", "Bear Put Spread", "Covered Call",
    "Cash-Secured Put", "Iné",
]

# ─── Tab: Vytvoriť skupinu ────────────────────────────────────────────────────
with tab_create:
    st.subheader("Nová skupina")

    with st.form("new_group_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            g_ticker = st.text_input("Ticker", placeholder="napr. AMZN", value="AMZN").upper()
        with c2:
            g_strategy = st.selectbox("Stratégia", STRATEGIES)

        g_name = st.text_input(
            "Group ID (názov skupiny) *",
            placeholder="napr. AMZN_DIA_MAR26",
            help="Odporúčaný formát: TICKER_STRATÉGIA_MESIACROK",
        )

        # Auto-návrh názvu
        if g_ticker and g_strategy and not g_name:
            from datetime import date
            month_year = date.today().strftime("%b%y").upper()
            strat_short = "".join(w[0] for w in g_strategy.split()[:2])
            st.caption(f"Návrh: `{g_ticker}_{strat_short}_{month_year}`")

        g_desc = st.text_area(
            "Popis / Komentár (voliteľné)",
            placeholder="Napr. 'Diagonal spread – short May 215, long Jul 205. Otvorený pri IV Rank 45%.'",
            height=100,
        )

        submitted = st.form_submit_button("Vytvoriť skupinu", type="primary", use_container_width=True)

    if submitted:
        if not g_name:
            st.error("Zadaj Group ID (názov skupiny).")
        else:
            gid = db.add_group(g_name, g_desc, g_ticker, g_strategy)
            if gid > 0:
                st.success(f"Skupina **{g_name}** vytvorená! (ID: {gid})")
                st.rerun()
            else:
                st.warning(f"Skupina **{g_name}** už existuje.")

    # Existujúce skupiny ako rýchly prehľad
    existing = db.get_groups()
    if existing:
        st.divider()
        st.caption("Existujúce skupiny:")
        for g in existing:
            st.markdown(f"- `{g['name']}` — {g.get('ticker','')} {g.get('strategy','')} {('· '+g['description']) if g.get('description') else ''}")


# ─── Tab: Prehľad a úprava ────────────────────────────────────────────────────
with tab_manage:
    st.subheader("Všetky skupiny")

    groups = db.get_groups()
    all_trades = db.get_all_trades()
    all_notes = db.get_notes()
    all_events = db.get_all_events()

    # Pre-fetch IBKR positions to get Greeks without calling API in a loop
    ibkr_positions = []
    ibkr_orders = []
    
    # Zobrazí sa tlačidlo na stiahnutie Live Dát (Greeks, Orders), ak ich chce užívateľ hneď vidieť.
    # Inak sa použijú cacheované dáta alebo sa stiahnu až pri stlačení 'Analyzovať' na konkrétnej skupine.
    
    # NOVÉ: Skúsime vytiahnuť dáta z pamäte (Dashboard auto-refresh)
    def get_live_data():
        pos = []
        ords = []
        if "live_positions" in st.session_state:
            pos = st.session_state["live_positions"]
        if "live_orders" in st.session_state:
            ords = st.session_state["live_orders"]
        return pos, ords

    ibkr_positions, ibkr_orders = get_live_data()
    
    if ibkr.is_connected():
        job = ibkr.FETCH_JOB
        job_status = job["status"]

        # ── Spracuj výsledok ak vlákno dobehlo ──────────────────────────────
        if job_status == "done":
            res = job["positions"]
            res_ord = job["orders"]
            if res and not res.get("error"):
                ibkr_positions = res.get("positions", [])
                st.session_state["live_positions"] = ibkr_positions
            elif res and res.get("error"):
                st.error(f"Chyba pozície: {res['error']}")
            if res_ord and not res_ord.get("error"):
                ibkr_orders = res_ord.get("orders", [])
                st.session_state["live_orders"] = ibkr_orders
            job["status"] = "idle"
            _n_g = sum(1 for p in ibkr_positions if p.get("sec_type") == "OPT" and p.get("theta") is not None)
            _t_o = sum(1 for p in ibkr_positions if p.get("sec_type") == "OPT")
            st.success(f"Načítané: {len(ibkr_positions)} pozícií · Greeks: {_n_g}/{_t_o} opcií · {len(ibkr_orders)} objednávok")

        elif job_status == "cancelled":
            st.warning("Sťahovanie bolo zastavené.")
            job["status"] = "idle"

        elif job_status == "error":
            st.error(f"Chyba pri sťahovaní: {job['error']}")
            job["status"] = "idle"

        # ── Tlačidlá ────────────────────────────────────────────────────────
        col_btn, col_stop, col_status = st.columns([2, 1.5, 3])

        is_running = (job_status == "running")

        with col_btn:
            do_refresh = st.button(
                "Aktualizovať Greeks a TWS objednávky",
                key="refresh_greeks_btn",
                disabled=is_running,
            )
        with col_stop:
            do_stop = st.button(
                "⏹ Zastaviť",
                key="stop_fetch_btn",
                disabled=not is_running,
                type="secondary",
            )
        with col_status:
            _n_pos = len(ibkr_positions)
            _n_with_greeks = sum(1 for p in ibkr_positions if p.get("sec_type") == "OPT" and p.get("theta") is not None)
            _n_opts = sum(1 for p in ibkr_positions if p.get("sec_type") == "OPT")
            _n_ords = len(ibkr_orders)
            if is_running:
                st.info("⏳ Sťahujem z TWS...")
            elif _n_pos > 0:
                st.caption(f"Cache: {_n_pos} pozícií · {_n_with_greeks}/{_n_opts} opcií má Greeks · {_n_ords} objednávok")
            else:
                st.caption("Cache: prázdna — klikni na tlačidlo pre načítanie")

        # ── Spusti fetch ─────────────────────────────────────────────────────
        if do_refresh and not is_running:
            # Objednávky: hlavné vlákno s nest_asyncio + timeout na IB požiadavke
            ib_obj = ibkr.get_ib()
            if ib_obj:
                ib_obj.RequestTimeout = 8   # max 8s čakania na TWS odpoveď
            res_orders = ibkr.fetch_open_orders()
            if ib_obj:
                ib_obj.RequestTimeout = 0   # obnov default (bez limitu)
            if not res_orders.get("error"):
                ibkr_orders = res_orders.get("orders", [])
                st.session_state["live_orders"] = ibkr_orders

            # Greeks: background vlákno (čistý Black-Scholes výpočet)
            stop_evt = threading.Event()
            job.update({
                "status": "running",
                "positions": None,
                "orders": None,
                "error": None,
                "stop_event": stop_evt,
                "thread": None,
            })
            t = threading.Thread(target=_run_fetch_job, args=(stop_evt,), daemon=True)
            job["thread"] = t
            t.start()
            st.rerun()

        # ── Zastav vlákno ────────────────────────────────────────────────────
        if do_stop:
            ev = job.get("stop_event")
            if ev:
                ev.set()
            job["status"] = "cancelled"
            st.rerun()

        # ── Automatický rerun kým vlákno beží (každú sekundu) ───────────────
        if is_running:
            time.sleep(1)
            st.rerun()

    def _match_greeks(t: dict) -> dict:
        """Nájde Greeks v IBKR portfóliu pre danú nohu v databáze."""
        if not ibkr_positions:
            return {}
        for p in ibkr_positions:
            if p.get("sec_type") != "OPT":
                continue
            # Porovnanie podľa tickera, strike, expiry, option_type, leg_type
            if (p.get("ticker") == t.get("ticker") and 
                float(p.get("strike", 0)) == float(t.get("strike", 0) or 0) and 
                p.get("option_type") == t.get("option_type") and 
                p.get("leg_type") == t.get("leg_type")):
                
                # Zjednodušená kontrola expiry (DB: YYYYMMDD vs TWS: YYYYMMDD)
                if str(p.get("expiry")).replace("-", "") == str(t.get("expiry")).replace("-", ""):
                    return {
                        "delta": p.get("delta"),
                        "gamma": p.get("gamma"),
                        "theta": p.get("theta"),
                        "vega": p.get("vega"),
                    }
        return {}

    if not groups:
        st.info("Žiadne skupiny. Vytvor ich v záložke **Vytvoriť skupinu**.")
    else:
        for g in groups:
            gname = g["name"]
            trade_count = sum(1 for t in all_trades if t.get("group_id") == gname)
            note_count = sum(1 for n in all_notes if n.get("group_id") == gname)

            with st.expander(
                f"**{gname}** &nbsp; · &nbsp; {g.get('ticker','')} {g.get('strategy','')} "
                f"&nbsp; · &nbsp; {trade_count} nôh &nbsp; · &nbsp; {note_count} poznámok",
                expanded=False,
            ):
                with st.form(f"edit_group_{g['id']}"):
                    ec1, ec2 = st.columns(2)
                    with ec1:
                        e_ticker = st.text_input("Ticker", value=g.get("ticker", ""),
                                                  key=f"gt_{g['id']}")
                    with ec2:
                        strat_idx = STRATEGIES.index(g["strategy"]) if g.get("strategy") in STRATEGIES else len(STRATEGIES)-1
                        e_strategy = st.selectbox("Stratégia", STRATEGIES, index=strat_idx,
                                                   key=f"gs_{g['id']}")
                    e_name = st.text_input("Group ID", value=gname, key=f"gn_{g['id']}")
                    e_desc = st.text_area("Popis", value=g.get("description", ""),
                                          height=80, key=f"gd_{g['id']}")
                    col_s, col_d = st.columns(2)
                    with col_s:
                        save_btn = st.form_submit_button("Uložiť", type="primary",
                                                          use_container_width=True)
                    with col_d:
                        del_btn = st.form_submit_button("Zmazať skupinu", type="secondary",
                                                         use_container_width=True)

                if save_btn:
                    db.update_group(g["id"], e_name, e_desc, e_ticker, e_strategy)
                    # Ak sa zmenil názov, aktualizuj aj všetky referencie
                    if e_name != gname:
                        db.bulk_set_group_id(
                            [t["id"] for t in all_trades if t.get("group_id") == gname],
                            e_name,
                        )
                    st.success(f"Skupina **{e_name}** aktualizovaná.")
                    st.rerun()

                if del_btn:
                    db.delete_group(g["id"])
                    st.warning(f"Skupina **{gname}** zmazaná. Obchody a poznámky si zachovali Group ID text.")
                    st.rerun()

                # Nohy v tejto skupine – zobrazujeme len Open, Closed filtrujeme
                all_legs = [t for t in all_trades if t.get("group_id") == gname]
                legs = [t for t in all_legs if t.get("status") == "Open"]
                closed_legs = [t for t in all_legs if t.get("status") != "Open"]
                open_legs = legs
                net_theta = 0.0
                net_gamma = 0.0
                has_greeks = False

                # Uzavreté nohy so skupinou – možnosť odstrániť ich zo skupiny
                if closed_legs:
                    with st.expander(f"⚠️ {len(closed_legs)} uzavreté nohy priradené tejto skupine", expanded=False):
                        st.caption("Tieto nohy sú uzavreté ale stále majú nastavenú skupinu. Môžeš ich zo skupiny odstrániť.")
                        for _cl in closed_legs:
                            _cl_lbl = (
                                f"#{_cl['id']} {_cl.get('leg_type','')} "
                                f"{_cl.get('option_type','')} ${float(_cl.get('strike',0)):.0f} "
                                f"exp {_cl.get('expiry','')} – Closed"
                            )
                            if st.button(f"❌ Odstrániť zo skupiny: {_cl_lbl}",
                                         key=f"rm_closed_{_cl['id']}"):
                                db.update_trade(_cl["id"], group_id="")
                                st.success(f"Noha #{_cl['id']} odstránená zo skupiny.")
                                st.rerun()

                if legs:
                    st.markdown(f"**Nohy ({len(legs)}):**")
                    rows = []
                    for t in legs:
                        # Ak je otvorená, zistíme Greeks
                        greeks = {}
                        if t.get("status") == "Open":
                            greeks = _match_greeks(t)
                            t["_greeks"] = greeks # Uložíme do dict pre agenta
                            
                            c = float(t.get("contracts", 1))
                            m = -1 if t.get("leg_type") == "Short" else 1
                            
                            if greeks.get("theta") is not None:
                                net_theta += greeks["theta"] * c * m * 100
                                has_greeks = True
                            if greeks.get("gamma") is not None:
                                net_gamma += greeks["gamma"] * c * m * 100

                        rows.append({
                            "ID": t["id"],
                            "Ticker": t["ticker"],
                            "Noha": t.get("leg_type", ""),
                            "Typ": t.get("option_type", ""),
                            "Strike": t.get("strike"),
                            "Expiry": t.get("expiry", ""),
                            "Status": t.get("status", ""),
                            "Theta (1x)": greeks.get("theta"),
                            "Gamma (1x)": greeks.get("gamma"),
                        })
                    
                    df_legs = pd.DataFrame(rows)
                    if not has_greeks:
                        df_legs = df_legs.drop(columns=["Theta (1x)", "Gamma (1x)"], errors="ignore")
                        
                    st.dataframe(df_legs, use_container_width=True, hide_index=True,
                                 column_config={
                                     "Strike": st.column_config.NumberColumn(format="$%.2f"),
                                     "Theta (1x)": st.column_config.NumberColumn(format="%.4f"),
                                     "Gamma (1x)": st.column_config.NumberColumn(format="%.4f"),
                                 })

                    if has_greeks:
                        c_t, c_g, _ = st.columns([1, 1, 2])
                        c_t.metric("Net Theta (skupina)", f"${net_theta:+.2f} / deň")
                        c_g.metric("Net Gamma (skupina)", f"{net_gamma:+.4f}")

                # Nájdi otvorené objednávky patriace tejto skupine
                group_orders = []
                if ibkr_orders:
                    for o in ibkr_orders:
                        if o.get("ticker") == e_ticker:
                            # Priradenie objednávky na opciu ku skupine
                            if o.get("sec_type") == "OPT":
                                for t in open_legs:
                                    if (t.get("option_type") == o.get("option_type") and
                                        float(t.get("strike", 0)) == o.get("strike") and
                                        str(t.get("expiry")).replace("-", "") == str(o.get("expiry")).replace("-", "")):
                                        group_orders.append(o)
                                        break
                            else:
                                # Pre akcie len priradíme, ak sa zhoduje ticker (zjednodušený predpoklad pre Covered Call a pod)
                                group_orders.append(o)

                if group_orders:
                    st.markdown("**Čakajúce TWS objednávky:**")
                    o_rows = []
                    for o in group_orders:
                        desc = f"{o.get('option_type')} ${o.get('strike',0):.0f} ({o.get('expiry')})" if o.get("sec_type") == "OPT" else "Akcia"
                        price_info = []
                        if o.get("limit_price"): price_info.append(f"LMT: ${o['limit_price']}")
                        if o.get("aux_price"): price_info.append(f"STP: ${o['aux_price']}")
                        
                        o_rows.append({
                            "Akcia": f"{o.get('action')} {o.get('total_qty')}",
                            "Kontrakt": desc,
                            "Typ": o.get("order_type"),
                            "Cena": " | ".join(price_info),
                            "Status": o.get("status"),
                        })
                    st.dataframe(pd.DataFrame(o_rows), use_container_width=True, hide_index=True)

                # ── AI Analýza na požiadanie (len otvorené nohy) ─────────────
                st.divider()
                if open_legs:
                    ai_col1, ai_col2 = st.columns([3, 1])
                    with ai_col1:
                        st.markdown("**🤖 AI Analýza pozície**")
                        ibkr_badge = "🟢 IBKR live cena" if ibkr.is_connected() else "⚪ IBKR nepripojený – bez live ceny"
                        st.caption(f"Claude Sonnet · {len(open_legs)} otvorených nôh · {ibkr_badge} · výsledok sa uloží do Konzultácií")
                    with ai_col2:
                        run_analysis = st.button(
                            "Analyzovať",
                            key=f"ai_analyze_{g['id']}",
                            type="primary",
                            use_container_width=True,
                        )
                    custom_question = st.text_input(
                        "Špeciálna otázka (voliteľné)",
                        placeholder="napr. Kedy uzavrieť pri raste AMZN do 15. mája?",
                        key=f"ai_question_{g['id']}",
                        label_visibility="collapsed",
                    )

                    # Zobraz poslednú uloženú AI analýzu pre túto skupinu
                    ai_notes = [
                        n for n in all_notes
                        if n.get("group_id") == gname and n.get("title", "").startswith("🤖 AI Analýza:")
                    ]
                    if ai_notes:
                        with st.expander(f"Posledná AI analýza ({ai_notes[0].get('created_at','')[:10]})", expanded=False):
                            st.markdown(ai_notes[0].get("content", ""))

                    if run_analysis:
                        with st.spinner("Claude analyzuje pozíciu..."):
                            try:
                                # Live cena z IBKR ak je pripojený
                                live_price_info = ""
                                ticker_name = g.get("ticker", "")
                                if ticker_name and ibkr.is_connected():
                                    with st.spinner(f"Načítavam live cenu {ticker_name}..."):
                                        price_res = ibkr.fetch_underlying(ticker_name)
                                    if price_res.get("price"):
                                        live_price_info = f"Aktuálna live cena {ticker_name}: {price_res['price']:.2f} USD (zdroj: IBKR)"
                                    elif price_res.get("error"):
                                        live_price_info = f"Live cena nedostupná: {price_res['error']}"

                                # Doplň live cenu do skupiny pre prompt
                                group_with_price = dict(g)
                                if live_price_info:
                                    existing_desc = group_with_price.get("description") or ""
                                    group_with_price["description"] = f"{existing_desc}\n{live_price_info}".strip()
                                
                                # Pridáme celkové greeks do dict, aby ich agent videl
                                if has_greeks:
                                    group_with_price["net_theta"] = net_theta
                                    group_with_price["net_gamma"] = net_gamma

                                group_notes = [
                                    n for n in all_notes
                                    if n.get("group_id") == gname
                                    and not n.get("title", "").startswith("🤖 AI Analýza:")
                                ]
                                
                                # Vyhľadáme všetky udalosti/alerty pre túto skupinu alebo pre daný ticker
                                group_events = [
                                    e for e in all_events
                                    if e.get("group_id") == gname or (e.get("ticker") and e.get("ticker") == ticker_name)
                                ]

                                # Načítanie otvorených objednávok z TWS pre túto skupinu
                                open_orders_for_group = group_orders

                                # Zmazať cache po analýze nie je nutné (lepšie držať cache kým user neklikne na "Aktualizovať Greeks...")

                                analysis_text = ai_agent.analyze_group(
                                    group=group_with_price,
                                    trades=open_legs,
                                    compute_pnl=db.compute_pnl,
                                    question=custom_question,
                                    notes=group_notes,
                                    events=group_events,
                                    orders=open_orders_for_group,
                                )
                                from datetime import date as _date
                                note_title = f"🤖 AI Analýza: {gname} ({_date.today().isoformat()})"
                                db.add_note(
                                    title=note_title,
                                    content=analysis_text,
                                    group_id=gname,
                                )
                                st.success("Analýza dokončená a uložená do Konzultácií!")
                                with st.container(border=True):
                                    st.markdown(analysis_text)
                                st.rerun()
                            except ValueError as e:
                                st.error(f"Chyba: {e}")
                            except ImportError as e:
                                st.error(f"Chýbajúci balíček: {e}")
                            except Exception as e:
                                st.error(f"Chyba pri volaní AI: {e}")
                else:
                    st.caption("🤖 AI Analýza nie je dostupná – skupina nemá otvorené nohy.")


# ─── Tab: Priradiť obchodom ───────────────────────────────────────────────────
with tab_assign:
    st.subheader("Priradiť skupinu obchodom")
    st.caption("Vyber skupinu a potom označ obchody, ktoré do nej patria.")

    groups_assign = db.get_groups()
    if not groups_assign:
        st.info("Najprv vytvor skupinu v záložke **Vytvoriť skupinu**.")
    else:
        group_options = {g["name"]: g["name"] for g in groups_assign}
        sel_group = st.selectbox("Vyber skupinu", list(group_options.keys()), key="assign_group")

        all_trades_assign = db.get_all_trades()
        trade_labels = {
            f"#{t['id']} | {t['ticker']} {t.get('leg_type','')} {t.get('option_type','')} "
            f"${t.get('strike',0):.0f} {t.get('expiry','')} "
            f"[{t.get('group_id') or '—'}]": t["id"]
            for t in all_trades_assign
        }

        # Predvyber aktuálne priradené
        preselected = [
            lbl for lbl, tid in trade_labels.items()
            if next((t for t in all_trades_assign if t["id"] == tid), {}).get("group_id") == sel_group
        ]

        selected = st.multiselect(
            "Vyber nohy pre túto skupinu",
            options=list(trade_labels.keys()),
            default=preselected,
            key="assign_trades_ms",
        )

        if st.button("Priradiť", type="primary", key="assign_btn"):
            ids = [trade_labels[lbl] for lbl in selected]
            db.bulk_set_group_id(ids, sel_group)
            st.success(f"Skupina **{sel_group}** priradená {len(ids)} nohám.")
            st.rerun()
