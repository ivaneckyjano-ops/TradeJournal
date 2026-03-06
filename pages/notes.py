import streamlit as st
from datetime import datetime, date

from core import database as db

db.init_db()

st.title("Konzultácie a Poznámky")

tab_new, tab_timeline, tab_history, tab_edit = st.tabs([
    "Nová poznámka", "Strategy Timeline", "História (Log)", "Upraviť / Zmazať"
])

# ─── Tab: Nová poznámka ────────────────────────────────────────────────────────
with tab_new:
    st.subheader("Pridať záznam")

    all_trades = db.get_all_trades()
    trade_map = {
        f"#{t['id']} — {t['ticker']} {t.get('leg_type','')} {t.get('option_type','')} ${t.get('strike',0):.0f} ({t.get('status','')})": t["id"]
        for t in all_trades
    }
    trade_options = ["— (bez priradenia) —"] + list(trade_map.keys())

    with st.form("new_note_form", clear_on_submit=True):
        title = st.text_input("Nadpis *", placeholder="Napr. 'Analýza pred rollom AMZN'")

        col1, col2 = st.columns(2)
        with col1:
            selected_trade = st.selectbox("Priradiť k Trade", trade_options)
        with col2:
            group_names_n = ["— (bez skupiny) —"] + db.get_group_names()
            group_sel_n = st.selectbox("Skupina (Group ID)", group_names_n,
                                       help="Skupiny spravuješ v záložke Skupiny")
            group_id = group_sel_n if group_sel_n != "— (bez skupiny) —" else None

        content = st.text_area(
            "Obsah (Markdown podporovaný)",
            height=280,
            placeholder="""## Analýza

**Ticker:** AMZN  
**Stratégia:** Diagonal Call

### Dôvod vstupu
- IV Rank > 30%
- Support na $190

### Plán
1. Short call pri $210 vyprší OTM
2. Ak cena dosiahne $205 → zvažujem roll

> **Riziko:** Earnings 28. jan — sledovať!
""",
        )

        col_prev, col_sub = st.columns([3, 1])
        with col_prev:
            preview = st.checkbox("Zobraziť náhľad (Markdown)", value=True)
        with col_sub:
            submitted = st.form_submit_button("Uložiť poznámku", type="primary", use_container_width=True)

    if preview and content:
        with st.container(border=True):
            st.markdown(content)

    if submitted:
        if not title:
            st.error("Zadaj nadpis.")
        else:
            from datetime import date as _date
            trade_id = trade_map.get(selected_trade) if selected_trade != "— (bez priradenia) —" else None
            note_id = db.add_note(
                title=title,
                content=content,
                trade_id=trade_id,
                group_id=group_id if group_id else None,
            )
            # Automaticky pridaj udalosť do Kalendára
            _ticker = ""
            if trade_id:
                _t = next((t for t in all_trades if t["id"] == trade_id), None)
                _ticker = _t["ticker"] if _t else ""
            db.add_event(
                date=_date.today().isoformat(),
                event_type="note",
                title=title,
                ticker=_ticker or None,
                description=content[:200] if content else None,
                group_id=group_id if group_id else None,
                trade_id=trade_id,
            )
            st.success(f"Poznámka #{note_id} uložená a pridaná do Kalendára!")
            st.rerun()


