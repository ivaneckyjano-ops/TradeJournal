"""
Mapovanie názvov riadkov (tabuľka S&P 500 sektorov, slovenské názvy) na vnútorné štítky ``SYMBOL_SECTOR_VALUES``.

**Symboly → Sektor:** pevný zoznam ``SP500_SLOVAK_SECTOR_INDEX_ROWS`` (11 riadkov) — jediný primárny výber v UI;
ďalej sa dopĺňajú staršie hodnoty z ``symbols.sector``, ktoré v zozname nie sú.

``SYMBOL_SECTOR_LABELS_SK`` — voliteľná slovenská referencia v tom istom poradí ako ``SYMBOL_SECTOR_VALUES`` (GICS skrátene).
"""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

# Bez zástupného „—“ — ten je len vo formulári Symboly.
SYMBOL_SECTOR_VALUES: tuple[str, ...] = (
    "Technology",
    "Consumer Discretionary",
    "Consumer Staples",
    "Healthcare",
    "Financials",
    "Energy",
    "Utilities",
    "Real Estate",
    "Materials",
    "Industrials",
    "Communication Services",
    "Iné",
)

SECTOR_SELECT_OPTIONS: tuple[str, ...] = ("—",) + SYMBOL_SECTOR_VALUES

# Jediný zdroj názvov riadkov v UI (tabuľka S&P 500 sektorov, slovenské názvy).
_SLOVAK_SP500_ROWS: tuple[tuple[str, str], ...] = (
    ("Energetický index S&P 500", "Energy"),
    ("Materiály indexu S&P 500", "Materials"),
    ("Finančné ukazovatele indexu S&P 500", "Financials"),
    ("Index S&P 500 – Verejné služby", "Utilities"),
    ("S&P 500 Real Estate", "Real Estate"),
    ("Priemyselný index S&P 500", "Industrials"),
    ("Spotrebiteľské tovary indexu S&P 500", "Consumer Staples"),
    ("Zdravotná starostlivosť v indexe S&P 500", "Healthcare"),
    ("Informačné technológie indexu S&P 500", "Technology"),
    ("S&P 500 spotrebiteľský diskrečný", "Consumer Discretionary"),
    ("Komunikačné služby indexu S&P 500", "Communication Services"),
)

SP500_SLOVAK_SECTOR_INDEX_ROWS: tuple[str, ...] = tuple(n for n, _ in _SLOVAK_SP500_ROWS)
CANONICAL_SECTOR_TO_SP500_SLOVAK_ROW: dict[str, str] = {c: n for n, c in _SLOVAK_SP500_ROWS}

# Slovenský prehľad 1:1 s SYMBOL_SECTOR_VALUES (pre návody a stránku Sektory — insight).
SYMBOL_SECTOR_LABELS_SK: tuple[str, ...] = (
    "Informačné technológie",
    "Spotrebiteľské cyklické",
    "Spotrebiteľské necyklické",
    "Zdravotníctvo",
    "Finančné služby",
    "Energetika",
    "Verejné služby",
    "Nehnuteľnosti",
    "Materiály",
    "Priemysel",
    "Komunikačné služby",
    "Ostatné",
)

_SYMBOL_SET_LOWER = frozenset(x.lower() for x in SYMBOL_SECTOR_VALUES)

# Pravidlá v poradí: prvé zhody vyhrávajú (dlhšie / špecifickejšie frázy skôr).
_TABLE_SUBSTRING_TO_JOURNAL: tuple[tuple[str, str], ...] = (
    ("consumer staple", "Consumer Staples"),
    ("consumer discretionary", "Consumer Discretionary"),
    ("consumer cyclical", "Consumer Discretionary"),
    ("non-cyclical", "Consumer Staples"),
    ("noncyclical", "Consumer Staples"),
    ("health care", "Healthcare"),
    ("healthcare", "Healthcare"),
    ("health technology", "Healthcare"),
    ("medical", "Healthcare"),
    ("pharma", "Healthcare"),
    ("biotech", "Healthcare"),
    ("bank", "Financials"),
    ("insurance", "Financials"),
    ("capital market", "Financials"),
    ("mortgage", "Financials"),
    ("finance", "Financials"),
    ("financial", "Financials"),
    ("oil", "Energy"),
    ("gas", "Energy"),
    ("petroleum", "Energy"),
    ("energy minerals", "Energy"),
    ("renewable", "Energy"),
    ("electric", "Utilities"),
    ("water utility", "Utilities"),
    ("utility", "Utilities"),
    ("utilities", "Utilities"),
    ("reit", "Real Estate"),
    ("real estate", "Real Estate"),
    ("chemical", "Materials"),
    ("mining", "Materials"),
    ("steel", "Materials"),
    ("metal", "Materials"),
    ("construction", "Materials"),
    ("process industries", "Materials"),
    ("aerospace", "Industrials"),
    ("machinery", "Industrials"),
    ("transportation", "Industrials"),
    ("industrial", "Industrials"),
    ("producer manufacturing", "Industrials"),
    ("commercial services", "Industrials"),
    ("telecom", "Communication Services"),
    ("communication", "Communication Services"),
    ("media", "Communication Services"),
    ("internet", "Communication Services"),
    ("retail", "Consumer Discretionary"),
    ("automotive", "Consumer Discretionary"),
    ("leisure", "Consumer Discretionary"),
    ("apparel", "Consumer Discretionary"),
    ("hotel", "Consumer Discretionary"),
    ("restaurant", "Consumer Discretionary"),
    ("software", "Technology"),
    ("semiconductor", "Technology"),
    ("electronic", "Technology"),
    ("technology", "Technology"),
    ("tech services", "Technology"),
    ("miscellaneous manufacturing", "Industrials"),
    ("miscellaneous", "Iné"),
)

