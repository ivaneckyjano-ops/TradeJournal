"""
Black-Scholes Greeks a IV výpočty.

Samostatná vrstva bez závislostí na Streamlit alebo ib_insync.
Používajú: core/ibkr.py, core/portfolio_data.py.
"""
from __future__ import annotations

import math


def bs_price(S: float, K: float, T: float, iv: float, right: str, r: float = 0.045) -> float:
    """Black-Scholes cena opcie. right='C' alebo 'P'."""
    from scipy.stats import norm as _norm
    if T <= 0 or iv <= 0:
        return max(0.0, (S - K) if right == "C" else (K - S))
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrtT)
    d2 = d1 - iv * sqrtT
    if right == "C":
        return S * _norm.cdf(d1) - K * math.exp(-r * T) * _norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * _norm.cdf(-d2) - S * _norm.cdf(-d1)


def calc_iv(S: float, K: float, T: float, opt_price: float, right: str, r: float = 0.045) -> float | None:
    """
    Vypočíta implied volatility bisekčnou metódou.
    Vráti IV ako desatinné číslo (napr. 0.35 = 35%) alebo None ak sa nepodarí.
    """
    if T <= 0 or opt_price <= 0 or S <= 0 or K <= 0:
        return None
    low, high = 0.001, 5.0
    for _ in range(60):
        mid = (low + high) / 2
        price = bs_price(S, K, T, mid, right, r)
        if abs(price - opt_price) < 0.0001:
            return mid
        if price < opt_price:
            low = mid
        else:
            high = mid
    return mid if 0.01 < mid < 4.9 else None


def bs_greeks(S: float, K: float, T: float, iv: float, right: str, r: float = 0.045) -> dict:
    """
    Vypočíta delta, gamma, theta, vega z Black-Scholes.
    S=spot, K=strike, T=roky do expirácie, iv=impl. vol (napr. 0.35),
    right='C' alebo 'P'.
    Theta je denná (vydelená 365). Vega je na 1% zmenu IV.
    """
    try:
        from scipy.stats import norm as _norm
        if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
            return {}
        sqrtT = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrtT)
        d2 = d1 - iv * sqrtT
        nd1 = _norm.pdf(d1)
        if right == "C":
            delta = _norm.cdf(d1)
            theta = (-(S * nd1 * iv) / (2 * sqrtT) - r * K * math.exp(-r * T) * _norm.cdf(d2)) / 365
        else:
            delta = _norm.cdf(d1) - 1
            theta = (-(S * nd1 * iv) / (2 * sqrtT) + r * K * math.exp(-r * T) * _norm.cdf(-d2)) / 365
        gamma = nd1 / (S * iv * sqrtT)
        vega  = S * nd1 * sqrtT / 100
        return {
            "delta": round(delta, 4),
            "gamma": round(gamma, 6),
            "theta": round(theta, 4),
            "vega":  round(vega, 4),
            "iv":    round(iv, 4),
        }
    except Exception:
        return {}


def bs_delta_raw(S: float, K: float, T: float, iv: float, right: str, r: float = 0.045) -> float | None:
    """Delta opcie bez zaokrúhľovania. ``right`` je ``C`` / ``Call`` alebo ``P`` / ``Put``."""
    try:
        from scipy.stats import norm as _norm
        if T <= 0 or iv <= 0 or S <= 0 or K <= 0:
            return None
        sqrtT = math.sqrt(T)
        d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * sqrtT)
        ru = str(right).strip().upper()
        if ru in ("C", "CALL"):
            return float(_norm.cdf(d1))
        if ru in ("P", "PUT"):
            return float(_norm.cdf(d1) - 1.0)
        return None
    except Exception:
        return None


def spot_for_abs_delta_bs(
    strike: float,
    dte_days: int,
    iv: float,
    right: str,
    target_abs_delta: float,
    r: float = 0.045,
) -> float | None:
    """
    Orientačný spot ``S`` taký, že podľa BS platí ``|delta(S)| ≈ target_abs_delta``
    (konštantná IV, bez dividend).

    ``dte_days`` = kalendárne dni do expirácie (0 = expirácia dnes). Pri 0 použijeme
    na výpočet ``T`` minimálne **1 deň**, inak by bolo ``T=0`` a BS delta nemá zmysel.

    Ak ``dte_days < 0`` (už po expirácii), vráti ``None``.
    Ak cieľ nie je v (0, 1) alebo ho rozumný rozsah spotov nedosiahne, vráti ``None``.
    """
    try:
        from scipy.optimize import brentq
    except ImportError:
        return None

    if strike <= 0 or iv <= 0:
        return None
    if dte_days < 0:
        return None
    dte_eff = max(1, int(dte_days))
    T = dte_eff / 365.0
    tgt = float(target_abs_delta)
    if tgt <= 0 or tgt >= 1:
        return None
    tgt = min(max(tgt, 1e-9), 1.0 - 1e-9)

    ru = str(right).strip().upper()
    if ru not in ("C", "CALL", "P", "PUT"):
        return None
    rc = "C" if ru in ("C", "CALL") else "P"

    def g(spot: float) -> float:
        d = bs_delta_raw(spot, strike, T, iv, rc, r)
        if d is None:
            return float("nan")
        return abs(d) - tgt

    try:
        import numpy as np

        xs = np.geomspace(strike * 0.03, strike * 14.0, 140)
    except Exception:
        return None

    last_x = float(xs[0])
    last_g = g(last_x)
    if math.isnan(last_g):
        return None
    for i in range(1, len(xs)):
        x = float(xs[i])
        gv = g(x)
        if math.isnan(gv):
            continue
        if last_g == 0:
            return last_x
        if last_g * gv < 0:
            try:
                root = brentq(g, last_x, x, xtol=1e-7, rtol=1e-10)
                return float(root) if root > 0 else None
            except ValueError:
                return None
        last_x, last_g = x, gv
    return None


def iv_display_to_bs_fraction(raw: float | None) -> float | None:
    """
    IV zo vstupu používateľa: **percentá** (napr. 76,7) alebo **zlomok** (0,767).
    Ak je číslo > 2,5, berieme ako percentá a delíme 100.
    """
    if raw is None:
        return None
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    if x <= 0:
        return None
    if x > 2.5:
        x = x / 100.0
    if x <= 0 or x > 4.0:
        return None
    return x


# Spätná kompatibilita – niektoré moduly importujú s podčiarkovníkom
_bs_price  = bs_price
_calc_iv   = calc_iv
_bs_greeks = bs_greeks