# ─── Tab: Strategy Timeline ───────────────────────────────────────────────────
with tab_timeline:
    st.subheader("Strategy Timeline — Chronologický vývoj stratégie")
    st.caption("Vyber Group ID a uvidíš celý príbeh stratégie — obchody aj poznámky v časovom poradí.")

    all_trades_tl = db.get_all_trades()
    group_ids_tl = sorted({t.get("group_id") for t in all_trades_tl if t.get("group_id")})

    if not group_ids_tl:
        st.info("Žiadne Group ID. Najprv priraď Group ID pozíciám v Trade Log → Upraviť / Zoskupiť.")
    else:
        selected_group = st.selectbox("Vyber Group ID", group_ids_tl, key="tl_group")

        group_trades = [t for t in all_trades_tl if t.get("group_id") == selected_group]
        group_notes = db.get_notes(group_id=selected_group)

        # Zozbieraj všetky trade-notes kombinované
        timeline_events = []

        for t in group_trades:
            entry_d = t.get("entry_date") or ""
            timeline_events.append({
                "date": entry_d,
                "type": "trade_open",
                "data": t,
            })
            if t.get("exit_date"):
                timeline_events.append({
                    "date": t["exit_date"],
                    "type": "trade_close",
                    "data": t,
                })

        for n in group_notes:
            timeline_events.append({
                "date": n.get("created_at", "")[:10],
                "type": "note",
                "data": n,
            })

        # Zoraď podľa dátumu
        timeline_events.sort(key=lambda x: x["date"] or "")

        if not timeline_events:
            st.info("Žiadne udalosti pre túto skupinu.")
        else:
            st.markdown(f"### {selected_group}")

            # Sumár skupiny
            open_legs = [t for t in group_trades if t.get("status") == "Open"]
            closed_legs = [t for t in group_trades if t.get("status") == "Closed"]
            total_pnl = sum(db.compute_pnl(t) or 0 for t in closed_legs)

            sm1, sm2, sm3, sm4 = st.columns(4)
            sm1.metric("Otvorené nohy", len(open_legs))
            sm2.metric("Uzavreté nohy", len(closed_legs))
            sm3.metric("Realizovaný P&L", f"${total_pnl:.2f}")
            sm4.metric("Poznámky", len(group_notes))

            st.markdown("---")

            # Helper — inline formulár na pridanie poznámky
            def _quick_note_form(key_suffix: str, trade_id_val=None, group_id_val=None):
                with st.expander("+ Pridať poznámku"):
                    with st.form(f"quick_note_{key_suffix}", clear_on_submit=True):
                        qn_title = st.text_input("Nadpis", placeholder="Napr. 'Dôvod rollovania'",
                                                  key=f"qnt_{key_suffix}")
                        qn_content = st.text_area("Obsah (Markdown)", height=120,
                                                   key=f"qnc_{key_suffix}")
                        qn_submit = st.form_submit_button("Uložiť poznámku", type="primary",
                                                           use_container_width=True)
                    if qn_submit:
                        if not qn_title:
                            st.error("Zadaj nadpis.")
                        else:
                            db.add_note(
                                title=qn_title,
                                content=qn_content,
                                trade_id=trade_id_val,
                                group_id=group_id_val,
                            )
                            st.success("Poznámka uložená!")
                            st.rerun()

            # Timeline
            for idx, ev in enumerate(timeline_events):
                ev_date = ev["date"] or "?"
                ev_type = ev["type"]
                d = ev["data"]

                if ev_type == "trade_open":
                    leg = d.get("leg_type", "")
                    opt = d.get("option_type", "")
                    strike = d.get("strike", 0)
                    expiry = d.get("expiry", "")
                    entry_p = d.get("entry_price", 0)
                    contracts = d.get("contracts", 1)
                    strategy = d.get("strategy", "")
                    icon = "🔴" if leg == "Short" else "🟢"
                    with st.container(border=True):
                        st.markdown(
                            f"**{ev_date}** &nbsp; {icon} &nbsp; **VSTUP** &nbsp;|&nbsp; "
                            f"{leg} {opt} ${strike:.0f} &nbsp; exp {expiry} &nbsp; "
                            f"×{contracts} &nbsp; @ **${entry_p:.2f}** &nbsp; "
                            f"*{strategy}* &nbsp; `#{d['id']}`"
                        )
                        _quick_note_form(
                            key_suffix=f"open_{d['id']}_{idx}",
                            trade_id_val=d["id"],
                            group_id_val=selected_group,
                        )

                elif ev_type == "trade_close":
                    leg = d.get("leg_type", "")
                    opt = d.get("option_type", "")
                    strike = d.get("strike", 0)
                    exit_p = d.get("exit_price", 0)
                    pnl = db.compute_pnl(d)
                    pnl_str = f"P&L: **${pnl:.2f}**" if pnl is not None else ""
                    icon = "⬛"
                    with st.container(border=True):
                        st.markdown(
                            f"**{ev_date}** &nbsp; {icon} &nbsp; **VÝSTUP** &nbsp;|&nbsp; "
                            f"{leg} {opt} ${strike:.0f} &nbsp; @ **${exit_p:.2f}** &nbsp; "
                            f"{pnl_str} &nbsp; `#{d['id']}`"
                        )
                        _quick_note_form(
                            key_suffix=f"close_{d['id']}_{idx}",
                            trade_id_val=d["id"],
                            group_id_val=selected_group,
                        )

                elif ev_type == "note":
                    with st.container(border=True):
                        nc1, nc2 = st.columns([9, 1])
                        with nc1:
                            st.markdown(f"**{ev_date}** &nbsp; 📝 &nbsp; **{d.get('title', 'Poznámka')}**")
                        with nc2:
                            if st.button("🗑", key=f"del_note_{d['id']}_{idx}",
                                         help="Zmazať poznámku"):
                                db.delete_note(d["id"])
                                st.rerun()
                        with st.expander("Zobraziť / Upraviť"):
                            with st.form(f"edit_note_tl_{d['id']}_{idx}", clear_on_submit=False):
                                en_title = st.text_input("Nadpis", value=d.get("title", ""),
                                                          key=f"ent_{d['id']}_{idx}")
                                en_content = st.text_area("Obsah", value=d.get("content", ""),
                                                           height=150, key=f"enc_{d['id']}_{idx}")
                                en_save = st.form_submit_button("Uložiť zmeny", type="primary",
                                                                 use_container_width=True)
                            if en_save:
                                db.update_note(d["id"], en_title, en_content)
                                st.success("Uložené.")
                                st.rerun()


