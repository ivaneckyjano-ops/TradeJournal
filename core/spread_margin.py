"""
Orientačný odhad počiatočnej / udržiavacej marže pre opčné spready (Reg T štýl, zjednodušené).

Nie je to presná hodnota IB Portfolio Margin — pre presné číslo použi what-if v TWS alebo API.
"""
from __future__ import annotations

from typing import Any, Optional


def _net_premium_flow_usd(legs: list[dict]) -> float:
    """Kladné = čistý kredit z prémií, záporné = čistý debet (platíš)."""
    s = 0.0
    for leg in legs:
        n = max(1, int(leg.get("contracts", 1) or 1))
        px = float(leg.get("entry_price", 0) or 0)
        if str(leg.get("leg_type")) == "Long":
            s -= px * n * 100
        else:
            s += px * n * 100
    return s


def _group_by_expiry(legs: list[dict]) -> dict[str, list[dict]]:
    g: dict[str, list[dict]] = {}
    for leg in legs:
        e = str(leg.get("expiry") or "")
        g.setdefault(e, []).append(leg)
    return g


def _vertical_credit_max_loss_usd(legs_pair: list[dict]) -> Optional[float]:
    """Dve nohy, rovnaká expirácia, rovnaký C/P, jedna Long jedna Short."""
    if len(legs_pair) != 2:
        return None
    a, b = legs_pair[0], legs_pair[1]
    if str(a.get("right")) != str(b.get("right")):
        return None
    if a.get("leg_type") == b.get("leg_type"):
        return None
    long_leg = a if a.get("leg_type") == "Long" else b
    short_leg = b if long_leg is a else a
    k_long = float(long_leg["strike"])
    k_short = float(short_leg["strike"])
    n = min(
        max(1, int(long_leg.get("contracts", 1) or 1)),
        max(1, int(short_leg.get("contracts", 1) or 1)),
    )
    width = abs(k_short - k_long)
    if width <= 0:
        return None
    # Kredit / debet len z týchto dvoch nôh
    sub = [long_leg, short_leg]
    credit = _net_premium_flow_usd(sub)
    # Max straty pri definovanom riziku: šírka * 100 * n - kredit (ak kredit)
    if str(a.get("right")) == "C":
        # Credit call spread: short nižší strike, long vyšší (bear call / kredit)
        if not (k_short < k_long):
            return None
    else:
        # Credit put spread: short vyšší strike, long nižší
        if not (k_short > k_long):
            return None
    max_loss = width * 100.0 * n - max(0.0, credit)
    return max(0.0, max_loss)


def estimate_spread_margin_usd(legs: list[dict]) -> dict[str, Any]:
    """
    Vráti odhad marže v USD a krátku poznámku.
    """
    if not legs:
        return {
            "initial_usd": None,
            "maintenance_usd": None,
            "note": "Žiadne nohy.",
            "method": "none",
        }

    shorts = [lg for lg in legs if str(lg.get("leg_type")) == "Short"]
    longs = [lg for lg in legs if str(lg.get("leg_type")) == "Long"]

    if not shorts:
        return {
            "initial_usd": 0.0,
            "maintenance_usd": 0.0,
            "note": "Iba long opcie — pri Reg T typicky žiadna dodatočná marža (okrem zaplateného debetu).",
            "method": "long_only",
        }

    by_exp = _group_by_expiry(legs)

    # Iron condor: 4 nohy, jedna expirácia, 2C + 2P
    if len(legs) == 4 and len(by_exp) == 1:
        rs = {str(x.get("right")) for x in legs}
        if rs == {"C", "P"}:
            calls = [x for x in legs if x.get("right") == "C"]
            puts = [x for x in legs if x.get("right") == "P"]
            if len(calls) == 2 and len(puts) == 2:
                mc = _vertical_credit_max_loss_usd(calls)
                mp = _vertical_credit_max_loss_usd(puts)
                if mc is not None and mp is not None:
                    # Konzervatívny odhad: väčšia z dvoch vertikál (často blízko IB pre Reg T)
                    mx = max(mc, mp)
                    return {
                        "initial_usd": mx,
                        "maintenance_usd": mx,
                        "note": "Železný kondor (odhad): max. z call / put vertikálu (šírka × 100 × n − kredit na stranu).",
                        "method": "iron_condor_max_vertical",
                    }

    # Jedna expirácia, dve nohy — vertikál
    if len(legs) == 2 and len(by_exp) == 1:
        ml = _vertical_credit_max_loss_usd(legs)
        if ml is not None:
            return {
                "initial_usd": ml,
                "maintenance_usd": ml,
                "note": "Kreditný vertikál (odhad): šírka strikov × 100 × min(kontrakty) − čistý kredit z dvojice.",
                "method": "credit_vertical",
            }
        a, b = legs[0], legs[1]
        if str(a.get("right")) == str(b.get("right")) and a.get("leg_type") != b.get("leg_type"):
            flow = _net_premium_flow_usd(legs)
            if flow < 0:
                prem = abs(flow)
                return {
                    "initial_usd": prem,
                    "maintenance_usd": prem,
                    "note": "Debetný vertikál (hrubý odhad): max. strata často blízko zaplatenému debetu (prémie).",
                    "method": "debit_vertical_premium",
                }

    # Kalendár: 2 nohy, rôzna expirácia, rovnaký strike a right
    if len(legs) == 2 and len(by_exp) == 2:
        a, b = legs[0], legs[1]
        if (
            str(a.get("right")) == str(b.get("right"))
            and float(a.get("strike", 0)) == float(b.get("strike", 0))
            and str(a.get("expiry")) != str(b.get("expiry"))
            and a.get("leg_type") != b.get("leg_type")
        ):
            flow = _net_premium_flow_usd(legs)
            if flow < 0:
                return {
                    "initial_usd": abs(flow),
                    "maintenance_usd": abs(flow),
                    "note": "Debit kalendár (hrubý odhad): často blízko zaplatenému debetu; presná marža závisí od IB (time spread).",
                    "method": "calendar_debit_guess",
                }
            return {
                "initial_usd": None,
                "maintenance_usd": None,
                "note": "Kalendár s kreditom / časový spread — presnú maržu zadaj z TWS what-if alebo IB.",
                "method": "calendar_unknown",
            }

    return {
        "initial_usd": None,
        "maintenance_usd": None,
        "note": "Neznáma štruktúra alebo naked short — použij IB what-if alebo Margin Impact v TWS.",
        "method": "unknown",
    }
