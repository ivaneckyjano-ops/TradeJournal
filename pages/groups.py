import streamlit as st
import pandas as pd

from core import database as db
from core import agent as ai_agent

db.init_db()

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

                # Nohy v tejto skupine
                legs = [t for t in all_trades if t.get("group_id") == gname]
                if legs:
                    st.markdown(f"**Nohy ({len(legs)}):**")
                    rows = []
                    for t in legs:
                        rows.append({
                            "ID": t["id"],
                            "Ticker": t["ticker"],
                            "Noha": t.get("leg_type", ""),
                            "Typ": t.get("option_type", ""),
                            "Strike": t.get("strike"),
                            "Expiry": t.get("expiry", ""),
                            "Status": t.get("status", ""),
                        })
                    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                                 column_config={"Strike": st.column_config.NumberColumn(format="$%.2f")})

                # ── AI Analýza na požiadanie (len otvorené nohy) ─────────────
                open_legs = [t for t in legs if t.get("status") == "Open"]
                st.divider()
                if open_legs:
                    ai_col1, ai_col2 = st.columns([3, 1])
                    with ai_col1:
                        st.markdown("**🤖 AI Analýza pozície**")
                        st.caption(f"Claude Sonnet · {len(open_legs)} otvorených nôh · výsledok sa uloží do Konzultácií")
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
                                group_notes = [
                                    n for n in all_notes
                                    if n.get("group_id") == gname
                                    and not n.get("title", "").startswith("🤖 AI Analýza:")
                                ]
                                analysis_text = ai_agent.analyze_group(
                                    group=g,
                                    trades=open_legs,
                                    compute_pnl=db.compute_pnl,
                                    question=custom_question,
                                    notes=group_notes,
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
