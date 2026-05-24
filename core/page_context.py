"""
Označenie aktívnej stránky — globálny auto-refresh v streamlit_app.py
sa na vybraných „ťažkých“ stránkach vypína (inak React removeChild pri veľkom DOM).

``tj_active_page`` sa v child skriptoch nastavuje až počas ``pg.run()``; rozhodnutie
v entrypointe **pred** ``pg.run()`` by ho vždy malo o beh pozadu. Preto shell
používa ``st.context.url`` (pathname aktuálnej stránky MPA v2) a voliteľný fallback
na ``tj_active_page``.
"""
from __future__ import annotations

from urllib.parse import urlparse

import streamlit as st

# Musí sedieť s hodnotou v portfolio_dashboard.py
TWS_DASHBOARD_PAGE = "portfolio_tws"

# Slugy z URL cesty = inferovaný ``url_path`` z názvu súboru (``*.py``), pozri Streamlit ``source_util.page_icon_and_name``.
# Pri prázdnej ceste (root) ``skip_global_autorefresh_for_current_page`` radšej vypne sync (default je ťažký Dashboard).
HEAVY_PAGE_URL_PATHS_SKIP_GLOBAL_AUTOREFRESH: frozenset[str] = frozenset(
    {
        # Domov + pomocník: veľa widgetov / expandre; globálny autorefresh tu často spôsobí
        # React chybu „removeChild: The node to be removed is not a child of this node“.
        "dashboard",
        "help",
        "journal_main",
        "portfolio_dashboard",
        "spread_builder",
        "steady_yields",
        "modeler",
        "delta_search_diagonal",
        "portfolio_agent",
        "trading_commands",
        "option_chain_greeks",
        "roll_breakeven",
        "screenshot_to_spread",
        "sector_insights",
        "groups",
        "symbols",
        "notes",
        "calendar",
        "flex_trades",
        "csv_variants",
    }
)


def current_navigation_url_slug() -> str:
    """Posledný segment pathname z ``st.context.url`` (MPA v2), napr. ``journal_main``."""
    try:
        raw = str(st.context.url or "").strip()
        if not raw:
            return ""
        path = (urlparse(raw).path or "").strip("/")
        if not path:
            return ""
        return path.split("/")[-1].strip()
    except Exception:
        return ""


def _slug_from_tj_active_page() -> str:
    """Mapovanie legacy ``tj_active_page`` na rovnaký slovník slugov ako URL."""
    ap = str(st.session_state.get("tj_active_page") or "").strip()
    if not ap:
        return ""
    if ap == "portfolio":
        return "journal_main"
    if ap == TWS_DASHBOARD_PAGE:
        return "portfolio_dashboard"
    return ap


def skip_global_autorefresh_for_current_page() -> bool:
    """
    True = nevolať ``st_autorefresh`` ani globálnu IB synchronizáciu v entrypointe.

    Primárne z URL (aktuálna stránka v tomto behu); ak path chýba, fallback na
    ``tj_active_page`` z predchádzajúceho behu (lepšie ako nič).
    """
    slug = current_navigation_url_slug()
    # Prázdny pathname (root / prvý beh) — default je ťažký Dashboard; radšej nesyncovať,
    # kým Streamlit nevyplní ``st.context.url`` (inak občas removeChild v prehliadači).
    if not slug:
        return True
    if slug in HEAVY_PAGE_URL_PATHS_SKIP_GLOBAL_AUTOREFRESH:
        return True
    fb = _slug_from_tj_active_page()
    return bool(fb and fb in HEAVY_PAGE_URL_PATHS_SKIP_GLOBAL_AUTOREFRESH)


def set_tradejournal_page(page_id: str) -> None:
    st.session_state["tj_active_page"] = page_id


def render_ai_chat_markdown(messages: list | None) -> None:
    """
    Zobrazí históriu AI chatu bez ``st.chat_message``.

    ``st.chat_message`` vnútri ``st.expander`` + ``st.chat_input`` pod ním často spôsobí
    v prehliadači chybu „removeChild: The node to be removed is not a child of this node“
    (nesúlad React DOM vo fronte Streamlitu).
    """
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = str(msg.get("content") or "").strip()
        if not content:
            continue
        if role == "assistant":
            with st.container(border=True):
                st.caption("🤖 Asistent")
                st.markdown(content)
        elif role == "user":
            with st.container(border=True):
                st.caption("👤 Ty")
                st.markdown(content)
