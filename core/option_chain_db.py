"""
Samostatná SQLite databáza reťazcov opcií — jeden súbor na ticker pod data/option_chains/.
Nie je viazaná na Streamlit; vhodné na import z Barchart CSV a neskoršiu analýzu (diagonály atď.).
"""

from __future__ import annotations

import io
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

import pandas as pd

OPTION_CHAINS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "data", "option_chains"
)

# Koniec názvu: -MM-DD-YYYY.csv (dátum snímky / stiahnutia)
_SNAPSHOT_SUFFIX = re.compile(
    r"-(?P<mm>\d{1,2})-(?P<dd>\d{1,2})-(?P<yyyy>\d{4})\.csv$", re.IGNORECASE
)
_PREFIX_META = re.compile(
    r"^(?P<ticker>[a-z0-9]+)-(?P<kind>options-exp|volatility-greeks-exp)-(?P<expiry>\d{4}-\d{2}-\d{2})-",
    re.IGNORECASE,
)


def db_path_for_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if not t or not re.fullmatch(r"[A-Z][A-Z0-9.\-]*", t):
        raise ValueError(f"Neplatný ticker: {ticker!r}")
    os.makedirs(OPTION_CHAINS_DIR, exist_ok=True)
    return os.path.join(OPTION_CHAINS_DIR, f"{t}.db")


def get_connection(ticker: str) -> sqlite3.Connection:
    path = db_path_for_ticker(ticker)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@dataclass(frozen=True)
class ParsedFilename:
    ticker: str
    kind: str  # "options" | "greeks"
    expiry: str  # YYYY-MM-DD
    as_of_date: str  # YYYY-MM-DD snímka z konca názvu


