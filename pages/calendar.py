import streamlit as st
import pandas as pd
from datetime import date, datetime, timedelta
import calendar

from core import database as db

db.init_db()

# ─── Konštanty ─────────────────────────────────────────────────────────────────
EVENT_TYPES = ["earnings", "expiry", "alert", "reminder", "event", "note"]
EVENT_LABELS = {
    "earnings": "📊 Earnings",
    "expiry":   "⏳ Expirácia",
    "alert":    "🔔 Alert",
    "reminder": "📌 Pripomienka",
    "event":    "📅 Udalosť",
    "note":     "💬 Poznámka",
}
EVENT_COLORS = {
    "earnings": "#f59e0b",
    "expiry":   "#ef4444",
    "alert":    "#8b5cf6",
    "reminder": "#3b82f6",
    "event":    "#10b981",
    "note":     "#6b7280",
}

DAY_NAMES = ["Po", "Ut", "St", "Št", "Pi", "So", "Ne"]

st.title("📅 Kalendár")
st.caption("Expirácie, earnings, alerty, udalosti a poznámky na jednom mieste.")

# ─── Navigácia mesiacov ─────────────────────────────────────────────────────────
today = date.today()

if "cal_year" not in st.session_state:
    st.session_state["cal_year"] = today.year
if "cal_month" not in st.session_state:
    st.session_state["cal_month"] = today.month
if "cal_selected_day" not in st.session_state:
    st.session_state["cal_selected_day"] = None

col_prev, col_title, col_next = st.columns([1, 4, 1])
with col_prev:
    if st.button("◀ Predošlý", use_container_width=True):
        m = st.session_state["cal_month"] - 1
        y = st.session_state["cal_year"]
        if m < 1:
            m = 12
            y -= 1
        st.session_state["cal_month"] = m
        st.session_state["cal_year"] = y
        st.session_state["cal_selected_day"] = None
with col_title:
    month_name = datetime(st.session_state["cal_year"], st.session_state["cal_month"], 1).strftime("%B %Y")
    st.markdown(f"<h2 style='text-align:center; margin:0'>{month_name}</h2>", unsafe_allow_html=True)
with col_next:
    if st.button("Nasledujúci ▶", use_container_width=True):
        m = st.session_state["cal_month"] + 1
        y = st.session_state["cal_year"]
        if m > 12:
            m = 1
            y += 1
        st.session_state["cal_month"] = m
        st.session_state["cal_year"] = y
        st.session_state["cal_selected_day"] = None

# ─── Načítaj udalosti ──────────────────────────────────────────────────────────
year  = st.session_state["cal_year"]
month = st.session_state["cal_month"]
events = db.get_events(year, month)

# Zoskup podľa dátumu
events_by_day: dict[int, list] = {}
for ev in events:
    try:
        d = int(ev["date"].split("-")[2])
        if d not in events_by_day:
            events_by_day[d] = []
        events_by_day[d].append(ev)
    except Exception:
        pass

# ─── Filter ─────────────────────────────────────────────────────────────────────
with st.expander("Filtre", expanded=False):
    filter_types = st.multiselect(
        "Zobraziť typy",
        options=EVENT_TYPES,
        default=EVENT_TYPES,
        format_func=lambda t: EVENT_LABELS[t],
    )
    _sym_tickers_cal = db.get_symbol_tickers()
    if _sym_tickers_cal:
        _flt_opts = ["— všetky —"] + _sym_tickers_cal
        _flt_sel = st.selectbox("Filter podľa tickera", _flt_opts)
        filter_ticker = "" if _flt_sel == "— všetky —" else _flt_sel
    else:
        filter_ticker = st.text_input("Filter podľa tickera", "").upper().strip()

def _matches(ev: dict) -> bool:
    if ev.get("type") not in filter_types:
        return False
    if filter_ticker and (ev.get("ticker") or "").upper() != filter_ticker:
        return False
    return True