# ─── Tab: História ─────────────────────────────────────────────────────────────
with tab_history:
    st.subheader("História poznámok")

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        all_trades_hist = db.get_all_trades()
        trade_map_hist = {
            f"#{t['id']} — {t['ticker']} ${t.get('strike',0):.0f} {t.get('option_type','')}": t["id"]
            for t in all_trades_hist
        }
        filter_trade_lbl = st.selectbox(
            "Filtrovať podľa Trade",
            ["Všetky"] + list(trade_map_hist.keys()),
            key="hist_filter_trade",
        )
    with col_f2:
        filter_group = st.text_input("Filtrovať podľa Group ID", key="hist_filter_group")

    filter_trade_id = trade_map_hist.get(filter_trade_lbl) if filter_trade_lbl != "Všetky" else None

    if filter_trade_id:
        notes = db.get_notes(trade_id=filter_trade_id)
    elif filter_group:
        notes = db.get_notes(group_id=filter_group)
    else:
        notes = db.get_notes()

    if not notes:
        st.info("Žiadne poznámky.")
    else:
        st.caption(f"Zobrazujem {len(notes)} záznam(ov)")
        for note in notes:
            trade_label = ""
            if note.get("trade_id"):
                trade_label = f"  |  Trade #{note['trade_id']}"
            group_label = f"  |  Group: {note['group_id']}" if note.get("group_id") else ""
            ts = note.get("created_at", "")[:16]

            with st.expander(f"**{note['title']}**  —  {ts}{trade_label}{group_label}", expanded=False):
                st.markdown(note.get("content", "*(bez obsahu)*"))
                if note.get("updated_at") != note.get("created_at"):
                    st.caption(f"Naposledy upravené: {note.get('updated_at','')[:16]}")


# ─── Tab: Upraviť / Zmazať ────────────────────────────────────────────────────
with tab_edit:
    st.subheader("Upraviť alebo zmazať poznámku")

    all_notes = db.get_notes()
    if not all_notes:
        st.info("Žiadne poznámky na úpravu.")
    else:
        note_options = {
            f"#{n['id']} — {n['title']} ({n.get('created_at','')[:10]})": n["id"]
            for n in all_notes
        }
        selected_note_lbl = st.selectbox("Vyber poznámku", list(note_options.keys()))
        note_id = note_options[selected_note_lbl]
        note = db.get_note_by_id(note_id)

        if note:
            with st.form("edit_note_form"):
                new_title = st.text_input("Nadpis", value=note["title"])
                new_content = st.text_area("Obsah (Markdown)", value=note.get("content", ""), height=250)
                col_e, col_d = st.columns(2)
                with col_e:
                    edit_btn = st.form_submit_button("Uložiť zmeny", type="primary", use_container_width=True)
                with col_d:
                    del_btn = st.form_submit_button("Zmazať poznámku", type="secondary", use_container_width=True)

            if edit_btn:
                db.update_note(note_id, new_title, new_content)
                st.success("Poznámka aktualizovaná.")
                st.rerun()
            if del_btn:
                db.delete_note(note_id)
                st.warning("Poznámka zmazaná.")
                st.rerun()

            st.divider()
            st.markdown("**Náhľad:**")
            with st.container(border=True):
                st.markdown(note.get("content", "*(prázdne)*"))
