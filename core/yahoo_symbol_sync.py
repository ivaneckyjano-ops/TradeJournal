"""
Pravidelná obnova trhových a klasifikačných údajov cez Yahoo Finance (yfinance).

- Spot, názov, sektor, industry z ``Ticker.info`` / histórie.
- Implied volatility (IV %) z ATM callu na najbližšej expirácii s DTE ≥ 7 (ak existuje reťazec).
- IV rank: percentil aktuálneho IV oproti min/max z **histórie denných snapshotov** v DB
  (tabuľka ``symbol_market_snapshots``). Po niekoľkých dňoch/týždňoch behu približuje
  klasický „IV vs rozsah“; nie je to brokerovský IV Rank 1:1.
"""
from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

YAHOO_SYMBOL_SYNC_SETTING_KEY = "yahoo_symbols_last_sync"

YAHOO_SECTOR_TO_APP = {
    "Technology": "Technology",
    "Healthcare": "Healthcare",
    "Financial Services": "Financials",
    "Financials": "Financials",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Defensive": "Consumer Staples",
    "Consumer Staples": "Consumer Staples",
    "Communication Services": "Communication Services",
    "Industrials": "Industrials",
    "Basic Materials": "Materials",
    "Materials": "Materials",
    "Energy": "Energy",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
}


def yahoo_symbol_for_api(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    if "." in t and "/" not in t:
        return t.replace(".", "-")
    return t


def compute_iv_rank_from_history(current_iv_pct: float, past_iv_pcts: list[float]) -> float | None:
    past = [float(x) for x in past_iv_pcts if x is not None and float(x) > 0]
    if current_iv_pct is None or float(current_iv_pct) <= 0 or len(past) < 5:
        return None
    lo, hi = min(past), max(past)
    if hi <= lo:
        return None
    r = 100.0 * (float(current_iv_pct) - lo) / (hi - lo)
    return round(max(0.0, min(100.0, r)), 1)


def _atm_iv_pct_from_chain(t: Any) -> float | None:
    try:
        exps = list(t.options or [])
    except Exception:
        return None
    if not exps:
        return None
    today = date.today()
    spot: float | None = None
    try:
        fh = t.fast_info
        spot = fh.get("last_price") or fh.get("regular_market_previous_close")
    except Exception:
        pass
    if spot is None or float(spot) <= 0:
        try:
            hist = t.history(period="5d")
            if hist is not None and len(hist) > 0:
                spot = float(hist["Close"].iloc[-1])
        except Exception:
            return None
    if spot is None or float(spot) <= 0:
        return None
    spot = float(spot)
    chosen = None
    for exp in exps:
        try:
            exp_s = str(exp)[:10]
            dte = (datetime.strptime(exp_s, "%Y-%m-%d").date() - today).days
        except ValueError:
            continue
        if dte >= 7:
            chosen = exp
            break
    if not chosen and exps:
        chosen = exps[0]
    if not chosen:
        return None
    try:
        chain = t.option_chain(chosen)
        calls = chain.calls
        if calls is None or calls.empty:
            return None
        strikes = calls["strike"].astype(float)
        idx = int((strikes - spot).abs().idxmin())
        iv = float(calls.loc[idx, "impliedVolatility"])
        if iv <= 0 or iv != iv:
            return None
        return round(iv * 100.0, 2)
    except Exception:
        return None


def fetch_yahoo_symbol_row(ticker: str) -> dict[str, Any]:
    sym_api = yahoo_symbol_for_api(ticker)
    out: dict[str, Any] = {
        "ticker": (ticker or "").strip().upper(),
        "ok": False,
        "error": None,
        "company_name": None,
        "sector": None,
        "industry": None,
        "spot": None,
        "iv_pct": None,
    }
    try:
        import yfinance as yf
    except ImportError:
        out["error"] = "Chýba balík yfinance. Nainštaluj: pip install yfinance"
        return out
    try:
        t = yf.Ticker(sym_api)
        info = t.info or {}
        name = (info.get("longName") or info.get("shortName") or "").strip()
        if name:
            out["company_name"] = name[:200]
        ysec = (info.get("sector") or "").strip()
        if ysec:
            out["sector"] = YAHOO_SECTOR_TO_APP.get(ysec, "Iné")
        ind = (info.get("industry") or "").strip()
        if ind:
            out["industry"] = ind[:200]
        px = info.get("regularMarketPrice") or info.get("currentPrice")
        if px is not None and float(px) > 0:
            out["spot"] = round(float(px), 4)
        else:
            try:
                h = t.history(period="5d")
                if h is not None and len(h) > 0:
                    out["spot"] = round(float(h["Close"].iloc[-1]), 4)
            except Exception:
                pass
        out["iv_pct"] = _atm_iv_pct_from_chain(t)
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:400]
    return out


