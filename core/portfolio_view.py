"""
Agregácie pre Portfolio Command Center (bez Streamlit).
"""
from __future__ import annotations


def open_groups_count(groups_map: dict[str, list]) -> int:
    """Počet skupín (kľúčov), ktoré majú aspoň jednu otvorenú nohu."""
    return sum(
        1
        for legs in groups_map.values()
        if any(t.get("status") == "Open" for t in legs)
    )


def min_short_dte_open(open_trades: list, dte_fn) -> int | None:
    """Min. DTE medzi otvorenými short nohami; ``dte_fn(expiry_str) -> int``."""
    dtes = [
        dte_fn(t.get("expiry", ""))
        for t in open_trades
        if t.get("leg_type") == "Short" and dte_fn(t.get("expiry", "")) > 0
    ]
    return min(dtes) if dtes else None