if len(SYMBOL_SECTOR_LABELS_SK) != len(SYMBOL_SECTOR_VALUES):
    raise RuntimeError("SYMBOL_SECTOR_LABELS_SK must match SYMBOL_SECTOR_VALUES length")


def symbol_sector_evidence_guide_markdown() -> str:
    """Kompatibilita: rovnaký text ako expander „Prehľad sektorov“ (Barchart → Symboly)."""
    from core.sector_select_options import barchart_insight_sector_guide_markdown

    return barchart_insight_sector_guide_markdown()


def _norm_tokens(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip()
    s = re.sub(r"[^\w\s]+", " ", s, flags=re.UNICODE)
    s = re.sub(r"\s+", " ", s).strip()
    return s


_SP500_SK_NORM_CANON: tuple[tuple[str, str], ...] = tuple(
    (_norm_tokens(name), canon) for name, canon in _SLOVAK_SP500_ROWS
)


def journal_sector_from_table_row_name(table_row_name: str) -> Optional[str]:
    """
    Odhadne štítok sektora z denníka (``SYMBOL_SECTOR_VALUES``) z jedného riadku OCR tabuľky.
    """
    raw = (table_row_name or "").strip()
    if not raw:
        return None
    rn = _norm_tokens(raw)
    for sk_norm, canon in _SP500_SK_NORM_CANON:
        if rn == sk_norm:
            return canon
    for sk_norm, canon in sorted(_SP500_SK_NORM_CANON, key=lambda x: -len(x[0])):
        if len(sk_norm) >= 12 and sk_norm in rn:
            return canon
        if len(rn) >= 12 and rn in sk_norm:
            return canon
    if rn in _SYMBOL_SET_LOWER:
        for j in SYMBOL_SECTOR_VALUES:
            if j.lower() == rn:
                return j
    for needle, journal in _TABLE_SUBSTRING_TO_JOURNAL:
        if needle == "technology" and "non technology" in rn:
            continue
        if needle in rn:
            return journal
    # slabá fuzzy zhoda pri preklepoch / skráteniach
    best: Optional[str] = None
    best_r = 0.62
    for j in SYMBOL_SECTOR_VALUES:
        jl = j.lower()
        r = SequenceMatcher(None, rn, jl).ratio()
        if len(rn) >= 6 and (jl in rn or rn in jl):
            boost = True
            if jl in rn:
                idx = rn.find(jl)
                if idx > 0 and re.search(r"non\s*$", rn[max(0, idx - 7) : idx]):
                    boost = False
            if boost:
                r = max(r, 0.78)
        if r > best_r:
            best_r, best = r, j
    return best


def canonical_journal_sector(portfolio_sector: str) -> Optional[str]:
    """Zhoda s GICS kanónom; ak je v DB názov riadku z Barchart, mapuje cez ``journal_sector_from_table_row_name``."""
    ps = (portfolio_sector or "").strip()
    if not ps or ps == "Neznámy sektor":
        return None
    pn = _norm_tokens(ps)
    for sk_norm, canon in _SP500_SK_NORM_CANON:
        if pn == sk_norm:
            return canon
    pl = ps.lower()
    for j in SYMBOL_SECTOR_VALUES:
        if j.lower() == pl:
            return j
    jmapped = journal_sector_from_table_row_name(ps)
    if jmapped:
        return jmapped
    best: Optional[str] = None
    best_r = 0.82
    for j in SYMBOL_SECTOR_VALUES:
        r = SequenceMatcher(None, pl, j.lower()).ratio()
        if r > best_r:
            best_r, best = r, j
    return best
