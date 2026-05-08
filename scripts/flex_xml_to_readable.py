#!/usr/bin/env python3
"""
Konverzia exportu Interactive Brokers **Flex Query** (XML) do čitateľných **CSV** tabuliek.

Logika parsovania je v ``core.flex_xml_readable``.

Príklad::

    python scripts/flex_xml_to_readable.py ~/Stiahnuté/flex.*.xml
    python scripts/flex_xml_to_readable.py report.xml --out-dir ~/Documents/flex_csv
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Spustenie priamo: python scripts/… — doplníme koreň projektu do PYTHONPATH
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from core.flex_xml_readable import parse_flex_xml, write_csv_file


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="IB Flex XML → CSV tabuľky")
    p.add_argument(
        "xml_files",
        nargs="+",
        type=Path,
        help="Cesta(y) k .xml súborom z Flex Query",
    )
    p.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Adresár pre výstup (predvolene = priečinok vstupného súboru)",
    )
    args = p.parse_args(argv)

    for xml_path in args.xml_files:
        if not xml_path.is_file():
            print(f"Chýba súbor: {xml_path}", file=sys.stderr)
            return 1

        out_dir = args.out_dir if args.out_dir is not None else xml_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = xml_path.stem

        data, _meta = parse_flex_xml(xml_path)

        written: list[tuple[str, int]] = []
        mapping = [
            ("executions", f"{stem}_executions.csv"),
            ("orders", f"{stem}_orders.csv"),
            ("symbol_summary", f"{stem}_symbol_summary.csv"),
            ("asset_summary", f"{stem}_asset_summary.csv"),
            ("prior_period_positions", f"{stem}_prior_period_positions.csv"),
            ("other_trades", f"{stem}_other_trade_rows.csv"),
        ]
        for key, fname in mapping:
            n = write_csv_file(out_dir / fname, data[key])
            if n or key not in ("other_trades",):
                written.append((fname, n))

        readme = out_dir / f"{stem}_readme.txt"
        lines = [
            f"Vstupný súbor: {xml_path.resolve()}",
            "Query / typ v XML: (pozri koreň FlexQueryResponse)",
            "",
            "Vygenerované CSV:",
        ]
        for fname, n in written:
            lines.append(f"  - {fname}: {n} riadkov")
        lines.append("")
        lines.append(
            "Executions = skutočné obchody (EXECUTION). "
            "Prior period positions = stav podľa dňa v tomto Flex výpise."
        )
        readme.write_text("\n".join(lines), encoding="utf-8")

        print(f"OK {xml_path.name} → {out_dir.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
