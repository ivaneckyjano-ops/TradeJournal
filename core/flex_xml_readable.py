"""
Parsovanie exportu Interactive Brokers **Flex Query** (XML) do tabuliek (dict riadkov).

Používa UI stránka ``pages/flex_trades.py`` a skript ``scripts/flex_xml_to_readable.py``.
"""

from __future__ import annotations

import csv
import io
import xml.etree.ElementTree as ET
from pathlib import Path


def normalize_flex_xml_text(raw: str) -> str:
    """Opraví častú chybu exportu / kopírovania — chýbajúci ``<`` pred koreňovým tagom."""
    t = raw.strip()
    if not t.startswith("<"):
        if t.startswith("FlexQueryResponse"):
            t = "<" + t
    return t


def _row(elem: ET.Element) -> dict[str, str]:
    return {k: (v if v is not None else "") for k, v in elem.attrib.items()}


def _collect_buckets(root: ET.Element) -> dict[str, list[dict[str, str]]]:
    buckets: dict[str, list[dict[str, str]]] = {
        "executions": [],
        "orders": [],
        "symbol_summary": [],
        "asset_summary": [],
        "prior_period_positions": [],
        "other_trades": [],
    }

    for stmt in root.iter("FlexStatement"):
        stmt_meta = {
            "stmt_fromDate": stmt.get("fromDate") or "",
            "stmt_toDate": stmt.get("toDate") or "",
            "stmt_accountId": stmt.get("accountId") or "",
            "stmt_whenGenerated": stmt.get("whenGenerated") or "",
        }
        trades_el = stmt.find("Trades")
        if trades_el is not None:
            for child in trades_el:
                tag = child.tag
                row = _row(child)
                row.update(stmt_meta)
                lod = (row.get("levelOfDetail") or "").strip()

                if tag == "Trade":
                    if lod == "EXECUTION":
                        buckets["executions"].append(row)
                    elif lod == "ORDER":
                        buckets["orders"].append(row)
                    else:
                        buckets["other_trades"].append(row)
                elif tag == "Order":
                    buckets["orders"].append(row)
                elif tag == "SymbolSummary":
                    buckets["symbol_summary"].append(row)
                elif tag == "AssetSummary":
                    buckets["asset_summary"].append(row)

        for pos in stmt.iter("PriorPeriodPosition"):
            row = _row(pos)
            row.update(stmt_meta)
            buckets["prior_period_positions"].append(row)

    return buckets


def parse_flex_xml_string(raw: str) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    """
    Vráti ``(buckets, meta)`` — ``meta`` má ``queryName`` a ``type`` z koreňa Flex odpovede.
    """
    xml_text = normalize_flex_xml_text(raw)
    root = ET.fromstring(xml_text)
    meta = {
        "queryName": root.get("queryName") or "",
        "type": root.get("type") or "",
    }
    return _collect_buckets(root), meta


def parse_flex_xml(path: Path) -> tuple[dict[str, list[dict[str, str]]], dict[str, str]]:
    raw_text = path.read_text(encoding="utf-8-sig")
    return parse_flex_xml_string(raw_text)


def sorted_columns(rows: list[dict[str, str]]) -> list[str]:
    keys: set[str] = set()
    for r in rows:
        keys.update(r.keys())
    return sorted(keys)


def rows_to_csv_text(rows: list[dict[str, str]]) -> str:
    if not rows:
        return ""
    cols = sorted_columns(rows)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def write_csv_file(path: Path, rows: list[dict[str, str]]) -> int:
    txt = rows_to_csv_text(rows)
    path.write_text(txt, encoding="utf-8")
    return len(rows)
