"""
Heuristické čítanie riadka z IBKR / screenshotu (OCR text alebo skopírovaný text).

Cieľ: doplniť strike, expiráciu, typ opcie, IV, orientačnú deltu.
Δ pri vstupe brocker často neukáže — používateľ ju musí doplniť ručne.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass
from datetime import date


_MONTH = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


@dataclass
class ParsedIbRow:
    ticker: str | None
    strike: float | None
    expiry: date | None
    right: str | None  # "C" alebo "P"
    iv_raw: float | None  # ako na obrazovke (76.773 alebo 0.767)
    delta_current: float | None  # odhad z textu (aktuálna Δ kontraktu)
    has_short_qty: bool | None  # detekcia -1 kontraktov
    notes: str


def _century_year(yy: int) -> int:
    return 2000 + yy if yy < 70 else 1900 + yy


def parse_expiry_from_text(text: str) -> date | None:
    """May08'26, May 08 '26, May08 26, …"""
    t = text.replace("’", "'")
    m = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s*0?(\d{1,2})\s*'?(\d{2})\b",
        t,
        re.I,
    )
    if not m:
        return None
    key = m.group(1).lower()[:3]
    mon = _MONTH.get(key)
    if not mon:
        return None
    day = int(m.group(2))
    yy = int(m.group(3))
    year = _century_year(yy)
    try:
        return date(year, mon, day)
    except ValueError:
        return None


def _norm_num(s: str) -> float:
    return float(s.replace(",", ".").replace(" ", ""))


def parse_ibkr_row_text(text: str) -> ParsedIbRow:
    """
    Parsuje voľný text / OCR výstup. Žiadna záruka — výsledok vždy skontroluj.
    """
    if not (text or "").strip():
        return ParsedIbRow(
            None, None, None, None, None, None, None, "Prázdny text."
        )

    raw = text.strip()
    notes: list[str] = []

    # Typ opcie
    right = None
    if re.search(r"\bCALL\b", raw, re.I):
        right = "C"
    elif re.search(r"\bPUT\b", raw, re.I):
        right = "P"

    # Strike + CALL|PUT (170 CALL)
    strike = None
    m_st = re.search(r"\b(\d{2,4}(?:\.\d{1,2})?)\s+(CALL|PUT)\b", raw, re.I)
    if m_st:
        try:
            strike = _norm_num(m_st.group(1))
        except ValueError:
            strike = None
        if right is None:
            right = "C" if m_st.group(2).upper() == "CALL" else "P"

    # Ticker — prvé 2–5 veľkých písmen na začiatku riadku alebo po whitespace
    ticker = None
    m_tk = re.search(r"(?:^|\s)([A-Z]{2,5})\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", raw)
    if m_tk:
        ticker = m_tk.group(1).upper()
    else:
        m_tk2 = re.match(r"^\s*([A-Z]{1,5})\b", raw)
        if m_tk2:
            ticker = m_tk2.group(1).upper()

    expiry = parse_expiry_from_text(raw)

    # IV — percentá
    iv_raw = None
    m_iv = re.search(r"([\d.,]+)\s*%", raw)
    if m_iv:
        try:
            iv_raw = _norm_num(m_iv.group(1))
        except ValueError:
            iv_raw = None

    # Krátke kontrakty -1
    has_short = None
    if re.search(r"(?:^|\s)-\s*1\b", raw) or re.search(r"\b-\s*1\s", raw):
        has_short = True

    # Kandidáti na deltu: poradie v texte (ľavá → pravá ako v IB)
    delta_guess = None
    candidates: list[tuple[int, float]] = []
    for m in re.finditer(r"\b(0\.\d{2,4})\b", raw):
        try:
            v = _norm_num(m.group(1))
        except ValueError:
            continue
        if 0.03 <= v <= 0.97:
            candidates.append((m.start(), v))

    if iv_raw is not None and iv_raw > 2.0:
        frac_iv = iv_raw / 100.0
        candidates = [(pos, c) for pos, c in candidates if abs(c - frac_iv) > 0.02]

    in_band = [(pos, c) for pos, c in candidates if 0.12 <= c <= 0.88]
    pool = in_band if in_band else candidates
    if pool:
        pool.sort(key=lambda x: x[0])
        delta_guess = pool[0][1]
        if len(pool) > 1:
            notes.append("Ak je Δ zlá, skontroluj stĺpec Delta v IB — OCR občas pomýli s Gamma.")

    if strike is None:
        notes.append("Strike sa nepodarilo spoľahlivo nájsť.")
    if expiry is None:
        notes.append("Expiráciu (May08'26) dopĺň ručne v kalendári.")
    if iv_raw is None:
        notes.append("IV % sa nepodarilo nájsť (hľadám číslo pred %).")

    return ParsedIbRow(
        ticker=ticker,
        strike=strike,
        expiry=expiry,
        right=right,
        iv_raw=iv_raw,
        delta_current=delta_guess,
        has_short_qty=has_short,
        notes=" · ".join(notes) if notes else "",
    )


def ocr_image_to_text(image_bytes: bytes) -> tuple[str | None, str | None]:
    """
    OCR obrázka cez pytesseract. Vráti (text, chybová_hláška ak None).
    Vyžaduje systémový balík **tesseract-ocr**.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError as e:
        return None, f"Chýba knižnica: {e}"

    try:
        img = Image.open(io.BytesIO(image_bytes))
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        text = pytesseract.image_to_string(img, lang="eng")
        return (text.strip() or None), None
    except Exception as e:
        return None, str(e)
