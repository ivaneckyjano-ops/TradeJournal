"""
Lokálna SQLite DB pre uložené riadky z Hľadania delty — diagonály.
Súbor: data/saved_diagonals.db (mimo journal.db).
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "saved_diagonals.db"
)


def db_path() -> str:
    return _DB_PATH


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(_DB_PATH), exist_ok=True)
    c = sqlite3.connect(_DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS saved_diagonal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            ticker TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            strategy_id TEXT NOT NULL,
            row_json TEXT NOT NULL
        );
        """
    )
    conn.commit()


def save_rows(
    ticker: str,
    as_of_date: str,
    strategy_id: str,
    df: pd.DataFrame,
) -> int:
    """
    Uloží každý riadok z ``df`` (bez stĺpca „Uložiť“). Vráti počet vložených záznamov.
    """
    if df is None or df.empty:
        return 0
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = 0
    conn = _conn()
    try:
        init_schema(conn)
        for _, row in df.iterrows():
            d = {k: _json_safe(v) for k, v in row.items() if k != "Uložiť"}
            conn.execute(
                """
                INSERT INTO saved_diagonal (created_at, ticker, as_of_date, strategy_id, row_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (now, ticker.strip().upper(), as_of_date, strategy_id, json.dumps(d, ensure_ascii=False)),
            )
            n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def _json_safe(v: Any) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if hasattr(v, "item"):
        try:
            return v.item()
        except Exception:
            pass
    if isinstance(v, (pd.Timestamp, datetime)):
        return str(v)[:10]
    return v


def list_saved(ticker: Optional[str] = None) -> pd.DataFrame:
    """Všetky uložené riadky, zoradené od najnovších. Voliteľný filter ticker."""
    if not os.path.isfile(_DB_PATH):
        return pd.DataFrame()
    conn = _conn()
    try:
        init_schema(conn)
        q = "SELECT id, created_at, ticker, as_of_date, strategy_id, row_json FROM saved_diagonal WHERE 1=1"
        params: list[str] = []
        if ticker:
            q += " AND ticker = ?"
            params.append(ticker.strip().upper())
        q += " ORDER BY id DESC"
        rows = conn.execute(q, params).fetchall()
    finally:
        conn.close()
    if not rows:
        return pd.DataFrame()
    out_rows: list[dict[str, Any]] = []
    for r in rows:
        d = json.loads(r["row_json"])
        d["ID"] = int(r["id"])
        d["Uložené"] = r["created_at"]
        d["Ticker"] = r["ticker"]
        d["Snímka uloženia"] = r["as_of_date"]
        d["Stratégia ID"] = r["strategy_id"]
        out_rows.append(d)
    return pd.DataFrame(out_rows)


def delete_by_ids(ids: list[int]) -> int:
    if not ids:
        return 0
    conn = _conn()
    try:
        init_schema(conn)
        ph = ",".join("?" * len(ids))
        cur = conn.execute(f"DELETE FROM saved_diagonal WHERE id IN ({ph})", [int(i) for i in ids])
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()
