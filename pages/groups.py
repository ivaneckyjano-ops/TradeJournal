import streamlit as st
import pandas as pd
import threading
import time
from datetime import date

from core import database as db
from core import agent as ai_agent
from core import ibkr
from core.page_context import set_tradejournal_page
from core.portfolio_data import match_greeks

db.init_db()
set_tradejournal_page("groups")


def _run_fetch_job(stop_event: threading.Event, job: dict) -> None:
    """Beží v separátnom vlákne. Stiahne iba pozície s Greeks (čistá matematika)."""
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
st.caption(
    "**Návod:** **Vytvoriť** = nový Group ID. **Prehľad a úprava** = AI, IB dáta a správa poznámok v expandéroch. **Priradiť obchodom** = tabuľka s celým popisom nohy a zaškrtnutím členstva v skupine "
    "(funguje aj pre **akcie podkladu** — riadky **STK** z importu IB alebo ručne pridané). "
    "Rovnaké meno skupiny potom vyberáš v Trade Log a Konzultáciách."
)

# ── Model selector v sidebari (zdieľaný s Portfolio Agent) ──────────────────
with st.sidebar:
    st.markdown("**🤖 Claude model pre analýzu**")
    _model_options = list(ai_agent.AVAILABLE_MODELS.keys())
    _model_labels  = [ai_agent.AVAILABLE_MODELS[m]["label"] for m in _model_options]
    _saved_model   = st.session_state.get("selected_claude_model", "claude-sonnet-4-6")
    _saved_idx     = _model_options.index(_saved_model) if _saved_model in _model_options else 1
    _model_sel_idx = st.radio(
        "Model",
        options=range(len(_model_options)),
        format_func=lambda i: _model_labels[i],
        index=_saved_idx,
        label_visibility="collapsed",
    )
    st.session_state["selected_claude_model"] = _model_options[_model_sel_idx]

tab_create, tab_manage, tab_assign = st.tabs([
    "Vytvoriť skupinu", "Prehľad a úprava", "Priradiť obchodom"
])

STRATEGIES = [
    "Diagonal", "Calendar Spread", "Iron Condor", "Straddle", "Strangle",
    "Butterfly", "Bull Call Spread", "Bear Put Spread", "Covered Call",
    "Cash-Secured Put", "Iné",
]


def _ticker_choices_from_symbols(*, ensure_ticker: str | None = None) -> list[tuple[str, str]]:
    """Ticker pre skupiny berieme zo Symbolov; ak chýba, doplníme existujúcu hodnotu ako fallback."""
    choices: list[tuple[str, str]] = [("", "— vyber ticker zo Symboly —")]
    raw = db.get_symbol_tickers()
    known = sorted({str(t).strip().upper() for t in raw if str(t).strip()})
    ensure = (ensure_ticker or "").strip().upper()
    if ensure and ensure not in known:
        choices.append((ensure, f"{ensure} (nie je v Symboly)"))
    choices.extend((t, t) for t in known)
    return choices


def _ticker_choice_index(choices: list[tuple[str, str]], current: str | None) -> int:
    cur = (current or "").strip().upper()
    for i, (val, _) in enumerate(choices):
        if val == cur:
            return i
    return 0

