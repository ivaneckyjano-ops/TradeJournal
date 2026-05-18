import json
import os
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
LIVE_DB_PATH = os.path.join(_DATA_DIR, "journal_live.db")
PAPER_DB_PATH = os.path.join(_DATA_DIR, "journal_paper.db")
LEGACY_DB_PATH = os.path.join(_DATA_DIR, "journal.db")

# Backward-compatible alias used by tests / temporary overrides.
DB_PATH = LIVE_DB_PATH


def _session_ib_mode() -> Optional[str]:
    try:
        import streamlit as st
    except Exception:
        return None
    mode = str(getattr(st, "session_state", {}).get("ib_mode") or "").strip().upper()
    return mode if mode in ("LIVE", "PAPER") else None


def get_active_db_path(mode: Optional[str] = None) -> str:
    """
    Vráti aktuálny SQLite súbor.

    Poradie:
    1. explicitný override cez `DB_PATH` (napr. testy),
    2. DB podľa `ib_mode` v `st.session_state`,
    3. default `DB_PATH`.
    """
    override = globals().get("DB_PATH", LIVE_DB_PATH)
    if override not in (LIVE_DB_PATH, PAPER_DB_PATH, LEGACY_DB_PATH):
        return str(override)

    current_mode = (mode or _session_ib_mode() or "").strip().upper()
    if current_mode == "PAPER":
        return PAPER_DB_PATH
    if current_mode == "LIVE":
        return LIVE_DB_PATH
    return str(override)


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or get_active_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    path = get_active_db_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with get_connection(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id     TEXT,
                ticker       TEXT NOT NULL,
                strategy     TEXT,
                leg_type     TEXT CHECK(leg_type IN ('Long','Short')),
                option_type  TEXT CHECK(option_type IN ('Call','Put','STK')),
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
    with get_connection() as _conn_stk:
        _migrate_trades_option_type_allow_stk(_conn_stk)
    _migrate_group_apr_snapshots(get_connection())
    _migrate_spread_builder(get_connection())
    _migrate_portfolio_greek_history(get_connection())
    _migrate_steady_yields(get_connection())
    _migrate_steady_yields_alerts(get_connection())
    _migrate_symbol_market_snapshots(get_connection())
    _migrate_symbol_ib_option_snapshots(get_connection())
    _migrate_sector_performance_snapshots(get_connection())
    _migrate_ticker_correlation_data(get_connection())
    _migrate_trade_journal_greeks(get_connection())
    _migrate_trading_commands(get_connection())
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
        "industry":        "ALTER TABLE symbols ADD COLUMN industry TEXT",
        "market_synced_at": "ALTER TABLE symbols ADD COLUMN market_synced_at TEXT",
        "iv_rank_13w": "ALTER TABLE symbols ADD COLUMN iv_rank_13w REAL",
        "iv_rank_52w": "ALTER TABLE symbols ADD COLUMN iv_rank_52w REAL",
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
        "delta_at_entry": "ALTER TABLE trades ADD COLUMN delta_at_entry REAL",
        "theta_at_entry": "ALTER TABLE trades ADD COLUMN theta_at_entry REAL",
        "delta_current": "ALTER TABLE trades ADD COLUMN delta_current REAL",
    }
    for col, sql in migrations.items():
        if col not in existing:
            conn.execute(sql)
    conn.commit()
    conn.close()


_STK_OPT_CHECK = re.compile(
    r"option_type\s+TEXT\s+CHECK\s*\(\s*option_type\s+IN\s*\(\s*'Call'\s*,\s*'Put'\s*\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _trades_create_sql_relaxed_option_type(conn: sqlite3.Connection, source_table: str) -> str:
    """CREATE TABLE trades … bez CHECK na ``option_type`` (hodnota STK)."""
    cols = conn.execute(f'PRAGMA table_info("{source_table}")').fetchall()
    parts: list[str] = []
    for _cid, name, typ, notnull, dflt, pk in cols:
        if name == "option_type":
            parts.append('  "option_type" TEXT')
            continue
        coltype = typ or "TEXT"
        if name == "id" and int(pk or 0) == 1:
            parts.append('  "id" INTEGER PRIMARY KEY AUTOINCREMENT')
            continue
        seg = [f'  "{name}" {coltype}']
        if dflt is not None:
            seg.append(f"DEFAULT {dflt}")
        if int(notnull or 0) == 1 and int(pk or 0) == 0:
            seg.append("NOT NULL")
        parts.append(" ".join(seg))
    return "CREATE TABLE trades (\n" + ",\n".join(parts) + "\n)"


def _tables_with_foreign_key_to(conn: sqlite3.Connection, parent: str) -> list[str]:
    out: list[str] = []
    for (tname,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ):
        for fk in conn.execute(f'PRAGMA foreign_key_list("{tname}")'):
            if str(fk[2]) == parent:
                out.append(str(tname))
                break
    return out


def _rebuild_table_fix_fk_parent(
    conn: sqlite3.Connection,
    table: str,
    old_parent: str,
    new_parent: str,
) -> None:
    """Prekopíruje tabuľku s rovnakým menom a opraveným REFERENCES na rodiča."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not row or not row[0]:
        return
    ddl = row[0]
    if old_parent not in ddl:
        return
    fixed = ddl.replace(f'REFERENCES "{old_parent}"', f'REFERENCES "{new_parent}"').replace(
        f"REFERENCES {old_parent}(", f"REFERENCES {new_parent}("
    )
    cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    colq = ", ".join(f'"{c}"' for c in cols)
    tmp = f"{table}_mig_fk_tmp"
    conn.execute(f'ALTER TABLE "{table}" RENAME TO "{tmp}"')
    conn.execute(fixed)
    conn.execute(f'INSERT INTO "{table}" ({colq}) SELECT {colq} FROM "{tmp}"')
    conn.execute(f'DROP TABLE "{tmp}"')


def _migrate_trades_option_type_allow_stk(conn: sqlite3.Connection) -> None:
    """
    Existujúce DB mali CHECK len Call/Put — rozšíri uloženie o akcie (STK).
    Prekopíruje ``trades`` a tabuľky s FK na ``trades`` (notes, events, trade_greek_snapshots).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if not row or not row[0]:
        return
    if not _STK_OPT_CHECK.search(row[0]):
        return
    old_tbl = "trades_mig_old"
    fk_prev = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute(f'ALTER TABLE trades RENAME TO "{old_tbl}"')
        conn.execute(_trades_create_sql_relaxed_option_type(conn, old_tbl))
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{old_tbl}")').fetchall()]
        colq = ", ".join(f'"{c}"' for c in cols)
        conn.execute(f"INSERT INTO trades ({colq}) SELECT {colq} FROM \"{old_tbl}\"")
        for child in _tables_with_foreign_key_to(conn, old_tbl):
            _rebuild_table_fix_fk_parent(conn, child, old_tbl, "trades")
        conn.execute(f'DROP TABLE "{old_tbl}"')
    finally:
        conn.execute(f"PRAGMA foreign_keys={int(fk_prev)}")


def _migrate_trade_journal_greeks(conn: sqlite3.Connection) -> None:
    """Vega / aktuálna IV a Θ + časová história Grékov pre journal (bez TWS)."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
    for col, sql in {
        "vega_at_entry": "ALTER TABLE trades ADD COLUMN vega_at_entry REAL",
        "vega_current": "ALTER TABLE trades ADD COLUMN vega_current REAL",
        "iv_current": "ALTER TABLE trades ADD COLUMN iv_current REAL",
        "theta_current": "ALTER TABLE trades ADD COLUMN theta_current REAL",
    }.items():
        if col not in existing:
            conn.execute(sql)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trade_greek_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id     INTEGER NOT NULL REFERENCES trades(id) ON DELETE CASCADE,
            recorded_at  TEXT NOT NULL,
            delta          REAL,
            theta_usd      REAL,
            vega           REAL,
            iv             REAL,
            source         TEXT DEFAULT 'journal'
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tgs_trade_time ON trade_greek_snapshots (trade_id, recorded_at)"
    )
    conn.commit()


def _migrate_trading_commands(conn: sqlite3.Connection) -> None:
    """Poznámky k plánovaným príkazom (nie IBKR rozkazy — lokálny denník)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trading_commands (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
            title         TEXT NOT NULL,
            ticker        TEXT,
            action        TEXT,
            order_kind    TEXT,
            quantity      REAL,
            limit_price   REAL,
            stop_price    REAL,
            body          TEXT,
            status        TEXT NOT NULL DEFAULT 'draft'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tc_status ON trading_commands (status, updated_at DESC)")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trading_commands)").fetchall()}
    for col, sql in {
        "plan_group": "ALTER TABLE trading_commands ADD COLUMN plan_group TEXT",
        "step_index": "ALTER TABLE trading_commands ADD COLUMN step_index INTEGER",
        "tws_perm_id": "ALTER TABLE trading_commands ADD COLUMN tws_perm_id TEXT",
        "tws_order_id": "ALTER TABLE trading_commands ADD COLUMN tws_order_id TEXT",
        "tws_manual_note": "ALTER TABLE trading_commands ADD COLUMN tws_manual_note TEXT",
        "cond_under_cmp": "ALTER TABLE trading_commands ADD COLUMN cond_under_cmp TEXT",
        "cond_under_price": "ALTER TABLE trading_commands ADD COLUMN cond_under_price REAL",
        "cond_after_fill": "ALTER TABLE trading_commands ADD COLUMN cond_after_fill TEXT",
        "cond_detail": "ALTER TABLE trading_commands ADD COLUMN cond_detail TEXT",
        "trigger_kind": "ALTER TABLE trading_commands ADD COLUMN trigger_kind TEXT",
        "close_sec_type": "ALTER TABLE trading_commands ADD COLUMN close_sec_type TEXT",
        "close_expiry": "ALTER TABLE trading_commands ADD COLUMN close_expiry TEXT",
        "close_strike": "ALTER TABLE trading_commands ADD COLUMN close_strike REAL",
        "close_right": "ALTER TABLE trading_commands ADD COLUMN close_right TEXT",
        "linked_trade_id": "ALTER TABLE trading_commands ADD COLUMN linked_trade_id INTEGER",
        "assignment_watch_trade_id": "ALTER TABLE trading_commands ADD COLUMN assignment_watch_trade_id INTEGER",
        "assignment_check_at": "ALTER TABLE trading_commands ADD COLUMN assignment_check_at TEXT",
        "assignment_check_summary": "ALTER TABLE trading_commands ADD COLUMN assignment_check_summary TEXT",
    }.items():
        if col not in existing:
            conn.execute(sql)
    conn.commit()


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
        # INSERT OR IGNORE — duplicitný ticker; nevrátiť existujúce id (volajúci by mylne považoval za nový riadok)
        return 0