# ─── Kalendárna mriežka ─────────────────────────────────────────────────────────
st.markdown("""
<style>
.cal-grid { width: 100%; border-collapse: collapse; }
.cal-grid th {
    background: #1e293b; color: #94a3b8;
    padding: 6px 4px; text-align: center;
    font-size: 0.8rem; font-weight: 600;
}
.cal-cell {
    vertical-align: top; padding: 4px;
    border: 1px solid #334155;
    min-height: 80px; width: 14.28%;
}
.cal-cell.today { background: #1e3a5f; }
.cal-cell.other { background: #0f172a; opacity: 0.5; }
.cal-cell.normal { background: #1e293b; }
.day-num {
    font-size: 0.85rem; font-weight: 700;
    color: #e2e8f0; margin-bottom: 2px;
}
.day-num.today-num { color: #60a5fa; }
.ev-badge {
    display: inline-block; font-size: 0.65rem;
    padding: 1px 5px; border-radius: 8px;
    margin: 1px 0; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis;
    max-width: 100%; cursor: pointer;
}
</style>
""", unsafe_allow_html=True)

cal_matrix = calendar.monthcalendar(year, month)
_, days_in_month = calendar.monthrange(year, month)

# Hlavička dní
header_html = "<table class='cal-grid'><thead><tr>"
for dn in DAY_NAMES:
    header_html += f"<th>{dn}</th>"
header_html += "</tr></thead><tbody>"

body_html = ""
for week in cal_matrix:
    body_html += "<tr>"
    for day in week:
        if day == 0:
            body_html += "<td class='cal-cell other'></td>"
            continue
        is_today = (day == today.day and month == today.month and year == today.year)
        cell_class = "cal-cell today" if is_today else "cal-cell normal"
        num_class = "day-num today-num" if is_today else "day-num"
        body_html += f"<td class='{cell_class}'><div class='{num_class}'>{day}</div>"

        day_evs = [e for e in events_by_day.get(day, []) if _matches(e)]
        for ev in day_evs[:3]:
            color = EVENT_COLORS.get(ev.get("type", "event"), "#6b7280")
            label = (ev.get("title") or "")[:18]
            ticker = ev.get("ticker", "")
            if ticker:
                label = f"{ticker}: {label}"

            if not ev.get("auto") and isinstance(ev.get("id"), int):
                # Generuj odkaz pre otvorenie v novom okne
                href = f"?view_event={ev['id']}"
                tag_start = f"<a href='{href}' target='_blank' style='text-decoration:none; color:inherit;'>"
                tag_end = "</a>"
            else:
                tag_start = ""
                tag_end = ""

            body_html += (
                f"{tag_start}<div class='ev-badge' style='background:{color}20; "
                f"color:{color}; border:1px solid {color}50'>{label}</div>{tag_end}"
            )
        if len(day_evs) > 3:
            body_html += f"<div style='font-size:0.6rem;color:#94a3b8'>+{len(day_evs)-3} ďalšie</div>"
        body_html += "</td>"
    body_html += "</tr>"

full_html = header_html + body_html + "</tbody></table>"
st.markdown(full_html, unsafe_allow_html=True)

st.markdown("---")

# ─── Detail dňa (výber cez selectbox) ──────────────────────────────────────────
col_sel, col_add = st.columns([2, 2])
with col_sel:
    day_options = [None] + list(range(1, days_in_month + 1))
    selected_day = st.selectbox(
        "Vyber deň pre detail / pridanie udalosti",
        options=day_options,
        format_func=lambda d: "— vyber deň —" if d is None else f"{d}. {month_name}",
        index=0,
        key="cal_day_select",
    )