# ─── Tab: Vytvoriť skupinu ────────────────────────────────────────────────────
with tab_create:
    st.caption(
        "**Návod:** Zadaj jedinečný **Group ID** (odporúčaný formát v pomocníku). Tento názov potom vyberáš v **Trade Log**, "
        "**Konzultáciách** a pri hromadnom priradení. Ticker vyberaj zo **Symboly** a stratégia je len orientačné metadáta."
    )
    st.subheader("Nová skupina")
    _ticker_opts_new = _ticker_choices_from_symbols()

    with st.form("new_group_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            g_ticker_pair = st.selectbox(
                "Ticker zo záložky Symboly",
                options=_ticker_opts_new,
                format_func=lambda x: x[1],
                index=0,
                help="Symbol pridaj najprv na stránke **Symboly**. Tu už len vyberáš existujúci ticker.",
            )
            g_ticker = (g_ticker_pair[0] or "").strip().upper()
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
    st.caption(
        "**Návod:** Každá skupina je v **expandéri** — živé dáta z IB (ak si pripojený), AI analýza/plán, úprava poznámok a udalostí. "
        "Nižšie môžeš priradiť obchody hromadne v záložke **Priradiť obchodom**."
    )
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
        pos = ibkr.get_scoped_session_value("live_positions", [])
        ords = ibkr.get_scoped_session_value("live_orders", [])
        return pos, ords

    ibkr_positions, ibkr_orders = get_live_data()
    
    if ibkr.is_connected():
        job = ibkr.get_ib_fetch_job()
        job_status = job["status"]

        # ── Spracuj výsledok ak vlákno dobehlo ──────────────────────────────
        if job_status == "done":
            res = job["positions"]
            res_ord = job["orders"]
            if res and not res.get("error"):
                ibkr_positions = res.get("positions", [])
                ibkr.set_scoped_session_value("live_positions", ibkr_positions)
            elif res and res.get("error"):
                st.error(f"Chyba pozície: {res['error']}")
            if res_ord and not res_ord.get("error"):
                ibkr_orders = res_ord.get("orders", [])
                ibkr.set_scoped_session_value("live_orders", ibkr_orders)
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
                ibkr.set_scoped_session_value("live_orders", ibkr_orders)

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
            t = threading.Thread(target=_run_fetch_job, args=(stop_evt, job), daemon=True)
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
        return match_greeks(t, ibkr_positions)

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
                _ticker_opts_edit = _ticker_choices_from_symbols(ensure_ticker=g.get("ticker"))
                _ticker_idx_edit = _ticker_choice_index(_ticker_opts_edit, g.get("ticker"))
                with st.form(f"edit_group_{g['id']}"):
                    ec1, ec2 = st.columns(2)
                    with ec1:
                        e_ticker_pair = st.selectbox(
                            "Ticker zo záložky Symboly",
                            options=_ticker_opts_edit,
                            format_func=lambda x: x[1],
                            index=_ticker_idx_edit,
                            key=f"gt_{g['id']}",
                            help="Ak ticker chýba v Symboly, doplň ho najprv tam.",
                        )
                        e_ticker = (e_ticker_pair[0] or "").strip().upper()
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

                    _rm_map: dict[str, int] = {}
                    _rm_opts: list[str] = []
                    for t in legs:
                        tid = int(t["id"])
                        tk = str(t.get("ticker") or "").strip()
                        lt = str(t.get("leg_type") or "").strip()
                        ot = str(t.get("option_type") or "").strip()
                        if ot.upper() in ("STK", "STOCK"):
                            q = int(t.get("contracts") or 1)
                            lbl = f"#{tid} · {tk} · {lt} · STK · {q} ks"
                        else:
                            k = float(t.get("strike") or 0)
                            ex = str(t.get("expiry") or "").strip()
                            q = int(t.get("contracts") or 1)
                            lbl = (
                                f"#{tid} · {tk} · {lt} · {ot} · strike {k:.0f} · exp {ex} · ×{q}"
                            )
                        _rm_opts.append(lbl)
                        _rm_map[lbl] = tid
                    st.multiselect(
                        "Vyber nohy na vyradenie zo skupiny",
                        options=_rm_opts,
                        key=f"grp_rm_pick_{g['id']}",
                        help="Nohám sa vymaže Group ID (skončia bez skupiny). Gréky v DB sa nemenia.",
                    )
                    if st.button(
                        "Vyradiť vybrané zo skupiny",
                        key=f"grp_rm_btn_{g['id']}",
                        type="secondary",
                    ):
                        _picked_rm = list(st.session_state.get(f"grp_rm_pick_{g['id']}") or [])
                        n_rm = 0
                        for lab in _picked_rm:
                            tid_rm = _rm_map.get(str(lab))
                            if tid_rm is None:
                                continue
                            db.update_trade(int(tid_rm), group_id="")
                            n_rm += 1
                        if n_rm:
                            st.success(f"Vyradených nôh zo skupiny **{gname}**: **{n_rm}**.")
                            st.session_state[f"grp_rm_pick_{g['id']}"] = []
                            st.rerun()
                        else:
                            st.warning("Vyber aspoň jednu nohu v zozname vyššie.")

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
                                    model=st.session_state.get("selected_claude_model"),
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
                    # ── Plánovaná analýza (pre skupiny bez otvorených nôh) ────
                    st.divider()
                    st.markdown("**🤖 AI Analýza – plánovaná pozícia**")
                    st.caption("Skupina nemá otvorené nohy – agent analyzuje plánovaný vstup (popis skupiny + TWS objednávky).")

                    plan_col1, plan_col2 = st.columns([3, 1])
                    with plan_col1:
                        plan_question = st.text_input(
                            "Otázka / zámer (voliteľné)",
                            placeholder="napr. Je teraz vhodný čas otvoriť AMZN diagonal?",
                            key=f"plan_question_{g['id']}",
                            label_visibility="collapsed",
                        )
                    with plan_col2:
                        run_plan_analysis = st.button(
                            "Analyzovať plán",
                            key=f"plan_analyze_{g['id']}",
                            type="secondary",
                            use_container_width=True,
                        )

                    # Zobraz poslednú uloženú plánovanú analýzu
                    plan_notes = [
                        n for n in all_notes
                        if n.get("group_id") == gname and n.get("title", "").startswith("📋 Plán:")
                    ]
                    if plan_notes:
                        with st.expander(f"Posledná plánovaná analýza ({plan_notes[0].get('created_at','')[:10]})", expanded=False):
                            st.markdown(plan_notes[0].get("content", ""))

                    if run_plan_analysis:
                        with st.spinner("Claude analyzuje plánovaný vstup..."):
                            try:
                                ticker_name = g.get("ticker", "")
                                live_price_info = ""
                                if ticker_name and ibkr.is_connected():
                                    price_res = ibkr.fetch_underlying(ticker_name)
                                    if price_res.get("price"):
                                        live_price_info = f"Aktuálna live cena {ticker_name}: {price_res['price']:.2f} USD"

                                # Prompt pre plánovaný vstup
                                from datetime import date as _date
                                plan_desc = g.get("description") or "(nevyplnené)"
                                if live_price_info:
                                    plan_desc = f"{plan_desc}\n{live_price_info}"

                                orders_text_plan = ""
                                if group_orders:
                                    lines = []
                                    for o in group_orders:
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
                                        cond_str = f" ⟦{conds}⟧" if conds else ""
                                        lines.append(
                                            f"  - {o.get('action')} {o.get('total_qty')}x {detail}"
                                            f" | {o.get('order_type')} | {o.get('status')}{cond_str}"
                                        )
                                    orders_text_plan = "\n## Plánované objednávky v TWS:\n" + "\n".join(lines)

                                q_extra = f"\n## Otázka obchodníka:\n{plan_question}" if plan_question else ""
                                model_id = st.session_state.get("selected_claude_model") or "claude-sonnet-4-6"

                                from core.agent import _load_client, AVAILABLE_MODELS, MODEL as DEFAULT_MODEL
                                client = _load_client()
                                m_info = AVAILABLE_MODELS.get(model_id, {})
                                max_tok = m_info.get("max_tokens", 1200)

                                prompt = f"""Si skúsený obchodník s opciami. Vyhodnoť plánovaný vstup do pozície.

PRAVIDLÁ:
- Píš v slovenčine
- Ceny píš ako "190 USD", bez LaTeX
- Buď konkrétny – uveď strike, expiry, odhadovanú cenu, podmienky vstupu
- Max 300 slov

## Plánovaná pozícia
- Ticker: {g.get('ticker', '?')}
- Stratégia: {g.get('strategy', '?')}
- Zámer / tézis: {plan_desc}
- Dátum: {_date.today().strftime('%d.%m.%Y')}
{orders_text_plan}
{q_extra}
---
Odpovedaj v tomto formáte:

## Hodnotenie vstupu
(2-3 vety – vhodnosť teraz, sentiment, IV kontext)

## Konkrétny návrh spreadu
TICKER | Short noha | Long noha | Net debet ~$XXX | Theta ~+$X/deň | Podmienka vstupu

## Riziká a podmienky
- (čo sledovať, kedy nevstupovať, stop-loss úroveň)

## Záver
(vstúpiť / počkať / upraviť plán)
"""
                                msg = client.messages.create(
                                    model=model_id,
                                    max_tokens=max_tok,
                                    messages=[{"role": "user", "content": prompt}],
                                )
                                plan_text = msg.content[0].text

                                note_title = f"📋 Plán: {gname} ({_date.today().isoformat()})"
                                db.add_note(title=note_title, content=plan_text, group_id=gname)
                                st.success("Analýza uložená do Konzultácií!")
                                with st.container(border=True):
                                    st.markdown(plan_text)
                                st.rerun()
                            except Exception as e:
                                st.error(f"Chyba: {e}")


