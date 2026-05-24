"""
Delta hedge — výpočtová vrstva bez Streamlit (časopis / journal).

Architektúra (na testovanie a doladenie)
----------------------------------------
1. **Zdroj Δ** — otvorené nohy z aktívnej DB (`trades.status='Open'`). Na nohe sa
   berie ``delta_current``, ak chýba ``delta_at_entry`` (rovnako ako súčty v časopise).
2. **Agregácia** — na ticker: súčet ``sign(leg) * δ_per_share * contracts * 100`` pre opcie,
   pre **STK** v denníku ``sign * počet kusov`` (Δ/akcia = 1),
   kde short noha má ``-1`` (zhoda s ``portfolio_data.build_group_data``).
3. **Spot** — prednosť tabuľka **Symboly** (`symbols.spot`); v UI možnosť ručného
   override pre paper experiment. Bez spotu nie je **$Δ**.
4. **Hedge podkladom** — prvý ráden: počet akcií podkladu *doplniť* tak, aby
   ``net_Δ_akcie + hedge_akcie ≈ cieľ`` (predvolene cieľ **0**). Kladné hedge =
   **nákup** akcií, záporné = **predaj / short**. V UI je k tomu orientačne
   doplnená **opčná alternatíva** (počet long call / long put pri predpokladanej Δ/akcia).
5. **Deadband** — ak ``|hedge| < deadband``, panel označí *neobchodovať* (šetriť
   náklady). Hodnotu budeme doladiť podľa paper účtu.
6. **LIVE / PAPER** — panel v časopise je len **výpočet** (neodosiela príkazy); rovnaká logika pre oba režimy.
7. **Budúce rozšírenia** — napojenie na živý ``fetch_underlying`` / STK z IB,
   upozornenia pri prekročení prahu, zápis odporúčaného hedge do ``settings`` alebo
   samostatná tabuľka „hedge log“.
8. **Úvaha bez DB** — manuálne nohy (Δ z OptionTrader/TWS); neukladá sa do ``trades``.

Všetko je **orientačné** (opčná Δ nie je konštantná); neodosiela príkazy do TWS.
"""
from __future__ import annotations

from typing import Any, Optional


def _leg_sign(leg_type: Optional[str]) -> int:
    return -1 if str(leg_type or "").strip().lower() == "short" else 1


def leg_delta_shares(trade: dict[str, Any]) -> float:
    """
    Príspevok jednej nohy do Δ v jednotkách „ekvivalent akcií“
    (opcia: 1 kontrakt = 100 ks; akcia STK: 1 ks = 1 ks).
    """
    ot = str(trade.get("option_type") or "").strip().upper()
    if ot in ("STK", "STOCK"):
        try:
            c = float(trade.get("contracts") or 1)
        except (TypeError, ValueError):
            c = 1.0
        return _leg_sign(trade.get("leg_type")) * c
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


def net_delta_shares_for_ticker(legs: list[dict[str, Any]], ticker: str) -> float:
    """
    Súčet ``leg_delta_shares`` len pre nohy daného tickera (bez filtra ``status``).
    Použitie: manuálne / hypotetické nohy v UI.
    """
    u = str(ticker or "").strip().upper()
    if not u:
        return 0.0
    return sum(
        leg_delta_shares(t)
        for t in legs
        if str(t.get("ticker") or "").strip().upper() == u
    )


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


def hedge_option_alternative_hint(
    hedge_shares: float,
    *,
    assumed_call_delta_per_share: float = 0.45,
    assumed_put_delta_per_share: float = -0.45,
) -> str:
    """
    Krátky orientačný text: koľko približne **long call** / **long put** kontraktov by
    dalo rovnakú zmenu v **ekvivalente akcií** ako nákup/predaj podkladu (Δ/akcia × 100).

    Predpokladá sa „približne ATM“ delta na akciu (voliteľné parametre); skutočná Δ
    závisí od striku, času do expirácie a IV — **nie** výber konkrétneho kontraktu v TWS.
    """
    h = float(hedge_shares)
    if abs(h) < 1e-9:
        return ""
    if h > 0:
        d = max(0.05, min(0.95, float(assumed_call_delta_per_share)))
        eq_per_contract = d * 100.0
        n = max(1, int(round(h / eq_per_contract)))
        suffix = "kontrakt" if n == 1 else "kontrakty"
        return f"Kúp {n} call {suffix} s deltou okolo {d:.2f}"
    d = min(-0.05, max(-0.95, float(assumed_put_delta_per_share)))
    eq_per_contract = abs(d) * 100.0
    n = max(1, int(round(abs(h) / eq_per_contract)))
    suffix = "kontrakt" if n == 1 else "kontrakty"
    return f"Kúp {n} put {suffix} s deltou okolo {d:.2f}"


def hedge_recommendation_label(hedge_shares: float, *, inside_deadband: bool) -> str:
    """Jeden riadok (podklad + opcia); pre tabuľku v UI radšej dva stĺpce — ``st.dataframe`` orezáva dlhý text."""
    if inside_deadband:
        return "Zatiaľ neobchodovať (deadband)"
    base = hedge_action_label(hedge_shares)
    alt = hedge_option_alternative_hint(hedge_shares)
    if alt:
        return f"{base} — {alt}"
    return base


def hedge_table_recommendation_cells(
    hedge_shares: float, *, inside_deadband: bool
) -> tuple[str, str]:
    """
    Dva stĺpce pre ``st.dataframe``: podklad vs. opčná alternatíva (inak sa dlhý text v bunke orezáva).
    """
    if inside_deadband:
        return ("Zatiaľ neobchodovať (deadband)", "—")
    base = hedge_action_label(hedge_shares)
    alt = hedge_option_alternative_hint(hedge_shares)
    return (base, alt if alt else "—")


def apply_deadband(hedge_shares: float, deadband: float) -> tuple[float, bool]:
    """
    Vráti (hedge_shares, inside_band). Ak ``deadband > 0`` a ``|hedge| < deadband``,
    považuj za *vnútri pásma* (odporúčanie: neobchodovať).
    """
    db = float(deadband)
    if db > 0 and abs(float(hedge_shares)) < db:
        return (float(hedge_shares), True)
    return (float(hedge_shares), False)
