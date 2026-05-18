import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

import streamlit as st

from core import database as db, ibkr
from core.page_context import TWS_DASHBOARD_PAGE

st.set_page_config(
    page_title="TradeJournal",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _infer_ib_mode() -> str:
    try:
        port = int(st.session_state.get("ib_port") or 7496)
    except (TypeError, ValueError):
        port = 7496
    return "PAPER" if port == 7497 else "LIVE"


_IB_DEFAULT_CLIENT_IDS = {
    "LIVE": 10,
    "PAPER": 15,
}


def _apply_ib_mode() -> None:
    mode = str(st.session_state.get("ib_mode") or "LIVE").strip().upper()
    prev_mode = str(st.session_state.get("_ib_mode_prev") or "").strip().upper()
    if mode != prev_mode and ibkr.get_ib() is not None:
        try:
            ibkr.disconnect()
        except Exception:
            pass
    if mode == "PAPER":
        st.session_state["ib_port"] = 7497
        st.session_state["ib_cid"] = _IB_DEFAULT_CLIENT_IDS["PAPER"]
        st.session_state["ib_conn_preset"] = "TWS — paper"
    else:
        st.session_state["ib_port"] = 7496
        st.session_state["ib_cid"] = _IB_DEFAULT_CLIENT_IDS["LIVE"]
        st.session_state["ib_conn_preset"] = "TWS — live"
    st.session_state["_ib_mode_prev"] = mode


_ib_mode = _infer_ib_mode()
if st.session_state.get("ib_mode") not in ("LIVE", "PAPER"):
    st.session_state["ib_mode"] = _ib_mode
# ib_mode určuje DB (journal_*); neprepisovať ho z portu pri každom rerune — používateľ
# môže mať LIVE v sidebari a iný port (Gateway, vlastné), inak by navigácia prepínala PAPER/LIVE.

if "ib_cid" not in st.session_state:
    st.session_state["ib_cid"] = _IB_DEFAULT_CLIENT_IDS.get(st.session_state["ib_mode"], 10)

if "_ib_mode_prev" not in st.session_state:
    st.session_state["_ib_mode_prev"] = st.session_state["ib_mode"]

_sidebar_bg = "#f3f8ff" if st.session_state["ib_mode"] == "LIVE" else "#fff8e6"
st.markdown(
    f"""
    <style>
    section[data-testid="stSidebar"] > div {{
        background-color: {_sidebar_bg};
        border-right: 1px solid rgba(100, 116, 139, 0.12);
    }}
    </style>
    """,
    unsafe_allow_html=True,
)


def _render_ib_mode_badge() -> None:
    mode = str(st.session_state.get("ib_mode") or "LIVE").strip().upper()
    if mode == "PAPER":
        bg = "#fff3c4"
        fg = "#7a5b00"
        label = "PAPER"
        dot = "#eab308"
    else:
        bg = "#dbeafe"
        fg = "#1d4ed8"
        label = "LIVE"
        dot = "#3b82f6"
    st.markdown(
        f"""
        <div style="
            display:inline-flex;
            align-items:center;
            gap:0.5rem;
            padding:0.3rem 0.8rem;
            border-radius:999px;
            background:{bg};
            border:1px solid rgba(15, 23, 42, 0.08);
            box-shadow:0 1px 2px rgba(15, 23, 42, 0.04);
            color:{fg};
            font-weight:600;
            font-size:0.85rem;
            line-height:1;
            margin:0.2rem 0 0.65rem 0;
        ">
            <span style="width:0.55rem;height:0.55rem;border-radius:50%;background:{dot};display:inline-block;"></span>
            <span style="opacity:0.78;">IB režim</span>
            <span>{label}</span>
        </div>
        """,
        unsafe_allow_html=True,
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

_render_ib_mode_badge()

# ─── Globálny auto-refresh (beží na VŠETKÝCH stránkach) ───────────────────────
from streamlit_autorefresh import st_autorefresh

if "auto_refresh_on" not in st.session_state:
    st.session_state["auto_refresh_on"] = False
if "auto_refresh_interval" not in st.session_state:
    st.session_state["auto_refresh_interval"] = 60
if "sync_count" not in st.session_state:
    st.session_state["sync_count"] = 0

auto_on = st.session_state.get("auto_refresh_on", False)

# Počas predchádzajúceho behu nastavila aktuálna stránka túto hodnotu.
# Na TWS Dash je veľký dataframe — globálny st_autorefresh + sync spôsobovali NotFoundError removeChild.
_tj_skip_global = st.session_state.get("tj_active_page") == TWS_DASHBOARD_PAGE

with st.sidebar:
    st.markdown("### LIVE / PAPER")
    st.radio(
        "Režim",
        options=["LIVE", "PAPER"],
        horizontal=True,
        key="ib_mode",
        on_change=_apply_ib_mode,
        label_visibility="visible",
    )
    st.caption("Prepne `7496` / `7497` a farbu panela.")
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
        if not _tj_skip_global:
            st_autorefresh(
                interval=st.session_state["auto_refresh_interval"] * 1000,
                key="global_auto_refresh",
            )
            last_sync = st.session_state.get("last_sync")
            sync_cnt = st.session_state.get("sync_count", 0)
            st.caption(
                f"Synchro #{sync_cnt} &nbsp;·&nbsp; "
                + (f"posledná: **{last_sync}**" if last_sync else "čaká na prvú...")
            )
        else:
            st.caption(
                "Na **TWS Dashboard** je globálna synchronizácia vypnutá (stabilita prehliadača). "
                "Obnov dáta tlačidlom na stránke."
            )
    st.caption(f"Aktívne IB pripojenie: `{ibkr.current_connection_label()}`")
    st.caption(f"Aktívna DB: `{os.path.basename(db.get_active_db_path())}`")

# ─── Globálna auto-synchronizácia (funguje na každej stránke) ─────────────────
if auto_on and not _tj_skip_global:
    from core import ibkr, database as db
    db.init_db()

    if ibkr.is_connected():
        # Pri auto-sync berieme ceny z historical last, aby dashboard/portfolio
        # neboli hneď po reloade prepísané späť na portfolio mark.
        _res = ibkr.fetch_positions(use_historical_last=False)
        if not _res.get("error"):
            _fetched_positions = _res["positions"]
            ibkr.set_scoped_session_value("live_positions", _fetched_positions)
            _sync = ibkr.sync_positions_to_db(_fetched_positions, db)
            st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
            st.session_state["sync_count"] = st.session_state.get("sync_count", 0) + 1
            if _sync.get("added", 0) > 0:
                st.toast(f"Auto-sync: +{_sync['added']} nových pozícií", icon="🔄")
            if _sync.get("updated", 0) > 0:
                st.toast(f"Auto-sync: {_sync['updated']} pozícií aktualizovaných", icon="🔄")
        # Objednávky z cache (openTrades) – bez sieťového volania, bezpečné
        _live_orders = ibkr.get_ib().openTrades() if ibkr.get_ib() else []
        ibkr.set_scoped_session_value(
            "live_orders",
            [
                {
                    "ticker": t.contract.symbol,
                    "sec_type": t.contract.secType,
                    "action": t.order.action,
                    "total_qty": t.order.totalQuantity,
                    "order_type": t.order.orderType,
                    "status": t.orderStatus.status,
                    "limit_price": t.order.lmtPrice if t.order.orderType in ("LMT", "STP LMT") else None,
                    "option_type": ("Call" if t.contract.right == "C" else "Put")
                    if t.contract.secType == "OPT"
                    else None,
                    "strike": float(t.contract.strike) if t.contract.secType == "OPT" else None,
                    "expiry": t.contract.lastTradeDateOrContractMonth
                    if t.contract.secType == "OPT"
                    else None,
                }
                for t in _live_orders
                if t.orderStatus.status in ("PendingSubmit", "PreSubmitted", "Submitted")
                and t.contract.secType in ("OPT", "STK")
            ],
        )

# ─── Navigácia ────────────────────────────────────────────────────────────────
dashboard = st.Page("pages/dashboard.py",  title="Dashboard",         icon=":material/dashboard:",      default=True)
journal_main = st.Page(
    "pages/journal_main.py",
    title="Casopis — Gréky",
    icon=":material/analytics:",
)
trading_commands = st.Page(
    "pages/trading_commands.py",
    title="Obchodné príkazy",
    icon=":material/assignment_add:",
)
groups    = st.Page("pages/groups.py",     title="Skupiny",           icon=":material/folder:")
symbols   = st.Page("pages/symbols.py",    title="Symboly",           icon=":material/bookmarks:")
flex_trades = st.Page(
    "pages/flex_trades.py",
    title="Flex Trades",
    icon=":material/table_chart:",
)
notes     = st.Page("pages/notes.py",      title="Konzultácie",       icon=":material/chat_bubble:")
modeler        = st.Page("pages/modeler.py",          title="Roll Simulátor",      icon=":material/model_training:")
roll_breakeven = st.Page("pages/roll_breakeven.py",   title="Rolovanie — breakeven spot", icon=":material/anchor:")
spread_bld     = st.Page("pages/spread_builder.py",   title="Spread Builder",      icon=":material/construction:")
portfolio_agent= st.Page("pages/portfolio_agent.py",  title="Portfolio Agent",     icon=":material/smart_toy:")
portfolio_dash = st.Page("pages/portfolio_dashboard.py", title="TWS Dashboard",    icon=":material/monitor_heart:")
steady_yields  = st.Page("pages/steady_yields.py",     title="Steady Yields",     icon=":material/trending_up:")
csv_variants   = st.Page("pages/csv_variants.py",      title="CSV Varianty",     icon=":material/table_view:")
shot_spread    = st.Page("pages/screenshot_to_spread.py", title="Obrázok → Spread", icon=":material/photo_library:")
greeks_db      = st.Page("pages/option_chain_greeks.py", title="DB Grékov",      icon=":material/storage:")
delta_diag     = st.Page("pages/delta_search_diagonal.py", title="Hľadanie delty — diagonály", icon=":material/merge:")
sector_ins     = st.Page("pages/sector_insights.py", title="Sektory — insight", icon=":material/hub:")
calendar       = st.Page("pages/calendar.py",         title="Kalendár",            icon=":material/calendar_month:")
help_page      = st.Page("pages/help.py",             title="Pomocník",            icon=":material/help:")

pg = st.navigation(
    {
        "Prehľad":  [dashboard, journal_main, calendar],
        "Obchody":  [trading_commands, groups, symbols, flex_trades],
        "Analýza":  [notes, modeler, roll_breakeven, spread_bld, csv_variants, shot_spread, greeks_db, delta_diag, sector_ins, steady_yields, portfolio_agent, portfolio_dash],
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
    st.caption(
        "Zahrnuté je všetko mimo .gitignore — vrátane aktuálnej DB pre režim LIVE/PAPER (obchody, kalendár, konzultácie, nápady)."
    )

    st.divider()
    # IBKR sem patrí PRED pg.run(): widgety v sidebari volané až po vykonaní stránky
    # dostanú iný active_script_hash (MPA v2) a frontend ich vie zobraziť zastaralé /
    # nesúladné s hlavným obsahom (Dashboard ukazoval Pripojený, sidebar Odpojený).
    from core import ibkr

    with st.container(key="tj_sidebar_ibkr_status"):
        ib_connected = st.session_state.get("ib_connected")
        if ib_connected is None:
            ib_connected = ibkr.is_connected()
        if ib_connected:
            st.success("IBKR: Pripojený")
        else:
            st.warning("IBKR: Odpojený")
    st.caption("TradeJournal v1.0")

pg.run()
