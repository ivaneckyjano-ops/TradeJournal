"""
Čistá logika skenera (bez IBKR): likvidita, IV rank, limity sektorov.
"""
from __future__ import annotations

from typing import Any


def spread_pct_of_mid(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None:
        return None
    b, a = float(bid), float(ask)
    if b <= 0 or a <= 0:
        return None
    mid = (a + b) / 2.0
    if mid <= 0:
        return None
    return round((a - b) / mid * 100.0, 3)


def liquidity_passes(
    *,
    open_interest: int | None,
    bid: float | None,
    ask: float | None,
    min_oi: int,
    max_spread_pct: float,
) -> tuple[bool, str]:
    if open_interest is not None and open_interest < min_oi:
        return False, f"OI {open_interest} < {min_oi}"
    sp = spread_pct_of_mid(bid, ask)
    if sp is None:
        return False, "Chýba bid/ask"
    if sp > max_spread_pct:
        return False, f"Spread {sp:.2f}% mid > {max_spread_pct}%"
    return True, "OK"


def iv_rank_passes(iv_rank: float | None, max_iv_rank: float) -> tuple[bool, str]:
    if iv_rank is None:
        return True, "IV rank neznámy — preskočený filter"
    if float(iv_rank) > max_iv_rank:
        return False, f"IV rank {iv_rank:.1f}% > {max_iv_rank}%"
    return True, "OK"


def apply_sector_caps(
    tickers_in_order: list[str],
    ticker_sector: dict[str, str],
    max_per_sector: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    Vráti (vybrané tickery v poradí), záznamy zamietnutých {ticker, reason}.
    """
    counts: dict[str, int] = {}
    selected: list[str] = []
    rejected: list[dict[str, Any]] = []
    for t in tickers_in_order:
        u = (t or "").strip().upper()
        if not u:
            continue
        sec = (ticker_sector.get(u) or "").strip() or "Neznámy"
        if sec == "Neznámy" or sec == "—":
            selected.append(u)
            continue
        c = counts.get(sec, 0)
        if c >= max_per_sector:
            rejected.append({"ticker": u, "reason": f"Sektor {sec}: už {max_per_sector} tickery"})
            continue
        counts[sec] = c + 1
        selected.append(u)
    return selected, rejected
