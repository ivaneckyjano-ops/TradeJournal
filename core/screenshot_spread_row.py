"""
Parsovanie jedného riadku spreadu z OCR textu (TWS / podobné tabuľky).

Cieľ: z jedného riadku typu
``151.07  05/15/26  157.50P  13.65  05/08/26  146.00P  4.05  $9.60 ...``
vytiahnuť spot, dve nohy (expirácia, strike, Call/Put, cena) a voliteľné netto / gréky.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd


@dataclass
class ParsedSpreadRow:
    spot: float
    leg_a_exp: str  # YYYYMMDD
    leg_a_strike: float
    leg_a_right: str  # C | P
    leg_a_price: float
    leg_b_exp: str
    leg_b_strike: float
    leg_b_right: str
    leg_b_price: float
    net_debit: Optional[float]
    pct_tokens: list[float]
    net_delta_per_share: Optional[float]
    gamma_per_share: Optional[float]
    vega_per_share: Optional[float]
    raw_line: str


_DATE_RE = re.compile(
    r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})\b",
)
_STRIKE_RIGHT_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*([CPcp])\b",
)


def _norm_date_to_yyyymmdd(m: re.Match[str]) -> Optional[str]:
    mm, dd, yy = m.group(1), m.group(2), m.group(3)
    y = int(yy)
    if y < 100:
        y += 2000
    try:
        d = datetime(y, int(mm), int(dd))
        return d.strftime("%Y%m%d")
    except ValueError:
        return None


def parse_spread_row_line(line: str) -> Optional[ParsedSpreadRow]:
    """
    Pokúsi sa rozparsovať jeden riadok. Pri zlyhaní vráti None.

    Očakávaný tvar (TWS / podobné): spot, dátum1, strike1+C/P, cena1, dátum2, strike2+C/P, cena2, netto, %…, delta, …
    """
    raw = " ".join((line or "").split())
    if len(raw) < 12:
        return None

    dates: list[tuple[str, re.Match[str]]] = []
    for m in _DATE_RE.finditer(raw):
        ymd = _norm_date_to_yyyymmdd(m)
        if ymd:
            dates.append((ymd, m))
    if len(dates) < 2:
        return None

    strikes_r: list[tuple[float, str, re.Match[str]]] = []
    for m in _STRIKE_RIGHT_RE.finditer(raw):
        k = float(m.group(1).replace(",", "."))
        r = m.group(2).upper()
        if r not in ("C", "P"):
            continue
        strikes_r.append((k, r, m))
    if len(strikes_r) < 2:
        return None

    k1, r1 = strikes_r[0][0], strikes_r[0][1]
    k2, r2 = strikes_r[1][0], strikes_r[1][1]
    exp_a, exp_b = dates[0][0], dates[1][0]

    # Všetky desatinné čísla v poradí zľava doprava (bez dátumových „05“ ako 05.15)
    nums: list[float] = []
    for m in re.finditer(r"-?\d+\.\d+", raw.replace(",", ".")):
        try:
            nums.append(float(m.group(0)))
        except ValueError:
            continue
    if len(nums) < 6:
        return None

    spot = nums[0]
    # očakávané: [spot, k1?, px1, k2?, px2, net, ...] — k1/k2 môžu byť v nums alebo len v strikes_r
    idx = 1
    if abs(nums[idx] - k1) < 0.02:
        idx += 1
    pa = nums[idx]
    idx += 1
    if idx < len(nums) and abs(nums[idx] - k2) < 0.02:
        idx += 1
    pb = nums[idx]
    idx += 1
    pnet = nums[idx] if idx < len(nums) else 0.0
    idx += 1

    rest = nums[idx:] if idx < len(nums) else []
    pct_tokens: list[float] = []
    nd = gm = vg = None
    for x in rest:
        if x >= 5.0 and len(pct_tokens) < 4:
            pct_tokens.append(x)
        elif x >= 2.5 and len(pct_tokens) < 3:
            pct_tokens.append(x)
        elif abs(x) < 1.5 and abs(x - pnet) > 0.2:
            if nd is None:
                nd = x
            elif gm is None:
                gm = x
            elif vg is None:
                vg = x

    return ParsedSpreadRow(
        spot=spot,
        leg_a_exp=exp_a,
        leg_a_strike=k1,
        leg_a_right=r1,
        leg_a_price=float(pa),
        leg_b_exp=exp_b,
        leg_b_strike=k2,
        leg_b_right=r2,
        leg_b_price=float(pb),
        net_debit=float(pnet),
        pct_tokens=pct_tokens,
        net_delta_per_share=nd,
        gamma_per_share=gm,
        vega_per_share=vg,
        raw_line=raw,
    )


def infer_short_leg_tag(p: ParsedSpreadRow) -> str:
    """Heuristika kalendára/diagonálu: skoršia expirácia = typicky short noha."""
    if p.leg_b_exp < p.leg_a_exp:
        return "B"
    if p.leg_b_exp > p.leg_a_exp:
        return "A"
    return "A"


def parsed_to_dataframe(p: ParsedSpreadRow) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "spot": p.spot,
                "LegA_exp": p.leg_a_exp,
                "LegA_strike": p.leg_a_strike,
                "LegA_CP": p.leg_a_right,
                "LegA_cena": p.leg_a_price,
                "LegB_exp": p.leg_b_exp,
                "LegB_strike": p.leg_b_strike,
                "LegB_CP": p.leg_b_right,
                "LegB_cena": p.leg_b_price,
                "Netto": p.net_debit,
                "delta_sucet": p.net_delta_per_share,
                "gamma": p.gamma_per_share,
                "vega": p.vega_per_share,
            }
        ]
    )


def build_sb_legs(
    p: ParsedSpreadRow,
    *,
    short_leg: str,  # "A" | "B" — ktorá noha je Short (druhá je Long)
    contracts: int = 1,
    iv: float = 0.32,
) -> list[dict[str, Any]]:
    """
    Zostaví zoznam nôh pre ``_sb_coerce_legs_from_import`` (Spread Builder).
    """
    legs_meta = [
        ("A", p.leg_a_exp, p.leg_a_strike, p.leg_a_right, p.leg_a_price),
        ("B", p.leg_b_exp, p.leg_b_strike, p.leg_b_right, p.leg_b_price),
    ]
    out: list[dict[str, Any]] = []
    for i, (tag, exp, strike, right, px) in enumerate(legs_meta, start=1):
        lt = "Short" if (short_leg == tag) else "Long"
        out.append(
            {
                "id": i,
                "leg_type": lt,
                "right": right,
                "strike": float(strike),
                "expiry": str(exp),
                "contracts": int(contracts),
                "entry_price": max(0.01, float(px)),
                "iv": float(iv),
            }
        )
    return out