def parse_barchart_option_filename(filename: str) -> Optional[ParsedFilename]:
    """
    Rozparsuje typické názvy:
    amzn-options-exp-2026-05-15-...-04-16-2026.csv
    amzn-volatility-greeks-exp-2026-05-15-...-04-16-2026.csv
    """
    base = os.path.basename(filename)
    ms = _SNAPSHOT_SUFFIX.search(base)
    if not ms:
        return None
    yyyy, mm, dd = ms.group("yyyy"), ms.group("mm"), ms.group("dd")
    as_of = f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    prefix = base[: ms.start()]
    mp = _PREFIX_META.match(prefix)
    if not mp:
        return None
    raw_kind = mp.group("kind").lower()
    if raw_kind == "options-exp":
        kind = "options"
    elif raw_kind == "volatility-greeks-exp":
        kind = "greeks"
    else:
        return None
    return ParsedFilename(
        ticker=mp.group("ticker").upper(),
        kind=kind,
        expiry=mp.group("expiry"),
        as_of_date=as_of,
    )


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS option_rows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            expiry TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            strike REAL NOT NULL,
            option_type TEXT NOT NULL,
            bid REAL,
            mid REAL,
            ask REAL,
            last_price REAL,
            moneyness_pct REAL,
            iv REAL,
            delta REAL,
            gamma REAL,
            theta REAL,
            vega REAL,
            rho REAL,
            theor REAL,
            volume INTEGER,
            open_interest INTEGER,
            vol_oi_ratio REAL,
            itm_prob REAL,
            source_options_csv TEXT,
            source_greeks_csv TEXT,
            imported_at TEXT DEFAULT (datetime('now')),
            UNIQUE(expiry, as_of_date, strike, option_type)
        );
        CREATE INDEX IF NOT EXISTS idx_option_rows_expiry
            ON option_rows(expiry, as_of_date);
        CREATE INDEX IF NOT EXISTS idx_option_rows_type
            ON option_rows(option_type, strike);
        """
    )
    conn.commit()


def _parse_float(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return None if math.isnan(v) else v
    s = str(value).strip().replace("\u00a0", " ").replace("%", "").replace(" ", "")
    if not s or s.lower() in ("-", "n/a", "na", "#n/a"):
        return None
    # Open interest style: 17,610 ako tisícky (jedna čiarka, tri číslice za ňou)
    if "," in s and "." not in s:
        parts = s.split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and len(parts[1]) == 3:
            try:
                return float(parts[0] + parts[1])
            except ValueError:
                pass
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        v = float(s)
        return None if math.isnan(v) else v
    except ValueError:
        return None


def _parse_int(value: Any) -> Optional[int]:
    f = _parse_float(value)
    if f is None:
        return None
    try:
        return int(round(f))
    except (ValueError, OverflowError):
        return None


def _norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).strip().lower()).strip("_")


def _canon_type(cell: Any) -> str:
    s = str(cell or "").strip().lower()
    if s in ("c", "call", "zavolajte"):
        return "Call"
    if s in ("p", "put", "putn"):
        return "Put"
    return str(cell or "").strip().title() or "Call"


def _read_barchart_csv(path: str) -> pd.DataFrame:
    return pd.read_csv(path, sep=None, engine="python", dtype=str, encoding_errors="replace")


def _read_barchart_csv_fileobj(file_obj: Any) -> pd.DataFrame:
    """Path alebo Streamlit UploadedFile / buffer — načítame text do StringIO (pd vyžaduje readline)."""
    if hasattr(file_obj, "read") and not isinstance(file_obj, (str, bytes)):
        try:
            file_obj.seek(0)
        except (OSError, AttributeError):
            pass
        raw = file_obj.read()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8", errors="replace")
        else:
            text = str(raw)
        return pd.read_csv(io.StringIO(text), sep=None, engine="python", dtype=str)
    return _read_barchart_csv(str(file_obj))


def _df_canonical(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [_norm_col(c) for c in out.columns]
    return out


def merge_options_and_greeks(
    df_options: pd.DataFrame,
    df_greeks: pd.DataFrame,
) -> pd.DataFrame:
    """
    Zlúči stacked options + volatility greeks podľa strike a typu (Call/Put).
    Ceny primárne z options, Gréky z greeks; ak chýba jedna strana, doplní sa z druhej kde je stĺpec.
    """
    o = _df_canonical(df_options)
    g = _df_canonical(df_greeks)
    if "strike" not in o.columns or "strike" not in g.columns:
        raise ValueError("V CSV musí byť stĺpec Strike.")
    o = o.copy()
    g = g.copy()
    o["_type"] = (
        o["type"].map(_canon_type) if "type" in o.columns else pd.Series("Call", index=o.index, dtype=object)
    )
    g["_type"] = (
        g["type"].map(_canon_type) if "type" in g.columns else pd.Series("Call", index=g.index, dtype=object)
    )
    o["_k"] = o["strike"].map(_parse_float)
    g["_k"] = g["strike"].map(_parse_float)
    o = o.dropna(subset=["_k"])
    g = g.dropna(subset=["_k"])

    merged = pd.merge(
        o,
        g,
        on=["_k", "_type"],
        how="outer",
        suffixes=("_opt", "_gk"),
    )
    nrow = len(merged)

    def first(*names: str) -> pd.Series:
        for name in names:
            if name in merged.columns:
                return merged[name]
        return pd.Series([None] * nrow, index=merged.index, dtype=object)

    row: dict[str, Any] = {
        "strike": merged["_k"],
        "option_type": merged["_type"],
        "bid": first("bid_opt", "bid_gk", "bid"),
        "mid": first("mid_opt", "mid_gk", "mid"),
        "ask": first("ask_opt", "ask_gk", "ask"),
        "last_price": first("latest_opt", "latest_gk", "latest"),
        "moneyness_pct": first("moneyness_opt", "moneyness_gk", "moneyness"),
        "theor": first("theor_gk", "theor_opt", "theor"),
        "iv": first("iv_gk", "iv_opt", "iv"),
        "delta": first("delta_gk", "delta_opt", "delta"),
        "gamma": first("gamma_gk", "gamma_opt", "gamma"),
        "theta": first("theta_gk", "theta_opt", "theta"),
        "vega": first("vega_gk", "vega_opt", "vega"),
        "rho": first("rho_gk", "rho_opt", "rho"),
        "volume": first("volume_opt", "volume_gk", "volume"),
        "open_interest": first("open_int_opt", "open_int_gk", "open_int"),
        "vol_oi_ratio": first("vol_oi_opt", "vol_oi_gk", "vol_oi"),
        "itm_prob": first("itm_prob_gk", "itm_prob_opt", "itm_prob"),
    }

    out_df = pd.DataFrame(row)
    return out_df


def _iv_to_fraction(v: Any) -> Optional[float]:
    x = _parse_float(v)
    if x is None:
        return None
    if x > 1.0:
        return x / 100.0
    return x


def _pct_col_to_fraction(s: pd.Series) -> pd.Series:
    return s.map(_iv_to_fraction)


def import_merged_dataframe(
    conn: sqlite3.Connection,
    *,
    expiry: str,
    as_of_date: str,
    merged: pd.DataFrame,
    source_options_csv: Optional[str] = None,
    source_greeks_csv: Optional[str] = None,
) -> int:
    init_schema(conn)
    iv_frac = (
        _pct_col_to_fraction(merged["iv"])
        if "iv" in merged.columns
        else pd.Series([None] * len(merged))
    )
    mny = merged["moneyness_pct"].map(_iv_to_fraction) if "moneyness_pct" in merged.columns else None
    itm = merged["itm_prob"].map(_iv_to_fraction) if "itm_prob" in merged.columns else None

    rows: list[tuple] = []
    for i in range(len(merged)):
        r = merged.iloc[i]
        k = _parse_float(r.get("strike"))
        if k is None:
            continue
        rows.append(
            (
                expiry,
                as_of_date,
                float(k),
                str(r["option_type"]),
                _parse_float(r.get("bid")),
                _parse_float(r.get("mid")),
                _parse_float(r.get("ask")),
                _parse_float(r.get("last_price")),
                float(mny.iloc[i]) if mny is not None and pd.notna(mny.iloc[i]) else None,
                float(iv_frac.iloc[i]) if iv_frac is not None and pd.notna(iv_frac.iloc[i]) else None,
                _parse_float(r.get("delta")),
                _parse_float(r.get("gamma")),
                _parse_float(r.get("theta")),
                _parse_float(r.get("vega")),
                _parse_float(r.get("rho")),
                _parse_float(r.get("theor")),
                _parse_int(r.get("volume")),
                _parse_int(r.get("open_interest")),
                _parse_float(r.get("vol_oi_ratio")),
                float(itm.iloc[i]) if itm is not None and pd.notna(itm.iloc[i]) else None,
                source_options_csv,
                source_greeks_csv,
            )
        )
    conn.executemany(
        """
        INSERT INTO option_rows (
            expiry, as_of_date, strike, option_type,
            bid, mid, ask, last_price, moneyness_pct, iv,
            delta, gamma, theta, vega, rho, theor,
            volume, open_interest, vol_oi_ratio, itm_prob,
            source_options_csv, source_greeks_csv
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(expiry, as_of_date, strike, option_type) DO UPDATE SET
            bid=COALESCE(excluded.bid, option_rows.bid),
            mid=COALESCE(excluded.mid, option_rows.mid),
            ask=COALESCE(excluded.ask, option_rows.ask),
            last_price=COALESCE(excluded.last_price, option_rows.last_price),
            moneyness_pct=COALESCE(excluded.moneyness_pct, option_rows.moneyness_pct),
            iv=COALESCE(excluded.iv, option_rows.iv),
            delta=COALESCE(excluded.delta, option_rows.delta),
            gamma=COALESCE(excluded.gamma, option_rows.gamma),
            theta=COALESCE(excluded.theta, option_rows.theta),
            vega=COALESCE(excluded.vega, option_rows.vega),
            rho=COALESCE(excluded.rho, option_rows.rho),
            theor=COALESCE(excluded.theor, option_rows.theor),
            volume=COALESCE(excluded.volume, option_rows.volume),
            open_interest=COALESCE(excluded.open_interest, option_rows.open_interest),
            vol_oi_ratio=COALESCE(excluded.vol_oi_ratio, option_rows.vol_oi_ratio),
            itm_prob=COALESCE(excluded.itm_prob, option_rows.itm_prob),
            source_options_csv=COALESCE(excluded.source_options_csv, option_rows.source_options_csv),
            source_greeks_csv=COALESCE(excluded.source_greeks_csv, option_rows.source_greeks_csv),
            imported_at=datetime('now')
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def _merge_options_greeks_frames(df_o: pd.DataFrame, df_g: pd.DataFrame) -> pd.DataFrame:
    if df_o.empty and df_g.empty:
        raise ValueError("Oba súbory sú prázdne.")
    if df_o.empty:
        merged = _df_canonical(df_g)
        merged["option_type"] = (
            merged["type"].map(_canon_type)
            if "type" in merged.columns
            else pd.Series("Call", index=merged.index, dtype=object)
        )
        merged = merged.rename(columns={"latest": "last_price"})
        for c in ("bid", "mid", "ask", "moneyness_pct"):
            if c not in merged.columns:
                merged[c] = None
        return merged
    if df_g.empty:
        merged = _df_canonical(df_o)
        merged["option_type"] = (
            merged["type"].map(_canon_type)
            if "type" in merged.columns
            else pd.Series("Call", index=merged.index, dtype=object)
        )
        merged = merged.rename(columns={"latest": "last_price"})
        for c in ("gamma", "theta", "vega", "rho", "theor"):
            merged[c] = None
        return merged
    return merge_options_and_greeks(df_o, df_g)


def _validate_import_metas(
    meta_o: Optional[ParsedFilename],
    meta_g: Optional[ParsedFilename],
    ticker: str,
) -> ParsedFilename:
    meta = meta_o or meta_g
    if not meta:
        raise ValueError(
            "Nepodarilo sa rozpoznať názov súboru (očakávaný vzor Barchart *-options-exp-* "
            "alebo *-volatility-greeks-exp-* s dátumom -MM-DD-YYYY pred .csv)."
        )
    if meta_o and meta_g:
        if meta_o.expiry != meta_g.expiry or meta_o.as_of_date != meta_g.as_of_date:
            raise ValueError(
                f"Expirácia alebo snímka sa nezhodujú: options {meta_o.expiry}/{meta_o.as_of_date} "
                f"vs greeks {meta_g.expiry}/{meta_g.as_of_date}"
            )
        if meta_o.ticker != meta_g.ticker:
            raise ValueError(f"Ticker v názvoch: {meta_o.ticker} vs {meta_g.ticker}")
    t = ticker.strip().upper()
    if meta.ticker != t:
        raise ValueError(f"Ticker musí sedieť s názvom súboru: očakávané **{meta.ticker}**, zadané **{t}**.")
    return meta


def import_pair_core(
    ticker: str,
    df_o: pd.DataFrame,
    df_g: pd.DataFrame,
    *,
    meta_o: Optional[ParsedFilename],
    meta_g: Optional[ParsedFilename],
    source_options: Optional[str],
    source_greeks: Optional[str],
) -> int:
    """Zlúči dátové rámce a zapíše do DB `ticker` (cesta k DB: `db_path_for_ticker`)."""
    meta = _validate_import_metas(meta_o, meta_g, ticker)
    merged = _merge_options_greeks_frames(df_o, df_g)
    conn = get_connection(ticker)
    try:
        return import_merged_dataframe(
            conn,
            expiry=meta.expiry,
            as_of_date=meta.as_of_date,
            merged=merged,
            source_options_csv=source_options,
            source_greeks_csv=source_greeks,
        )
    finally:
        conn.close()


def import_pair_from_paths(
    ticker: str,
    path_options: Optional[str],
    path_greeks: Optional[str],
) -> int:
    """
    Načíta 0–2 súbory, zlúči a zapíše do DB daného tickera.
    as_of a expiry berie z názvu (musia sedieť na oboch súboroch, ak sú oba).
    """
    meta_o = parse_barchart_option_filename(path_options) if path_options else None
    meta_g = parse_barchart_option_filename(path_greeks) if path_greeks else None
    meta = meta_o or meta_g
    if not meta:
        raise ValueError(
            "Nepodarilo sa rozpoznať názov súboru (očakávaný vzor Barchart *-options-exp-* alebo *-volatility-greeks-exp-*)."
        )
    df_o = _read_barchart_csv(path_options) if path_options else pd.DataFrame()
    df_g = _read_barchart_csv(path_greeks) if path_greeks else pd.DataFrame()
    return import_pair_core(
        ticker.strip().upper(),
        df_o,
        df_g,
        meta_o=meta_o,
        meta_g=meta_g,
        source_options=os.path.basename(path_options) if path_options else None,
        source_greeks=os.path.basename(path_greeks) if path_greeks else None,
    )


def import_pair_from_uploads(
    file_options: Optional[Any],
    file_greeks: Optional[Any],
) -> tuple[str, int]:
    """
    Import z Streamlit UploadedFile (alebo ľubovoľného objektu s .name a .read()).
    Vráti (ticker, počet_zapísaných_riadkov).
    """
    meta_o = parse_barchart_option_filename(getattr(file_options, "name", "") or "") if file_options else None
    meta_g = parse_barchart_option_filename(getattr(file_greeks, "name", "") or "") if file_greeks else None
    meta = meta_o or meta_g
    if not meta:
        raise ValueError("Z názvu uploadovaných súborov sa nepodarilo určiť ticker / exp / snímku.")
    df_o = _read_barchart_csv_fileobj(file_options) if file_options else pd.DataFrame()
    df_g = _read_barchart_csv_fileobj(file_greeks) if file_greeks else pd.DataFrame()
    n = import_pair_core(
        meta.ticker,
        df_o,
        df_g,
        meta_o=meta_o,
        meta_g=meta_g,
        source_options=os.path.basename(getattr(file_options, "name", "") or "") if file_options else None,
        source_greeks=os.path.basename(getattr(file_greeks, "name", "") or "") if file_greeks else None,
    )
    return meta.ticker, n


def list_chain_tickers() -> list[str]:
    """Tickery, pre ktoré existuje súbor data/option_chains/<T>.db."""
    if not os.path.isdir(OPTION_CHAINS_DIR):
        return []
    out: list[str] = []
    for fn in os.listdir(OPTION_CHAINS_DIR):
        if fn.endswith(".db") and len(fn) > 3:
            out.append(fn[:-3].upper())
    return sorted(set(out))


def list_distinct_snapshots(ticker: str) -> pd.DataFrame:
    """Dvojice (expiry, as_of_date) v DB tickera."""
    conn = get_connection(ticker)
    try:
        init_schema(conn)
        return pd.read_sql_query(
            "SELECT DISTINCT expiry, as_of_date FROM option_rows "
            "ORDER BY expiry DESC, as_of_date DESC",
            conn,
        )
    finally:
        conn.close()


def count_rows_for_snapshot(ticker: str, expiry: str, as_of_date: str) -> int:
    conn = get_connection(ticker)
    try:
        init_schema(conn)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM option_rows WHERE expiry = ? AND as_of_date = ?",
            (expiry, as_of_date),
        ).fetchone()
        return int(row["n"] if row else 0)
    finally:
        conn.close()


