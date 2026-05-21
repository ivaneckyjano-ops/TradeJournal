"""
Obchody → Flex Trades: IB Flex Query XML → čitateľné tabuľky v aplikácii.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core.flex_xml_readable import parse_flex_xml_string, rows_to_csv_text
from core.page_context import set_tradejournal_page

set_tradejournal_page("flex_trades")

_BUCKET_ORDER = (
    ("executions", "Exekúcie (EXECUTION)"),
    ("orders", "Príkazy / ORDER"),
    ("symbol_summary", "Súhrn pod symbolom"),
    ("asset_summary", "Súhrn aktíva"),
    ("prior_period_positions", "Pozície (prior period)"),
    ("other_trades", "Ostatné Trade riadky"),
)

st.title("Flex Trades")
st.caption(
    "Nahraj **XML** z Interactive Brokers (**Flex Query** / Activity Flex), alebo **vlož text** XML. "
    "Rovnaké rozdelenie ako pri skripte ``scripts/flex_xml_to_readable.py`` — CSV si vieš stiahnuť pod tabuľkami."
)

_file = st.file_uploader("Flex XML súbor", type=["xml"], accept_multiple_files=False)
_paste = st.text_area(
    "Alebo vlož celý obsah XML",
    height=160,
    placeholder="FlexQueryResponse …",
    key="flex_xml_paste",
)
_go = st.button("Spracovať", type="primary")

_raw: str | None = None
if _go:
    if _file is not None:
        _raw = _file.read().decode("utf-8", errors="replace")
    elif (_paste or "").strip():
        _raw = _paste
    else:
        st.warning("Vyber súbor alebo vlož XML text.")

if _go and _raw:
    try:
        buckets, meta = parse_flex_xml_string(_raw)
    except Exception as e:
        st.error(f"XML sa nepodarilo spracovať: {e}")
        st.caption("Ak export začína bez znaku **<** pred `FlexQueryResponse`, aplikácia to skúsi opraviť automaticky.")
    else:
        qn = meta.get("queryName") or "—"
        qt = meta.get("type") or "—"
        st.success(f"**Flex query:** {qn} · **typ:** {qt}")

        flex_sections = [label for _key, label in _BUCKET_ORDER]
        flex_section = st.selectbox("Sekcia", flex_sections, key="flex_trades_section")
        for i, (key, label) in enumerate(_BUCKET_ORDER):
            rows = buckets.get(key) or []
            if flex_section == label:
                st.caption(f"**{label}** — {len(rows)} riadkov")
                if not rows:
                    st.info("Žiadne riadky v tomto bloku.")
                else:
                    df = pd.DataFrame(rows)
                    st.dataframe(df, use_container_width=True, hide_index=True)
                    csv_txt = rows_to_csv_text(rows)
                    fn = f"flex_{key}.csv"
                    st.download_button(
                        f"Stiahnuť {fn}",
                        data=csv_txt.encode("utf-8"),
                        file_name=fn,
                        mime="text/csv",
                        key=f"dl_{key}",
                    )