def get_symbols() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT * FROM symbols ORDER BY ticker").fetchall()
    return [dict(r) for r in rows]


def get_symbol_tickers() -> list[str]:
    return [s["ticker"] for s in get_symbols()]


def get_distinct_trade_tickers() -> list[str]:
    """Distinct tickery z Trade Log (obchody) — underlying symboly z nôh."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT UPPER(TRIM(ticker)) AS t FROM trades "
            "WHERE ticker IS NOT NULL AND TRIM(ticker) != '' ORDER BY t"
        ).fetchall()
    return [r["t"] for r in rows]


def get_symbol(ticker: str) -> Optional[dict]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM symbols WHERE ticker=?", (ticker.upper(),)).fetchone()
    return dict(row) if row else None


def update_symbol(symbol_id: int, ticker: str, company_name: str, sector: str,
                  asset_type: str, description: str,
                  earnings_date: str = None, iv_rank: float = None,
                  earnings_date_2: str = None, earnings_date_3: str = None,
                  earnings_date_4: str = None, ir_url: str = None,
                  spot: float = None, iv_pct: float = None,
                  iv_rank_13w: Optional[float] = None,
                  iv_rank_52w: Optional[float] = None) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE symbols SET ticker=?, company_name=?, sector=?, asset_type=?, "
            "description=?, earnings_date=?, earnings_date_2=?, earnings_date_3=?, "
            "earnings_date_4=?, ir_url=?, iv_rank=?, spot=?, iv_pct=?, "
            "iv_rank_13w=?, iv_rank_52w=? WHERE id=?",
            (ticker.strip().upper(), company_name, sector, asset_type,
             description, earnings_date, earnings_date_2, earnings_date_3,
             earnings_date_4, ir_url, iv_rank, spot, iv_pct,
             iv_rank_13w, iv_rank_52w, symbol_id),
        )


def delete_symbol(symbol_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM symbols WHERE id=?", (symbol_id,))


def upsert_symbol_market_snapshot(
    ticker: str,
    recorded_date: str,
    *,
    iv_pct: Optional[float] = None,
    spot: Optional[float] = None,
    source: str = "yahoo",
) -> None:
    tk = (ticker or "").strip().upper()
    if not tk:
        return
    d = (recorded_date or "")[:10]
    if not d:
        return
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO symbol_market_snapshots (ticker, recorded_date, iv_pct, spot, source)
               VALUES (?,?,?,?,?)
               ON CONFLICT(ticker, recorded_date) DO UPDATE SET
                 iv_pct=COALESCE(excluded.iv_pct, iv_pct),
                 spot=COALESCE(excluded.spot, spot),
                 source=excluded.source""",
            (tk, d, iv_pct, spot, source),
        )
        conn.commit()


