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

from datetime import date
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


def _parse_expiry_date(expiry_value: Any) -> date | None:
    s = str(expiry_value or "").strip()
    if not s:
        return None
    try:
        if len(s) == 8 and s.isdigit():
            return date.fromisoformat(f"{s[:4]}-{s[4:6]}-{s[6:8]}")
        return date.fromisoformat(s)
    except Exception:
        return None


def preferred_short_expiry_for_ticker(open_trades: list[dict[str, Any]], ticker: str) -> str | None:
    """
    Preferovaná expirácia pre hedge opciu:
    najbližšia expirácia otvorenej short opčnej nohy daného tickera.
    """
    tk = str(ticker or "").strip().upper()
    if not tk:
        return None
    expiries: list[date] = []
    for t in open_trades or []:
        if str(t.get("status") or "Open").strip().lower() != "open":
            continue
        if str(t.get("ticker") or "").strip().upper() != tk:
            continue
        if str(t.get("leg_type") or "").strip().lower() != "short":
            continue
        ot = str(t.get("option_type") or "").strip().upper()
        if ot in ("STK", "STOCK", ""):
            continue
        ex = _parse_expiry_date(t.get("expiry"))
        if ex is not None:
            expiries.append(ex)
    if not expiries:
        return None
    return min(expiries).isoformat()


def pick_real_option_hedge_contract(
    chain_rows,
    hedge_shares: float,
    *,
    preferred_expiry: str | None = None,
    min_dte: int = 1,
) -> dict[str, Any] | None:
    """
    Vyberie jeden konkrétny kontrakt z reálneho option chainu.

    Postup:
    - smer hedgu určí Call vs Put,
    - expirácie filtruje na min. ``min_dte`` a preferuje expiráciu short nohy
      (alebo najbližšiu neskoršiu dostupnú),
    - v rámci zvolenej expirácie hľadá kombináciu ``počet kontraktov × reálna delta``,
      ktorá čo najlepšie sedí na požadovaný hedge v ekvivalente akcií,
      ale len v rozsahu **1 až 2 kontrakty**.
    """
    try:
        import pandas as pd
    except Exception:
        return None

    target_eq = abs(float(hedge_shares))
    if target_eq < 1e-9 or chain_rows is None:
        return None
    if not isinstance(chain_rows, pd.DataFrame) or chain_rows.empty:
        return None

    side = "Call" if float(hedge_shares) > 0 else "Put"
    df = chain_rows.copy()
    if "option_type" not in df.columns or "delta" not in df.columns:
        return None

    df = df[df["option_type"].astype(str).str.strip().str.title() == side].copy()
    if df.empty:
        return None

    df["delta_abs"] = pd.to_numeric(df["delta"], errors="coerce").abs()
    df = df[df["delta_abs"].notna()]
    df = df[(df["delta_abs"] >= 0.01) & (df["delta_abs"] <= 0.99)].copy()
    if df.empty:
        return None

    today = date.today()
    min_expiry_date = today.fromordinal(today.toordinal() + max(1, int(min_dte)))
    df["expiry_date"] = df.get("expiry").map(_parse_expiry_date) if "expiry" in df.columns else None
    df = df[df["expiry_date"].notna()].copy()
    if df.empty:
        return None
    df = df[df["expiry_date"] >= min_expiry_date].copy()
    if df.empty:
        return None

    pref_date = _parse_expiry_date(preferred_expiry)
    target_expiry = max(pref_date, min_expiry_date) if pref_date else min_expiry_date
    later = df[df["expiry_date"] >= target_expiry].copy()
    if not later.empty:
        chosen_expiry = later["expiry_date"].min()
        df = later[later["expiry_date"] == chosen_expiry].copy()
    else:
        chosen_expiry = df["expiry_date"].min()
        df = df[df["expiry_date"] == chosen_expiry].copy()

    if "expiry" in df.columns:
        df["expiry_sort"] = df["expiry"].astype(str)
    else:
        df["expiry_sort"] = ""
    df["open_interest_sort"] = pd.to_numeric(df.get("open_interest"), errors="coerce").fillna(0.0)
    df["volume_sort"] = pd.to_numeric(df.get("volume"), errors="coerce").fillna(0.0)

    candidates: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        delta_real = float(row["delta"])
        delta_abs_real = abs(delta_real)
        eq_per_contract = delta_abs_real * 100.0
        if eq_per_contract <= 0:
            continue
        nearest_n = max(1, int(round(target_eq / eq_per_contract)))
        candidate_counts = {n for n in (1, 2, nearest_n) if 1 <= n <= 2}
        for n_contracts in sorted(candidate_counts):
            covered_eq = n_contracts * eq_per_contract
            residual_eq = abs(target_eq - covered_eq)
            candidates.append(
                {
                    "contracts": n_contracts,
                    "option_type": side,
                    "delta": delta_real,
                    "delta_abs": delta_abs_real,
                    "strike": row.get("strike"),
                    "expiry": str(row.get("expiry") or "").strip(),
                    "eq_shares_per_contract": eq_per_contract,
                    "open_interest": float(row.get("open_interest_sort") or 0.0),
                    "volume": float(row.get("volume_sort") or 0.0),
                    "residual_eq": residual_eq,
                }
            )
    if not candidates:
        return None

    candidates.sort(
        key=lambda x: (
            float(x["residual_eq"]),
            abs(float(x["contracts"]) * float(x["eq_shares_per_contract"]) - target_eq),
            -float(x["open_interest"]),
            -float(x["volume"]),
            abs(float(x["delta_abs"]) - min(0.95, max(0.01, target_eq / (100.0 * max(1, int(x["contracts"])))))),
        )
    )
    best = candidates[0]
    return {
        "contracts": int(best["contracts"]),
        "option_type": side,
        "delta": float(best["delta"]),
        "delta_abs": float(best["delta_abs"]),
        "strike": best.get("strike"),
        "expiry": str(best.get("expiry") or "").strip(),
        "eq_shares_per_contract": float(best["eq_shares_per_contract"]),
        "open_interest": int(best["open_interest"]),
        "volume": int(best["volume"]),
        "residual_eq": float(best["residual_eq"]),
    }


