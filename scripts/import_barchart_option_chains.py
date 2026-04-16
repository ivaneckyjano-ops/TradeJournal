#!/usr/bin/env python3
"""
Import Barchart option CSV (stacked options + volatility greeks) do samostatnej SQLite DB na ticker.

Príklady:
  python3 scripts/import_barchart_option_chains.py --dir ~/Downloads/amzn_csv
  python3 scripts/import_barchart_option_chains.py a.csv b.csv

Názvy súborov musia zodpovedať vzoru:
  TICKER-options-exp-YYYY-MM-DD-...-MM-DD-YYYY.csv
  TICKER-volatility-greeks-exp-YYYY-MM-DD-...-MM-DD-YYYY.csv

Výstup: data/option_chains/TICKER.db
"""

from __future__ import annotations

import argparse
import glob
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core.option_chain_db import (  # noqa: E402
    import_pair_from_paths,
    parse_barchart_option_filename,
)


def _collect_csvs(paths: list[str], scan_dir: list[str]) -> list[str]:
    out: list[str] = []
    for d in scan_dir:
        pat = os.path.join(os.path.expanduser(d), "*.csv")
        out.extend(glob.glob(pat))
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            out.extend(glob.glob(os.path.join(p, "*.csv")))
        elif os.path.isfile(p):
            out.append(p)
    # unikátne, stabilné poradie
    seen: set[str] = set()
    uniq: list[str] = []
    for x in sorted(out):
        ap = os.path.abspath(x)
        if ap not in seen:
            seen.add(ap)
            uniq.append(ap)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser(description="Import Barchart option CSV do data/option_chains/<TICKER>.db")
    ap.add_argument("paths", nargs="*", help="CSV súbory alebo priečinky s *.csv")
    ap.add_argument("--dir", dest="dirs", action="append", default=[], help="Priečinok s CSV (možno viackrát)")
    args = ap.parse_args()

    files = _collect_csvs(list(args.paths), list(args.dirs))
    if not files:
        print("Žiadne CSV súbory.", file=sys.stderr)
        return 1

    groups: dict[tuple[str, str, str], dict[str, str]] = {}
    for fp in files:
        meta = parse_barchart_option_filename(fp)
        if not meta:
            print(f"Ignorujem (nepoznaný názov): {fp}", file=sys.stderr)
            continue
        key = (meta.ticker, meta.expiry, meta.as_of_date)
        groups.setdefault(key, {})[meta.kind] = fp

    if not groups:
        print("Žiadny rozpoznaný Barchart súbor.", file=sys.stderr)
        return 1

    total = 0
    for (ticker, expiry, as_of), kinds in sorted(groups.items()):
        po = kinds.get("options")
        pg = kinds.get("greeks")
        if not po and not pg:
            continue
        n = import_pair_from_paths(ticker, po, pg)
        total += n
        print(f"{ticker}  exp={expiry}  as_of={as_of}  riadkov={n}  options={po or '-'}  greeks={pg or '-'}")

    print(f"Hotovo. Spolu zapísaných riadkov (súčet behov): {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