def list_snapshot_status(ticker: str) -> pd.DataFrame:
    """Prehľad skupín v DB spolu s počtom riadkov a prítomnosťou zdrojových CSV."""
    conn = get_connection(ticker)
    try:
        init_schema(conn)
        return pd.read_sql_query(
            """
            SELECT
                expiry,
                as_of_date,
                COUNT(*) AS rows,
                MAX(CASE WHEN source_options_csv IS NOT NULL THEN 1 ELSE 0 END) AS has_options,
                MAX(CASE WHEN source_greeks_csv IS NOT NULL THEN 1 ELSE 0 END) AS has_greeks,
                COUNT(CASE WHEN source_options_csv IS NOT NULL THEN 1 END) AS rows_with_options,
                COUNT(CASE WHEN source_greeks_csv IS NOT NULL THEN 1 END) AS rows_with_greeks
            FROM option_rows
            GROUP BY expiry, as_of_date
            ORDER BY expiry DESC, as_of_date DESC
            """,
            conn,
        )
    finally:
        conn.close()


def delete_snapshot(ticker: str, expiry: str, as_of_date: str) -> int:
    conn = get_connection(ticker)
    try:
        init_schema(conn)
        cur = conn.execute(
            "DELETE FROM option_rows WHERE expiry = ? AND as_of_date = ?",
            (expiry, as_of_date),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def delete_snapshot_side(ticker: str, expiry: str, as_of_date: str, side: str) -> int:
    """Zmaže len jednu stranu snapshotu podľa source CSV stĺpca."""
    side = side.strip().lower()
    if side not in {"options", "greeks"}:
        raise ValueError("side musí byť 'options' alebo 'greeks'")
    col = "source_options_csv" if side == "options" else "source_greeks_csv"
    conn = get_connection(ticker)
    try:
        init_schema(conn)
        cur = conn.execute(
            f"DELETE FROM option_rows WHERE expiry = ? AND as_of_date = ? AND {col} IS NOT NULL",
            (expiry, as_of_date),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


def import_snapshot_side_only(
    ticker: str,
    expiry: str,
    as_of_date: str,
    side: str,
    *,
    source_name: Optional[str] = None,
    df: Optional[pd.DataFrame] = None,
) -> int:
    """
    Importuje len jednu stranu snapshotu. Používa sa na doplnenie chýbajúcich dát.
    `df` môže byť už pripravený dátový rámec z uploadu.
    """
    side = side.strip().lower()
    if side not in {"options", "greeks"}:
        raise ValueError("side musí byť 'options' alebo 'greeks'")
    if df is None:
        raise ValueError("df je povinný pre import_snapshot_side_only")
    if df.empty:
        return 0
    merged = _merge_options_greeks_frames(df if side == "options" else pd.DataFrame(), df if side == "greeks" else pd.DataFrame())
    conn = get_connection(ticker)
    try:
        return import_merged_dataframe(
            conn,
            expiry=expiry,
            as_of_date=as_of_date,
            merged=merged,
            source_options_csv=source_name if side == "options" else None,
            source_greeks_csv=source_name if side == "greeks" else None,
        )
    finally:
        conn.close()


def read_chain(
    ticker: str,
    *,
    expiry: Optional[str] = None,
    as_of_date: Optional[str] = None,
) -> pd.DataFrame:
    """Načíta riadky z DB tickera (pre Jupyter / skripty)."""
    conn = get_connection(ticker)
    try:
        init_schema(conn)
        q = "SELECT * FROM option_rows WHERE 1=1"
        params: list[str] = []
        if expiry:
            q += " AND expiry = ?"
            params.append(expiry)
        if as_of_date:
            q += " AND as_of_date = ?"
            params.append(as_of_date)
        q += " ORDER BY expiry DESC, as_of_date DESC, strike"
        return pd.read_sql_query(q, conn, params=params)
    finally:
        conn.close()