if selected_day:
    sel_date = date(year, month, selected_day)
    sel_date_str = sel_date.isoformat()
    day_evs = [e for e in events_by_day.get(selected_day, []) if _matches(e)]

    st.subheader(f"📆 {sel_date.strftime('%A, %-d. %B %Y')}")

    if day_evs:
        for ev_i, ev in enumerate(day_evs):
            ev_type = ev.get("type", "event")
            color = EVENT_COLORS.get(ev_type, "#6b7280")
            label_icon = EVENT_LABELS.get(ev_type, "📅")
            ticker_txt = f" · {ev['ticker']}" if ev.get("ticker") else ""
            auto_badge = " *(auto)*" if ev.get("auto") else ""

            with st.container(border=True):
                hc1, hc2 = st.columns([9, 1])
                with hc1:
                    st.markdown(
                        f"<div style='border-left:3px solid {color}; padding:6px 10px; "
                        f"background:{color}15; border-radius:0 6px 6px 0'>"
                        f"<b>{label_icon}{ticker_txt}{auto_badge}</b> — {ev.get('title','')}<br>"
                        f"<span style='font-size:0.82rem;color:#94a3b8'>"
                        f"{ev.get('description','') or ''}</span>"
                        "</div>",
                        unsafe_allow_html=True,
                    )
                with hc2:
                    if not ev.get("auto") and isinstance(ev.get("id"), int):
                        if st.button("🗑", key=f"del_ev_{ev['id']}_{ev_i}",
                                     help="Vymazať"):
                            db.delete_event(ev["id"])
                            st.rerun()

                # ── Prepojená poznámka / konzultácia ─────────────────────────
                linked_note = None
                # Najprv skúsime nájsť poznámku podľa presného trade_id alebo group_id z udalosti
                if ev.get("trade_id"):
                    _notes = db.get_notes(trade_id=ev["trade_id"])
                    if _notes: linked_note = _notes[0]
                
                if not linked_note and ev.get("group_id"):
                    _notes = db.get_notes(group_id=ev.get("group_id"))
                    if _notes: linked_note = _notes[0]
                
                # Ak sme stále nič nenašli, skúsime vyhľadať podľa názvu (napr. MSFT Konzultácia)
                if not linked_note:
                    _all_notes = db.get_notes()
                    for n in _all_notes:
                        if n["title"].lower() in ev["title"].lower() or ev["title"].lower() in n["title"].lower():
                            if n["created_at"][:10] == ev["date"]:
                                linked_note = n
                                break

                # Priamy obsah udalosti (ak je typ "note") alebo prepojená poznámka
                note_content = ev.get("description") or ""
                is_note_type = ev_type == "note"

                if is_note_type and note_content:
                    with st.expander("📄 Zobraziť obsah", expanded=True):
                        st.markdown(note_content)

                if linked_note:
                    with st.expander(
                        f"💬 Konzultácia: **{linked_note.get('title','')}**",
                        expanded=True,
                    ):
                        st.markdown(linked_note.get("content") or "*Bez obsahu*")
                        st.caption(f"ID: {linked_note['id']} · Vytvorené: {linked_note['created_at']}")
    else:
        st.info("V tento deň nie sú žiadne udalosti.")

    # ─── Pridanie novej udalosti ───────────────────────────────────────────────
    with st.expander(f"➕ Pridať udalosť na {sel_date.strftime('%-d. %B')}", expanded=False):
        groups_list = db.get_groups()
        group_options = ["—"] + [g["name"] for g in groups_list]

        with st.form(f"add_event_{sel_date_str}", clear_on_submit=True):
            ev_type_sel = st.selectbox(
                "Typ",
                options=EVENT_TYPES,
                format_func=lambda t: EVENT_LABELS[t],
            )
            _sym_tickers_ev = db.get_symbol_tickers()
            if _sym_tickers_ev:
                _ev_sym_opts = ["— (žiadny) —"] + _sym_tickers_ev + ["— vlastný —"]
                _ev_sym_sel = st.selectbox("Ticker (nepovinné)", _ev_sym_opts)
                if _ev_sym_sel == "— vlastný —":
                    ev_ticker = st.text_input("Vlastný ticker").upper().strip()
                elif _ev_sym_sel == "— (žiadny) —":
                    ev_ticker = ""
                else:
                    ev_ticker = _ev_sym_sel
            else:
                ev_ticker = st.text_input("Ticker (nepovinné)").upper().strip()
            ev_title  = st.text_input("Názov / popis *", placeholder="napr. AMZN Q4 Earnings")
            ev_desc   = st.text_area("Podrobnosti (nepovinné)", height=80)
            ev_group  = st.selectbox("Skupina (nepovinné)", options=group_options)
            ev_date_override = st.date_input(
                "Dátum",
                value=sel_date,
                min_value=date(today.year - 1, 1, 1),
                max_value=date(today.year + 3, 12, 31),
            )

            submitted = st.form_submit_button("Uložiť udalosť", type="primary", use_container_width=True)
            if submitted:
                if not ev_title.strip():
                    st.error("Názov je povinný.")
                else:
                    db.add_event(
                        date=ev_date_override.isoformat(),
                        event_type=ev_type_sel,
                        title=ev_title.strip(),
                        ticker=ev_ticker or None,
                        description=ev_desc.strip() or None,
                        group_id=ev_group if ev_group != "—" else None,
                    )
                    st.success("Udalosť uložená!")
                    st.rerun()

