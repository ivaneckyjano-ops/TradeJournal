import sqlite3
import os
from datetime import datetime
from typing import Optional

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "journal.db")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id     TEXT,
                ticker       TEXT NOT NULL,
                strategy     TEXT,
                leg_type     TEXT CHECK(leg_type IN ('Long','Short')),
                option_type  TEXT CHECK(option_type IN ('Call','Put')),
                strike       REAL,
                expiry       TEXT,
                contracts    INTEGER DEFAULT 1,
                entry_price  REAL,
                exit_price   REAL,
                entry_date   TEXT,
                exit_date    TEXT,
                status       TEXT DEFAULT 'Open' CHECK(status IN ('Open','Closed')),
                iv_at_entry  REAL,
                pop_at_entry REAL,
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL UNIQUE,
                description TEXT,
                ticker      TEXT,
                strategy    TEXT,
                created_at  TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS notes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id   INTEGER REFERENCES trades(id) ON DELETE SET NULL,
                group_id   TEXT,
                title      TEXT NOT NULL,
                content    TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS symbols (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker          TEXT NOT NULL UNIQUE,
                company_name    TEXT,
                sector          TEXT,
                asset_type      TEXT DEFAULT 'Stock',
                description     TEXT,
                earnings_date   TEXT,
                earnings_date_2 TEXT,
                earnings_date_3 TEXT,
                earnings_date_4 TEXT,
                ir_url          TEXT,
                iv_rank         REAL,
                created_at      TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                date        TEXT NOT NULL,
                type        TEXT NOT NULL CHECK(type IN ('earnings','expiry','alert','reminder','event','note')),
                ticker      TEXT,
                title       TEXT NOT NULL,
                description TEXT,
                group_id    TEXT,
                trade_id    INTEGER REFERENCES trades(id) ON DELETE SET NULL,
                created_at  TEXT DEFAULT (datetime('now'))
            );
        """)
    # Migrácia: pridaj nové stĺpce do existujúcich DB
    _migrate_symbols(get_connection())
    _migrate_trades(get_connection())


def _migrate_symbols(conn: sqlite3.Connection) -> None:
    """Bezpečne pridá nové stĺpce do symbols ak ešte neexistujú."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(symbols)").fetchall()}
    migrations = {
        "earnings_date_2": "ALTER TABLE symbols ADD COLUMN earnings_date_2 TEXT",
        "earnings_date_3": "ALTER TABLE symbols ADD COLUMN earnings_date_3 TEXT",
        "earnings_date_4": "ALTER TABLE symbols ADD COLUMN earnings_date_4 TEXT",
        "ir_url":          "ALTER TABLE symbols ADD COLUMN ir_url TEXT",
    }
    for col, sql in migrations.items():
        if col not in existing:
            conn.execute(sql)
    conn.commit()


