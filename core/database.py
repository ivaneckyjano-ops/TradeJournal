import json
import os
import sqlite3
from datetime import date, datetime, timedelta
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
    _migrate_group_apr_snapshots(get_connection())
    _migrate_spread_builder(get_connection())
    _migrate_portfolio_greek_history(get_connection())
    with get_connection() as conn:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events (date)")
        conn.commit()
    maybe_purge_old_calendar_events()


def _migrate_symbols(conn: sqlite3.Connection) -> None:
    """Bezpečne pridá nové stĺpce do symbols ak ešte neexistujú."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(symbols)").fetchall()}
    migrations = {
        "earnings_date_2": "ALTER TABLE symbols ADD COLUMN earnings_date_2 TEXT",
        "earnings_date_3": "ALTER TABLE symbols ADD COLUMN earnings_date_3 TEXT",
        "earnings_date_4": "ALTER TABLE symbols ADD COLUMN earnings_date_4 TEXT",
        "ir_url":          "ALTER TABLE symbols ADD COLUMN ir_url TEXT",
        "spot":            "ALTER TABLE symbols ADD COLUMN spot REAL DEFAULT 0",
        "iv_pct":          "ALTER TABLE symbols ADD COLUMN iv_pct REAL DEFAULT 30",
    }
    for col, sql in migrations.items():
        if col not in existing:
            conn.execute(sql)

    # Settings tabuľka pre jednoduchý key-value store (margin, atď.)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)
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


def _migrate_group_apr_snapshots(conn: sqlite3.Connection) -> None:
    """História APR skupín z Portfolio Dashboard (po každom Načítať z TWS)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS group_apr_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id      TEXT NOT NULL,
            captured_at   TEXT NOT NULL,
            basis_kind    TEXT NOT NULL,
            apr_pct       REAL NOT NULL,
            pnl_total     REAL NOT NULL,
            basis_value   REAL NOT NULL,
            days          INTEGER NOT NULL,
            unreal_ib     REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_gas_group_time ON group_apr_snapshots (group_id, captured_at)"
    )
    conn.commit()
    conn.close()


MAX_GROUP_APR_SNAPSHOTS = 400

# Virtuálny group_id pre históriu Portfólio APTR (Θ) na dashboarde — nekoliduje s Trade Log group_id.
PORTFOLIO_APTR_SNAPSHOT_GROUP_ID = "__portfolio_aptr__"


def append_group_apr_snapshot(
    group_id: str,
    captured_at: str,
    basis_kind: str,
    apr_pct: float,
    pnl_total: float,
    basis_value: float,
    days: int,
    unreal_ib: float,
) -> None:
    """Uloží jeden bod histórie APR; staršie záznamy nad limit zmaže."""
    gid = (group_id or "").strip()
    if not gid:
        return
    bk = basis_kind if basis_kind in ("maint", "premium", "theta") else "maint"
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO group_apr_snapshots "
            "(group_id, captured_at, basis_kind, apr_pct, pnl_total, basis_value, days, unreal_ib) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (gid, captured_at, bk, apr_pct, pnl_total, basis_value, int(days), unreal_ib),
        )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM group_apr_snapshots WHERE group_id=?",
            (gid,),
        ).fetchone()["c"]
        if n > MAX_GROUP_APR_SNAPSHOTS:
            excess = n - MAX_GROUP_APR_SNAPSHOTS
            conn.execute(
                "DELETE FROM group_apr_snapshots WHERE id IN "
                "(SELECT id FROM group_apr_snapshots WHERE group_id=? ORDER BY captured_at ASC LIMIT ?)",
                (gid, excess),
            )
        conn.commit()


