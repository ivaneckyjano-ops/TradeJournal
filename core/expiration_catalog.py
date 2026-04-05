"""
Centrálny zoznam expirácií pre Spread Builder (a súvisiace nudge).

Uložené v DB (`settings`). Ak nie je uložené nič, použije sa generátor piatkov / 3. piatok.
"""
from __future__ import annotations

import json
from datetime import date
from typing import Optional

from core import database as db
from core import ibkr

EXPIRATION_CATALOG_KEY = "spread_builder_expiration_catalog"


def _default_generated(months: int = 18) -> list[str]:
    return sorted(ibkr.generate_expirations_local(months=months)["expirations"])


def _load_stored_raw() -> Optional[list[str]]:
    raw = db.get_setting(EXPIRATION_CATALOG_KEY, "")
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, list):
            return None
        out: list[str] = []
        for x in data:
            s = str(x).strip().replace("-", "")
            if len(s) == 8 and s.isdigit():
                try:
                    date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                    out.append(s)
                except ValueError:
                    continue
        return sorted(set(out)) if out else None
    except json.JSONDecodeError:
        return None


def get_catalog_expiries(months: int = 18) -> list[str]:
    """Zoradené YYYYMMDD — buď z DB, alebo predvolený generovaný zoznam."""
    stored = _load_stored_raw()
    if stored is not None:
        return stored
    return _default_generated(months)


def save_catalog_expiries(dates: list[str]) -> None:
    norm: list[str] = []
    for x in dates:
        s = str(x).strip().replace("-", "")
        if len(s) == 8 and s.isdigit():
            try:
                date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                norm.append(s)
            except ValueError:
                continue
    db.set_setting(EXPIRATION_CATALOG_KEY, json.dumps(sorted(set(norm)), ensure_ascii=False))


def replace_catalog_with_generated(months: int = 18) -> list[str]:
    """Nahrádza katalóg čerstvým generovaným zoznamom."""
    g = _default_generated(months)
    save_catalog_expiries(g)
    return g


def merge_catalog_with_generated(months: int = 18) -> list[str]:
    """Zlúči uložené (alebo prázdne) s generovaným."""
    cur = set(get_catalog_expiries(months))
    cur |= set(_default_generated(months))
    out = sorted(cur)
    save_catalog_expiries(out)
    return out


def append_expiries_from_text(block: str, months: int = 18) -> list[str]:
    """Pridá riadky YYYYMMDD (alebo YYYY-MM-DD) do katalógu."""
    cur = set(get_catalog_expiries(months))
    for line in (block or "").splitlines():
        s = line.strip().replace("-", "").replace(".", "")
        if len(s) == 8 and s.isdigit():
            try:
                date(int(s[:4]), int(s[4:6]), int(s[6:8]))
                cur.add(s)
            except ValueError:
                continue
    out = sorted(cur)
    save_catalog_expiries(out)
    return out


def format_expiry_select_options(expiries: list[str]) -> tuple[list[str], dict[str, str]]:
    """
    Vráti (zoznam popisov pre selectbox, mapa popis → YYYYMMDD).
    Zoradené podľa kalendárneho dátumu.
    """
    td = date.today()
    rows: list[tuple[date, str, str]] = []
    for e in sorted(set(expiries)):
        if len(e) < 8:
            continue
        try:
            ed = date(int(e[:4]), int(e[4:6]), int(e[6:8]))
            dte = (ed - td).days
            lbl = f"{ed.strftime('%d.%m.%Y')} ({dte} dní) · {e}"
            rows.append((ed, lbl, e))
        except ValueError:
            continue
    rows.sort(key=lambda x: x[0])
    labels = [r[1] for r in rows]
    mapping = {r[1]: r[2] for r in rows}
    return labels, mapping
