"""
Nahratie screenshotu riadku z TWS (alebo podobného), OCR → úprava textu → CSV / Spread Builder.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core import database as db
from core.page_context import set_tradejournal_page
import core.sector_performance_ocr as spo
from core.screenshot_spread_row import (
    ParsedSpreadRow,
    build_sb_legs,
    infer_short_leg_tag,
    parse_spread_row_line,
    parsed_to_dataframe,
)

db.init_db()
set_tradejournal_page("screenshot_to_spread")

st.title("Obrázok → spread")
st.caption(
    "Nahraj **screenshot jedného riadku** (spot, dátumy, striky P/C, ceny, netto, voliteľne gréky). "
    "Skontroluj **OCR text**, stiahni **CSV** alebo odošli údaje do **Spread Buildera**."
)


def _iv_from_symbol_row(sym: dict | None) -> float:
    if not sym:
        return 0.30
    raw = sym.get("iv_pct")
    if raw is None:
        return 0.30
    try:
        fv = float(raw)
    except (TypeError, ValueError):
        return 0.30
    if fv > 1.0:
        fv = fv / 100.0
    return float(min(max(fv, 0.01), 5.0))


def _parse_best_from_text(text: str) -> ParsedSpreadRow | None:
    blob = " ".join((text or "").split())
    if not blob:
        return None
    for line in (text or "").splitlines():
        s = line.strip()
        if len(s) < 12:
            continue
        p = parse_spread_row_line(s)
        if p:
            return p
    return parse_spread_row_line(blob)


if not spo.ocr_stack_available():
    st.warning(
        "Chýba OCR stack (**pytesseract**, **opencv-python-headless**) alebo **Tesseract** v systéme. "
        "Môžeš stále vložiť text riadku ručne do poľa nižšie."
    )

up = st.file_uploader("Obrázok (PNG / JPG)", type=["png", "jpg", "jpeg", "webp", "tif", "tiff"])

if "ss_ocr_text" not in st.session_state:
    st.session_state["ss_ocr_text"] = ""

if up is not None:
    raw_bytes = up.getvalue()
    if st.button("Spustiť OCR", type="primary"):
        if not spo.ocr_stack_available():
            st.error("OCR nie je dostupné — nainštaluj závislosti a Tesseract.")
        else:
            try:
                st.session_state["ss_ocr_text"] = spo.ocr_image_bytes_to_text(raw_bytes, psm=6)
            except Exception as e:
                st.error(f"OCR zlyhal: {e}")

st.text_area(
    "Text z OCR (jeden riadok tabuľky; uprav podľa potreby)",
    height=120,
    key="ss_ocr_text",
)

parsed = _parse_best_from_text(st.session_state.get("ss_ocr_text") or "")

if parsed is None:
    st.info(
        "Po nahratí obrázka spusti **OCR**, prípadne **vlož alebo uprav text** jedného riadku "
        "(spot, dátumy MM/DD/YY, striky ako **157.50P**, ceny s desatinnou bodkou). Parser očakáva aspoň dve expirácie a dva striky."
    )
    st.stop()

st.success("Riadok rozpoznaný — skontroluj náhľad a doplň ticker.")

df_prev = parsed_to_dataframe(parsed)
st.dataframe(df_prev, use_container_width=True, hide_index=True)

if parsed.pct_tokens:
    st.caption("Percentá / pomocné hodnoty z riadku: " + ", ".join(str(x) for x in parsed.pct_tokens))

csv_buf = df_prev.to_csv(index=False).encode("utf-8-sig")
st.download_button(
    "Stiahnuť CSV",
    data=csv_buf,
    file_name="spread_z_obrazka.csv",
    mime="text/csv",
)

st.divider()
st.subheader("Odoslanie do Spread Buildera")

sym_iv = 0.30
st.text_input(
    "Ticker podkladu",
    key="ss_sb_ticker",
    placeholder="napr. SPY",
    help="Musí byť vyplnený — IV sa predvyplní zo záložky Symboly, ak ticker existuje.",
)
tk = (st.session_state.get("ss_sb_ticker") or "").strip().upper()
if tk:
    sym = db.get_symbol(tk)
    sym_iv = _iv_from_symbol_row(sym)
    if sym and float(sym.get("spot") or 0) > 0:
        st.caption(f"V **Symboly** je pre {tk} spot **{float(sym['spot']):.2f}** (do modelu sa berie pole „Spot“ nižšie, predvolené z OCR).")

c1, c2, c3 = st.columns(3)
with c1:
    spot_use = st.number_input("Spot (pre model)", value=float(parsed.spot), step=0.01, format="%.4f")
with c2:
    iv_use = st.number_input("IV (zlomok)", value=float(sym_iv), min_value=0.01, max_value=3.0, step=0.01, format="%.4f")
with c3:
    short_def = infer_short_leg_tag(parsed)
    short_leg = st.selectbox(
        "Short noha",
        options=["A", "B"],
        index=0 if short_def == "A" else 1,
        help="A = prvá noha v texte (prvá expirácia/strike), B = druhá. Predvolené: skoršia expirácia = short.",
    )

contracts = st.number_input("Kontrakty", min_value=1, max_value=500, value=1, step=1)

if st.button("Odoslať do Spread Buildera", type="primary"):
    if not tk:
        st.error("Zadaj **ticker** podkladu.")
    else:
        legs = build_sb_legs(
            parsed,
            short_leg=str(short_leg),
            contracts=int(contracts),
            iv=float(iv_use),
        )
        notice = (
            f"Z OCR / screenshotu ({tk}). Spot v Builderi: **{spot_use:.2f}**. "
            f"Short noha: **{short_leg}**. Skontroluj nohy a ceny v tabuľke."
        )
        st.session_state["_sb_pending_patch"] = {
            "op": "csv_calendar_variant",
            "ticker": tk,
            "spot": float(spot_use),
            "iv": float(iv_use),
            "legs": legs,
            "notice": notice,
        }
        try:
            st.switch_page("pages/spread_builder.py")
        except Exception:
            st.success("Údaje sú v session. Otvor v menu **Spread Builder**.")
