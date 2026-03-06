"""
Výpočty pravdepodobností a SD línií pre opcie.
"""
from __future__ import annotations

import numpy as np
from scipy.stats import norm
from dataclasses import dataclass
from typing import Optional


@dataclass
class SDLines:
    spot: float
    iv: float
    dte: int          # days to expiration
    sd_move: float
    upper_1sd: float
    lower_1sd: float
    upper_2sd: float
    lower_2sd: float
    prob_1sd: float = 0.6827
    prob_2sd: float = 0.9545


def calc_sd_lines(spot: float, iv: float, dte: int) -> SDLines:
    """
    Vypočíta 1SD a 2SD pohybové pásma na základe IV.
    SD = S * IV * sqrt(T / 365)
    """
    sd = spot * iv * np.sqrt(dte / 365.0)
    return SDLines(
        spot=spot,
        iv=iv,
        dte=dte,
        sd_move=sd,
        upper_1sd=spot + sd,
        lower_1sd=spot - sd,
        upper_2sd=spot + 2 * sd,
        lower_2sd=spot - 2 * sd,
    )


def bs_d1_d2(
    spot: float,
    strike: float,
    dte: int,
    iv: float,
    r: float = 0.05,
) -> tuple[Optional[float], Optional[float]]:
    """Black-Scholes d1 a d2."""
    T = dte / 365.0
    if T <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return None, None
    d1 = (np.log(spot / strike) + (r + 0.5 * iv ** 2) * T) / (iv * np.sqrt(T))
    d2 = d1 - iv * np.sqrt(T)
    return d1, d2


def pop_short_call(spot: float, strike: float, dte: int, iv: float, r: float = 0.05) -> Optional[float]:
    """PoP pre short call: pravdepodobnosť, že cena zostane pod strike."""
    _, d2 = bs_d1_d2(spot, strike, dte, iv, r)
    if d2 is None:
        return None
    return float(norm.cdf(-d2))


def pop_short_put(spot: float, strike: float, dte: int, iv: float, r: float = 0.05) -> Optional[float]:
    """PoP pre short put: pravdepodobnosť, že cena zostane nad strike."""
    _, d2 = bs_d1_d2(spot, strike, dte, iv, r)
    if d2 is None:
        return None
    return float(norm.cdf(d2))


def pop_long_call(spot: float, strike: float, dte: int, iv: float, r: float = 0.05) -> Optional[float]:
    """PoP pre long call: pravdepodobnosť, že cena bude nad strike."""
    _, d2 = bs_d1_d2(spot, strike, dte, iv, r)
    if d2 is None:
        return None
    return float(norm.cdf(d2))


def pop_long_put(spot: float, strike: float, dte: int, iv: float, r: float = 0.05) -> Optional[float]:
    """PoP pre long put: pravdepodobnosť, že cena bude pod strike."""
    _, d2 = bs_d1_d2(spot, strike, dte, iv, r)
    if d2 is None:
        return None
    return float(norm.cdf(-d2))


def pop_diagonal(
    spot: float,
    short_strike: float,
    short_dte: int,
    iv: float,
    spread_type: str = "call",  # 'call' alebo 'put'
    r: float = 0.05,
) -> Optional[float]:
    """
    PoP pre diagonal spread — aproximácia cez PoP short nohy.
    Call diagonal: zisk ak cena zostane pod short_strike pri expirácii short nohy.
    Put diagonal: zisk ak cena zostane nad short_strike.
    """
    if spread_type == "call":
        return pop_short_call(spot, short_strike, short_dte, iv, r)
    return pop_short_put(spot, short_strike, short_dte, iv, r)


def pop_strangle(
    spot: float,
    put_strike: float,
    call_strike: float,
    dte: int,
    iv: float,
    r: float = 0.05,
) -> Optional[float]:
    """PoP pre short strangle: cena zostane medzi oboma strikmi."""
    _, d2_call = bs_d1_d2(spot, call_strike, dte, iv, r)
    _, d2_put = bs_d1_d2(spot, put_strike, dte, iv, r)
    if d2_call is None or d2_put is None:
        return None
    p_above_call = norm.cdf(d2_call)   # P(S > call_strike)
    p_below_put = norm.cdf(-d2_put)    # P(S < put_strike)
    pop = 1 - p_above_call - p_below_put
    return float(max(0.0, pop))


