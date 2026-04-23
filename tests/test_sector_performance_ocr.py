from __future__ import annotations

import numpy as np
import pandas as pd

from core.sector_performance_ocr import (
    _resize_gray_for_table_ocr,
    dataframe_to_payload_rows,
    normalize_sector_dataframe,
    parse_sector_performance_text,
    payload_rows_to_dataframe,
)


def test_resize_wide_short_image_keeps_readable_height():
    """Široký nízky snímok sa má zväčšiť na dostatočnú výšku (nie zmenšiť podľa max. strany)."""
    gray = np.zeros((500, 4000), dtype=np.uint8)
    out = _resize_gray_for_table_ocr(gray)
    assert out.shape[0] >= 850
    assert out.shape[1] >= 4000


def test_parse_sector_performance_text_tail_five_numbers():
    raw = """
    Sector  Weight  1d 5d 1m 3m 1y
    Electronic Technology 12.3 +0.5 -1.2 2.3 4.1 10.0
    Finance 8.1 -0.1 0.0 1.0 2.0 5.5
    """
    df = parse_sector_performance_text(raw)
    assert len(df) == 2
    r0 = df[df["sector"].str.contains("Electronic")].iloc[0]
    assert abs(float(r0["pct_1d"]) - 0.5) < 1e-6
    assert abs(float(r0["pct_1y"]) - 10.0) < 1e-6


def test_parse_four_columns():
    raw = "Utilities -0.2 0.1 1.0 2.0\n"
    df = parse_sector_performance_text(raw)
    assert len(df) == 1
    assert pd.isna(df.iloc[0]["pct_1y"])


def test_payload_roundtrip():
    df = pd.DataFrame(
        [
            {"sector": "A", "pct_1d": 1.0, "pct_5d": None, "pct_1m": 2.0, "pct_3m": 3.0, "pct_1y": 4.0},
        ]
    )
    df = normalize_sector_dataframe(df)
    p = dataframe_to_payload_rows(df)
    df2 = payload_rows_to_dataframe(p)
    assert len(df2) == 1
    assert df2.iloc[0]["sector"] == "A"
