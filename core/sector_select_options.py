"""
Zoznam sektorov pre **Symboly** a text pre expander na **Sektory — insight**.

Primárny zdroj: pevných **11 slovenských názvov** indexov S&P 500 (`SP500_SLOVAK_SECTOR_INDEX_ROWS` v ``journal_sectors``).
Snímok Barchart / OCR slúži na výkonnosť na stránke **Sektory — insight**; riadky tabuľky odporúčame zarovnať na rovnaké názvy.
"""

from __future__ import annotations

from core import database as db
from core.journal_sectors import SP500_SLOVAK_SECTOR_INDEX_ROWS


def symbol_sector_table_options() -> list[str]:
    """``"—"`` + 11 slovenských názvov S&P sektorov (tabuľka z denníka)."""
    return ["—"] + list(SP500_SLOVAK_SECTOR_INDEX_ROWS)


def symbol_sector_edit_options(current_sector: str | None) -> list[str]:
    """
    Možnosti pre výber sektora pri úprave symbolu: tabuľka S&P + voliteľne **aktuálna**
    hodnota z DB, ak nie je v tabuľke (legacy), aby ju bolo vidieť a vedel si ju zmeniť.
    """
    base = symbol_sector_table_options()
    v = (current_sector or "").strip()
    if v and v != "—" and v not in base:
        return base + [v]
    return base


def symbol_sector_select_options() -> tuple[list[str], str | None]:
    """
    Možnosti pre ``st.selectbox`` v **Symboly** — ``"—"`` + 11 pevných slovenských názvov S&P sektorov,
    doplnené o hodnoty už v ``symbols.sector``, ktoré v tomto zozname nie sú (legacy / „Iné“).
    """
    fixed = list(SP500_SLOVAK_SECTOR_INDEX_ROWS)
    fixed_set = set(fixed)
    seen = set(fixed)
    legacy: list[str] = []
    for row in db.get_symbols():
        s = (row.get("sector") or "").strip()
        if s and s != "—" and s not in seen:
            seen.add(s)
            legacy.append(s)
    legacy.sort(key=lambda x: x.lower())

    opts = ["—"] + fixed + legacy
    return opts, None


def symbol_sector_dropdown_options() -> list[str]:
    """
    Výber sektora v **Symboly** (pridať / úprava): „—“ + 11 S&P (sk) + hodnoty z ``symbols.sector``,
    doplnené o **názvy riadkov** z posledných snímok ``sector_performance_snapshots`` (krátky + dlhý horizont),
    ak existujú — zodpovedá „databáze“ zo stránky **Sektory — insight**.
    """
    opts, _ = symbol_sector_select_options()
    seen: set[str] = set(opts)
    extra: list[str] = []
    for horizon in ("short", "long"):
        snap = db.get_latest_sector_performance_snapshot(horizon)
        if not snap:
            continue
        rows = (snap.get("payload") or {}).get("rows") or []
        for r in rows:
            name = str(r.get("sector") or "").strip()
            if name and name not in seen:
                seen.add(name)
                extra.append(name)
    extra.sort(key=lambda x: x.lower())
    return opts + extra


def barchart_insight_sector_guide_markdown() -> str:
    """
    Markdown pre expander **Prehľad sektorov** na stránke Sektory — insight:
    rovnaký pevný zoznam ako v **Symboly → Sektor**.
    """
    lines: list[str] = [
        "**Sektory (S&P 500, slovenské názvy):** rovnaký zoznam ako v **Symboly → Sektor**.",
        "",
        "Poradie a text sú pevné — zodpovedajú tabuľke sektorových indexov (čísla môžeš importovať s desatinnou čiarkou).",
        "",
    ]
    for i, name in enumerate(SP500_SLOVAK_SECTOR_INDEX_ROWS, start=1):
        lines.append(f"{i}. `{name}`")
    lines.extend(
        [
            "",
            "Snímok z Barchart na tejto stránke môže po OCR mierne líšiť od týchto názvov — odporúčame riadky v tabuľke "
            "zarovnať na vyššie uvedené reťazce, aby sa výkonnostné riadky zhodovali s výberom v **Symboly**. "
            "Mapovanie na spoločné štítky: `journal_sector_from_table_row_name` v `core/journal_sectors.py`.",
        ]
    )
    return "\n".join(lines)
