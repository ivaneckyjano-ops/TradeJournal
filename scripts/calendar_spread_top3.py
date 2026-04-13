#!/usr/bin/env python3
"""
Načíta CSV s variantmi kalendárnej/diagonálnej spreadu a vypíše top N podľa zvolenej stratégie.

Očakávané stĺpce (názvy sú znormalizované — medzery/veľké písmená nezáležia):
  Net Debit, IV Skew, Net Delta, Net Vega, Net Theta, Exp Leg2, Ask2, Bid1, ...

Čísla môžu mať desatinnú čiarku (245,04) a percentá (57,93%).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def _norm_header(h: str) -> str:
    s = h.strip().lower().replace("~", "").strip()
    s = re.sub(r"\s+", "_", s)
    return s


def _parse_number(x) -> float:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return float("nan")
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    s = str(x).strip().replace("\u00a0", " ")
    s = s.replace("%", "").strip()
    if not s:
        return float("nan")
    s = s.replace(" ", "")
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


def _read_spread_csv(path: Path, encoding: str, sep: str | None) -> pd.DataFrame:
    kwargs = dict(encoding=encoding, dtype=str)
    if sep is None:
        df = pd.read_csv(path, sep=None, engine="python", **kwargs)
    else:
        df = pd.read_csv(path, sep=sep, **kwargs)
    df.columns = [_norm_header(c) for c in df.columns]
    return df


def _numeric_series(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(np.nan, index=df.index)
    return df[col].map(_parse_number)


def _ensure_net_debit(df: pd.DataFrame) -> pd.Series:
    if "net_debit" in df.columns:
        d = _numeric_series(df, "net_debit")
        if d.notna().any():
            return d
    ask2 = _numeric_series(df, "ask2")
    bid1 = _numeric_series(df, "bid1")
    if ask2.notna().any() and bid1.notna().any():
        return ask2 - bid1
    raise ValueError(
        "V CSV chýba stĺpec Net Debit a nie je možné ho dopočítať (Ask2 − Bid1). "
        f"Dostupné stĺpce: {list(df.columns)}"
    )


def _min_max_norm(s: pd.Series) -> pd.Series:
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or hi == lo:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def add_scores(df: pd.DataFrame, strategy: str) -> pd.DataFrame:
    out = df.copy()
    debit = _ensure_net_debit(out)
    out["_net_debit"] = debit

    skew = _numeric_series(out, "iv_skew")
    theta = _numeric_series(out, "net_theta")
    delta = _numeric_series(out, "net_delta")

    if strategy == "cheap":
        out["_score"] = -debit
    elif strategy == "skew":
        out["_score"] = skew.fillna(skew.min())
    elif strategy == "theta":
        out["_score"] = theta.fillna(theta.min())
    elif strategy == "balanced":
        # Vyššie skóre = výhodnejšie: nižší debit, vyšší skew, vyššia theta, bližšie k delta 0
        debit_better = 1.0 - _min_max_norm(debit)
        skew_better = _min_max_norm(skew.fillna(skew.min()))
        theta_better = _min_max_norm(theta.fillna(theta.min()))
        ad = delta.abs()
        if ad.notna().any() and ad.max() > 0:
            delta_better = 1.0 - _min_max_norm(ad.fillna(ad.max()))
        else:
            delta_better = pd.Series(0.5, index=out.index)
        out["_score"] = (
            0.40 * debit_better + 0.30 * skew_better + 0.20 * theta_better + 0.10 * delta_better
        )
    else:
        raise ValueError(f"Neznáma stratégia: {strategy}")

    return out


def main() -> int:
    p = argparse.ArgumentParser(
        description="Vyberie najvýhodnejšie varianty z CSV (kalendár / diagonála)."
    )
    p.add_argument("csv", type=Path, help="Cesta k CSV súboru")
    p.add_argument(
        "-n",
        "--top",
        type=int,
        default=3,
        help="Počet variantov (predvolene 3)",
    )
    p.add_argument(
        "--strategy",
        choices=("balanced", "cheap", "skew", "theta"),
        default="balanced",
        help=(
            "balanced: kombinácia nižší debit, vyšší IV skew, vyššia net theta, neutrálnejší delta; "
            "cheap: najnižší Net Debit; skew: najvyšší IV Skew; theta: najvyšší Net Theta"
        ),
    )
    p.add_argument("--sep", default=None, help="Oddeľovač stĺpcov (predvolene automaticky)")
    p.add_argument("--encoding", default="utf-8-sig", help="Kódovanie súboru")
    p.add_argument(
        "--show-score",
        action="store_true",
        help="Zobrazí stĺpec interného skóre",
    )
    args = p.parse_args()

    if not args.csv.is_file():
        print(f"Súbor neexistuje: {args.csv}", file=sys.stderr)
        return 1

    try:
        raw = _read_spread_csv(args.csv, args.encoding, args.sep)
    except Exception as e:
        print(f"Chyba pri čítaní CSV: {e}", file=sys.stderr)
        return 1

    if raw.empty:
        print("CSV neobsahuje žiadne riadky.", file=sys.stderr)
        return 1

    ranked = add_scores(raw, args.strategy)
    ranked = ranked.sort_values("_score", ascending=False, kind="mergesort")
    top = ranked.head(args.top)

    display_cols = [c for c in raw.columns if not c.startswith("_")]
    if args.show_score:
        display_cols = display_cols + ["_score"]
    else:
        display_cols = display_cols + ["_net_debit"] if "_net_debit" in top.columns else display_cols

    out = top[display_cols] if display_cols else top
    # Peknejší výpis: čísla ako v CSV nie sú nutné, tabuľka stačí
    with pd.option_context("display.max_columns", None, "display.width", 200, "display.max_colwidth", 24):
        print(out.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