def get_group_apr_snapshots(
    group_id: str,
    limit: int = 120,
    basis_kind: Optional[str] = None,
) -> list[dict]:
    """Posledných ``limit`` snímok skupiny, od najstaršej (vhodné pre graf). Ak je ``basis_kind``, filtruje (napr. ``theta``)."""
    gid = (group_id or "").strip()
    if not gid:
        return []
    lim = max(1, min(int(limit), 2000))
    with get_connection() as conn:
        if basis_kind:
            rows = conn.execute(
                "SELECT * FROM group_apr_snapshots WHERE group_id=? AND basis_kind=? "
                "ORDER BY captured_at DESC LIMIT ?",
                (gid, basis_kind, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM group_apr_snapshots WHERE group_id=? ORDER BY captured_at DESC LIMIT ?",
                (gid, lim),
            ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()
    return out


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
                  earnings_date_4: str = None, ir_url: str = None,
                  spot: float = None, iv_pct: float = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE symbols SET ticker=?, company_name=?, sector=?, asset_type=?, "
            "description=?, earnings_date=?, earnings_date_2=?, earnings_date_3=?, "
            "earnings_date_4=?, ir_url=?, iv_rank=?, spot=?, iv_pct=? WHERE id=?",
            (ticker.strip().upper(), company_name, sector, asset_type,
             description, earnings_date, earnings_date_2, earnings_date_3,
             earnings_date_4, ir_url, iv_rank, spot, iv_pct, symbol_id),
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

# Manuálne udalosti v kalendári (tabuľka events). Obchody / poznámky / Spread Builder sa nemažú.
CALENDAR_EVENTS_RETENTION_DAYS = 365
CALENDAR_EVENTS_LAST_PURGE_DAY_KEY = "calendar_events_last_purge_day"
# "1" = zapnuté (predvolené). "0" = vypnuté — cez db.set_setting v konzole alebo budúci prepínač v UI.
CALENDAR_AUTO_PURGE_ENABLED_KEY = "calendar_auto_purge_enabled"


def purge_old_calendar_events(retention_days: int = CALENDAR_EVENTS_RETENTION_DAYS) -> int:
    """
    Vymaže riadky v ``events``, kde ``date`` (YYYY-MM-DD) je staršia ako ``retention_days``.
    Expirácie z otvorených obchodov v kalendári sú virtuálne (z ``trades``), tie sa týmto nedotknú.
    """
    rd = max(30, int(retention_days))
    cutoff = (date.today() - timedelta(days=rd)).isoformat()
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM events WHERE date < ?", (cutoff,))
        conn.commit()
        return int(cur.rowcount or 0)


def maybe_purge_old_calendar_events() -> None:
    """Najviac raz za kalendárny deň spustí purge, ak je zapnuté v nastaveniach."""
    raw = get_setting(CALENDAR_AUTO_PURGE_ENABLED_KEY, "1").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return
    today_s = date.today().isoformat()
    if get_setting(CALENDAR_EVENTS_LAST_PURGE_DAY_KEY, "") == today_s:
        return
    purge_old_calendar_events(CALENDAR_EVENTS_RETENTION_DAYS)
    set_setting(CALENDAR_EVENTS_LAST_PURGE_DAY_KEY, today_s)


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


# ─── Settings (key-value store) ───────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    """Načíta hodnotu nastavenia podľa kľúča."""
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """Uloží alebo aktualizuje nastavenie."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ─── Udržiavacia marža po skupine (spread) — ručne pre TWS Dashboard / APR ───

GROUP_MAINT_MARGIN_KEY = "group_maintenance_margin"


def get_group_maint_margins() -> dict[str, float]:
    """Slovník ``group_id`` → udržiavacia marža (USD), len kladné hodnoty (kľúče ``strip``)."""
    import json
    raw = get_setting(GROUP_MAINT_MARGIN_KEY, "{}")
    try:
        d = json.loads(raw)
        return {
            str(k).strip(): float(v)
            for k, v in d.items()
            if float(v) > 0
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


MAX_SPREAD_BUILDER_SNAPSHOTS = 400


def _migrate_spread_builder(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS spread_builder_ideas (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            ticker        TEXT,
            spot          REAL NOT NULL,
            global_iv     REAL NOT NULL,
            maint_margin  REAL NOT NULL DEFAULT 0,
            legs_json     TEXT NOT NULL,
            notes         TEXT,
            created_at    TEXT DEFAULT (datetime('now')),
            updated_at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS spread_builder_snapshots (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            idea_id       INTEGER NOT NULL,
            captured_at   TEXT NOT NULL,
            aptr_pct      REAL NOT NULL,
            theta_per_day REAL NOT NULL,
            capital_basis REAL NOT NULL,
            spot          REAL,
            global_iv     REAL,
            FOREIGN KEY (idea_id) REFERENCES spread_builder_ideas(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_sb_snap_idea_time
        ON spread_builder_snapshots (idea_id, captured_at);
    """)
    _sb_cols = {row[1] for row in conn.execute("PRAGMA table_info(spread_builder_ideas)").fetchall()}
    if "variant_of_id" not in _sb_cols:
        conn.execute(
            "ALTER TABLE spread_builder_ideas ADD COLUMN variant_of_id INTEGER "
            "REFERENCES spread_builder_ideas(id) ON DELETE SET NULL"
        )
    conn.commit()
    conn.close()


def _migrate_portfolio_greek_history(conn: sqlite3.Connection) -> None:
    """História APR z Thety + Greky z Portfolio (jeden záznam na deň a scope)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS portfolio_greek_history (
            ticker_scope   TEXT NOT NULL DEFAULT '',
            snapshot_date  TEXT NOT NULL,
            apr_theta_pct  REAL,
            theta_usd      REAL,
            delta_usd      REAL,
            vega_usd       REAL,
            saved_at       TEXT NOT NULL,
            PRIMARY KEY (ticker_scope, snapshot_date)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pgh_scope_date ON portfolio_greek_history (ticker_scope, snapshot_date)"
    )
    _pgh_cols = {row[1] for row in conn.execute("PRAGMA table_info(portfolio_greek_history)").fetchall()}
    if "capital_basis_usd" not in _pgh_cols:
        conn.execute("ALTER TABLE portfolio_greek_history ADD COLUMN capital_basis_usd REAL")
    conn.commit()
    conn.close()


PORTFOLIO_GREEKS_APR_BACKUP_KEY = "portfolio_greeks_apr_backup"
PORTFOLIO_TOTAL_TRADING_CAPITAL_KEY = "portfolio_total_trading_capital_usd"
PORTFOLIO_CAPITAL_RESERVE_PCT_KEY = "portfolio_capital_reserve_pct"


def upsert_portfolio_greek_history(
    ticker_scope: str,
    snapshot_date: str,
    apr_theta_pct: Optional[float],
    theta_usd: Optional[float],
    delta_usd: Optional[float],
    vega_usd: Optional[float],
    capital_basis_usd: Optional[float] = None,
) -> None:
    """
    Jedna snímka na kalendárny deň (``snapshot_date`` YYYY-MM-DD) a ``ticker_scope``.
    Opakovaný zápis v ten istý deň starý riadok prepíše.
    """
    scope = (ticker_scope or "").strip().upper()
    saved_at = datetime.now().isoformat(timespec="seconds")
    with get_connection() as conn:
        conn.execute(
            """
            INSERT INTO portfolio_greek_history
            (ticker_scope, snapshot_date, apr_theta_pct, theta_usd, delta_usd, vega_usd, saved_at, capital_basis_usd)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker_scope, snapshot_date) DO UPDATE SET
                apr_theta_pct = excluded.apr_theta_pct,
                theta_usd = excluded.theta_usd,
                delta_usd = excluded.delta_usd,
                vega_usd = excluded.vega_usd,
                saved_at = excluded.saved_at,
                capital_basis_usd = excluded.capital_basis_usd
            """,
            (
                scope,
                snapshot_date,
                apr_theta_pct,
                theta_usd,
                delta_usd,
                vega_usd,
                saved_at,
                capital_basis_usd,
            ),
        )
        conn.commit()


def list_portfolio_greek_history(
    ticker_scope: str = "",
    since_date: Optional[str] = None,
    limit: int = 500,
) -> list[dict]:
    """Riadky od ``since_date`` (vrátane), zoradené podľa dátumu vzostupne (pre graf)."""
    scope = (ticker_scope or "").strip().upper()
    lim = max(1, min(int(limit), 2000))
    with get_connection() as conn:
        if since_date:
            rows = conn.execute(
                "SELECT * FROM portfolio_greek_history WHERE ticker_scope=? AND snapshot_date >= ? "
                "ORDER BY snapshot_date ASC LIMIT ?",
                (scope, since_date, lim),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM portfolio_greek_history WHERE ticker_scope=? "
                "ORDER BY snapshot_date DESC LIMIT ?",
                (scope, lim),
            ).fetchall()
            rows = list(reversed(rows))
    return [dict(r) for r in rows]


def list_spread_builder_ideas() -> list[dict]:
    """Zoznam nápadov; každý riadok má ``leg_count`` a ``snapshot_count`` (bez ``legs_json`` v návrate)."""
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT i.id, i.name, i.ticker, i.spot, i.global_iv, i.maint_margin,
                   i.created_at, i.updated_at, i.legs_json, i.variant_of_id,
                   (SELECT COUNT(*) FROM spread_builder_snapshots s WHERE s.idea_id = i.id) AS snapshot_count
            FROM spread_builder_ideas i
            ORDER BY i.updated_at DESC
            """
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        lj = d.pop("legs_json", "[]")
        try:
            d["leg_count"] = len(json.loads(lj)) if lj else 0
        except (json.JSONDecodeError, TypeError, ValueError):
            d["leg_count"] = 0
        out.append(d)
    return out


