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


# Spätná kompatibilita – niektoré moduly importujú s podčiarkovníkom
_bs_price  = bs_price
_calc_iv   = calc_iv
_bs_greeks = bs_greeks
