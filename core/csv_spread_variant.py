"""
CSV variant kalendára / diagonály → nohy pre Spread Builder + krátke slovné zhrnutie top 1.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from core.expiration_catalog import get_catalog_expiries
from core.probability import bs_price, calc_greeks

_LEG_DELTA = "leg_delta_usd"
_LEG_THETA = "leg_theta_per_day_usd"
_LEG_VEGA = "leg_vega_usd"
_LEG_GAMMA = "leg_gamma"


def norm_header(col: str) -> str:
    s = str(col).strip().lower().replace("~", "")
    return re.sub(r"\s+", "_", s)


def parse_number(value: Any) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return float("nan")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip().replace("\u00a0", " ").replace("%", "").replace(" ", "")
    if not s:
        return float("nan")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse_expiry_to_yyyymmdd(raw: Any) -> Optional[str]:
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("/", "-").replace(".", "-")
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        try:
            datetime.strptime(digits, "%Y%m%d")
            return digits
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            d = datetime.strptime(s.replace("/", "-"), fmt).date()
            return d.strftime("%Y%m%d")
        except ValueError:
            continue
    return None


def series_to_norm_dict(row: pd.Series) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in row.index:
        nk = norm_header(str(k))
        v = row[k]
        if pd.isna(v):
            out[nk] = ""
        else:
            out[nk] = str(v).strip()
    return out


def _dte_yyyymmdd(expiry_str: str) -> int:
    try:
        e = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
        return max(0, (e - date.today()).days)
    except Exception:
        return 0


def _infer_right(norm: dict[str, str]) -> str:
    for key in ("right", "cp", "call_put", "option_type", "typ", "type"):
        v = (norm.get(key) or "").strip().lower()
        if not v:
            continue
        if v in ("c", "call", "calls"):
            return "C"
        if v in ("p", "put", "puts"):
            return "P"
        if v.startswith("call"):
            return "C"
        if v.startswith("put"):
            return "P"
    return "C"


def _infer_strike(norm: dict[str, str]) -> Optional[float]:
    for key in (
        "leg1_strike",
        "leg_1_strike",
        "strike",
        "long_strike",
        "short_strike",
        "leg2_strike",
        "leg_2_strike",
        "k",
        "atm_strike",
        "strike_price",
    ):
        v = norm.get(key, "").strip()
        if not v:
            continue
        x = parse_number(v)
        if not np.isnan(x) and x > 0:
            return float(x)
    return None


def _pick_first_expiry(norm: dict[str, str], keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        cell = norm.get(k, "").strip()
        if not cell:
            continue
        y = parse_expiry_to_yyyymmdd(cell)
        if y:
            return y
    return None


def resolve_calendar_expiries(norm: dict[str, str]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Vráti (near, far, err). Near = skrátená noha (bližšia expirácia), far = dlhá.
    """
    e1 = _pick_first_expiry(
        norm,
        (
            "exp_leg1",
            "exp_leg_1",
            "expleg1",
            "exp1",
            "exp_short",
            "short_exp",
            "short_expiry",
            "near_exp",
            "front_exp",
            "predna_exp",
        ),
    )
    e2 = _pick_first_expiry(
        norm,
        (
            "exp_leg2",
            "exp_leg_2",
            "expleg2",
            "exp2",
            "exp_long",
            "long_exp",
            "long_expiry",
            "far_exp",
            "back_exp",
            "zadna_exp",
        ),
    )

    if e1 and e2:
        d1, d2 = _dte_yyyymmdd(e1), _dte_yyyymmdd(e2)
        if d1 <= d2:
            return e1, e2, None
        return e2, e1, None

    only = e1 or e2
    if not only:
        return None, None, "V riadku chýbajú expirácie (očakávam napr. **Exp Leg1** + **Exp Leg2** alebo jednu + katalóg)."

    long_exp = only
    exps = get_catalog_expiries(months=18)
    candidates: list[str] = []
    ld = _dte_yyyymmdd(long_exp)
    for e in exps:
        de = _dte_yyyymmdd(e)
        if 0 < de < ld and ld - de >= 7:
            candidates.append(e)
    if candidates:
        short_exp = max(candidates, key=_dte_yyyymmdd)
        return short_exp, long_exp, None

    try:
        ld_dt = datetime.strptime(long_exp, "%Y%m%d").date()
        sd_dt = ld_dt - timedelta(days=35)
        if sd_dt <= date.today():
            sd_dt = date.today() + timedelta(days=14)
        short_exp = sd_dt.strftime("%Y%m%d")
        return short_exp, long_exp, None
    except Exception:
        return None, None, "Nepodarilo sa dopočítať druhú expiráciu — doplň **Exp Leg1** a **Exp Leg2** v CSV."


def _set_leg_greeks(leg: dict, spot: float) -> None:
    iv = float(leg.get("iv") or 0.3)
    dte = max(1, _dte_yyyymmdd(str(leg.get("expiry") or "")))
    sign = -1 if leg.get("leg_type") == "Short" else 1
    n = int(leg.get("contracts", 1) or 1)
    if dte <= 0 or spot <= 0 or iv <= 0:
        leg[_LEG_DELTA] = leg[_LEG_THETA] = leg[_LEG_VEGA] = leg[_LEG_GAMMA] = 0.0
        return
    g = calc_greeks(spot, float(leg["strike"]), dte, iv, str(leg.get("right") or "C"))
    leg[_LEG_DELTA] = (g.get("delta") or 0) * sign * n * 100
    leg[_LEG_THETA] = (g.get("theta") or 0) * sign * n * 100
    leg[_LEG_VEGA] = (g.get("vega") or 0) * sign * n * 100
    leg[_LEG_GAMMA] = (g.get("gamma") or 0) * sign * n * 100