def sync_symbol_from_yahoo(symbol_row: dict, *, sleep_s: float = 0.0) -> dict[str, Any]:
    from core import database as db

    tid = str(symbol_row["ticker"]).strip().upper()
    sid = int(symbol_row["id"])
    if sleep_s > 0:
        time.sleep(sleep_s)
    row = fetch_yahoo_symbol_row(tid)
    report: dict[str, Any] = {
        "ticker": tid,
        "ok": bool(row.get("ok")),
        "error": row.get("error"),
        "iv_rank": None,
        "spot": row.get("spot"),
        "iv_pct": row.get("iv_pct"),
    }
    if not row.get("ok"):
        return report
    today = date.today().isoformat()
    db.upsert_symbol_market_snapshot(tid, today, iv_pct=row.get("iv_pct"), spot=row.get("spot"))
    past = db.get_symbol_iv_pct_history_before(tid, today)
    iv_rank = None
    if row.get("iv_pct") is not None:
        iv_rank = compute_iv_rank_from_history(float(row["iv_pct"]), past)
    report["iv_rank"] = iv_rank
    patch: dict[str, Any] = {"market_synced_at": datetime.now().replace(microsecond=0).isoformat()}
    if row.get("company_name"):
        patch["company_name"] = row["company_name"]
    if row.get("sector"):
        patch["sector"] = row["sector"]
    if row.get("industry"):
        patch["industry"] = row["industry"]
    if row.get("spot") is not None:
        patch["spot"] = row["spot"]
    if row.get("iv_pct") is not None:
        patch["iv_pct"] = row["iv_pct"]
    if iv_rank is not None:
        patch["iv_rank"] = iv_rank
    db.apply_symbol_yahoo_patch(sid, patch)
    return report


def sync_all_symbols_from_yahoo(
    tickers: list[str] | None = None,
    *,
    pause_s: float = 0.65,
) -> list[dict[str, Any]]:
    from core import database as db

    rows = db.get_symbols()
    if tickers:
        want = {str(t).strip().upper() for t in tickers if t and str(t).strip()}
        rows = [r for r in rows if str(r["ticker"]).strip().upper() in want]
    out: list[dict[str, Any]] = []
    for i, sym in enumerate(rows):
        pause = pause_s if i > 0 else 0.0
        out.append(sync_symbol_from_yahoo(sym, sleep_s=pause))
    if out:
        db.set_setting(
            YAHOO_SYMBOL_SYNC_SETTING_KEY,
            datetime.now().replace(microsecond=0).isoformat(),
        )
    return out


if __name__ == "__main__":
    import sys
    from pathlib import Path

    _root = Path(__file__).resolve().parents[1]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))

    from core import database as db

    db.init_db()
    only = [x.strip().upper() for x in sys.argv[1:] if x.strip()]
    rep = sync_all_symbols_from_yahoo(only if only else None, pause_s=0.65)
    ok = sum(1 for r in rep if r.get("ok"))
    print(f"Hotovo: {ok}/{len(rep)} OK")
    for r in rep:
        if not r.get("ok"):
            print(f"  × {r.get('ticker')}: {r.get('error')}")
        else:
            print(
                f"  ✓ {r.get('ticker')} spot={r.get('spot')} iv%={r.get('iv_pct')} iv_rank={r.get('iv_rank')}"
            )