def get_symbol_iv_pct_history_before(
    ticker: str,
    before_date: str,
    limit: int = 260,
) -> list[float]:
    tk = (ticker or "").strip().upper()
    if not tk:
        return []
    lim = max(5, min(int(limit), 500))
    bd = (before_date or "")[:10]
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT iv_pct FROM symbol_market_snapshots
               WHERE ticker=? AND recorded_date < ? AND iv_pct IS NOT NULL AND iv_pct > 0
               ORDER BY recorded_date DESC LIMIT ?""",
            (tk, bd, lim),
        ).fetchall()
    return [float(r["iv_pct"]) for r in rows]


def apply_symbol_yahoo_patch(symbol_id: int, patch: dict) -> None:
    """Čiastočná aktualizácia riadku symbols (Yahoo sync). Kľúče: company_name, sector, industry, spot, iv_pct, iv_rank, market_synced_at."""
    allowed = (
        "company_name",
        "sector",
        "industry",
        "spot",
        "iv_pct",
        "iv_rank",
        "market_synced_at",
    )
    cols: list[str] = []
    vals: list = []
    for k in allowed:
        if k not in patch:
            continue
        cols.append(f"{k}=?")
        vals.append(patch[k])
    if not cols:
        return
    vals.append(int(symbol_id))
    with get_connection() as conn:
        conn.execute(f"UPDATE symbols SET {', '.join(cols)} WHERE id=?", vals)
        conn.commit()


def normalize_symbols_iv_pct_fraction_scale() -> tuple[int, int]:
    """
    Opraví iv_pct uložený ako desatinný zlomok (napr. 0.35 namiesto 35 %) — vynásobí 100.
    Platí pre ``symbols`` aj ``symbol_market_snapshots``. Vráti (počet riadkov symbols, snapshots).
    """
    with get_connection() as conn:
        cur1 = conn.execute(
            "UPDATE symbols SET iv_pct = iv_pct * 100.0 "
            "WHERE iv_pct IS NOT NULL AND iv_pct > 0 AND iv_pct < 2"
        )
        cur2 = conn.execute(
            "UPDATE symbol_market_snapshots SET iv_pct = iv_pct * 100.0 "
            "WHERE iv_pct IS NOT NULL AND iv_pct > 0 AND iv_pct < 2"
        )
        conn.commit()
        n1 = cur1.rowcount if cur1.rowcount is not None else 0
        n2 = cur2.rowcount if cur2.rowcount is not None else 0
    return (int(n1), int(n2))


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
    delta_at_entry: Optional[float] = None,
    theta_at_entry: Optional[float] = None,
) -> int:
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (ticker, strategy, leg_type, option_type, strike, expiry,
                contracts, entry_price, entry_date, group_id, iv_at_entry, pop_at_entry,
                commission, delta_at_entry, theta_at_entry)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticker, strategy, leg_type, option_type, strike, expiry,
             contracts, entry_price, entry_date, group_id, iv_at_entry, pop_at_entry,
             commission or 0.0, delta_at_entry, theta_at_entry),
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
    iv_at_entry: Optional[float] = None,
    pop_at_entry: Optional[float] = None,
    delta_at_entry: Optional[float] = None,
    theta_at_entry: Optional[float] = None,
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
            "iv_at_entry": iv_at_entry,
            "pop_at_entry": pop_at_entry,
            "delta_at_entry": delta_at_entry,
            "theta_at_entry": theta_at_entry,
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


def set_trade_entry_iv_delta_theta(
    trade_id: int,
    iv_at_entry: Optional[float],
    delta_at_entry: Optional[float],
    theta_at_entry: Optional[float],
) -> None:
    """Nastaví IV, Δ a Θ pri vstupe naraz (None = NULL v DB)."""
    with get_connection() as conn:
        conn.execute(
            "UPDATE trades SET iv_at_entry=?, delta_at_entry=?, theta_at_entry=? WHERE id=?",
            (iv_at_entry, delta_at_entry, theta_at_entry, trade_id),
        )


def set_trade_portfolio_greeks(
    trade_id: int,
    iv_at_entry: Optional[float],
    delta_at_entry: Optional[float],
    theta_at_entry: Optional[float],
    delta_current: Optional[float],
    *,
    vega_at_entry: Optional[float] = None,
    vega_current: Optional[float] = None,
    iv_current: Optional[float] = None,
    theta_current: Optional[float] = None,
) -> None:
    """
    Journal / Portfolio: vstupné a aktuálne Gréky + IV v denníku (doplnenie oproti TWS).
    ``None`` pri volaní znamená „nezmenené“ len ak volajúci posiela výhradne staré API — pri úplnom zápise z UI pošli všetky hodnoty z riadka.
    """
    with get_connection() as conn:
        conn.execute(
            "UPDATE trades SET iv_at_entry=?, delta_at_entry=?, theta_at_entry=?, delta_current=?, "
            "vega_at_entry=?, vega_current=?, iv_current=?, theta_current=? WHERE id=?",
            (
                iv_at_entry,
                delta_at_entry,
                theta_at_entry,
                delta_current,
                vega_at_entry,
                vega_current,
                iv_current,
                theta_current,
                trade_id,
            ),
        )


def insert_trade_greek_snapshot(
    trade_id: int,
    *,
    delta: Optional[float] = None,
    theta_usd: Optional[float] = None,
    vega: Optional[float] = None,
    iv: Optional[float] = None,
    recorded_at: Optional[str] = None,
    source: str = "journal",
) -> int:
    """Uloží jeden bod časovej série Grékov pre nohu (od otvorenia po uzavretie)."""
    rid = int(trade_id)
    if rid <= 0:
        raise ValueError("trade_id")
    ts = recorded_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    src = (source or "journal").strip() or "journal"
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO trade_greek_snapshots (trade_id, recorded_at, delta, theta_usd, vega, iv, source)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (rid, ts, delta, theta_usd, vega, iv, src),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_trade_greek_snapshots(trade_id: int, limit: int = 800) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 5000))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, trade_id, recorded_at, delta, theta_usd, vega, iv, source
            FROM trade_greek_snapshots WHERE trade_id=?
            ORDER BY recorded_at ASC, id ASC
            LIMIT ?
            """,
            (int(trade_id), lim),
        ).fetchall()
    return [dict(r) for r in rows]


def get_trade_by_id(trade_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (int(trade_id),)).fetchone()
    return dict(row) if row else None


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
                    pop_at_entry, commission, delta_at_entry, theta_at_entry,
                    delta_current, vega_at_entry, vega_current, iv_current, theta_current,
                    exit_price, exit_date, status)
                   VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    t["ticker"], t["strategy"], t["leg_type"], t["option_type"],
                    t["strike"], t["expiry"], t["entry_price"], t["entry_date"],
                    gid if gid else None, t["iv_at_entry"], t["pop_at_entry"],
                    t.get("commission") or 0.0, t.get("delta_at_entry"), t.get("theta_at_entry"),
                    t.get("delta_current"),
                    t.get("vega_at_entry"),
                    t.get("vega_current"),
                    t.get("iv_current"),
                    t.get("theta_current"),
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
    ot = str(trade.get("option_type") or "").strip().upper()
    multiplier = 1 if ot in ("STK", "STOCK") else 100
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

# Voľný text: IBKR predplatné trhových dát — portfóliový agent (analýza + follow-up chat)
AGENT_IBKR_MARKET_DATA_KEY = "agent_ibkr_market_data"
# Archív hotových sedení portfóliového agenta (JSON pole; v UI posledných ~90 dní)
PORTFOLIO_AGENT_EVAL_ARCHIVE_KEY = "portfolio_agent_eval_archive"
SPREAD_BUILDER_AGENT_CHAT_KEY = "spread_builder_agent_chat"
# AI chat: porovnanie uložených diagonál (Hľadanie diagonálu — 2+ riadky)
DIAGONAL_COMPARE_AGENT_CHAT_KEY = "diagonal_compare_agent_chat"


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


