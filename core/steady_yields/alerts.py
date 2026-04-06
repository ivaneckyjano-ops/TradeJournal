"""
Upozornenia Steady Yields: podiel zisku na short prémií (cieľ napr. 50 % max.).
Čistá logika — persistencia v ``steady_yield_alert_events`` (DB).
"""
from __future__ import annotations

import json


def short_premium_profit_pct(
    entry_price_per_share: float,
    mark_price_per_share: float | None,
) -> float | None:
    """
    Short opcia: max. zisk z prémií = keď mark → 0 (zjednodušenie).
    Realizovaný zisk na kontrakt (prémiá): (entry - mark) za akciu.
    Vráti % max. zisku: 100 * (entry - mark) / entry, orezané na [0, 100].
    """
    ent = float(entry_price_per_share)
    if ent <= 0:
        return None
    if mark_price_per_share is None:
        return None
    mk = float(mark_price_per_share)
    profit = ent - mk
    if profit <= 0:
        return 0.0
    return round(min(100.0, max(0.0, (profit / ent) * 100.0)), 2)


def profit_target_message(
    *,
    ticker: str,
    strike: float,
    expiry: str,
    profit_pct: float,
    threshold_pct: float,
) -> str:
    return (
        f"Profit target ({ticker} short K{strike:g} {expiry}): "
        f"~{profit_pct:.0f} % max. zisku z prémií (prah {threshold_pct:.0f} %). "
        "Zváž zatvorenie / roll."
    )


def semafor_alert_detail(level: str, reasons: list[str]) -> str:
    return json.dumps({"level": level, "reasons": reasons}, ensure_ascii=False)
