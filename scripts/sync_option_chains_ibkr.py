#!/usr/bin/env python3
"""
Synchronizácia úzkeho opčného reťazca z IBKR do ``data/option_chains/<TICKER>.db``.

Vyžaduje pripojený TWS / IB Gateway (rovnako ako ostatné IBKR skripty v projekte).

Príklad:
  python3 scripts/sync_option_chains_ibkr.py --ticker SPY --right call \\
    --expiry 2026-06-18 --expiry 2026-07-17 --strikes 9

Ak niektorá expirácia v zozname nie je v IBKR reťazci, skript sa opýta (okrem ``--continue-with-valid-only``).
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import ibkr  # noqa: E402
from core.option_chain_ibkr_sync import (  # noqa: E402
    parse_expiry_text,
    sync_chain_snapshot,
    validate_expiries_against_secdef,
)


def _parse_expiry_cli(values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        out.extend(parse_expiry_text(v))
    return out


def _prompt_continue(missing: list[str]) -> bool:
    print("Tieto expirácie nie sú v IBKR reťazci:", ", ".join(missing))
    if not sys.stdin.isatty():
        return False
    r = input("Pokračovať len s platnými expiráciami? [y/N]: ")
    return r.strip().lower() in ("y", "yes", "a", "ano")


def main() -> int:
    ap = argparse.ArgumentParser(description="IBKR → option_chain_db (jeden ticker, Call alebo Put).")
    ap.add_argument("--ticker", required=True, help="Symbol podkladu (napr. SPY).")
    ap.add_argument(
        "--right",
        required=True,
        choices=("call", "put", "c", "p"),
        help="call / put (alebo c / p).",
    )
    ap.add_argument(
        "--expiry",
        dest="expiries",
        action="append",
        default=[],
        metavar="DATE",
        help="Expirácia YYYY-MM-DD alebo YYYYMMDD (opakovane; v jednej hodnote môžu byť čiarky).",
    )
    ap.add_argument(
        "--strikes",
        type=int,
        default=11,
        metavar="N",
        help="Počet strike-ov najbližších k spotu (predvolene 11).",
    )
    ap.add_argument(
        "--as-of",
        dest="as_of",
        default=None,
        help="Dátum snímky v DB YYYY-MM-DD (predvolene dnes).",
    )
    ap.add_argument("--pause", type=float, default=0.2, help="Pauza medzi kontrakty v sekundách.")
    ap.add_argument(
        "--continue-with-valid-only",
        action="store_true",
        help="Pri chýbajúcich expiráciách v secdef pokračovať len s platnými bez interaktívnej otázky.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Iba overenie expirácií voči secdef a odhad počtu kontraktov (bez zápisu do DB).",
    )
    args = ap.parse_args()
    raw_exps = _parse_expiry_cli(args.expiries)
    if not raw_exps:
        print("Chýba aspoň jedna --expiry.", file=sys.stderr)
        return 2

    if not ibkr.is_connected():
        print("IBKR nie je pripojený (TWS / IB Gateway + pripojenie z app).", file=sys.stderr)
        return 1

    valid, missing, err = validate_expiries_against_secdef(args.ticker, raw_exps)
    if err:
        print(f"SecDef chyba: {err}", file=sys.stderr)
        return 1

    if missing:
        print("Neplatné alebo v reťazci chýbajúce expirácie:", ", ".join(missing))
        print("Platné expirácie:", ", ".join(valid) if valid else "(žiadne)")
        if not valid:
            print("Žiadna platná expirácia — končím.", file=sys.stderr)
            return 3
        if not args.continue_with_valid_only:
            if not _prompt_continue(missing):
                print("Zrušené.", file=sys.stderr)
                return 4

    if args.dry_run:
        n = len(valid) * max(1, int(args.strikes))
        print(
            f"Dry-run: ticker={args.ticker.strip().upper()} right={args.right} "
            f"expiries={valid} → cca {n} kontraktov na sken."
        )
        return 0

    if args.as_of is not None:
        from datetime import date as _date

        _ = _date.fromisoformat(args.as_of)

    res = sync_chain_snapshot(
        args.ticker,
        right=args.right,
        expiries_yyyy_mm_dd=valid,
        strike_count=int(args.strikes),
        as_of_yyyy_mm_dd=args.as_of,
        pause_s=float(args.pause),
    )
    for w in res.warnings:
        print(f"Varovanie: {w}")
    for e in res.errors:
        print(f"Chyba: {e}", file=sys.stderr)
    print(f"Hotovo: {res.rows_written} riadkov, expirácie: {res.expiries_processed}")
    return 0 if res.ok else 5


if __name__ == "__main__":
    raise SystemExit(main())