st.markdown("---")

# ─── Zoznam všetkých budúcich udalostí ─────────────────────────────────────────
with st.expander("📋 Všetky nadchádzajúce udalosti", expanded=True):
    all_evs = db.get_all_events()
    today_str = today.isoformat()
    upcoming = [e for e in all_evs if e.get("date", "") >= today_str]
    upcoming.sort(key=lambda e: e.get("date", ""))

    if not upcoming:
        st.info("Žiadne nadchádzajúce udalosti.")
    else:
        rows = []
        for e in upcoming:
            url = None
            if isinstance(e.get("id"), int):
                # Odkaz na detail
                url = f"/?view_event={e['id']}"

            rows.append({
                "Dátum": e.get("date", ""),
                "Typ": EVENT_LABELS.get(e.get("type", "event"), e.get("type", "")),
                "Ticker": e.get("ticker") or "—",
                "Názov": e.get("title", ""),
                "Popis": (e.get("description") or "")[:60],
                "Skupina": e.get("group_id") or "—",
                "Link": url,
            })
        df_ev = pd.DataFrame(rows)
        st.dataframe(
            df_ev, 
            hide_index=True, 
            use_container_width=True,
            column_config={
                "Link": st.column_config.LinkColumn(
                    "Detail", display_text="Otvoriť", width="small"
                )
            }
        )

# ─── Rýchle pridanie budúcej udalosti ─────────────────────────────────────────
with st.expander("➕ Rýchle pridanie udalosti", expanded=False):
    groups_list2 = db.get_groups()
    group_opts2 = ["—"] + [g["name"] for g in groups_list2]

    with st.form("quick_add_event", clear_on_submit=True):
        qc1, qc2, qc3 = st.columns([1, 2, 2])
        with qc1:
            q_date = st.date_input(
                "Dátum",
                value=today,
                min_value=date(today.year - 1, 1, 1),
                max_value=date(today.year + 3, 12, 31),
            )
        with qc2:
            q_type = st.selectbox("Typ", options=EVENT_TYPES, format_func=lambda t: EVENT_LABELS[t])
        with qc3:
            _sym_tickers_q = db.get_symbol_tickers()
            if _sym_tickers_q:
                _q_opts = ["— (žiadny) —"] + _sym_tickers_q + ["— vlastný —"]
                _q_sel = st.selectbox("Ticker", _q_opts)
                if _q_sel == "— vlastný —":
                    q_ticker = st.text_input("Vlastný ticker").upper().strip()
                elif _q_sel == "— (žiadny) —":
                    q_ticker = ""
                else:
                    q_ticker = _q_sel
            else:
                q_ticker = st.text_input("Ticker").upper().strip()
        q_title = st.text_input("Názov *", placeholder="napr. FED rozhodnutie o sadzbách")
        q_desc  = st.text_area("Podrobnosti", height=60)
        q_group = st.selectbox("Skupina", options=group_opts2)
        if st.form_submit_button("Uložiť", type="primary", use_container_width=True):
            if not q_title.strip():
                st.error("Názov je povinný.")
            else:
                db.add_event(
                    date=q_date.isoformat(),
                    event_type=q_type,
                    title=q_title.strip(),
                    ticker=q_ticker or None,
                    description=q_desc.strip() or None,
                    group_id=q_group if q_group != "—" else None,
                )
                st.success("Udalosť uložená!")
                st.rerun()

# ─── Konzultácie/Poznámky v kalendári ─────────────────────────────────────────
with st.expander("💬 Nedávne konzultácie & poznámky", expanded=False):
    all_notes = db.get_notes()
    if not all_notes:
        st.info("Žiadne poznámky.")
    else:
        for note in all_notes[:10]:
            dt = note.get("created_at", "")[:10]
            gid = note.get("group_id") or ""
            tid = note.get("trade_id") or ""
            tag = f"Skupina: {gid}" if gid else (f"Trade: {tid}" if tid else "")
            st.markdown(
                f"**{note.get('title', '')}** "
                f"<span style='color:#94a3b8;font-size:0.8rem'>{dt}  {tag}</span>",
                unsafe_allow_html=True,
            )
            st.caption((note.get("content") or "")[:120])
            st.markdown("---")