def load_real_option_hedge_contract(
    ticker: str,
    hedge_shares: float,
    *,
    preferred_expiry: str | None = None,
    min_dte: int = 1,
) -> dict[str, Any] | None:
    """
    Načíta posledný dostupný option chain z lokálnej DB a vyberie najbližší
    reálny kontrakt pre hedge.
    """
    tk = str(ticker or "").strip().upper()
    if not tk or abs(float(hedge_shares)) < 1e-9:
        return None
    try:
        from core import option_chain_db as odb
    except Exception:
        return None

    try:
        snaps = odb.list_distinct_snapshots(tk)
    except Exception:
        return None
    if snaps is None or getattr(snaps, "empty", True):
        return None

    try:
        latest_as_of = str(snaps["as_of_date"].dropna().astype(str).max() or "").strip()
    except Exception:
        latest_as_of = ""
    if not latest_as_of:
        return None

    try:
        chain = odb.read_chain(tk, as_of_date=latest_as_of)
    except Exception:
        return None
    return pick_real_option_hedge_contract(
        chain,
        hedge_shares,
        preferred_expiry=preferred_expiry,
        min_dte=min_dte,
    )


def hedge_option_alternative_hint(
    hedge_shares: float,
    *,
    ticker: str | None = None,
    preferred_expiry: str | None = None,
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
    if ticker:
        picked = load_real_option_hedge_contract(
            ticker,
            h,
            preferred_expiry=preferred_expiry,
            min_dte=1,
        )
        if picked:
            n = int(picked["contracts"])
            side = "call" if str(picked["option_type"]) == "Call" else "put"
            suffix = "kontrakt" if n == 1 else "kontrakty"
            return (
                f"Kúp {n} {side} {suffix} s deltou okolo {picked['delta']:.2f}"
                f" · strike {float(picked['strike']):.0f}"
                f" · exp {picked['expiry']}"
            )
        return "Chýba option chain pre reálnu deltu"

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
    hedge_shares: float,
    *,
    inside_deadband: bool,
    ticker: str | None = None,
    preferred_expiry: str | None = None,
) -> tuple[str, str]:
    """
    Dva stĺpce pre ``st.dataframe``: podklad vs. opčná alternatíva (inak sa dlhý text v bunke orezáva).
    """
    if inside_deadband:
        return ("Zatiaľ neobchodovať (deadband)", "—")
    base = hedge_action_label(hedge_shares)
    alt = hedge_option_alternative_hint(
        hedge_shares,
        ticker=ticker,
        preferred_expiry=preferred_expiry,
    )
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
