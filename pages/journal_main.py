"""
Jedna položka menu: Journal — predvolene Gréky (_portfolio_journal.py), voliteľne záznam obchodov (_trade_log_journal.py).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import streamlit as st


def _exec_script(filename: str) -> None:
    path = Path(__file__).parent / filename
    mod_name = "_tj_dyn_" + filename.replace(".py", "").replace("/", "_")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        st.error(f"Chýba súbor stránky: {path}")
        st.stop()
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)


st.caption(
    "Tu máš **jednu** položku journalu v menu. Prepni len ak potrebuješ zadať novú nohu alebo uzavrieť obchod."
)
section = st.radio(
    "Časť journalu",
    ["Journal — Gréky", "Obchody — záznam nôh"],
    horizontal=True,
    label_visibility="collapsed",
    key="tj_journal_main_section",
)

if section.startswith("Journal"):
    _exec_script("_portfolio_journal.py")
else:
    _exec_script("_trade_log_journal.py")
