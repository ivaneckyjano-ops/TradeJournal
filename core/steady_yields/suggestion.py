"""
Heuristika „roll up and out“: ďalšia expirácia po aktuálnej + posun striku (Call hore, Put dolu).
Čistá logika — bez IBKR; chain dáta z ``fetch_secdef_option_params``.
"""
from __future__ import annotations


def _norm_exp(e: str) -> str:
    return str(e or "").strip().replace("-", "")


def next_expiry_after(expirations: list[str], current_expiry: str) -> str | None:
    """Prvá expirácia v zoradenom zozname striktne po ``current_expiry`` (YYYYMMDD)."""
    cur = _norm_exp(current_expiry)
    if len(cur) != 8 or not cur.isdigit():
        return None
    for e in sorted(_norm_exp(x) for x in (expirations or [])):
        if len(e) == 8 and e.isdigit() and e > cur:
            return e
    return None


def suggest_roll_strike_short(
    strikes: list[float],
    current_strike: float,
    right: str,
) -> float | None:
    """
    PMCC / short call: „up“ = vyšší strike. Short put: nižší strike.
    Vyberie najbližší dostupný strike v reťazi.
    """
    ss = sorted({float(x) for x in (strikes or [])})
    if not ss:
        return None
    r = (right or "C").upper()[:1]
    k0 = float(current_strike)
    if r == "C":
        for k in ss:
            if k > k0 + 1e-9:
                return k
        return ss[-1]
    for k in reversed(ss):
        if k < k0 - 1e-9:
            return k
    return ss[0]


def build_roll_up_and_out_suggestion(
    *,
    expirations: list[str],
    strikes: list[float],
    current_expiry: str,
    current_strike: float,
    right: str,
) -> dict:
    """
    Výstup pre UI / RollAdvice.suggested_contracts:
    ``next_expiry``, ``next_strike``, ``notes``.
    """
    ne = next_expiry_after(expirations, current_expiry)
    ns = suggest_roll_strike_short(strikes, current_strike, right)
    r = (right or "C").upper()[:1]
    notes: list[str] = []
    if ne:
        notes.append(f"Ďalšia expirácia po {_norm_exp(current_expiry)}: **{ne}**")
    else:
        notes.append("V reťazi nie je expirácia po aktuálnej — skús iný exchange / symbol.")
    if ns is not None:
        notes.append(f"Navrhovaný strike ({r} short roll): **{ns:g}**")
    else:
        notes.append("Nepodarilo sa vybrať strike z mriežky.")
    return {
        "next_expiry": ne,
        "next_strike": ns,
        "right": r,
        "suggested_contracts": [
            {"role": "close_short", "expiry": _norm_exp(current_expiry), "strike": float(current_strike), "right": r},
            {"role": "open_short", "expiry": ne, "strike": ns, "right": r},
        ]
        if ne and ns is not None
        else [],
        "notes": notes,
    }
