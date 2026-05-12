"""Migrácia a CRUD trading_commands (ručné TWS polia)."""

from __future__ import annotations

from pathlib import Path


def test_trading_commands_manual_tws_fields(monkeypatch, tmp_path: Path) -> None:
    import core.database as db

    db_path = tmp_path / "journal.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path), raising=False)
    db.init_db()

    i1 = db.insert_trading_command(
        "Krok A",
        ticker="QQQ",
        status="draft",
        plan_group="ROLL-X",
        step_index=1,
        tws_perm_id=" 999 ",
    )
    i2 = db.insert_trading_command(
        "Krok B",
        ticker="QQQ",
        status="ready",
        plan_group="ROLL-X",
        step_index=2,
    )
    assert i1 > 0 and i2 > 0

    rows = db.list_trading_commands(limit=50, sort_by="plan")
    roll = [r for r in rows if (r.get("plan_group") or "").strip() == "ROLL-X"]
    assert len(roll) == 2
    assert [r["step_index"] for r in roll] == [1, 2]

    db.update_trading_command(
        i1,
        title="Krok A",
        ticker="QQQ",
        action=None,
        order_kind=None,
        quantity=None,
        limit_price=None,
        stop_price=None,
        body=None,
        status="submitted",
        plan_group="ROLL-X",
        step_index=1,
        tws_perm_id="999",
        tws_order_id="12",
        tws_manual_note="Submitted",
        cond_under_cmp="gt",
        cond_under_price=400.0,
        cond_after_fill="option",
        cond_detail=None,
        _all_fields=True,
    )
    r = db.get_trading_command(i1)
    assert r is not None
    assert r["tws_order_id"] == "12"
    assert r["tws_manual_note"] == "Submitted"
    assert r["status"] == "submitted"
    assert r["cond_under_cmp"] == "gt"
    assert r["cond_under_price"] == 400.0
    assert r["cond_after_fill"] == "option"
    assert (r.get("trigger_kind") or "manual") == "manual"

    i3 = db.insert_trading_command(
        "Zatvoriť akciu po assign",
        ticker="SPY",
        action="sell",
        order_kind="market",
        quantity=100.0,
        status="ready",
        trigger_kind="short_leg_assignment",
        close_sec_type="STK",
    )
    r3 = db.get_trading_command(i3)
    assert r3 is not None
    assert r3["trigger_kind"] == "short_leg_assignment"
    assert r3["close_sec_type"] == "STK"

    i4 = db.insert_trading_command(
        "Zatvoriť opciu",
        ticker="QQQ",
        action="sell",
        order_kind="limit",
        quantity=1.0,
        limit_price=2.5,
        status="ready",
        close_sec_type="OPT",
        close_expiry="20260320",
        close_strike=400.0,
        close_right="C",
    )
    r4 = db.get_trading_command(i4)
    assert r4 is not None
    assert r4["close_sec_type"] == "OPT"
    assert r4["close_expiry"] == "20260320"
    assert r4["close_strike"] == 400.0
    assert r4["close_right"] == "C"

    tid_short = db.add_trade(
        "QQQ",
        "TEST",
        "Short",
        "Call",
        400.0,
        "2026-06-19",
        1,
        1.0,
        "2026-05-01",
    )
    tid_long = db.add_trade(
        "QQQ",
        "TEST",
        "Long",
        "Call",
        410.0,
        "2026-06-19",
        1,
        2.0,
        "2026-05-01",
    )
    i5 = db.insert_trading_command(
        "Po assign — long close",
        ticker="QQQ",
        status="draft",
        trigger_kind="short_leg_assignment",
        assignment_watch_trade_id=tid_short,
        linked_trade_id=tid_long,
    )
    r5 = db.get_trading_command(i5)
    assert r5 is not None
    assert r5["assignment_watch_trade_id"] == tid_short

    n = db.record_trading_command_assignment_check(i5, "BLOK: short v účte.")
    assert n == 1
    r5b = db.get_trading_command(i5)
    assert r5b is not None
    assert r5b.get("assignment_check_summary") == "BLOK: short v účte."
    assert r5b.get("assignment_check_at")

    try:
        db.insert_trading_command(
            "Zlý watch",
            ticker="QQQ",
            assignment_watch_trade_id=tid_long,
        )
    except ValueError as e:
        assert "Short" in str(e)
    else:
        raise AssertionError("očakávaná ValueError pre Long nohu ako watch")