# ─── Tab: Priradiť obchodom ───────────────────────────────────────────────────
def _groups_assign_leg_label(t: dict) -> str:
    """Celý riadok pre tabuľku priradenia — čitateľný v tmavom UI (nie skrátené čipy)."""
    tid = int(t.get("id") or 0)
    tk = str(t.get("ticker") or "").strip()
    lt = str(t.get("leg_type") or "").strip()
    ot = str(t.get("option_type") or "").strip()
    cur = (t.get("group_id") or "").strip()
    cur_s = cur if cur else "— bez skupiny —"
    if ot.upper() in ("STK", "STOCK"):
        q = int(t.get("contracts") or 1)
        return f"#{tid} · {tk} · {lt} · STK · {q} ks · teraz: {cur_s}"
    k = float(t.get("strike") or 0)
    ex = str(t.get("expiry") or "").strip()
    q = int(t.get("contracts") or 1)
    return f"#{tid} · {tk} · {lt} · {ot} · strike {k:.0f} · exp {ex} · ×{q} · teraz: {cur_s}"


with tab_assign:
    st.subheader("Priradiť skupinu obchodom")
    st.caption(
        "**Návod:** Vyber **skupinu**, v tabuľke zaškrtni **Patrí do vybranej skupiny** pri nohách, ktoré do nej majú patriť, "
        "a stlač **Uložiť zostavu skupiny**. **Vyradenie:** odškrtni nohu, ktorá už v tejto skupine je, a znova **Uložiť zostavu skupiny** — Group ID sa vymaže. "
        "Zoznam sú len **otvorené** nohy (stav Open). **Akcie podkladu** sú v denníku ako nohy s typom **STK** (po **Importe z IB** "
        "alebo cez formulár nižšie) — v tabuľke ich spoznáš podľa popisu „STK · N ks“."
    )

    _fb = st.session_state.pop("groups_assign_last_msg", None)
    if _fb:
        st.success(_fb)
    _fb_stk = st.session_state.pop("groups_add_stk_msg", None)
    if _fb_stk:
        st.success(_fb_stk)

    groups_assign = db.get_groups()
    if not groups_assign:
        st.info("Najprv vytvor skupinu v záložke **Vytvoriť skupinu**.")
    else:
        group_options = {g["name"]: g["name"] for g in groups_assign}
        sel_group = st.selectbox("Vyber skupinu", list(group_options.keys()), key="assign_group")
        st.caption(
            "Správny **ticker** spravuj v **Symboly**. Tu len viažeš existujúcu otvorenú nohu k **Group ID** vybranej skupiny."
        )

        with st.expander("➕ Pridať akciu podkladu (STK) do denníka a do vybranej skupiny", expanded=False):
            st.caption(
                "Pre **delta hedge** alebo akcie, ktoré ešte nie sú v denníku. "
                "Ak máš rovnakú pozíciu v TWS, pri **Importe pozícií z IB** sa riadok zlúči podľa tickeru / STK (duplicita sa zvyčajne neobjaví)."
            )
            with st.form("groups_add_stk_leg_form", clear_on_submit=True):
                st.text_input("Ticker podkladu", placeholder="napr. AAPL", key="groups_stk_ticker")
                st.selectbox("Smer pozície", ["Long", "Short"], key="groups_stk_leg")
                st.number_input("Počet akcií (ks)", min_value=1, value=100, step=1, key="groups_stk_shares")
                st.number_input(
                    "Priemerná cena vstupu (USD / ks)",
                    min_value=0.0,
                    value=0.0,
                    step=0.01,
                    format="%.4f",
                    key="groups_stk_entry",
                )
                st.text_input("Poznámka / stratégia", value="Stock / hedge", key="groups_stk_strat")
                _stk_sub = st.form_submit_button("Uložiť STK do denníka", type="primary")
            if _stk_sub:
                _tn = str(st.session_state.get("groups_stk_ticker") or "").strip().upper()
                _leg = str(st.session_state.get("groups_stk_leg") or "Long")
                try:
                    _sh = int(st.session_state.get("groups_stk_shares") or 1)
                except (TypeError, ValueError):
                    _sh = 1
                if _sh < 1:
                    _sh = 1
                try:
                    _ep = float(st.session_state.get("groups_stk_entry") or 0.0)
                except (TypeError, ValueError):
                    _ep = 0.0
                _strat = str(st.session_state.get("groups_stk_strat") or "Stock / hedge").strip() or "Stock / hedge"
                if not _tn:
                    st.warning("Zadaj ticker.")
                else:
                    _tid = db.add_trade(
                        ticker=_tn,
                        strategy=_strat,
                        leg_type=_leg,
                        option_type="STK",
                        strike=0.0,
                        expiry="",
                        contracts=_sh,
                        entry_price=_ep,
                        entry_date=date.today().isoformat(),
                        group_id=sel_group,
                        delta_at_entry=1.0,
                    )
                    st.session_state["groups_add_stk_msg"] = (
                        f"Pridaná akcia **{_tn}** (ID obchodu **{_tid}**) do skupiny **{sel_group}**."
                    )
                    st.rerun()

        all_trades_assign = db.get_open_trades()
        hide_other = st.checkbox(
            "Skryť nohy, ktoré sú už v **inej** skupine (menej šumu pri práci s jednou skupinou)",
            value=True,
            key="assign_hide_other_groups",
            help="Vypni, ak chceš naraz presúvať nohy medzi dvoma skupinami alebo vidieť celý portfól.",
        )
        leg_kind = st.radio(
            "Zobraziť v tabuľke",
            options=["Všetky nohy", "Iba opčné kontrakty", "Iba akcie (STK)"],
            horizontal=True,
            key="assign_leg_kind_filter",
            help="STK = akcia podkladu v denníku (nie opčný kontrakt).",
        )
        q_filt = st.text_input(
            "Filter (časť ID, ticker, expirácia, skupina…)",
            value="",
            key="assign_row_filter",
            placeholder="napr. GLW alebo 202607",
        ).strip().lower()

        rows_src: list[dict] = []
        for t in sorted(all_trades_assign, key=lambda x: (str(x.get("ticker") or ""), int(x.get("id") or 0))):
            _ot_a = str(t.get("option_type") or "").strip().upper()
            _is_stk_row = _ot_a in ("STK", "STOCK")
            if leg_kind == "Iba opčné kontrakty" and _is_stk_row:
                continue
            if leg_kind == "Iba akcie (STK)" and not _is_stk_row:
                continue
            cur = (t.get("group_id") or "").strip()
            if hide_other and cur and cur != sel_group:
                continue
            hay = _groups_assign_leg_label(t).lower()
            if q_filt and q_filt not in hay:
                continue
            rows_src.append(t)

        if not all_trades_assign:
            st.info(
                "V denníku nemáš žiadne **otvorené** nohy (stav Open) — nie je čo priradiť. "
                "Skontroluj režim LIVE/PAPER a načítaj pozície z IB na Dashboarde."
            )
        elif not rows_src:
            st.warning(
                "Pri aktívnom filtri / skrytí iných skupín nezodpovedá žiadna noha — vypni filter alebo checkbox vyššie."
            )
        else:
            st.caption(f"Zobrazených **{len(rows_src)}** z **{len(all_trades_assign)}** otvorených nôh.")
            df_a = pd.DataFrame(
                [
                    {
                        "ID": int(t["id"]),
                        "Kontrakt": _groups_assign_leg_label(t),
                        "Patrí do vybranej skupiny": (t.get("group_id") or "").strip() == sel_group,
                    }
                    for t in rows_src
                ]
            )
            edited_a = st.data_editor(
                df_a,
                key=f"assign_members_{sel_group}",
                use_container_width=True,
                hide_index=True,
                disabled=["ID", "Kontrakt"],
                column_config={
                    "ID": st.column_config.NumberColumn("ID", disabled=True, width="small"),
                    "Kontrakt": st.column_config.TextColumn(
                        "Kontrakt (celý popis)",
                        disabled=True,
                        width="large",
                    ),
                    "Patrí do vybranej skupiny": st.column_config.CheckboxColumn(
                        f"V skupine „{sel_group}“",
                        help="Zaškrtni = noha patrí do vybranej skupiny. Odškrtni = odoberie z tejto skupiny (group_id vyprázdni).",
                    ),
                },
            )

            if st.button("Uložiť zostavu skupiny", type="primary", key="assign_btn"):
                checked_ids = set()
                for _, row in edited_a.iterrows():
                    if row.get("Patrí do vybranej skupiny"):
                        checked_ids.add(int(row["ID"]))

                shown_ids = {int(t["id"]) for t in rows_src}
                n_clear = 0
                for t in all_trades_assign:
                    tid = int(t["id"])
                    cur = (t.get("group_id") or "").strip()
                    if cur != sel_group:
                        continue
                    if tid not in shown_ids:
                        continue
                    if tid not in checked_ids:
                        db.update_trade(tid, group_id="")
                        n_clear += 1
                for tid in checked_ids:
                    db.update_trade(tid, group_id=sel_group)

                msg = (
                    f"Skupina **{sel_group}**: uložené. **{len(checked_ids)}** nôh má teraz túto skupinu "
                    f"(podľa zaškrtnutia v tabuľke). Odpojených od tejto skupiny: **{n_clear}**."
                )
                st.session_state["groups_assign_last_msg"] = msg
                try:
                    st.toast(msg, icon="✅")
                except Exception:
                    pass
                st.rerun()