def _migrate_steady_yields(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS steady_yield_roll_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        TEXT NOT NULL,
            occurred_at     TEXT NOT NULL,
            ticker          TEXT NOT NULL,
            leg_type        TEXT CHECK(leg_type IN ('Long','Short')),
            action          TEXT NOT NULL,
            net_premium     REAL NOT NULL DEFAULT 0,
            commission      REAL NOT NULL DEFAULT 0,
            delta_snapshot  REAL,
            dte_snapshot    INTEGER,
            strike          REAL,
            expiry          TEXT,
            option_type     TEXT,
            notes           TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sy_roll_group ON steady_yield_roll_events (group_id, occurred_at)"
    )
    conn.execute("""
        CREATE TABLE IF NOT EXISTS steady_yield_group_profile (
            group_id            TEXT PRIMARY KEY,
            expected_apr_pct    REAL,
            leap_initial_cost   REAL,
            notes               TEXT,
            updated_at          TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()


def _migrate_steady_yields_alerts(conn: sqlite3.Connection) -> None:
    """Rozšírenie profilu (profit target, semafor alerty) + história upozornení."""
    try:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(steady_yield_group_profile)").fetchall()}
    except sqlite3.OperationalError:
        existing = set()
    col_sql = {
        "profit_target_pct": "ALTER TABLE steady_yield_group_profile ADD COLUMN profit_target_pct REAL",
        "alert_semafor_enabled": (
            "ALTER TABLE steady_yield_group_profile ADD COLUMN alert_semafor_enabled INTEGER DEFAULT 1"
        ),
    }
    for col, sql in col_sql.items():
        if col not in existing:
            try:
                conn.execute(sql)
            except sqlite3.OperationalError:
                pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS steady_yield_alert_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id        TEXT NOT NULL,
            trade_id        INTEGER,
            alert_type      TEXT NOT NULL,
            message         TEXT NOT NULL,
            detail_json     TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            acknowledged    INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sy_alert_group_time ON steady_yield_alert_events (group_id, created_at)"
    )
    conn.commit()
    conn.close()


def _migrate_symbol_market_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbol_market_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            recorded_date   TEXT NOT NULL,
            iv_pct          REAL,
            spot            REAL,
            source          TEXT DEFAULT 'yahoo',
            UNIQUE (ticker, recorded_date)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sym_snap_ticker_date ON symbol_market_snapshots (ticker, recorded_date)"
    )
    conn.commit()
    conn.close()


SYMBOL_IB_OPTION_REFRESH_KEY = "symbol_ib_option_last_refresh_utc"


def _migrate_sector_performance_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sector_performance_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT NOT NULL,
            horizon      TEXT NOT NULL CHECK(horizon IN ('short','long')),
            note         TEXT,
            payload_json TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sector_perf_h_created "
        "ON sector_performance_snapshots (horizon, created_at DESC)"
    )
    conn.commit()


def _migrate_ticker_correlation_data(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticker_hist_snapshots (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT NOT NULL,
            ticker       TEXT NOT NULL,
            note         TEXT,
            bar_count    INTEGER NOT NULL,
            first_date   TEXT,
            last_date    TEXT,
            series_json  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ths_ticker_created ON ticker_hist_snapshots (ticker, created_at DESC)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ticker_corr_matrix_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at   TEXT NOT NULL,
            title        TEXT,
            max_days     INTEGER NOT NULL,
            method       TEXT NOT NULL DEFAULT 'pearson',
            return_kind  TEXT NOT NULL DEFAULT 'log',
            tickers_json TEXT NOT NULL,
            matrix_json  TEXT NOT NULL,
            n_obs_json   TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tcm_created ON ticker_corr_matrix_runs (created_at DESC)"
    )
    conn.commit()


def _migrate_symbol_ib_option_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS symbol_ib_option_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker          TEXT NOT NULL,
            category        TEXT NOT NULL,
            expiry          TEXT NOT NULL,
            strike          REAL NOT NULL,
            right           TEXT NOT NULL,
            bid             REAL,
            ask             REAL,
            iv              REAL,
            theta           REAL,
            gamma           REAL,
            vega            REAL,
            und_price       REAL,
            recorded_at     TEXT NOT NULL,
            source          TEXT DEFAULT 'ibkr',
            error           TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sym_ib_opt_t_rec "
        "ON symbol_ib_option_snapshots (ticker, category, recorded_at)"
    )
    conn.commit()


def insert_symbol_ib_option_snapshot(
    ticker: str,
    category: str,
    expiry: str,
    strike: float,
    right: str,
    *,
    bid: Optional[float] = None,
    ask: Optional[float] = None,
    iv: Optional[float] = None,
    theta: Optional[float] = None,
    gamma: Optional[float] = None,
    vega: Optional[float] = None,
    und_price: Optional[float] = None,
    recorded_at: str = "",
    source: str = "ibkr",
    error: Optional[str] = None,
) -> int:
    tk = (ticker or "").strip().upper()
    cat = (category or "").strip()
    if cat not in ("open_position", "watched_only"):
        cat = "watched_only"
    exp = str(expiry or "").strip()
    rr = (right or "C").upper()[:1]
    if rr not in ("C", "P"):
        rr = "C"
    ra = recorded_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO symbol_ib_option_snapshots
            (ticker, category, expiry, strike, right, bid, ask, iv, theta, gamma, vega,
             und_price, recorded_at, source, error)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tk,
                cat,
                exp,
                float(strike),
                rr,
                bid,
                ask,
                iv,
                theta,
                gamma,
                vega,
                und_price,
                ra,
                source or "ibkr",
                error,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_symbol_ib_option_snapshots_latest_batch() -> list[dict]:
    """Všetky riadky z posledného behu (rovnaký ``recorded_at`` = max)."""
    with get_connection() as conn:
        row = conn.execute("SELECT MAX(recorded_at) AS m FROM symbol_ib_option_snapshots").fetchone()
        mx = row["m"] if row else None
        if not mx:
            return []
        rows = conn.execute(
            "SELECT * FROM symbol_ib_option_snapshots WHERE recorded_at=? "
            "ORDER BY category, ticker, strike",
            (mx,),
        ).fetchall()
    return [dict(r) for r in rows]


def append_steady_yield_roll_event(
    group_id: str,
    occurred_at: str,
    ticker: str,
    action: str,
    net_premium: float = 0.0,
    commission: float = 0.0,
    *,
    leg_type: Optional[str] = None,
    delta_snapshot: Optional[float] = None,
    dte_snapshot: Optional[int] = None,
    strike: Optional[float] = None,
    expiry: Optional[str] = None,
    option_type: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    gid = (group_id or "").strip()
    if not gid:
        return -1
    with get_connection() as conn:
        cur = conn.execute(
            """INSERT INTO steady_yield_roll_events
               (group_id, occurred_at, ticker, leg_type, action, net_premium, commission,
                delta_snapshot, dte_snapshot, strike, expiry, option_type, notes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                gid,
                occurred_at,
                (ticker or "").strip().upper(),
                leg_type,
                action,
                float(net_premium),
                float(commission),
                delta_snapshot,
                dte_snapshot,
                strike,
                expiry,
                option_type,
                notes or "",
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_steady_yield_roll_events(group_id: str, limit: int = 500) -> list[dict]:
    gid = (group_id or "").strip()
    if not gid:
        return []
    lim = max(1, min(int(limit), 5000))
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT * FROM steady_yield_roll_events WHERE group_id=? "
            "ORDER BY occurred_at DESC, id DESC LIMIT ?",
            (gid, lim),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_steady_yield_roll_event(event_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM steady_yield_roll_events WHERE id=?", (int(event_id),))
        conn.commit()


def get_steady_yield_group_profile(group_id: str) -> Optional[dict]:
    gid = (group_id or "").strip()
    if not gid:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT * FROM steady_yield_group_profile WHERE group_id=?",
            (gid,),
        ).fetchone()
    return dict(row) if row else None


def upsert_steady_yield_group_profile(
    group_id: str,
    *,
    expected_apr_pct: Optional[float] = None,
    leap_initial_cost: Optional[float] = None,
    notes: Optional[str] = None,
    profit_target_pct: Optional[float] = None,
    alert_semafor_enabled: Optional[bool] = None,
) -> None:
    gid = (group_id or "").strip()
    if not gid:
        return
    existing = get_steady_yield_group_profile(gid) or {}
    exp_a = expected_apr_pct if expected_apr_pct is not None else existing.get("expected_apr_pct")
    leap_c = leap_initial_cost if leap_initial_cost is not None else existing.get("leap_initial_cost")
    nts = notes if notes is not None else (existing.get("notes") or "")
    if profit_target_pct is not None:
        pt = None if float(profit_target_pct) <= 0 else float(profit_target_pct)
    else:
        pt = existing.get("profit_target_pct")
    if alert_semafor_enabled is not None:
        sf = 1 if alert_semafor_enabled else 0
    else:
        v = existing.get("alert_semafor_enabled")
        sf = 1 if v is None else int(v)
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO steady_yield_group_profile
               (group_id, expected_apr_pct, leap_initial_cost, notes,
                profit_target_pct, alert_semafor_enabled, updated_at)
               VALUES (?,?,?,?,?,?, datetime('now'))
               ON CONFLICT(group_id) DO UPDATE SET
                 expected_apr_pct=excluded.expected_apr_pct,
                 leap_initial_cost=excluded.leap_initial_cost,
                 notes=excluded.notes,
                 profit_target_pct=excluded.profit_target_pct,
                 alert_semafor_enabled=excluded.alert_semafor_enabled,
                 updated_at=datetime('now')""",
            (gid, exp_a, leap_c, nts or "", pt, sf),
        )
        conn.commit()


def append_steady_yield_alert_event(
    group_id: str,
    alert_type: str,
    message: str,
    *,
    trade_id: Optional[int] = None,
    detail_json: Optional[str] = None,
    dedupe_hours: Optional[float] = 24.0,
) -> Optional[int]:
    """
    Uloží upozornenie. Ak ``dedupe_hours`` je zadané a rovnaký typ + trade už existuje v okne, vráti None.
    """
    gid = (group_id or "").strip()
    if not gid or not message.strip():
        return None
    with get_connection() as conn:
        if dedupe_hours is not None and dedupe_hours > 0:
            tid = trade_id if trade_id is not None else -1
            row = conn.execute(
                """
                SELECT id FROM steady_yield_alert_events
                WHERE group_id=? AND alert_type=? AND COALESCE(trade_id,-1)=?
                  AND IFNULL(acknowledged,0)=0
                  AND datetime(created_at) > datetime('now', ?)
                LIMIT 1
                """,
                (gid, alert_type, tid, f"-{int(max(1, round(float(dedupe_hours))))} hours"),
            ).fetchone()
            if row:
                return None
        cur = conn.execute(
            """INSERT INTO steady_yield_alert_events
               (group_id, trade_id, alert_type, message, detail_json)
               VALUES (?,?,?,?,?)""",
            (gid, trade_id, alert_type, message.strip(), detail_json),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_steady_yield_alert_events(
    group_id: str,
    limit: int = 80,
    *,
    unacknowledged_only: bool = False,
) -> list[dict]:
    gid = (group_id or "").strip()
    if not gid:
        return []
    lim = max(1, min(int(limit), 500))
    with get_connection() as conn:
        q = "SELECT * FROM steady_yield_alert_events WHERE group_id=?"
        params: list = [gid]
        if unacknowledged_only:
            q += " AND IFNULL(acknowledged,0)=0"
        q += " ORDER BY datetime(created_at) DESC LIMIT ?"
        params.append(lim)
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def acknowledge_steady_yield_alert(alert_id: int) -> None:
    with get_connection() as conn:
        conn.execute(
            "UPDATE steady_yield_alert_events SET acknowledged=1 WHERE id=?",
            (int(alert_id),),
        )
        conn.commit()


def list_steady_yield_group_ids_from_trades() -> list[str]:
    """Distinct group_id z obchodov (neprázdne)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT group_id FROM trades WHERE group_id IS NOT NULL AND TRIM(group_id) != '' "
            "ORDER BY group_id"
        ).fetchall()
    return [r["group_id"] for r in rows if r["group_id"]]


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


def insert_sector_performance_snapshot(
    horizon: str,
    payload: dict[str, Any],
    note: Optional[str] = None,
) -> int:
    """
    Uloží OCR/normalizovanú tabuľku výkonnosti sektorov.
    ``horizon``: ``short`` (krátkodobý screenshot) alebo ``long`` (dlhodobý).
    ``payload``: napr. ``{"rows": [{"sector": "...", "pct_1d": 0.1, ...}]}``.
    """
    h = (horizon or "").strip().lower()
    if h not in ("short", "long"):
        raise ValueError("horizon musí byť 'short' alebo 'long'")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO sector_performance_snapshots (created_at, horizon, note, payload_json)
            VALUES (?, ?, ?, ?)
            """,
            (now, h, (note or "").strip() or None, json.dumps(payload, ensure_ascii=False)),
        )
        conn.commit()
        return int(cur.lastrowid)


def get_latest_sector_performance_snapshot(horizon: str) -> Optional[dict[str, Any]]:
    h = (horizon or "").strip().lower()
    if h not in ("short", "long"):
        return None
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, created_at, horizon, note, payload_json
            FROM sector_performance_snapshots
            WHERE horizon = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (h,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.pop("payload_json"))
    except (json.JSONDecodeError, TypeError):
        d["payload"] = {}
    return d


def list_sector_performance_snapshots(limit: int = 30) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, horizon, note, payload_json
            FROM sector_performance_snapshots
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.pop("payload_json"))
        except (json.JSONDecodeError, TypeError):
            d["payload"] = {}
        out.append(d)
    return out


def insert_ticker_hist_snapshot(
    ticker: str,
    series_json: str,
    *,
    bar_count: int,
    first_date: str,
    last_date: str,
    note: Optional[str] = None,
) -> int:
    """Uloží denné uzávierky (JSON: zoznam objektov s kľúčmi ``d``, ``c`` — pozri ``hist_dataframe_to_series_json``)."""
    tk = (ticker or "").strip().upper()
    if not tk:
        raise ValueError("Ticker je prázdny.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO ticker_hist_snapshots
            (created_at, ticker, note, bar_count, first_date, last_date, series_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                tk,
                (note or "").strip() or None,
                int(bar_count),
                (first_date or "").strip() or None,
                (last_date or "").strip() or None,
                series_json,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_ticker_hist_snapshots(limit: int = 200) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, ticker, note, bar_count, first_date, last_date
            FROM ticker_hist_snapshots
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    return [dict(r) for r in rows]


def list_ticker_hist_snapshots_latest_per_ticker() -> list[dict[str, Any]]:
    """
    Jeden (najnovší) záznam **na každý ticker** — vhodné na UI výberu.

    Oproti ``list_ticker_hist_snapshots(limit)`` tým nepadnú mimo zoznam tickery, ktoré sú v DB
    skôr, ale medzi 300/500 „poslednými riadkami“ globálne neboli (najmä pri rozšírení matice o nové
    tickery).
    """
    with get_connection() as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT
                    id, created_at, ticker, note, bar_count, first_date, last_date,
                    ROW_NUMBER() OVER (
                        PARTITION BY UPPER(TRIM(ticker))
                        ORDER BY created_at DESC, id DESC
                    ) AS rn
                FROM ticker_hist_snapshots
            )
            SELECT id, created_at, ticker, note, bar_count, first_date, last_date
            FROM ranked
            WHERE rn = 1
            ORDER BY UPPER(ticker)
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_ticker_hist_snapshot(snap_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, created_at, ticker, note, bar_count, first_date, last_date, series_json
            FROM ticker_hist_snapshots
            WHERE id = ?
            """,
            (int(snap_id),),
        ).fetchone()
    if not row:
        return None
    return dict(row)