def _migrate_trades(conn: sqlite3.Connection) -> None:
    """Bezpečne pridá nové stĺpce do trades ak ešte neexistujú."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    migrations = {
        "commission": "ALTER TABLE trades ADD COLUMN commission REAL DEFAULT 0.0",
    }
    for col, sql in migrations.items():
        if col not in existing:
            conn.execute(sql)
    conn.commit()
    conn.close()


# ─── GROUPS ────────────────────────────────────────────────────────────────────

def add_group(name: str, description: str = "", ticker: str = "", strategy: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO groups (name, description, ticker, strategy) VALUES (?,?,?,?)",
            (name.strip(), description, ticker, strategy),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM groups WHERE name=?", (name.strip(),)).fetchone()
        return row["id"] if row else -1


def get_groups() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM groups ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def get_group_names() -> list[str]:
    return [g["name"] for g in get_groups()]


def update_group(group_id: int, name: str, description: str, ticker: str, strategy: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE groups SET name=?, description=?, ticker=?, strategy=? WHERE id=?",
            (name.strip(), description, ticker, strategy, group_id),
        )


def delete_group(group_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM groups WHERE id=?", (group_id,))


# ─── SYMBOLS ───────────────────────────────────────────────────────────────────

def add_symbol(ticker: str, company_name: str = "", sector: str = "",
               asset_type: str = "Stock", description: str = "",
               earnings_date: str = None, iv_rank: float = None,
               earnings_date_2: str = None, earnings_date_3: str = None,
               earnings_date_4: str = None, ir_url: str = None) -> int:
    ticker = ticker.strip().upper()
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO symbols "
            "(ticker, company_name, sector, asset_type, description, "
            "earnings_date, earnings_date_2, earnings_date_3, earnings_date_4, "
            "ir_url, iv_rank) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (ticker, company_name, sector, asset_type, description,
             earnings_date, earnings_date_2, earnings_date_3, earnings_date_4,
             ir_url, iv_rank),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM symbols WHERE ticker=?", (ticker,)).fetchone()
        return row["id"] if row else -1


def get_symbols() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM symbols ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]


def get_symbol_tickers() -> list[str]:
    return [s["ticker"] for s in get_symbols()]


def get_symbol(ticker: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM symbols WHERE ticker=?", (ticker.upper(),)).fetchone()
    return dict(row) if row else None


def update_symbol(symbol_id: int, ticker: str, company_name: str, sector: str,
                  asset_type: str, description: str,
                  earnings_date: str = None, iv_rank: float = None,
                  earnings_date_2: str = None, earnings_date_3: str = None,
                  earnings_date_4: str = None, ir_url: str = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE symbols SET ticker=?, company_name=?, sector=?, asset_type=?, "
            "description=?, earnings_date=?, earnings_date_2=?, earnings_date_3=?, "
            "earnings_date_4=?, ir_url=?, iv_rank=? WHERE id=?",
            (ticker.strip().upper(), company_name, sector, asset_type,
             description, earnings_date, earnings_date_2, earnings_date_3,
             earnings_date_4, ir_url, iv_rank, symbol_id),
        )


def delete_symbol(symbol_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM symbols WHERE id=?", (symbol_id,))


# ─── TRADES ────────────────────────────────────────────────────────────────────

def add_trade(
    ticker: str,
    strategy: str,
    leg_type: str,
    option_type: str,
    strike: float,
    expiry: str,
    contracts: int,
    entry_price: float,
    entry_date: str,
    group_id: Optional[str] = None,
    iv_at_entry: Optional[float] = None,
    pop_at_entry: Optional[float] = None,
    commission: Optional[float] = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (ticker, strategy, leg_type, option_type, strike, expiry,
                contracts, entry_price, entry_date, group_id, iv_at_entry, pop_at_entry,
                commission)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, strategy, leg_type, option_type, strike, expiry,
             contracts, entry_price, entry_date, group_id, iv_at_entry, pop_at_entry,
             commission or 0.0),
        )
        return cur.lastrowid


def close_trade(trade_id: int, exit_price: float, exit_date: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE trades SET exit_price=?, exit_date=?, status='Closed' WHERE id=?",
            (exit_price, exit_date, trade_id),
        )


def get_open_trades() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='Open' ORDER BY entry_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_closed_trades() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='Closed' ORDER BY exit_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_trades() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY entry_date DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def update_trade(
    trade_id: int,
    ticker: Optional[str] = None,
    strategy: Optional[str] = None,
    leg_type: Optional[str] = None,
    option_type: Optional[str] = None,
    strike: Optional[float] = None,
    expiry: Optional[str] = None,
    contracts: Optional[int] = None,
    entry_price: Optional[float] = None,
    exit_price: Optional[float] = None,
    exit_date: Optional[str] = None,
    status: Optional[str] = None,
    group_id: Optional[str] = None,
    commission: Optional[float] = None,
) -> None:
    """Aktualizuje akékoľvek pole obchodu."""
    with get_connection() as conn:
        fields = []
        values = []
        mapping = {
            "ticker": ticker,
            "strategy": strategy,
            "leg_type": leg_type,
            "option_type": option_type,
            "strike": strike,
            "expiry": expiry,
            "contracts": contracts,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "exit_date": exit_date,
            "status": status,
            "group_id": group_id,
            "commission": commission,
        }
        for k, v in mapping.items():
            if v is not None:
                fields.append(f"{k}=?")
                # Špeciálne ošetrenie pre group_id ak je prázdny string
                if k == "group_id" and v == "":
                    values.append(None)
                else:
                    values.append(v)
        
        if not fields:
            return
        values.append(trade_id)
        conn.execute(f"UPDATE trades SET {', '.join(fields)} WHERE id=?", values)


def bulk_set_group_id(trade_ids: list[int], group_id: str) -> None:
    """Nastaví rovnaké group_id pre viacero obchodov naraz."""
    with get_connection() as conn:
        conn.executemany(
            "UPDATE trades SET group_id=? WHERE id=?",
            [(group_id if group_id else None, tid) for tid in trade_ids],
        )


def split_trade(trade_id: int, group_ids: list[str]) -> list[int]:
    """
    Rozdelí trade s N kontraktmi na N samostatných 1-kontraktových nôh.
    group_ids = zoznam Group ID pre každú novú nohu (môžu byť rôzne).
    Pôvodný záznam sa vymaže.
    Vráti zoznam nových ID.
    """
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    if not row:
        return []
    t = dict(row)
    new_ids = []
    for gid in group_ids:
        with get_connection() as conn:
            cur = conn.execute(
                """INSERT INTO trades
                   (ticker, strategy, leg_type, option_type, strike, expiry,
                    contracts, entry_price, entry_date, group_id, iv_at_entry,
                    pop_at_entry, exit_price, exit_date, status)
                   VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)""",
                (
                    t["ticker"], t["strategy"], t["leg_type"], t["option_type"],
                    t["strike"], t["expiry"], t["entry_price"], t["entry_date"],
                    gid if gid else None, t["iv_at_entry"], t["pop_at_entry"],
                    t["exit_price"], t["exit_date"], t["status"],
                ),
            )
            new_ids.append(cur.lastrowid)
    delete_trade(trade_id)
    return new_ids


def delete_trade(trade_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM trades WHERE id=?", (trade_id,))


# ─── NOTES ─────────────────────────────────────────────────────────────────────

def add_note(
    title: str,
    content: str,
    trade_id: Optional[int] = None,
    group_id: Optional[str] = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO notes (title, content, trade_id, group_id) VALUES (?,?,?,?)",
            (title, content, trade_id, group_id),
        )
        return cur.lastrowid


def update_note(note_id: int, title: str, content: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE notes SET title=?, content=?, updated_at=datetime('now') WHERE id=?",
            (title, content, note_id),
        )


def delete_note(note_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM notes WHERE id=?", (note_id,))


def get_notes(trade_id: Optional[int] = None, group_id: Optional[str] = None) -> list[dict]:
    with get_connection() as conn:
        if trade_id is not None:
            rows = conn.execute(
                "SELECT * FROM notes WHERE trade_id=? ORDER BY created_at DESC",
                (trade_id,),
            ).fetchall()
        elif group_id:
            rows = conn.execute(
                "SELECT * FROM notes WHERE group_id=? ORDER BY created_at DESC",
                (group_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notes ORDER BY created_at DESC"
            ).fetchall()
    return [dict(r) for r in rows]


def get_note_by_id(note_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    return dict(row) if row else None


# ─── EVENTS ────────────────────────────────────────────────────────────────────

def get_events(year: int, month: int) -> list[dict]:
    """Vráti všetky udalosti pre daný mesiac + expirujúce obchody."""
    from calendar import monthrange
    first = f"{year:04d}-{month:02d}-01"
    _, last_day = monthrange(year, month)
    last = f"{year:04d}-{month:02d}-{last_day:02d}"
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE date BETWEEN ? AND ? ORDER BY date",
            (first, last),
        ).fetchall()
        # Automaticky pridaj expirácie z trades
        trade_rows = conn.execute(
            "SELECT id, ticker, expiry, strategy, group_id FROM trades "
            "WHERE expiry BETWEEN ? AND ? AND status='Open'",
            (first, last),
        ).fetchall()
    result = [dict(r) for r in rows]
    for t in trade_rows:
        result.append({
            "id": f"trade_{t['id']}",
            "date": t["expiry"],
            "type": "expiry",
            "ticker": t["ticker"],
            "title": f"Expirácia: {t['ticker']} {t['strategy'] or ''}".strip(),
            "description": f"Trade ID: {t['id']}",
            "group_id": t["group_id"],
            "trade_id": t["id"],
            "auto": True,
        })
    return result


def get_all_events() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM events ORDER BY date DESC").fetchall()
    return [dict(r) for r in rows]


def get_event_by_id(event_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    return dict(row) if row else None


def add_event(date: str, event_type: str, title: str, ticker: str = None,
              description: str = None, group_id: str = None, trade_id: int = None) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO events (date, type, ticker, title, description, group_id, trade_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, event_type, ticker, title, description, group_id, trade_id),
        )
        return cur.lastrowid


def update_event(event_id: int, date: str, event_type: str, title: str,
                 ticker: str = None, description: str = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE events SET date=?, type=?, title=?, ticker=?, description=? WHERE id=?",
            (date, event_type, title, ticker, description, event_id),
        )


def delete_event(event_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM events WHERE id=?", (event_id,))


# ─── UTILITY ───────────────────────────────────────────────────────────────────

def compute_pnl(trade: dict) -> Optional[float]:
    """Čistý P&L v USD pre jednu nohu (po odpočítaní komisie)."""
    ep = trade.get("entry_price")
    xp = trade.get("exit_price")
    if ep is None:
        return None
    contracts = trade.get("contracts", 1) or 1
    commission = trade.get("commission") or 0.0
    multiplier = 100
    if xp is not None:
        raw = (xp - ep) * contracts * multiplier
        gross = raw if trade.get("leg_type") == "Long" else -raw
        return gross - commission
    return None
