from __future__ import annotations

import pandas as pd

from core.journal_sectors import (
    SP500_SLOVAK_SECTOR_INDEX_ROWS,
    SYMBOL_SECTOR_LABELS_SK,
    SYMBOL_SECTOR_VALUES,
    canonical_journal_sector,
    journal_sector_from_table_row_name,
    symbol_sector_evidence_guide_markdown,
)
from core.sector_insights_engine import match_table_sector


def test_journal_sector_from_barchart_style_names():
    assert journal_sector_from_table_row_name("Electronic Technology") == "Technology"
    assert journal_sector_from_table_row_name("Finance") == "Financials"
    assert journal_sector_from_table_row_name("Health Technology") == "Healthcare"
    assert journal_sector_from_table_row_name("Utilities") == "Utilities"


def test_journal_sector_from_sp500_slovak_row_names():
    assert journal_sector_from_table_row_name("Informačné technológie indexu S&P 500") == "Technology"
    assert journal_sector_from_table_row_name("Energetický index S&P 500") == "Energy"
    assert journal_sector_from_table_row_name("Index S&P 500 - Verejné služby") == "Utilities"


def test_canonical_journal_sector_accepts_sp500_slovak_labels():
    assert canonical_journal_sector("Materiály indexu S&P 500") == "Materials"
    assert canonical_journal_sector(SP500_SLOVAK_SECTOR_INDEX_ROWS[-1]) == "Communication Services"


def test_canonical_journal_sector():
    assert canonical_journal_sector("technology") == "Technology"
    assert canonical_journal_sector("Technology") == "Technology"


def test_canonical_journal_sector_accepts_barchart_row_name():
    assert canonical_journal_sector("Electronic Technology") == "Technology"


def test_match_table_sector_aligns_symbols_with_ocr_rows():
    df = pd.DataFrame(
        [
            {
                "sector": "Electronic Technology",
                "pct_1d": 0.1,
                "pct_5d": 0.0,
                "pct_1m": 0.0,
                "pct_3m": 0.0,
                "pct_1y": 0.0,
            },
            {
                "sector": "Finance",
                "pct_1d": 0.0,
                "pct_5d": 0.0,
                "pct_1m": 0.0,
                "pct_3m": 0.0,
                "pct_1y": 0.0,
            },
        ]
    )
    assert match_table_sector("Technology", df) == "Electronic Technology"
    assert match_table_sector("Financials", df) == "Finance"


def test_non_technology_not_mapped_to_technology():
    assert journal_sector_from_table_row_name("Non Technology Products") != "Technology"


def test_sector_labels_sk_aligned_with_symbol_values():
    assert len(SYMBOL_SECTOR_LABELS_SK) == len(SYMBOL_SECTOR_VALUES)


def test_evidence_guide_delegates_to_barchart_preview():
    md = symbol_sector_evidence_guide_markdown()
    assert "Symboly" in md or "Barchart" in md or "snímok" in md.lower()