def _make_leg(
    leg_id: int,
    leg_type: str,
    right: str,
    strike: float,
    expiry: str,
    contracts: int,
    spot: float,
    iv: float,
) -> dict:
    dte = max(1, _dte_yyyymmdd(expiry))
    ep = bs_price(spot, strike, dte, iv, right)
    ep = round(max(0.01, ep or 0.5), 2)
    leg = {
        "id": leg_id,
        "leg_type": leg_type,
        "right": right,
        "strike": float(strike),
        "expiry": expiry,
        "contracts": int(contracts),
        "entry_price": ep,
        "iv": float(iv),
    }
    _set_leg_greeks(leg, spot)
    return leg


def calendar_legs_from_variant_row(
    row: pd.Series,
    *,
    spot: float,
    iv: float,
    contracts: int = 1,
) -> tuple[list[dict], Optional[str]]:
    norm = series_to_norm_dict(row)
    err: Optional[str]
    near, far, err = resolve_calendar_expiries(norm)
    if err or not near or not far:
        return [], err or "Chýbajú expirácie."

    strike = _infer_strike(norm)
    if strike is None or strike <= 0:
        return [], "V riadku chýba strike — skús stĺpec **Strike**, **Leg1 Strike** / **Leg2 Strike** alebo **K**."

    right = _infer_right(norm)
    sp = float(spot) if float(spot) > 0 else float(strike)
    iv_use = float(iv) if float(iv) > 0 else 0.30

    legs = [
        _make_leg(1, "Short", right, strike, near, contracts, sp, iv_use),
        _make_leg(2, "Long", right, strike, far, contracts, sp, iv_use),
    ]
    return legs, None


def csv_row_on_flag(norm: dict[str, str]) -> bool:
    for k in ("on", "active", "pick", "trade", "selected"):
        v = (norm.get(k) or "").strip().lower()
        if v in ("on", "yes", "áno", "ano", "1", "true", "x", "y", "✓", "ok"):
            return True
    return False


def verbally_assess_top1(
    top_row: pd.Series,
    full_ranked: pd.DataFrame,
    strategy: str,
) -> str:
    """Slovné zhrnutie prečo je prvý variant „on“ / na prvom mieste."""
    parts: list[str] = []
    norm = series_to_norm_dict(top_row)
    strat_labels = {
        "balanced": "Balanced (debit + skew + theta + delta)",
        "cheap": "Najnižší debit",
        "skew": "Najvyšší IV skew",
        "theta": "Najvyššia net theta",
    }
    sl = strat_labels.get(strategy, strategy)

    if csv_row_on_flag(norm):
        parts.append(
            "Stĺpec **On** (alebo ekvivalent) v CSV je pre tento riadok zapnutý — v tvojom výstupe to znamená „ber do úvahy / preferuj“ oproti riadkom bez On."
        )

    nd = top_row.get("_net_debit")
    try:
        debit_f = float(nd) if nd is not None and str(nd) != "" and not (isinstance(nd, float) and np.isnan(nd)) else float("nan")
    except (TypeError, ValueError):
        debit_f = float("nan")

    if full_ranked.empty:
        parts.append(f"**Top 1** podľa režimu *{sl}* — v dátach je len jeden použiteľný variant.")
        return " ".join(parts)

    if strategy == "cheap" and not np.isnan(debit_f):
        pool = pd.to_numeric(full_ranked["_net_debit"], errors="coerce").dropna()
        if len(pool) > 0:
            lo, hi = float(pool.min()), float(pool.max())
            parts.append(
                f"Medzi {len(pool)} variantmi má **najnižší Net debit** ({debit_f:g} $; v súbore od {lo:g} do {hi:g})."
            )
    elif strategy == "skew":
        parts.append(
            "Vyhráva **najvyšší IV skew** — predpoklad: lepší tvar volatility medzi nohami (krátka vs. dlhá expirácia) podľa tvojho exportu."
        )
    elif strategy == "theta":
        parts.append(
            "Vyhráva **najvyššia net theta** — variant s najsilnejším časovým rozpadom v prospech pozície podľa CSV."
        )
    elif strategy == "balanced":
        sc = top_row.get("_score")
        try:
            sc_f = float(sc) if sc is not None and str(sc) != "" else float("nan")
        except (TypeError, ValueError):
            sc_f = float("nan")
        if not np.isnan(sc_f):
            parts.append(f"Interné **balanced** skóre je **{sc_f:.4f}** (váhy: debit 40 %, skew 30 %, theta 20 %, |delta| 10 %).")
        if not np.isnan(debit_f):
            pool = pd.to_numeric(full_ranked["_net_debit"], errors="coerce").dropna()
            if len(pool) > 1:
                worse = (pool > debit_f).sum()
                parts.append(
                    f"Net debit **{debit_f:g}** $ je nižší než u **{worse}** z **{len(pool)}** variantov (nižší debit = lepší)."
                )
        parts.append(
            "Ide o kompromis: nečistí len jednu metriku, ale kombinuje lacnejší vstup, skew, theta a rozumnú vzdialenosť delty od nuly."
        )
    else:
        parts.append(f"Vybraný režim: **{sl}**.")

    parts.append("Po odoslaní do Spread Buildera skontroluj striky, expirácie a ceny oproti TWS.")
    return " ".join(parts)
