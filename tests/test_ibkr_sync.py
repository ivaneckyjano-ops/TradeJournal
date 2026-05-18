from __future__ import annotations

from pathlib import Path


def _prepare_temp_db(monkeypatch, tmp_path: Path):
    import core.database as db

    db_path = tmp_path / "journal.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path), raising=False)
    db.init_db()
    return db


def test_sync_positions_closes_missing_trade_and_keeps_manual_fields(monkeypatch, tmp_path):
    import core.ibkr as ibkr

    db = _prepare_temp_db(monkeypatch, tmp_path)

    trade_id = db.add_trade(
        ticker="AAPL",
        strategy="Import IBKR",
        leg_type="Long",
        option_type="Call",
        strike=200.0,
        expiry="20260619",
        contracts=1,
        entry_price=4.0,
        entry_date="2026-04-29",
        group_id="G1",
    )
    db.set_trade_portfolio_greeks(
        trade_id,
        0.10,
        0.20,
        -0.30,
        0.40,
        vega_at_entry=1.10,
        vega_current=1.20,
        iv_current=0.31,
        theta_current=-0.22,
    )
    db.insert_trade_greek_snapshot(
        trade_id,
        delta=0.40,
        theta_usd=-0.22,
        vega=1.20,
        iv=0.31,
    )

    result = ibkr.sync_positions_to_db([], db, close_missing=True)

    assert result["closed"] == 1
    assert result["closed_trades"][0]["id"] == trade_id
    assert db.get_open_trades() == []

    closed = db.get_closed_trades()
    assert len(closed) == 1
    row = closed[0]
    assert row["id"] == trade_id
    assert row["status"] == "Closed"
    assert row["delta_at_entry"] == 0.20
    assert row["theta_at_entry"] == -0.30
    assert row["delta_current"] == 0.40
    assert row["theta_current"] == -0.22
    assert row["vega_at_entry"] == 1.10
    assert row["vega_current"] == 1.20
    assert row["iv_current"] == 0.31
    assert db.list_trade_greek_snapshots(trade_id)  # history survives


def test_sync_positions_updates_existing_open_trade(monkeypatch, tmp_path):
    import core.ibkr as ibkr

    db = _prepare_temp_db(monkeypatch, tmp_path)

    trade_id = db.add_trade(
        ticker="QQQ",
        strategy="Import IBKR",
        leg_type="Short",
        option_type="Put",
        strike=400.0,
        expiry="20260619",
        contracts=1,
        entry_price=6.0,
        entry_date="2026-04-29",
        group_id="G2",
    )
    db.set_trade_portfolio_greeks(
        trade_id,
        0.11,
        -0.21,
        0.31,
        -0.41,
        vega_at_entry=1.01,
        vega_current=1.02,
        iv_current=0.29,
        theta_current=0.33,
    )

    positions = [
        {
            "sec_type": "OPT",
            "ticker": "QQQ",
            "contracts": 2,
            "leg_type": "Short",
            "option_type": "Put",
            "strike": 400.0,
            "expiry": "20260619",
            "avg_cost": 720.0,
        }
    ]

    result = ibkr.sync_positions_to_db(positions, db, close_missing=True)

    assert result["added"] == 0
    assert result["updated"] == 1
    assert result["closed"] == 0

    open_trades = db.get_open_trades()
    assert len(open_trades) == 1
    row = open_trades[0]
    assert row["id"] == trade_id
    assert row["contracts"] == 2
    assert row["entry_price"] == 7.2
    assert row["delta_current"] == -0.41
    assert row["theta_current"] == 0.33
    assert row["vega_current"] == 1.02


def test_sync_positions_adds_stock_stk(monkeypatch, tmp_path):
    import core.ibkr as ibkr

    db = _prepare_temp_db(monkeypatch, tmp_path)

    positions = [
        {
            "sec_type": "STK",
            "ticker": "GLW",
            "contracts": 42,
            "leg_type": "Long",
            "option_type": "STK",
            "strike": 0.0,
            "expiry": "",
            "avg_cost": 55.25,
        }
    ]

    result = ibkr.sync_positions_to_db(positions, db, close_missing=True)

    assert result["added"] == 1
    assert result["closed"] == 0
    open_trades = db.get_open_trades()
    assert len(open_trades) == 1
    row = open_trades[0]
    assert row["ticker"] == "GLW"
    assert row["option_type"] == "STK"
    assert row["contracts"] == 42
    assert abs(float(row["entry_price"]) - 55.25) < 1e-6
    assert float(row.get("delta_at_entry") or 0) == 1.0
