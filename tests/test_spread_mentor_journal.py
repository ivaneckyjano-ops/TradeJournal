"""spread_mentor — journal / option_type a čisté Gréky."""
from datetime import date

from core.spread_mentor import (
    analyze_calendar_mentor,
    compute_journal_group_greek_snapshot,
    journal_greek_comparison_rows,
)


def test_calendar_mentor_reads_option_type_call():
    td = date(2025, 1, 1)
    legs = [
        {
            "leg_type": "Short",
            "expiry": "20250205",
            "strike": 100.0,
            "option_type": "Call",
        },
        {
            "leg_type": "Long",
            "expiry": "20250405",
            "strike": 100.0,
            "option_type": "Call",
        },
    ]
    res = analyze_calendar_mentor(legs, today=td)
    assert res is not None
    assert res.strike == 100.0
    assert res.right == "C"


def test_journal_greek_snapshot_delta():
    legs = [
        {
            "leg_type": "Long",
            "contracts": 1,
            "delta_current": 0.5,
            "theta_current": 10.0,
            "vega_current": 20.0,
        },
        {
            "leg_type": "Short",
            "contracts": 1,
            "delta_current": 0.2,
            "theta_current": -5.0,
            "vega_current": -3.0,
        },
    ]
    snap = compute_journal_group_greek_snapshot(legs)
    assert snap.net_delta_shares == 50.0 - 20.0
    assert snap.net_theta_usd == 5.0
    assert snap.net_vega == 17.0
    rows = journal_greek_comparison_rows("diagonal", snap)
    assert len(rows) == 3
    assert any(r["Parameter"].startswith("Čistá Δ") for r in rows)
