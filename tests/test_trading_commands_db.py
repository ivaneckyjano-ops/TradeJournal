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
