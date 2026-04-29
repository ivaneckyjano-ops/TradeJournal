"""
Jedna položka menu: Casopis — Gréky (`_portfolio_journal.py`).
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
    "**Casopis** — skupiny a Gréky (Δ/Θ a súvisiace polia) pri otvorených nohách zarovnaných na TWS."
)
_exec_script("_portfolio_journal.py")
