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


def test_set_trade_portfolio_greeks_preserves_existing_when_none(monkeypatch, tmp_path):
    db = _prepare_temp_db(monkeypatch, tmp_path)

    trade_id = db.add_trade(
        ticker="GLW",
        strategy="Import IBKR",
        leg_type="Short",
        option_type="Put",
        strike=185.0,
        expiry="20260717",
        contracts=1,
        entry_price=2.5,
        entry_date="2026-05-01",
        group_id="G3",
    )
    db.set_trade_portfolio_greeks(
        trade_id,
        0.20,
        -0.33,
        0.44,
        -0.55,
        vega_at_entry=1.11,
        vega_current=1.22,
        iv_current=0.29,
        theta_current=0.66,
    )

    db.set_trade_portfolio_greeks(
        trade_id,
        None,
        None,
        None,
        None,
        vega_at_entry=None,
        vega_current=None,
        iv_current=None,
        theta_current=None,
    )

    row = db.get_trade_by_id(trade_id)
    assert row is not None
    assert row["iv_at_entry"] == 0.20
    assert row["delta_at_entry"] == -0.33
    assert row["theta_at_entry"] == 0.44
    assert row["delta_current"] == -0.55
    assert row["vega_at_entry"] == 1.11
    assert row["vega_current"] == 1.22
    assert row["iv_current"] == 0.29
    assert row["theta_current"] == 0.66


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


def test_insert_trade_greek_snapshot_skips_missing_trade(monkeypatch, tmp_path):
    db = _prepare_temp_db(monkeypatch, tmp_path)

    assert db.insert_trade_greek_snapshot(999_999, delta=0.5, source="tws_sync") == 0


def test_set_trade_portfolio_greeks_noop_when_trade_missing(monkeypatch, tmp_path):
    db = _prepare_temp_db(monkeypatch, tmp_path)

    db.set_trade_portfolio_greeks(
        999_999,
        0.20,
        -0.33,
        0.44,
        -0.55,
        vega_at_entry=1.11,
        vega_current=1.22,
        iv_current=0.29,
        theta_current=0.66,
    )

    assert db.get_trade_by_id(999_999) is None


def test_migrate_trade_greek_snapshots_fk_repairs_trades_mig_old(monkeypatch, tmp_path):
    import sqlite3

    db = _prepare_temp_db(monkeypatch, tmp_path)

    trade_id = db.add_trade(
        ticker="AAPL",
        strategy="Test",
        leg_type="Long",
        option_type="Call",
        strike=200.0,
        expiry="20260619",
        contracts=1,
        entry_price=4.0,
        entry_date="2026-04-29",
    )
    conn = sqlite3.connect(str(tmp_path / "journal.db"))
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute('ALTER TABLE trade_greek_snapshots RENAME TO trade_greek_snapshots_broken')
    conn.execute("""
        CREATE TABLE trade_greek_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL REFERENCES "trades_mig_old"(id) ON DELETE CASCADE,
            recorded_at TEXT NOT NULL,
            delta REAL,
            theta_usd REAL,
            vega REAL,
            iv REAL,
            source TEXT DEFAULT 'journal'
        )
    """)
    conn.execute(
        "INSERT INTO trade_greek_snapshots (trade_id, recorded_at, delta, source) VALUES (?,?,?,?)",
        (trade_id, "2026-05-01T00:00:00Z", 0.5, "journal"),
    )
    conn.commit()
    conn.close()

    db.init_db()

    snap_id = db.insert_trade_greek_snapshot(trade_id, delta=0.6, source="tws_sync")
    assert snap_id > 0
    rows = db.list_trade_greek_snapshots(trade_id)
    assert len(rows) >= 2

    verify = sqlite3.connect(str(tmp_path / "journal.db"))
    parent = verify.execute('PRAGMA foreign_key_list("trade_greek_snapshots")').fetchone()[2]
    verify.close()
    assert parent == "trades"
