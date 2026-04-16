"""
Hľadanie diagonálnych spreadov z lokálnej DB reťazcov (option_chain_db).

Stratégie (vždy 2 nohy: skoršia expirácia „blízka“, neskoršia „ďaleká“):
- long_call_diagonal: +1 Call(ďaleká), -1 Call(blízka)
- short_call_diagonal: -1 Call(ďaleká), +1 Call(blízka)
- long_put_diagonal: +1 Put(ďaleká), -1 Put(blízka)
- short_put_diagonal: -1 Put(ďaleká), +1 Put(blízka)

Gréky z reťazca sú za predpokladu **long 1 kontrakt**; váhy w_near / w_far zohľadňujú short/long.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Optional

import pandas as pd

from core import option_chain_db as odb

StrategyId = Literal[
    "long_call_diagonal",
    "short_call_diagonal",
    "long_put_diagonal",
    "short_put_diagonal",
]


@dataclass(frozen=True)
class StrategySpec:
    id: StrategyId
    option_type: str
    w_near: float
    w_far: float
    label_sk: str


STRATEGIES: dict[StrategyId, StrategySpec] = {
    "long_call_diagonal": StrategySpec(
        id="long_call_diagonal",
        option_type="Call",
        w_near=-1.0,
        w_far=1.0,
        label_sk="Long call diagonál (+Call ďaleká exp., −Call blízka exp.)",
    ),
    "short_call_diagonal": StrategySpec(
        id="short_call_diagonal",
        option_type="Call",
        w_near=1.0,
        w_far=-1.0,
        label_sk="Short call diagonál (−Call ďaleká exp., +Call blízka exp.)",
    ),
    "long_put_diagonal": StrategySpec(
        id="long_put_diagonal",
        option_type="Put",
        w_near=-1.0,
        w_far=1.0,
        label_sk="Long put diagonál (+Put ďaleká exp., −Put blízka exp.)",
    ),
    "short_put_diagonal": StrategySpec(
        id="short_put_diagonal",
        option_type="Put",
        w_near=1.0,
        w_far=-1.0,
        label_sk="Short put diagonál (−Put ďaleká exp., +Put blízka exp.)",
    ),
}


def _expiry_sort_key(expiry: str) -> datetime:
    return datetime.strptime(str(expiry).strip()[:10], "%Y-%m-%d")


def _dte_days(as_of_date: str, expiry: pd.Series) -> pd.Series:
    """Počet dní do expirácie od dátumu snímky (as-of)."""
    a = pd.to_datetime(as_of_date, errors="coerce")
    e = pd.to_datetime(expiry.astype(str).str[:10], errors="coerce")
    return (e - a).dt.days


def _subsample_strikes(strikes: list[float], max_n: int) -> list[float]:
    if max_n <= 0 or len(strikes) <= max_n:
        return strikes
    u = sorted(set(float(s) for s in strikes))
    if len(u) <= max_n:
        return u
    step = (len(u) - 1) / max(1, max_n - 1)
    idx = [int(round(i * step)) for i in range(max_n)]
    idx = sorted(set(min(i, len(u) - 1) for i in idx))
    return [u[i] for i in idx]


def list_as_of_dates(ticker: str) -> list[str]:
    """Zoradené dátumy snímky pre ticker."""
    df = odb.list_distinct_snapshots(ticker)
    if df.empty or "as_of_date" not in df.columns:
        return []
    return sorted(df["as_of_date"].astype(str).unique().tolist(), reverse=True)


def search_diagonal_spreads(
    ticker: str,
    *,
    as_of_date: str,
    strategy: StrategyId,
    target_net_delta: float = 0.0,
    top_n: int = 40,
    max_strikes_per_expiry: int = 55,
    strike_min: Optional[float] = None,
    strike_max: Optional[float] = None,
) -> pd.DataFrame:
    """
    Nájde kombinácie (blízka expirácia skôr ako ďaleká), strike blízky / ďaleký.

    Triedenie: najprv |čistá_delta − cieľ|, potom zostupne čistá_theta
    (väčší denný theta efekt pozície — súčet vážených theta z reťazca).

    ``strike_min`` / ``strike_max`` (voliteľné): obe nohy musia mať strike v uzavretom intervale.

    Vráti už **prehľadnú** tabuľku (stĺpce Short/Long, čisté gréky, debit/kredit).
    """
    spec = STRATEGIES.get(strategy)
    if not spec:
        raise ValueError(f"Neznáma stratégia: {strategy!r}")

    if strike_min is not None and strike_max is not None and float(strike_min) > float(strike_max):
        strike_min, strike_max = float(strike_max), float(strike_min)

    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return pd.DataFrame()

    need = {"expiry", "strike", "option_type", "delta", "theta"}
    miss = need - set(raw.columns)
    if miss:
        return pd.DataFrame()

    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.loc[df["strike"].notna()]
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    df = df.loc[df["delta"].notna() & df["theta"].notna()]
    if strike_min is not None:
        df = df.loc[df["strike"] >= float(strike_min)]
    if strike_max is not None:
        df = df.loc[df["strike"] <= float(strike_max)]
    if df.empty:
        return pd.DataFrame()

    expiries = sorted(df["expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    if len(expiries) < 2:
        return pd.DataFrame()

    blocks: list[pd.DataFrame] = []

    for i, exp_near in enumerate(expiries):
        for exp_far in expiries[i + 1 :]:
            sub_n = df.loc[df["expiry"] == exp_near].copy()
            sub_f = df.loc[df["expiry"] == exp_far].copy()
            if sub_n.empty or sub_f.empty:
                continue

            strikes_n = _subsample_strikes(sub_n["strike"].tolist(), max_strikes_per_expiry)
            strikes_f = _subsample_strikes(sub_f["strike"].tolist(), max_strikes_per_expiry)
            sub_n = sub_n.loc[sub_n["strike"].isin(strikes_n)].copy()
            sub_f = sub_f.loc[sub_f["strike"].isin(strikes_f)].copy()

            for _sub in (sub_n, sub_f):
                for col in ("bid", "ask", "mid"):
                    if col not in _sub.columns:
                        _sub[col] = pd.NA

            cols_n = ["strike", "delta", "theta"]
            for c in ("bid", "ask", "mid"):
                if c in sub_n.columns:
                    cols_n.append(c)
            cols_f = ["strike", "delta", "theta"]
            for c in ("bid", "ask", "mid"):
                if c in sub_f.columns:
                    cols_f.append(c)

            near_leg = sub_n[cols_n].rename(
                columns={
                    "strike": "strike_near",
                    "delta": "delta_near",
                    "theta": "theta_near",
                    "bid": "bid_near",
                    "ask": "ask_near",
                    "mid": "mid_near",
                }
            )
            near_leg["_k"] = 1

            far_leg = sub_f[cols_f].rename(
                columns={
                    "strike": "strike_far",
                    "delta": "delta_far",
                    "theta": "theta_far",
                    "bid": "bid_far",
                    "ask": "ask_far",
                    "mid": "mid_far",
                }
            )
            far_leg["_k"] = 1

            cart = near_leg.merge(far_leg, on="_k").drop(columns="_k")
            if cart.empty:
                continue

            cart["net_delta"] = spec.w_near * cart["delta_near"] + spec.w_far * cart["delta_far"]
            cart["net_theta"] = spec.w_near * cart["theta_near"] + spec.w_far * cart["theta_far"]
            cart["delta_err"] = (cart["net_delta"] - target_net_delta).abs()
            cart["strategia"] = spec.label_sk
            cart["strategia_id"] = spec.id
            cart["expiracia_near"] = exp_near
            cart["expiracia_far"] = exp_far
            cart["typ"] = spec.option_type
            blocks.append(cart)

    if not blocks:
        return pd.DataFrame()

    out = pd.concat(blocks, ignore_index=True)
    out = out.sort_values(
        by=["delta_err", "net_theta"],
        ascending=[True, False],
        kind="mergesort",
    ).head(int(max(1, top_n)))
    out = out.reset_index(drop=True)
    return _to_display_spread_table(out, spec, as_of_date)


def _to_display_spread_table(out: pd.DataFrame, spec: StrategySpec, as_of_date: str) -> pd.DataFrame:
    """
    Prehľadná tabuľka: noha short (DTE, exp, strike, bid), noha long (DTE, exp, strike, ask),
    čistá delta/theta, debit/kredit na **1 lot (100 akcií)** = (ask(long) − bid(short)) × 100.
    """
    if out.empty:
        return out

    for c in ("bid_near", "ask_near", "bid_far", "ask_far"):
        if c not in out.columns:
            out[c] = pd.NA

    if spec.w_near < 0:
        # short = blízka, long = ďaleká
        short_exp, long_exp = out["expiracia_near"], out["expiracia_far"]
        short_k, long_k = out["strike_near"], out["strike_far"]
        short_bid, long_ask = out["bid_near"], out["ask_far"]
    else:
        # short = ďaleká, long = blízka
        short_exp, long_exp = out["expiracia_far"], out["expiracia_near"]
        short_k, long_k = out["strike_far"], out["strike_near"]
        short_bid, long_ask = out["bid_far"], out["ask_near"]

    sb = pd.to_numeric(short_bid, errors="coerce")
    la = pd.to_numeric(long_ask, errors="coerce")
    debit_per_share = la - sb
    debit_lot = debit_per_share * 100.0

    slim = pd.DataFrame(
        {
            "Stratégia": out["strategia"].astype(str),
            "Typ": out["typ"].astype(str),
            "Short — DTE": _dte_days(as_of_date, short_exp),
            "Short — expirácia": short_exp.astype(str),
            "Short — strike": pd.to_numeric(short_k, errors="coerce"),
            "Short — bid": sb,
            "Long — DTE": _dte_days(as_of_date, long_exp),
            "Long — expirácia": long_exp.astype(str),
            "Long — strike": pd.to_numeric(long_k, errors="coerce"),
            "Long — ask": la,
            "Čistá delta": pd.to_numeric(out["net_delta"], errors="coerce"),
            "Čistá theta (+ príjem / − strata)": pd.to_numeric(out["net_theta"], errors="coerce"),
            "Debit/kredit ($/1 lot ×100)": debit_lot,
        }
    )
    return slim
