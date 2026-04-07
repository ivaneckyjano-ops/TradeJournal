"""
IBKR: bid/ask, IV, Theta, Gamma (a Vega) pre underlying zo zoznamu Symboly.

- **open_position** — skutočné opčné kontrakty z portfólia (OPT), underlying ∈ zoznam.
- **watched_only** — **call**: expirácia z IB reťazca zoradeného podľa DTE — ak najbližšia má DTE **> 21**, základ je index 0,
  inak základ je **ďalšia** expirácia (index 1); cieľová expirácia = základ **+ 3** v poradí (ďalej v čase). Strike = vždy
  **najbližší k spotu** (rovnaká logika bez ohľadu na DTE).
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone
from typing import Any

from core import database as db
from core import ibkr

REFRESH_INTERVAL_SEC = 3600


def _norm_opt_expiry(s: str) -> str:
    s = str(s or "").strip().split()[0].replace("-", "")
    return s[:8] if len(s) >= 8 else s


def _parse_ib_expiry(exp: str) -> date | None:
    s = str(exp or "").strip()[:10].replace("-", "")
    if len(s) < 8:
        return None
    s8 = s[:8]
    if not s8.isdigit():
        return None
    try:
        return date(int(s8[:4]), int(s8[4:6]), int(s8[6:8]))
    except ValueError:
        return None


def _pick_atm_call_from_chains(
    chains: list[dict],
    spot: float,
    *,
    near_term_days: int = 21,
    forward_steps: int = 3,
) -> tuple[str, float, str] | None:
    """
    Expirácie z MERGED reťazca zoradíme podľa DTE (budúce).

    - Ak DTE **najbližšej** expirácie je **> near_term_days** (predvolene 21), základ = index **0**.
    - Inak základ = **ďalšia** expirácia (index **1**), ak existuje.
    - Cieľ = základ + **forward_steps** (predvolene 3) v tom istom zoradení — vzdialenejší termín.
    - Strike: vždy **min |K − spot|** nad celým zoznamom strikov (nezávislé od zvolenej expirácie).
    """
    merged = None
    for c in chains or []:
        if (c.get("exchange") or "") == "MERGED":
            merged = c
            break
    if merged is None and chains:
        merged = chains[0]
    if merged is None:
        return None
    exps = list(merged.get("expirations") or [])
    strikes_f = sorted({float(x) for x in (merged.get("strikes") or [])})
    if not exps or not strikes_f:
        return None
    today = date.today()
    candidates: list[tuple[str, int]] = []
    for exp in exps:
        d = _parse_ib_expiry(exp)
        if d is None:
            continue
        dte = (d - today).days
        if dte < 1:
            continue
        candidates.append((exp, dte))
    if not candidates:
        return None

    sorted_c = sorted(candidates, key=lambda x: x[1])
    first_dte = sorted_c[0][1]
    if first_dte > int(near_term_days):
        base_idx = 0
    else:
        base_idx = 1 if len(sorted_c) > 1 else 0

    target_idx = min(base_idx + max(0, int(forward_steps)), len(sorted_c) - 1)
    chosen_exp, _chosen_dte = sorted_c[target_idx]

    s = float(spot)
    best_k = min(strikes_f, key=lambda k: abs(float(k) - s))
    return chosen_exp, float(best_k), "C"


def seconds_since_last_refresh() -> float | None:
    raw = (db.get_setting(db.SYMBOL_IB_OPTION_REFRESH_KEY, "") or "").strip()
    if not raw:
        return None
    try:
        t = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except Exception:
        return None


def run_symbol_ib_option_refresh(
    *,
    symbol_tickers: list[str] | None = None,
    pause_s: float = 0.2,
) -> dict[str, Any]:
    """
    Načíta metriky z IB a uloží jednu dávku (spoločný ``recorded_at``) do ``symbol_ib_option_snapshots``.
    """
    recorded_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out: dict[str, Any] = {
        "ok": True,
        "recorded_at": recorded_at,
        "errors": [],
        "open_rows": 0,
        "watched_rows": 0,
    }

    if not ibkr.is_connected():
        out["ok"] = False
        out["errors"].append("IBKR nie je pripojené.")
        return out

    sym_set = {str(t).strip().upper() for t in (symbol_tickers or db.get_symbol_tickers()) if t and str(t).strip()}
    if not sym_set:
        out["ok"] = False
        out["errors"].append("Žiadne symboly v zozname.")
        return out

    pos_res = ibkr.fetch_positions(with_greeks=False, use_mkt_snapshot=False)
    if pos_res.get("error"):
        out["errors"].append(str(pos_res["error"]))

    positions = pos_res.get("positions") or []
    open_underlying_with_opt: set[str] = set()
    open_option_tasks: list[tuple[str, str, float, str]] = []
    seen_open: set[tuple[str, str, float, str]] = set()

    for p in positions:
        if p.get("sec_type") != "OPT":
            continue
        u = str(p.get("ticker") or "").strip().upper()
        if u not in sym_set:
            continue
        if abs(float(p.get("contracts") or 0)) < 1e-9:
            continue
        exp = _norm_opt_expiry(str(p.get("expiry") or ""))
        strike = float(p.get("strike") or 0)
        if not exp or strike <= 0:
            continue
        right = "C" if str(p.get("option_type") or "").lower() == "call" else "P"
        open_underlying_with_opt.add(u)
        key = (u, exp, strike, right)
        if key in seen_open:
            continue
        seen_open.add(key)
        open_option_tasks.append(key)

    def _insert_from_metrics(
        u: str,
        category: str,
        exp: str,
        strike: float,
        right: str,
        m: dict,
    ) -> None:
        has_px = bool(m.get("bid") or m.get("ask") or m.get("last"))
        err = m.get("error") if not has_px else None
        db.insert_symbol_ib_option_snapshot(
            u,
            category,
            exp,
            strike,
            right,
            bid=m.get("bid"),
            ask=m.get("ask"),
            iv=m.get("iv"),
            theta=m.get("theta"),
            gamma=m.get("gamma"),
            vega=m.get("vega"),
            und_price=m.get("und_price"),
            recorded_at=recorded_at,
            error=err,
        )

    for u, exp, strike, right in open_option_tasks:
        time.sleep(max(0.0, float(pause_s)))
        m = ibkr.fetch_option_scan_metrics(u, exp, strike, right, timeout=12.0)
        _insert_from_metrics(u, "open_position", exp, strike, right, m)
        out["open_rows"] += 1
        if m.get("error") and not (m.get("bid") or m.get("ask") or m.get("last")):
            out["errors"].append(f"{u} otvorená {exp} K{strike:g}{right}: {m.get('error')}")

    for u in sorted(sym_set):
        if u in open_underlying_with_opt:
            continue
        time.sleep(max(0.0, float(pause_s)))
        und = ibkr.fetch_underlying(u, timeout=12.0)
        spot = und.get("price")
        if not spot:
            db.insert_symbol_ib_option_snapshot(
                u,
                "watched_only",
                "",
                0.0,
                "C",
                recorded_at=recorded_at,
                error=(und.get("error") or "Spot nedostupný"),
            )
            out["watched_rows"] += 1
            out["errors"].append(f"{u} sledovaný: {und.get('error') or 'spot —'}")
            continue

        time.sleep(max(0.0, float(pause_s)))
        ch = ibkr.fetch_secdef_option_params(u, timeout=14.0)
        if ch.get("error"):
            db.insert_symbol_ib_option_snapshot(
                u,
                "watched_only",
                "",
                0.0,
                "C",
                recorded_at=recorded_at,
                error=str(ch["error"]),
            )
            out["watched_rows"] += 1
            out["errors"].append(f"{u} reťazec: {ch['error']}")
            continue

        picked = _pick_atm_call_from_chains(ch.get("chains") or [], float(spot))
        if not picked:
            db.insert_symbol_ib_option_snapshot(
                u,
                "watched_only",
                "",
                0.0,
                "C",
                recorded_at=recorded_at,
                error="Žiadna platná expirácia/strike v reťazci",
            )
            out["watched_rows"] += 1
            out["errors"].append(f"{u}: prázdny reťazec opcií")
            continue

        exp, strike, right = picked
        time.sleep(max(0.0, float(pause_s)))
        m = ibkr.fetch_option_scan_metrics(u, exp, strike, right, timeout=12.0)
        _insert_from_metrics(u, "watched_only", exp, strike, right, m)
        out["watched_rows"] += 1
        if m.get("error") and not (m.get("bid") or m.get("ask") or m.get("last")):
            out["errors"].append(f"{u} ATM {exp} K{strike:g}{right}: {m.get('error')}")

    db.set_setting(db.SYMBOL_IB_OPTION_REFRESH_KEY, recorded_at)
    return out
