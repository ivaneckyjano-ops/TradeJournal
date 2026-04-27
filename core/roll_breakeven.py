"""
Bod (body) ceny podkladu, kde má viac-nohý spread podľa Black–Scholes zadané netto prémiové saldo.
Všetky vstupy (spot, K, T, IV, smer) musí zadať volajúci — žiadne trhové sťahovanie.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Sequence

from core.greeks import bs_price

Side = Literal["buy", "sell"]


@dataclass(frozen=True)
class ManualLeg:
    side: Side
    right: Literal["C", "P"]
    strike: float
    t_years: float
    iv: float
    contracts: int = 1


def _one_leg_contribution(spot: float, leg: ManualLeg, r: float) -> float:
    p = bs_price(float(spot), leg.strike, leg.t_years, leg.iv, leg.right, r)
    m = max(1, int(leg.contracts))
    sign = 1.0 if leg.side == "sell" else -1.0
    return sign * p * m


def net_premium(
    spot: float,
    legs: Sequence[ManualLeg],
    r: float = 0.045,
) -> float:
    """
    Súčet prémiových tokov (predaj = +, nákup = -) s cenami z BS v $/akciu, násobené `contracts` na nohe
    (rovnaká škála ako zobrazenie cien opcií v reťazci; násobok 100 $/kontrakt až pri prevode na hotovosť).
    """
    return sum(_one_leg_contribution(float(spot), leg, r) for leg in legs)


def _normalize_iv(iv_input: float, as_percent: bool) -> float:
    if as_percent and iv_input > 1.5:
        return iv_input / 100.0
    return float(iv_input)


def leg_from_dict(d: dict[str, Any], *, iv_as_percent: bool = True) -> ManualLeg:
    iv = _normalize_iv(float(d["iv"]), iv_as_percent)
    c = int(d.get("contracts", 1) or 1)
    s_raw = str(d.get("side", "buy")).lower()
    side: Side = "sell" if s_raw in ("sell", "s", "predaj") else "buy"
    r0 = str(d.get("right", "C")).upper()
    right: Literal["C", "P"] = "P" if r0.startswith("P") else "C"
    return ManualLeg(
        side=side,
        right=right,
        strike=float(d["strike"]),
        t_years=float(d["t_years"]),
        iv=iv,
        contracts=max(1, c),
    )


def find_spot_brackets(
    f,
    s_min: float,
    s_max: float,
    n_points: int = 400,
) -> list[tuple[float, float]]:
    """
    Nájde intervaly [a,b] kde f(a)*f(b) <= 0 (s hrubou mriežkou), pre hľadanie koreňov.
    """
    if s_max <= s_min or n_points < 2:
        return []
    step = (s_max - s_min) / float(n_points - 1)
    xs = [s_min + i * step for i in range(n_points)]
    out: list[tuple[float, float]] = []
    prev_x, prev_y = xs[0], f(xs[0])
    for x in xs[1:]:
        y = f(x)
        if prev_y == 0:
            out.append((prev_x, prev_x))
        elif y == 0:
            out.append((x, x))
        elif prev_y * y < 0:
            out.append((prev_x, x))
        elif prev_y * y == 0:
            out.append((prev_x, x))
        prev_x, prev_y = x, y
    return out


def breakeven_spots(
    legs: Sequence[ManualLeg],
    r: float = 0.045,
    target_net: float = 0.0,
    s_min: float = 1.0,
    s_max: float = 2000.0,
    *,
    n_scan: int = 500,
) -> list[float]:
    """
    Vráti ceny spotu, kde net(S) = target_net (BS, fixné IV na nohách),
    s použitím Brent metódy na každom nájdenom rozpätí.
    """
    if not legs:
        return []

    def g(spot: float) -> float:
        return net_premium(float(spot), legs, r) - float(target_net)

    from scipy.optimize import brentq

    roots: list[float] = []
    seen: set[str] = set()
    for a, b in find_spot_brackets(g, s_min, s_max, n_points=n_scan):
        try:
            z = brentq(g, a, b, xtol=1e-6, maxiter=80)
        except ValueError:
            continue
        key = f"{z:.4f}"
        if key in seen:
            continue
        seen.add(key)
        roots.append(float(z))
    roots.sort()
    return roots
