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
