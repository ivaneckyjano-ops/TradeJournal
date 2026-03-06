import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

st.set_page_config(
    page_title="TradeJournal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Note Viewer Mode (Samostatné okno) ───────────────────────────────────────
if "view_event" in st.query_params:
    try:
        from core import database as db
        eid = int(st.query_params["view_event"])
        ev = db.get_event_by_id(eid)
        if ev:
            st.markdown(f"## {ev['title']}")
            st.caption(f"📅 {ev['date']} | {ev['type'].upper()} | ID: {eid}")
            
            if ev.get("ticker"):
                st.markdown(f"**Ticker:** {ev['ticker']}")
            if ev.get("group_id"):
                st.markdown(f"**Skupina:** {ev['group_id']}")
            
            st.divider()
            
            # Obsah udalosti
            desc = ev.get("description") or ""
            if desc:
                st.markdown(desc)
            
            # Ak je to typ NOTE, skús nájsť prepojenú plnú poznámku
            linked_note = None
            if ev["type"] == "note":
                if ev.get("trade_id"):
                    _notes = db.get_notes(trade_id=ev["trade_id"])
                    if _notes: linked_note = _notes[0]
                elif ev.get("group_id"):
                    _notes = db.get_notes(group_id=ev["group_id"])
                    if _notes: linked_note = _notes[0]
                
                # Fallback search by title
                if not linked_note:
                    _all = db.get_notes()
                    for n in _all:
                        if n["title"] == ev["title"] and n["created_at"][:10] == ev["date"]:
                            linked_note = n
                            break
            
            if linked_note:
                st.divider()
                st.markdown(f"### 📝 {linked_note['title']}")
                st.markdown(linked_note.get("content") or "*Bez obsahu*")
                
        else:
            st.error("Udalosť sa nenašla.")
            
    except Exception as e:
        st.error(f"Chyba pri načítaní: {e}")
        
    if st.button("Zavrieť okno"):
        st.write("<script>window.close()</script>", unsafe_allow_html=True)
    
    st.stop()

if "note_id" in st.query_params:
    try:
        from core import database as db
        nid = int(st.query_params["note_id"])
        note = db.get_note_by_id(nid)
        if note:
            st.markdown(f"# {note['title']}")
            st.caption(f"📅 {note['created_at']} | ID: {nid}")
            st.divider()
            st.markdown(note['content'])
        else:
            st.error("Poznámka sa nenašla.")
    except Exception as e:
        st.error(f"Chyba pri načítaní: {e}")
    
    if st.button("Zavrieť okno"):
        st.write("<script>window.close()</script>", unsafe_allow_html=True)
    
    st.stop()  # Ukonči vykonávanie, nezobrazuj zvyšok aplikácie

# ─── Globálny auto-refresh (beží na VŠETKÝCH stránkach) ───────────────────────
from streamlit_autorefresh import st_autorefresh

if "auto_refresh_on" not in st.session_state:
    st.session_state["auto_refresh_on"] = False
if "auto_refresh_interval" not in st.session_state:
    st.session_state["auto_refresh_interval"] = 60
if "sync_count" not in st.session_state:
    st.session_state["sync_count"] = 0

auto_on = st.session_state.get("auto_refresh_on", False)

with st.sidebar:
    st.markdown("### ⟳ Auto-refresh")
    st.toggle(
        "Automatická synchronizácia",
        value=auto_on,
        key="auto_refresh_on",
    )
    st.select_slider(
        "Interval",
        options=[30, 60, 120, 300, 600],
        value=st.session_state["auto_refresh_interval"],
        format_func=lambda x: f"{x}s" if x < 60 else f"{x//60} min",
        disabled=not auto_on,
        key="auto_refresh_interval",
    )
    if auto_on:
        _count = st_autorefresh(
            interval=st.session_state["auto_refresh_interval"] * 1000,
            key="global_auto_refresh",
        )
        last_sync = st.session_state.get("last_sync")
        sync_cnt  = st.session_state.get("sync_count", 0)
        st.caption(
            f"Synchro #{sync_cnt} &nbsp;·&nbsp; "
            + (f"posledná: **{last_sync}**" if last_sync else "čaká na prvú...")
        )

# ─── Globálna auto-synchronizácia (funguje na každej stránke) ─────────────────
if auto_on:
    from core import ibkr, database as db
    db.init_db()
    if ibkr.is_connected():
        _res = ibkr.fetch_positions()
        if not _res.get("error"):
            _sync = ibkr.sync_positions_to_db(_res["positions"], db)
            st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
            st.session_state["sync_count"] = st.session_state.get("sync_count", 0) + 1
            st.session_state["possibly_closed"] = _sync.get("possibly_closed", [])
            if _sync.get("added", 0) > 0:
                st.toast(f"Auto-sync: +{_sync['added']} nových pozícií", icon="🔄")
            if _sync.get("updated", 0) > 0:
                st.toast(f"Auto-sync: {_sync['updated']} pozícií aktualizovaných", icon="🔄")
            if _sync.get("possibly_closed"):
                st.toast(
                    f"⚠️ {len(_sync['possibly_closed'])} pozícií chýba v IBKR — skontroluj Dashboard",
                    icon="⚠️"
                )

# ─── Navigácia ────────────────────────────────────────────────────────────────
dashboard = st.Page("pages/dashboard.py",  title="Dashboard",         icon=":material/dashboard:",      default=True)
portfolio = st.Page("pages/portfolio.py",  title="Portfolio",         icon=":material/analytics:")
trade_log = st.Page("pages/trade_log.py",  title="Trade Log",         icon=":material/edit_note:")
groups    = st.Page("pages/groups.py",     title="Skupiny",           icon=":material/folder:")
symbols   = st.Page("pages/symbols.py",    title="Symboly",           icon=":material/bookmarks:")
notes     = st.Page("pages/notes.py",      title="Konzultácie",       icon=":material/chat_bubble:")
modeler   = st.Page("pages/modeler.py",    title="Roll Simulátor",    icon=":material/model_training:")
calendar  = st.Page("pages/calendar.py",   title="Kalendár",          icon=":material/calendar_month:")
help_page = st.Page("pages/help.py",       title="Pomocník",          icon=":material/help:")

pg = st.navigation(
    {
        "Prehľad":  [dashboard, portfolio, calendar],
        "Obchody":  [trade_log, groups, symbols],
        "Analýza":  [notes, modeler],
        "Info":     [help_page],
    },
    position="sidebar",
)

# ─── Globálny sidebar info ─────────────────────────────────────────────────────
with st.sidebar:
    st.divider()
    st.markdown("### ☁️ Záloha")
    if st.button("Zálohovať na GitHub", use_container_width=True, help="Odošle aktuálny stav a databázu na GitHub"):
        with st.spinner("Zálohujem..."):
            import subprocess
            try:
                # Spusti backup skript
                res = subprocess.run(["./backup.sh"], capture_output=True, text=True)
                if res.returncode == 0:
                    st.success("Záloha úspešná!")
                    st.caption(f"Posledná: {datetime.now().strftime('%H:%M:%S')}")
                else:
                    st.error(f"Chyba pri zálohe: {res.stderr}")
            except Exception as e:
                st.error(f"Chyba: {e}")

    st.divider()
    from core import ibkr
    if ibkr.is_connected():
        st.success("IBKR: Pripojený")
    else:
        st.warning("IBKR: Odpojený")
    st.caption("TradeJournal v1.0")

pg.run()
