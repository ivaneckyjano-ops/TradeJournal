from __future__ import annotations

from core.journal_sectors import SP500_SLOVAK_SECTOR_INDEX_ROWS
from core.sector_select_options import (
    barchart_insight_sector_guide_markdown,
    symbol_sector_dropdown_options,
    symbol_sector_edit_options,
    symbol_sector_select_options,
    symbol_sector_table_options,
)


def test_symbol_sector_select_options_has_dash_first():
    opts, note = symbol_sector_select_options()
    assert opts and opts[0] == "—"
    assert note is None
    assert opts[1 : 1 + len(SP500_SLOVAK_SECTOR_INDEX_ROWS)] == list(SP500_SLOVAK_SECTOR_INDEX_ROWS)


def test_symbol_sector_dropdown_options_extends_select_options():
    base, _ = symbol_sector_select_options()
    full = symbol_sector_dropdown_options()
    assert len(full) >= len(base)
    assert full[: len(base)] == base


def test_symbol_sector_table_options_is_sp500_only():
    o = symbol_sector_table_options()
    assert o[0] == "—"
    assert o[1:] == list(SP500_SLOVAK_SECTOR_INDEX_ROWS)


def test_symbol_sector_edit_options_appends_legacy_current():
    legacy = "Electronic Technology"
    o = symbol_sector_edit_options(legacy)
    assert o[0] == "—"
    assert o[1:-1] == list(SP500_SLOVAK_SECTOR_INDEX_ROWS)
    assert o[-1] == legacy


def test_symbol_sector_edit_options_no_duplicate_for_table_row():
    row = SP500_SLOVAK_SECTOR_INDEX_ROWS[0]
    o = symbol_sector_edit_options(row)
    assert o == symbol_sector_table_options()


def test_barchart_guide_mentions_barchart_or_snapshot():
    md = barchart_insight_sector_guide_markdown()
    assert "Barchart" in md or "snímok" in md.lower() or "Symboly" in md
