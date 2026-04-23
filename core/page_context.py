"""
Označenie aktívnej stránky — globálny auto-refresh v streamlit_app.py
sa na TWS Portfolio Dash vypína (inak React removeChild pri veľkom dataframe).
"""
from __future__ import annotations

import streamlit as st

# Musí sedieť s hodnotou v portfolio_dashboard.py
TWS_DASHBOARD_PAGE = "portfolio_tws"


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