def get_latest_ticker_hist_snapshot_rows(
    tickers: list[str], *, max_scan: int = 500
) -> dict[str, dict[str, Any]]:
    """
    Pre každý zadaný symbol jeden **najnovší** snímok (priamo v ``ticker_hist_snapshots``) so ``series_json``.
    Ak ticker v DB ešte nie je, v slovníku chýba.

    Parameter ``max_scan`` sa ponecháva kvôli kompatibilite; výber je vždy **globálne najnovší** na symbol,
    neobmedzený počtom mladších iných záznamov.
    """
    _ = int(max_scan)  # zachovať signatúru
    want: set[str] = {str(t).strip().upper() for t in (tickers or []) if (t or "").strip()}
    if not want:
        return {}
    out: dict[str, dict[str, Any]] = {}
    with get_connection() as conn:
        for w in want:
            row = conn.execute(
                """
                SELECT id, created_at, ticker, note, bar_count, first_date, last_date, series_json
                FROM ticker_hist_snapshots
                WHERE UPPER(TRIM(ticker)) = ?
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (w,),
            ).fetchone()
            if not row or not (dict(row).get("series_json") or "").strip():
                continue
            d = dict(row)
            tk = str(d.get("ticker") or "").strip().upper()
            out[tk] = d
    return out


def delete_ticker_hist_snapshot(snap_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM ticker_hist_snapshots WHERE id = ?", (int(snap_id),))
        conn.commit()
        return int(cur.rowcount)


def delete_ticker_hist_snapshots_by_ticker(ticker: str) -> int:
    """Vymaže **všetky** uložené snímky daného symbolu (všetky verzie CSV v čase). Vráti počet zmazaných riadkov."""
    tk = (ticker or "").strip().upper()
    if not tk:
        return 0
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM ticker_hist_snapshots WHERE UPPER(TRIM(ticker)) = ?", (tk,))
        conn.commit()
        return int(cur.rowcount)


def delete_ticker_corr_matrix_runs_containing_ticker(ticker: str) -> int:
    """
    Vymaže záznamy v ``ticker_corr_matrix_runs``, v ktorých je tento symbol v zozname tickerov
    (uložené korelačné matice môžu ostať v DB aj po vymazaní historických snímok).
    Vráti počet zmazaných riadkov.
    """
    tk = (ticker or "").strip().upper()
    if not tk:
        return 0
    n_del = 0
    with get_connection() as conn:
        rows = conn.execute("SELECT id, tickers_json FROM ticker_corr_matrix_runs").fetchall()
        for rid, tj in rows:
            if not tj or not str(tj).strip():
                continue
            try:
                arr = json.loads(tj)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(arr, (list, tuple)):
                continue
            ups = {str(x).strip().upper() for x in arr if (x is not None and str(x).strip())}
            if tk in ups:
                conn.execute("DELETE FROM ticker_corr_matrix_runs WHERE id = ?", (int(rid),))
                n_del += 1
        conn.commit()
    return n_del


def insert_ticker_corr_matrix_run(
    title: str,
    tickers: list[str],
    matrix: list[list[Optional[float]]],
    *,
    max_days: int,
    method: str = "pearson",
    return_kind: str = "log",
    n_obs: Optional[list[list[Optional[int]]]] = None,
) -> int:
    if not tickers or len(matrix) != len(tickers):
        raise ValueError("Neplatná matica alebo zoznam tickerov.")
    if n_obs is not None and len(n_obs) != len(tickers):
        raise ValueError("n_obs musí mať rovnaký rozmer ako matica.")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tjson = json.dumps([str(t).strip().upper() for t in tickers], ensure_ascii=False)
    mjson = json.dumps(matrix, ensure_ascii=False)
    njson = json.dumps(n_obs, ensure_ascii=False) if n_obs else None
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO ticker_corr_matrix_runs
            (created_at, title, max_days, method, return_kind, tickers_json, matrix_json, n_obs_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                (title or "").strip() or None,
                int(max_days),
                (method or "pearson").strip().lower(),
                (return_kind or "log").strip().lower(),
                tjson,
                mjson,
                njson,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_ticker_corr_matrix_runs(limit: int = 50) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 200))
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, title, max_days, method, return_kind, tickers_json, matrix_json, n_obs_json
            FROM ticker_corr_matrix_runs
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (lim,),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["tickers"] = json.loads(d.pop("tickers_json"))
        except (json.JSONDecodeError, TypeError):
            d["tickers"] = []
        try:
            d["matrix"] = json.loads(d.pop("matrix_json"))
        except (json.JSONDecodeError, TypeError):
            d["matrix"] = []
        raw_n = d.pop("n_obs_json")
        try:
            d["n_obs"] = json.loads(raw_n) if raw_n else None
        except (json.JSONDecodeError, TypeError):
            d["n_obs"] = None
        out.append(d)
    return out


def get_ticker_corr_matrix_run(run_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute(
            """
            SELECT id, created_at, title, max_days, method, return_kind, tickers_json, matrix_json, n_obs_json
            FROM ticker_corr_matrix_runs
            WHERE id = ?
            """,
            (int(run_id),),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["tickers"] = json.loads(d.pop("tickers_json"))
    except (json.JSONDecodeError, TypeError):
        d["tickers"] = []
    try:
        d["matrix"] = json.loads(d.pop("matrix_json"))
    except (json.JSONDecodeError, TypeError):
        d["matrix"] = []
    raw_n = d.pop("n_obs_json")
    try:
        d["n_obs"] = json.loads(raw_n) if raw_n else None
    except (json.JSONDecodeError, TypeError):
        d["n_obs"] = None
    return d


def delete_ticker_corr_matrix_run(run_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM ticker_corr_matrix_runs WHERE id = ?", (int(run_id),))
        conn.commit()
        return int(cur.rowcount)


def _tc_step_index(v: Any) -> Optional[int]:
    if v is None:
        return None
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None
    return i if i > 0 else None


_ALLOWED_COND_UNDER_CMP = frozenset({"gt", "lt", "gte", "lte"})
_ALLOWED_COND_AFTER_FILL = frozenset({"option", "underlying", "option_or_underlying", "custom"})
_ALLOWED_TRIGGER_KIND = frozenset({"manual", "short_leg_assignment"})
_ALLOWED_CLOSE_SEC_TYPE = frozenset({"STK", "OPT"})
_ALLOWED_CLOSE_RIGHT = frozenset({"C", "P"})


def _tc_cond_under_cmp(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").strip().lower()
    return s if s in _ALLOWED_COND_UNDER_CMP else None


def _tc_cond_after_fill(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").strip().lower()
    if not s:
        return None
    return s if s in _ALLOWED_COND_AFTER_FILL else None


def _tc_trigger_kind(raw: Optional[str]) -> str:
    s = (raw or "").strip().lower()
    if not s:
        return "manual"
    if s not in _ALLOWED_TRIGGER_KIND:
        raise ValueError(f"Neplatný trigger_kind: {raw!r}. Povolené: {sorted(_ALLOWED_TRIGGER_KIND)}.")
    return s


def _tc_close_sec_type(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").strip().upper()
    if not s:
        return None
    if s not in _ALLOWED_CLOSE_SEC_TYPE:
        raise ValueError(f"Neplatný close_sec_type: {raw!r}. Povolené: {sorted(_ALLOWED_CLOSE_SEC_TYPE)}.")
    return s


def _tc_close_right(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").strip().upper()[:1]
    if not s:
        return None
    if s not in _ALLOWED_CLOSE_RIGHT:
        raise ValueError(f"Neplatný close_right: {raw!r}. Povolené: C, P.")
    return s


def _tc_close_expiry_yyyymmdd(raw: Optional[str]) -> Optional[str]:
    """Normalizuje na YYYYMMDD alebo None."""
    if raw is None:
        return None
    s = str(raw).strip().replace("-", "")
    if not s:
        return None
    if len(s) >= 8 and s[:8].isdigit():
        return s[:8]
    raise ValueError(f"Neplatná expirácia (očakávam YYYYMMDD): {raw!r}.")


def _tc_linked_trade_id(raw: Any) -> Optional[int]:
    if raw is None:
        return None
    try:
        i = int(raw)
    except (TypeError, ValueError):
        return None
    return i if i > 0 else None


def _tc_assignment_watch_trade_id(raw: Any) -> Optional[int]:
    tid = _tc_linked_trade_id(raw)
    if tid is None:
        return None
    t = get_trade_by_id(tid)
    if t is None:
        raise ValueError(f"Obchod assignment_watch_trade_id={tid} neexistuje.")
    if str(t.get("leg_type") or "").strip() != "Short":
        raise ValueError(
            "Sledovaná noha musí byť **Short** v denníku (tá blokuje uzavretie longu, kým je v účte otvorená)."
        )
    return tid


def record_trading_command_assignment_check(cmd_id: int, summary: str) -> int:
    """Zapíše čas a text poslednej kontroly short nohy voči IB."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    sum_clean = (summary or "").strip() or None
    with get_connection() as conn:
        cur = conn.execute(
            """
            UPDATE trading_commands SET
                assignment_check_at = ?,
                assignment_check_summary = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (now, sum_clean, now, int(cmd_id)),
        )
        conn.commit()
        return int(cur.rowcount)


def _normalize_tc_close_fields(
    *,
    close_sec_type: Optional[str],
    close_expiry: Optional[str],
    close_strike: Any,
    close_right: Optional[str],
) -> tuple[Optional[str], Optional[str], Optional[float], Optional[str]]:
    """Vráti (close_sec_type, close_expiry, close_strike, close_right) pre zápis do DB."""
    cst = _tc_close_sec_type(close_sec_type)
    cex: Optional[str] = None
    csr: Optional[str] = None
    cstk: Optional[float] = None
    if cst == "OPT":
        cex = _tc_close_expiry_yyyymmdd(close_expiry)
        csr = _tc_close_right(close_right)
        if cex is None or csr is None:
            raise ValueError("Pre kontrakt OPT vyplň expiráciu (YYYYMMDD) a typ (Call/Put).")
        try:
            cstk = float(close_strike) if close_strike is not None else None
        except (TypeError, ValueError) as e:
            raise ValueError("Neplatný strike pre OPT.") from e
        if cstk is None or cstk <= 0:
            raise ValueError("Pre kontrakt OPT vyplň strike väčší ako 0.")
    else:
        hx = str(close_expiry or "").strip()
        hr = str(close_right or "").strip()
        hs_raw = close_strike
        hs = 0.0
        try:
            if hs_raw is not None and str(hs_raw).strip() != "":
                hs = float(hs_raw)
        except (TypeError, ValueError) as e:
            raise ValueError("Neplatný strike — vyber typ OPT alebo vymaž strike.") from e
        if cst is None and (hx or hr or hs > 0):
            raise ValueError(
                "Vyber **Typ kontraktu na zatvorenie** „Opčný kontrakt (OPT)“, ak chceš vyplniť expiráciu/strike."
            )
    return cst, cex, cstk, csr


def insert_trading_command(
    title: str,
    *,
    ticker: Optional[str] = None,
    action: Optional[str] = None,
    order_kind: Optional[str] = None,
    quantity: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    body: Optional[str] = None,
    status: str = "draft",
    plan_group: Optional[str] = None,
    step_index: Optional[int] = None,
    tws_perm_id: Optional[str] = None,
    tws_order_id: Optional[str] = None,
    tws_manual_note: Optional[str] = None,
    cond_under_cmp: Optional[str] = None,
    cond_under_price: Optional[float] = None,
    cond_after_fill: Optional[str] = None,
    cond_detail: Optional[str] = None,
    trigger_kind: Optional[str] = None,
    close_sec_type: Optional[str] = None,
    close_expiry: Optional[str] = None,
    close_strike: Optional[float] = None,
    close_right: Optional[str] = None,
    linked_trade_id: Any = None,
    assignment_watch_trade_id: Any = None,
) -> int:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tit = (title or "").strip()
    if not tit:
        raise ValueError("Názov (title) je povinný.")
    st0 = (status or "draft").strip().lower()
    pg = (plan_group or "").strip() or None
    si = _tc_step_index(step_index)
    tperm = (tws_perm_id or "").strip() or None
    tord = (tws_order_id or "").strip() or None
    tnote = (tws_manual_note or "").strip() or None
    ccmp = _tc_cond_under_cmp(cond_under_cmp)
    cpx = cond_under_price if ccmp is not None else None
    caf = _tc_cond_after_fill(cond_after_fill)
    cdt = (cond_detail or "").strip() or None
    tk = _tc_trigger_kind(trigger_kind)
    cst, cex, cstk, csr = _normalize_tc_close_fields(
        close_sec_type=close_sec_type,
        close_expiry=close_expiry,
        close_strike=close_strike,
        close_right=close_right,
    )
    ltid = _tc_linked_trade_id(linked_trade_id)
    if ltid is not None:
        if get_trade_by_id(ltid) is None:
            raise ValueError(f"Obchod linked_trade_id={ltid} neexistuje.")
    awid = _tc_assignment_watch_trade_id(assignment_watch_trade_id)
    with get_connection() as conn:
        cur = conn.execute(
            """
            INSERT INTO trading_commands
            (created_at, updated_at, title, ticker, action, order_kind, quantity, limit_price, stop_price, body, status,
             plan_group, step_index, tws_perm_id, tws_order_id, tws_manual_note,
             cond_under_cmp, cond_under_price, cond_after_fill, cond_detail,
             trigger_kind, close_sec_type, close_expiry, close_strike, close_right, linked_trade_id,
             assignment_watch_trade_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                now,
                tit,
                (ticker or "").strip().upper() or None,
                (action or "").strip().lower() or None,
                (order_kind or "").strip().lower() or None,
                quantity,
                limit_price,
                stop_price,
                (body or "").strip() or None,
                st0,
                pg,
                si,
                tperm,
                tord,
                tnote,
                ccmp,
                cpx,
                caf,
                cdt,
                tk,
                cst,
                cex,
                cstk,
                csr,
                ltid,
                awid,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def _row_trading_command(r: sqlite3.Row) -> dict[str, Any]:
    return dict(r)


def list_trading_commands(
    *,
    status: Optional[str] = None,
    limit: int = 200,
    sort_by: str = "updated",
) -> list[dict[str, Any]]:
    lim = max(1, min(int(limit), 500))
    q = "SELECT * FROM trading_commands"
    args: list[Any] = []
    if status is not None and str(status).strip():
        q += " WHERE status = ?"
        args.append(str(status).strip().lower())
    sb = (sort_by or "updated").strip().lower()
    if sb == "plan":
        q += (
            " ORDER BY CASE WHEN IFNULL(TRIM(plan_group), '') = '' THEN 1 ELSE 0 END, "
            "UPPER(TRIM(plan_group)), COALESCE(step_index, 999999), updated_at DESC, id DESC"
        )
    else:
        q += " ORDER BY updated_at DESC, id DESC"
    q += " LIMIT ?"
    args.append(lim)
    with get_connection() as conn:
        rows = conn.execute(q, args).fetchall()
    return [_row_trading_command(r) for r in rows]


def get_trading_command(cmd_id: int) -> Optional[dict[str, Any]]:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trading_commands WHERE id = ?", (int(cmd_id),)).fetchone()
    return _row_trading_command(row) if row else None


def update_trading_command(
    cmd_id: int,
    *,
    title: Optional[str] = None,
    ticker: Optional[str] = None,
    action: Optional[str] = None,
    order_kind: Optional[str] = None,
    quantity: Optional[float] = None,
    limit_price: Optional[float] = None,
    stop_price: Optional[float] = None,
    body: Optional[str] = None,
    status: Optional[str] = None,
    plan_group: Optional[str] = None,
    step_index: Optional[int] = None,
    tws_perm_id: Optional[str] = None,
    tws_order_id: Optional[str] = None,
    tws_manual_note: Optional[str] = None,
    cond_under_cmp: Optional[str] = None,
    cond_under_price: Optional[float] = None,
    cond_after_fill: Optional[str] = None,
    cond_detail: Optional[str] = None,
    trigger_kind: Optional[str] = None,
    close_sec_type: Optional[str] = None,
    close_expiry: Optional[str] = None,
    close_strike: Optional[float] = None,
    close_right: Optional[str] = None,
    linked_trade_id: Any = None,
    assignment_watch_trade_id: Any = None,
    _all_fields: bool = False,
) -> int:
    """
    Ak ``_all_fields`` je True, berú sa vždy poskytnuté kľúče (vrátane ``0.0`` / None na vyčistenie);
    inak sa menia len ne-``None`` argumenty (staršie správanie).
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cur0 = get_trading_command(cmd_id)
    if not cur0:
        return 0
    m: dict[str, Any] = {k: cur0.get(k) for k in cur0}
    if _all_fields:
        m["title"] = (title or "").strip() or m["title"]
        m["ticker"] = (ticker or "").strip().upper() or None
        m["action"] = (action or "").strip().lower() or None
        m["order_kind"] = (order_kind or "").strip().lower() or None
        m["quantity"] = quantity
        m["limit_price"] = limit_price
        m["stop_price"] = stop_price
        m["body"] = (body or "").strip() or None
        m["status"] = (status or "").strip().lower() or "draft"
        m["plan_group"] = (plan_group or "").strip() or None
        m["step_index"] = _tc_step_index(step_index)
        m["tws_perm_id"] = (tws_perm_id or "").strip() or None
        m["tws_order_id"] = (tws_order_id or "").strip() or None
        m["tws_manual_note"] = (tws_manual_note or "").strip() or None
        m["cond_under_cmp"] = _tc_cond_under_cmp(cond_under_cmp)
        m["cond_under_price"] = cond_under_price if m["cond_under_cmp"] else None
        m["cond_after_fill"] = _tc_cond_after_fill(cond_after_fill)
        m["cond_detail"] = (cond_detail or "").strip() or None
        m["trigger_kind"] = _tc_trigger_kind(trigger_kind)
        cst_u, cex_u, cstk_u, csr_u = _normalize_tc_close_fields(
            close_sec_type=close_sec_type,
            close_expiry=close_expiry,
            close_strike=close_strike,
            close_right=close_right,
        )
        m["close_sec_type"] = cst_u
        m["close_expiry"] = cex_u
        m["close_strike"] = cstk_u
        m["close_right"] = csr_u
        lt_u = _tc_linked_trade_id(linked_trade_id)
        if lt_u is not None and get_trade_by_id(lt_u) is None:
            raise ValueError(f"Obchod linked_trade_id={lt_u} neexistuje.")
        m["linked_trade_id"] = lt_u
        aw_u = _tc_assignment_watch_trade_id(assignment_watch_trade_id)
        m["assignment_watch_trade_id"] = aw_u
    else:
        if title is not None:
            m["title"] = (title or "").strip() or m["title"]
        if ticker is not None:
            m["ticker"] = (ticker or "").strip().upper() or None
        if action is not None:
            m["action"] = (action or "").strip().lower() or None
        if order_kind is not None:
            m["order_kind"] = (order_kind or "").strip().lower() or None
        if quantity is not None:
            m["quantity"] = quantity
        if limit_price is not None:
            m["limit_price"] = limit_price
        if stop_price is not None:
            m["stop_price"] = stop_price
        if body is not None:
            m["body"] = (body or "").strip() or None
        if status is not None:
            m["status"] = (status or "").strip().lower() or m.get("status", "draft")
        if plan_group is not None:
            m["plan_group"] = (plan_group or "").strip() or None
        if step_index is not None:
            m["step_index"] = _tc_step_index(step_index)
        if tws_perm_id is not None:
            m["tws_perm_id"] = (tws_perm_id or "").strip() or None
        if tws_order_id is not None:
            m["tws_order_id"] = (tws_order_id or "").strip() or None
        if tws_manual_note is not None:
            m["tws_manual_note"] = (tws_manual_note or "").strip() or None
        if cond_under_cmp is not None:
            m["cond_under_cmp"] = _tc_cond_under_cmp(cond_under_cmp)
            m["cond_under_price"] = cond_under_price if m["cond_under_cmp"] else None
        if cond_after_fill is not None:
            m["cond_after_fill"] = _tc_cond_after_fill(cond_after_fill)
        if cond_detail is not None:
            m["cond_detail"] = (cond_detail or "").strip() or None
        if trigger_kind is not None:
            m["trigger_kind"] = _tc_trigger_kind(trigger_kind)
        if close_sec_type is not None or close_expiry is not None or close_strike is not None or close_right is not None:
            cst_u, cex_u, cstk_u, csr_u = _normalize_tc_close_fields(
                close_sec_type=close_sec_type if close_sec_type is not None else m.get("close_sec_type"),
                close_expiry=close_expiry if close_expiry is not None else m.get("close_expiry"),
                close_strike=close_strike if close_strike is not None else m.get("close_strike"),
                close_right=close_right if close_right is not None else m.get("close_right"),
            )
            m["close_sec_type"] = cst_u
            m["close_expiry"] = cex_u
            m["close_strike"] = cstk_u
            m["close_right"] = csr_u
        if linked_trade_id is not None:
            lt_u = _tc_linked_trade_id(linked_trade_id)
            if lt_u is not None and get_trade_by_id(lt_u) is None:
                raise ValueError(f"Obchod linked_trade_id={lt_u} neexistuje.")
            m["linked_trade_id"] = lt_u
        if assignment_watch_trade_id is not None:
            aw_u = _tc_assignment_watch_trade_id(assignment_watch_trade_id)
            m["assignment_watch_trade_id"] = aw_u
    with get_connection() as conn:
        conn.execute(
            """
            UPDATE trading_commands SET
                updated_at = ?, title = ?, ticker = ?, action = ?, order_kind = ?,
                quantity = ?, limit_price = ?, stop_price = ?, body = ?, status = ?,
                plan_group = ?, step_index = ?, tws_perm_id = ?, tws_order_id = ?, tws_manual_note = ?,
                cond_under_cmp = ?, cond_under_price = ?, cond_after_fill = ?, cond_detail = ?,
                trigger_kind = ?, close_sec_type = ?, close_expiry = ?, close_strike = ?, close_right = ?, linked_trade_id = ?,
                assignment_watch_trade_id = ?
            WHERE id = ?
            """,
            (
                now,
                m["title"],
                m["ticker"],
                m["action"],
                m["order_kind"],
                m["quantity"],
                m["limit_price"],
                m["stop_price"],
                m["body"],
                m["status"],
                m.get("plan_group"),
                m.get("step_index"),
                m.get("tws_perm_id"),
                m.get("tws_order_id"),
                m.get("tws_manual_note"),
                m.get("cond_under_cmp"),
                m.get("cond_under_price"),
                m.get("cond_after_fill"),
                m.get("cond_detail"),
                m.get("trigger_kind") or "manual",
                m.get("close_sec_type"),
                m.get("close_expiry"),
                m.get("close_strike"),
                m.get("close_right"),
                m.get("linked_trade_id"),
                m.get("assignment_watch_trade_id"),
                int(cmd_id),
            ),
        )
        conn.commit()
        return 1


def delete_trading_command(cmd_id: int) -> int:
    with get_connection() as conn:
        cur = conn.execute("DELETE FROM trading_commands WHERE id = ?", (int(cmd_id),))
        conn.commit()
        return int(cur.rowcount)