def prob_touch(spot: float, target: float, dte: int, iv: float, r: float = 0.05) -> float:
    """
    Pravdepodobnosť dotyku cenové úrovne kedykoľvek pred expiraciou.
    Aproximácia: 2 * N(-|d2|)
    """
    _, d2 = bs_d1_d2(spot, target, dte, iv, r)
    if d2 is None:
        return 0.0
    return float(2 * norm.cdf(-abs(d2)))


def bs_price(spot: float, strike: float, dte: int, iv: float, right: str = "C", r: float = 0.05) -> Optional[float]:
    """Teoretická cena opcie podľa Black-Scholes."""
    d1, d2 = bs_d1_d2(spot, strike, dte, iv, r)
    if d1 is None:
        return None
    T = dte / 365.0
    if right.upper() in ("C", "CALL"):
        return float(spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2))
    else:
        return float(strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1))


def calc_iv_from_price(
    option_price: float,
    spot: float,
    strike: float,
    dte: int,
    right: str = "C",
    r: float = 0.05,
    tol: float = 1e-5,
    max_iter: int = 200,
) -> Optional[float]:
    """
    Vypočíta implicitnú volatilitu z ceny opcie pomocou bisekcie.
    Vráti IV (napr. 0.35 = 35%) alebo None ak sa nedá vypočítať.
    """
    if option_price <= 0 or dte <= 0 or spot <= 0 or strike <= 0:
        return None
    lo, hi = 1e-4, 10.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        price = bs_price(spot, strike, dte, mid, right, r)
        if price is None:
            return None
        if abs(price - option_price) < tol:
            return mid
        if price < option_price:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def calc_greeks(
    spot: float,
    strike: float,
    dte: int,
    iv: float,
    right: str = "C",
    r: float = 0.05,
) -> dict:
    """
    Vypočíta Delta, Gamma, Theta, Vega z BS modelu.
    Vráti dict s kľúčmi: delta, gamma, theta, vega (alebo None pri chybe).
    """
    d1, d2 = bs_d1_d2(spot, strike, dte, iv, r)
    if d1 is None:
        return {"delta": None, "gamma": None, "theta": None, "vega": None}
    T = dte / 365.0
    sqrt_T = np.sqrt(T)
    pdf_d1 = norm.pdf(d1)
    gamma = float(pdf_d1 / (spot * iv * sqrt_T))
    vega = float(spot * pdf_d1 * sqrt_T / 100)  # per 1% IV change
    if right.upper() in ("C", "CALL"):
        delta = float(norm.cdf(d1))
        theta = float(
            (-spot * pdf_d1 * iv / (2 * sqrt_T) - r * strike * np.exp(-r * T) * norm.cdf(d2)) / 365
        )
    else:
        delta = float(norm.cdf(d1) - 1)
        theta = float(
            (-spot * pdf_d1 * iv / (2 * sqrt_T) + r * strike * np.exp(-r * T) * norm.cdf(-d2)) / 365
        )
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def lognormal_prices(spot: float, iv: float, dte: int, r: float = 0.05, n: int = 300) -> tuple[np.ndarray, np.ndarray]:
    """
    Vráti (ceny, pravdepodobnostné hustoty) log-normálnej distribúcie
    pre vizualizáciu bell curve.
    """
    T = dte / 365.0
    mu = np.log(spot) + (r - 0.5 * iv ** 2) * T
    sigma = iv * np.sqrt(T)
    lo = spot * np.exp(-4 * sigma)
    hi = spot * np.exp(4 * sigma)
    prices = np.linspace(lo, hi, n)
    densities = norm.pdf(np.log(prices), loc=mu, scale=sigma) / prices
    return prices, densities
