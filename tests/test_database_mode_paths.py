from __future__ import annotations


def test_get_active_db_path_switches_by_mode(monkeypatch):
    import core.database as db

    monkeypatch.setattr(db, "DB_PATH", db.LIVE_DB_PATH, raising=False)

    monkeypatch.setattr(db, "_session_ib_mode", lambda: "LIVE", raising=False)
    assert db.get_active_db_path() == db.LIVE_DB_PATH

    monkeypatch.setattr(db, "_session_ib_mode", lambda: "PAPER", raising=False)
    assert db.get_active_db_path() == db.PAPER_DB_PATH


def test_get_active_db_path_respects_explicit_override(monkeypatch, tmp_path):
    import core.database as db

    custom_db = tmp_path / "custom.db"
    monkeypatch.setattr(db, "DB_PATH", str(custom_db), raising=False)
    monkeypatch.setattr(db, "_session_ib_mode", lambda: "PAPER", raising=False)

    assert db.get_active_db_path() == str(custom_db)
