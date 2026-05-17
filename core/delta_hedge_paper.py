"""
Delta hedge — výpočtová vrstva bez Streamlit (časopis / journal).

Architektúra (na testovanie a doladenie)
----------------------------------------
1. **Zdroj Δ** — otvorené nohy z aktívnej DB (`trades.status='Open'`). Na nohe sa
   berie ``delta_current``, ak chýba ``delta_at_entry`` (rovnako ako súčty v časopise).
2. **Agregácia** — na ticker: súčet ``sign(leg) * δ_per_share * contracts * 100``,
   kde short noha má ``-1`` (zhoda s ``portfolio_data.build_group_data``).
3. **Spot** — prednosť tabuľka **Symboly** (`symbols.spot`); v UI možnosť ručného
   override pre paper experiment. Bez spotu nie je **$Δ**.
4. **Hedge podkladom** — prvý ráden: počet akcií podkladu *doplniť* tak, aby
   ``net_Δ_akcie + hedge_akcie ≈ cieľ`` (predvolene cieľ **0**). Kladné hedge =
   **nákup** akcií, záporné = **predaj / short**.
5. **Deadband** — ak ``|hedge| < deadband``, panel označí *neobchodovať* (šetriť
   náklady). Hodnotu budeme doladiť podľa paper účtu.
6. **LIVE / PAPER** — panel v časopise je len **výpočet** (neodosiela príkazy); rovnaká logika pre oba režimy.
7. **Budúce rozšírenia** — napojenie na živý ``fetch_underlying`` / STK z IB,
   upozornenia pri prekročení prahu, zápis odporúčaného hedge do ``settings`` alebo
   samostatná tabuľka „hedge log“.

Všetko je **orientačné** (opčná Δ nie je konštantná); neodosiela príkazy do TWS.
"""
from __future__ import annotations

from typing import Any, Optional


def _leg_sign(leg_type: Optional[str]) -> int:
    return -1 if str(leg_type or "").strip().lower() == "short" else 1


def leg_delta_shares(trade: dict[str, Any]) -> float:
    """
    Príspevok jednej nohy do Δ v jednotkách „ekvivalent akcií“ (1 kontrakt = 100 ks).
    """
    d = trade.get("delta_current")
    if d is None:
        d = trade.get("delta_at_entry")
    try:
        delta = float(d) if d is not None else 0.0
    except (TypeError, ValueError):
        delta = 0.0
    try:
        c = float(trade.get("contracts") or 1)
    except (TypeError, ValueError):
        c = 1.0
    return _leg_sign(trade.get("leg_type")) * delta * c * 100.0


def net_delta_shares_by_ticker(open_trades: list[dict[str, Any]]) -> dict[str, float]:
    """Súčet ``leg_delta_shares`` podľa ``ticker`` (uppercase)."""
    out: dict[str, float] = {}
    for t in open_trades:
        if str(t.get("status") or "Open").strip().lower() != "open":
            continue
        tk = str(t.get("ticker") or "").strip().upper()
        if not tk:
            continue
        out[tk] = out.get(tk, 0.0) + leg_delta_shares(t)
    return dict(sorted(out.items(), key=lambda kv: kv[0]))


def dollar_delta(net_shares: float, spot: float) -> float:
    return float(net_shares) * float(spot)


def hedge_shares_for_target(net_option_delta_shares: float, target_delta_shares: float = 0.0) -> float:
    """
    Počet akcií podkladu, ktoré treba **pridať** k účtu (klad = kúpiť, záporné = predať/short),
    aby sa dosiahla cieľová čistá Δ v akciách (opcie + podklad).
    """
    return float(target_delta_shares) - float(net_option_delta_shares)


def hedge_action_label(hedge_shares: float) -> str:
    if hedge_shares > 0:
        return f"Nakúpiť ~{hedge_shares:+.1f} akcií (podklad)"
    if hedge_shares < 0:
        return f"Predať / podshort ~{abs(hedge_shares):.1f} akcií (podklad)"
    return "Bez úpravy podkladu (Δ už na cieli)"


def apply_deadband(hedge_shares: float, deadband: float) -> tuple[float, bool]:
    """
    Vráti (hedge_shares, inside_band). Ak ``deadband > 0`` a ``|hedge| < deadband``,
    považuj za *vnútri pásma* (odporúčanie: neobchodovať).
    """
    db = float(deadband)
    if db > 0 and abs(float(hedge_shares)) < db:
        return (float(hedge_shares), True)
    return (float(hedge_shares), False)
