"""
APR, cost basis a efficiency z uložených roll udalostí a profilu skupiny.
Preferuj realizované čísla (net_premium, commission), nie teoretické ceny.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional


def _parse_iso_day(s: str | None) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def trades_for_group(trades: list[dict], group_id: str) -> list[dict]:
    gid = (group_id or "").strip()
    if not gid:
        return []
    return [t for t in trades if (t.get("group_id") or "").strip() == gid]


def leap_long_cost_usd(trades: list[dict]) -> float:
    """Debit zaplatený za Long nohy (PMCC LEAPS): súčet entry × kontrakty × 100."""
    s = 0.0
    for t in trades:
        if t.get("leg_type") != "Long":
            continue
        c = int(t.get("contracts") or 1)
        ep = float(t.get("entry_price") or 0)
        s += ep * c * 100
    return round(s, 2)


def short_roll_net_from_trades_closed(trades: list[dict]) -> float:
    """
    Orientačný realizovaný P&L z uzavretých Short nôh: (entry_short - exit) * mult - commission.
    Short: profit keď exit < entry (buy back cheaper).
    """
    total = 0.0
    for t in trades:
        if t.get("leg_type") != "Short" or t.get("status") != "Closed":
            continue
        c = int(t.get("contracts") or 1)
        mult = 100 * c
        ent = float(t.get("entry_price") or 0)
        ex = float(t.get("exit_price") or 0)
        comm = float(t.get("commission") or 0)
        total += (ent - ex) * mult - comm
    return round(total, 2)


def aggregate_roll_events_cash(events: list[dict]) -> dict[str, float]:
    """
    Z tabuľky roll_events: súčet net_premium a commission.
    Konvencia: net_premium = čistý hotovostný tok v prospech účtu pri udalosti (+ kredit).
    """
    gross = 0.0
    comm = 0.0
    for e in events:
        gross += float(e.get("net_premium") or 0)
        comm += float(e.get("commission") or 0)
    return {"net_premium_sum": round(gross, 2), "commission_sum": round(comm, 2), "net_after_comm": round(gross - comm, 2)}


def cost_basis_remaining(leap_initial: float, credits_to_leap: float) -> float:
    """Zostávajúci náklad LEAPS po „znížení“ inkasom z rollov (jednoduchý model)."""
    return round(max(0.0, float(leap_initial) - float(credits_to_leap)), 2)


def day_span_from_events(events: list[dict], fallback_days: int = 365) -> int:
    """Počet dní medzi najstaršou a najnovšou udalosťou (aspoň 1)."""
    days: list[date] = []
    for e in events:
        d = _parse_iso_day(e.get("occurred_at"))
        if d:
            days.append(d)
    if len(days) < 2:
        return max(1, fallback_days)
    span = (max(days) - min(days)).days
    return max(1, span)


def annualized_apr_pct(net_profit: float, capital_basis: float, days: int) -> Optional[float]:
    """
    Jednoduchá annualizácia: (zisk / báza) * (365 / dní) * 100.
    Vráti None ak báza <= 0.
    """
    if capital_basis <= 0 or days <= 0:
        return None
    return round((net_profit / capital_basis) * (365.0 / days) * 100.0, 2)


def efficiency_theta_delta(theta: float | None, delta: float | None) -> Optional[float]:
    """Pomer |theta| / max(|delta|, eps) — vyššie = viac časového decay na jednotku delty."""
    if theta is None or delta is None:
        return None
    d = abs(float(delta))
    if d < 1e-9:
        return None
    return round(abs(float(theta)) / d, 4)


def efficiency_credit_delta(net_credit: float | None, delta: float | None) -> Optional[float]:
    if net_credit is None or delta is None:
        return None
    d = abs(float(delta))
    if d < 1e-9:
        return None
    return round(float(net_credit) / d, 4)


def build_yield_summary(
    *,
    group_id: str,
    trades: list[dict],
    roll_events: list[dict],
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Zhrnutie pre UI: realizované toky, báza, APR, porovnanie s expected_apr_pct z profilu.
    """
    gt = trades_for_group(trades, group_id)
    ev_sorted = sorted(roll_events, key=lambda x: (x.get("occurred_at") or "", x.get("id") or 0))
    cash = aggregate_roll_events_cash(ev_sorted)
    leap_from_trades = leap_long_cost_usd(gt)
    leap_profile = float((profile or {}).get("leap_initial_cost") or 0)
    leap_basis = leap_profile if leap_profile > 0 else leap_from_trades
    credits_roll = cash["net_after_comm"]
    credits_trades = short_roll_net_from_trades_closed(gt)
    # Primárne manuálne roll_events; ak žiadne, fallback na uzavreté shorty z Trade Log
    if ev_sorted:
        total_credits = credits_roll
    else:
        total_credits = credits_trades
    remaining_basis = cost_basis_remaining(leap_basis, total_credits) if leap_basis > 0 else None
    days = day_span_from_events(ev_sorted, fallback_days=30)
    realized_apr = annualized_apr_pct(total_credits, leap_basis, days) if leap_basis > 0 else None
    expected = (profile or {}).get("expected_apr_pct")
    expected_f = float(expected) if expected is not None else None

    return {
        "group_id": group_id,
        "leap_basis_usd": leap_basis,
        "credits_from_roll_events_usd": credits_roll,
        "credits_from_closed_shorts_usd": credits_trades,
        "total_credits_used_usd": total_credits,
        "remaining_leap_basis_usd": remaining_basis,
        "span_days": days,
        "realized_apr_pct": realized_apr,
        "expected_apr_pct": expected_f,
        "apr_gap_pct": (round(realized_apr - expected_f, 2) if realized_apr is not None and expected_f is not None else None),
        "roll_event_count": len(ev_sorted),
    }
