"""Testy pre scripts/calendar_spread_top3.py — parsovanie a hodnotenie."""
import io
import sys
from pathlib import Path

import pandas as pd
import pytest

# Import modulu ako skript (nie je v balíku core)
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import calendar_spread_top3 as m  # noqa: E402


def test_parse_number_european_and_percent():
    assert m._parse_number("245,04") == pytest.approx(245.04)
    assert m._parse_number("11,5") == pytest.approx(11.5)
    assert m._parse_number("57,93%") == pytest.approx(57.93)
    assert m._parse_number("0,074422") == pytest.approx(0.074422)
    assert m._parse_number("1.234,56") == pytest.approx(1234.56)


def test_ensure_net_debit_from_ask_bid():
    csv = """Exp Leg2;Ask2;Bid1
2026-07-17;22,35;11,5
2026-06-18;18;11,5
"""
    df = pd.read_csv(io.StringIO(csv), sep=";", dtype=str)
    df.columns = [m._norm_header(c) for c in df.columns]
    d = m._ensure_net_debit(df)
    assert list(d.round(2)) == [pytest.approx(10.85), pytest.approx(6.5)]


def test_top3_balanced_order():
    csv = """Net Debit;IV Skew;Net Theta;Net Delta
10;4;0.1;0.05
6;2;0.05;0.02
8;5;0.08;0.5
"""
    df = pd.read_csv(io.StringIO(csv), sep=";", dtype=str)
    df.columns = [m._norm_header(c) for c in df.columns]
    ranked = m.add_scores(df, "balanced").sort_values("_score", ascending=False, kind="mergesort")
    # Riadok 8;5;0.08 má vysoký skew a rozumný debit — mal by byť pred 10;4;0.1
    assert ranked.index.tolist()[0] == 2  # tretí dátový riadok (index 2) — najvyšší skew