def get_spread_builder_idea(idea_id: int) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM spread_builder_ideas WHERE id=?",
            (int(idea_id),),
        ).fetchone()
    return dict(row) if row else None


def insert_spread_builder_idea(
    name: str,
    ticker: str,
    spot: float,
    global_iv: float,
    maint_margin: float,
    legs: list,
    notes: str = "",
    variant_of_id: Optional[int] = None,
) -> int:
    import json
    nm = (name or "").strip() or "Bez názvu"
    vpid = int(variant_of_id) if variant_of_id is not None else None
    with get_connection() as conn:
        cur = conn.execute(
            "INSERT INTO spread_builder_ideas "
            "(name, ticker, spot, global_iv, maint_margin, legs_json, notes, updated_at, variant_of_id) "
            "VALUES (?,?,?,?,?,?,?, datetime('now'), ?)",
            (
                nm,
                (ticker or "").strip().upper() or None,
                float(spot),
                float(global_iv),
                float(maint_margin),
                json.dumps(legs, ensure_ascii=False),
                notes or "",
                vpid,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def update_spread_builder_idea(
    idea_id: int,
    name: str,
    ticker: str,
    spot: float,
    global_iv: float,
    maint_margin: float,
    legs: list,
    notes: str = "",
) -> None:
    import json
    nm = (name or "").strip() or "Bez názvu"
    with get_connection() as conn:
        conn.execute(
            "UPDATE spread_builder_ideas SET name=?, ticker=?, spot=?, global_iv=?, maint_margin=?, "
            "legs_json=?, notes=?, updated_at=datetime('now') WHERE id=?",
            (
                nm,
                (ticker or "").strip().upper() or None,
                float(spot),
                float(global_iv),
                float(maint_margin),
                json.dumps(legs, ensure_ascii=False),
                notes or "",
                int(idea_id),
            ),
        )
        conn.commit()


def delete_spread_builder_idea(idea_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM spread_builder_ideas WHERE id=?", (int(idea_id),))
        conn.commit()


def append_spread_builder_snapshot(
    idea_id: int,
    captured_at: str,
    aptr_pct: float,
    theta_per_day: float,
    capital_basis: float,
    spot: float,
    global_iv: float,
) -> None:
    iid = int(idea_id)
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO spread_builder_snapshots "
            "(idea_id, captured_at, aptr_pct, theta_per_day, capital_basis, spot, global_iv) "
            "VALUES (?,?,?,?,?,?,?)",
            (iid, captured_at, aptr_pct, theta_per_day, capital_basis, spot, global_iv),
        )
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM spread_builder_snapshots WHERE idea_id=?",
            (iid,),
        ).fetchone()["c"]
        if n > MAX_SPREAD_BUILDER_SNAPSHOTS:
            excess = n - MAX_SPREAD_BUILDER_SNAPSHOTS
            conn.execute(
                "DELETE FROM spread_builder_snapshots WHERE id IN "
                "(SELECT id FROM spread_builder_snapshots WHERE idea_id=? ORDER BY captured_at ASC LIMIT ?)",
                (iid, excess),
            )
        conn.commit()


def get_spread_builder_snapshots(idea_id: int, limit: int = 120) -> list[dict]:
    iid = int(idea_id)
    lim = max(1, min(int(limit), 2000))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM spread_builder_snapshots WHERE idea_id=? ORDER BY captured_at DESC LIMIT ?",
            (iid, lim),
        ).fetchall()
    out = [dict(r) for r in rows]
    out.reverse()
    return out


def set_group_maint_margins(margins: dict[str, float]) -> None:
    """
    Zlúči marže do uloženého slovníka: aktualizuje len odovzdané ``group_id``,
    ostatné skupiny v DB ponechá. Kľúč s hodnotou ≤ 0 z uloženia odoberie.
    """
    import json
    raw = get_setting(GROUP_MAINT_MARGIN_KEY, "{}")
    try:
        existing = {
            str(k).strip(): round(float(v), 2)
            for k, v in json.loads(raw).items()
            if float(v) > 0
        }
    except (json.JSONDecodeError, TypeError, ValueError):
        existing = {}
    for k, v in margins.items():
        ks = str(k).strip()
        fv = float(v)
        if fv > 0:
            existing[ks] = round(fv, 2)
        else:
            existing.pop(ks, None)
    set_setting(GROUP_MAINT_MARGIN_KEY, json.dumps(existing, ensure_ascii=False))
