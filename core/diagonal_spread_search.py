"""
Hľadanie diagonálnych spreadov z lokálnej DB reťazcov (option_chain_db).

Stratégie (vždy 2 nohy: skoršia expirácia „blízka“, neskoršia „ďaleká“):
- long_call_diagonal: +1 Call(ďaleká), -1 Call(blízka)
- short_call_diagonal: -1 Call(ďaleká), +1 Call(blízka)
- long_put_diagonal: +1 Put(ďaleká), -1 Put(blízka)
- short_put_diagonal: -1 Put(ďaleká), +1 Put(blízka)

Gréky z reťazca sú za predpokladu **long 1 kontrakt**; váhy w_near / w_far zohľadňujú short/long.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional, Union

# Ďalší krok v RELAX_STEPS = úplne vypnúť filter (nastaviť pole v DiagonalSearchOptions na None).
_RELAX_DISABLE = object()

import numpy as np
import pandas as pd

from core import option_chain_db as odb

StrategyId = Literal[
    "long_call_diagonal",
    "short_call_diagonal",
    "long_put_diagonal",
    "short_put_diagonal",
]
RankMode = Literal["legacy", "score"]


@dataclass(frozen=True)
class StrategySpec:
    id: StrategyId
    option_type: str
    w_near: float
    w_far: float
    label_sk: str


STRATEGIES: dict[StrategyId, StrategySpec] = {
    "long_call_diagonal": StrategySpec(
        id="long_call_diagonal",
        option_type="Call",
        w_near=-1.0,
        w_far=1.0,
        label_sk="Long call diagonál (+Call ďaleká exp., −Call blízka exp.)",
    ),
    "short_call_diagonal": StrategySpec(
        id="short_call_diagonal",
        option_type="Call",
        w_near=1.0,
        w_far=-1.0,
        label_sk="Short call diagonál (−Call ďaleká exp., +Call blízka exp.)",
    ),
    "long_put_diagonal": StrategySpec(
        id="long_put_diagonal",
        option_type="Put",
        w_near=-1.0,
        w_far=1.0,
        label_sk="Long put diagonál (+Put ďaleká exp., −Put blízka exp.)",
    ),
    "short_put_diagonal": StrategySpec(
        id="short_put_diagonal",
        option_type="Put",
        w_near=1.0,
        w_far=-1.0,
        label_sk="Short put diagonál (−Put ďaleká exp., +Put blízka exp.)",
    ),
}


@dataclass
class DiagonalSearchOptions:
    """Voliteľné filtre a režim triedenia (None = vypnuté)."""

    spot: Optional[float] = None
    rank_mode: RankMode = "legacy"
    # Delta
    delta_tolerance: Optional[float] = None
    # Theta (reťazec; ak theta_scale_contracts=True, porovnáva sa net_theta*100)
    net_theta_min: Optional[float] = None
    net_theta_max: Optional[float] = None
    theta_scale_contracts: bool = False
    # Vega / gamma (net pozícia)
    net_vega_min: Optional[float] = None
    net_vega_max: Optional[float] = None
    net_gamma_min: Optional[float] = None
    net_gamma_max: Optional[float] = None
    # DTE skoršej / neskoršej expirácie (v kalendári; near = skorší dátum, far = neskorší)
    dte_near_min: Optional[int] = None
    dte_near_max: Optional[int] = None
    dte_far_min: Optional[int] = None
    dte_far_max: Optional[int] = None
    # Short noha vs spot (OTM)
    short_otm_min: Optional[float] = None
    # Debit na akciu / šírka strike
    max_debit_to_strike_width_ratio: Optional[float] = None
    # Relatívny spread (ask-bid)/mid na oboch nohách short/long
    max_rel_spread_short: Optional[float] = None
    max_rel_spread_long: Optional[float] = None
    min_open_interest: Optional[int] = None  # None = vypnuté; ak je OI v riadku NaN, riadok sa nevyhodí
    min_volume: Optional[int] = None
    require_iv_short_ge_long: bool = False
    iv_short_ge_long_margin: float = 0.0
    # Ktorá noha má mať strike bližší k spotu (menšie |K−spot|). None = bez filtra. Predvolene long (potrebuje spot > 0).
    strike_proximity_leg: Optional[Literal["long", "short"]] = "long"


OptScalar = Union[float, int, bool, str, None]


def _expiry_sort_key(expiry: str) -> datetime:
    return datetime.strptime(str(expiry).strip()[:10], "%Y-%m-%d")


def _dte_days(as_of_date: str, expiry: pd.Series) -> pd.Series:
    """Počet dní do expirácie od dátumu snímky (as-of)."""
    a = pd.to_datetime(as_of_date, errors="coerce")
    e = pd.to_datetime(expiry.astype(str).str[:10], errors="coerce")
    return (e - a).dt.days


def _dte_single(as_of_date: str, expiry: str) -> int:
    s = pd.Series([expiry])
    v = _dte_days(as_of_date, s).iloc[0]
    try:
        return int(v) if pd.notna(v) else 0
    except (TypeError, ValueError):
        return 0


def _subsample_strikes(strikes: list[float], max_n: int) -> list[float]:
    if max_n <= 0 or len(strikes) <= max_n:
        return strikes
    u = sorted(set(float(s) for s in strikes))
    if len(u) <= max_n:
        return u
    step = (len(u) - 1) / max(1, max_n - 1)
    idx = [int(round(i * step)) for i in range(max_n)]
    idx = sorted(set(min(i, len(u) - 1) for i in idx))
    return [u[i] for i in idx]


def list_as_of_dates(ticker: str) -> list[str]:
    """Zoradené dátumy snímky pre ticker."""
    df = odb.list_distinct_snapshots(ticker)
    if df.empty or "as_of_date" not in df.columns:
        return []
    return sorted(df["as_of_date"].astype(str).unique().tolist(), reverse=True)


def _expiries_and_dte_pair_count(
    ticker: str,
    as_of_date: str,
    strategy: StrategyId,
    opt: DiagonalSearchOptions,
) -> tuple[int, int]:
    """
    (počet distinktných expirácií, počet kalendárnych dvojíc vyhovujúcich hraniciam DTE v ``opt``)

    Rovnaký filter riadkov ako ``search_diagonal_spreads`` pred ``strike_min``/``strike_max``:
    typ opcie, strike číslo, delta aj theta. Ak reťazec vôbec nejde načítať alebo nie sú dve expirácie, ``(0|1, 0)``.
    """
    spec = STRATEGIES.get(strategy)
    if not spec:
        return (0, 0)
    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return (0, 0)
    need = {"expiry", "strike", "option_type", "delta", "theta"}
    if not need <= set(raw.columns):
        return (0, 0)
    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.loc[df["strike"].notna()]
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    df = df.loc[df["delta"].notna() & df["theta"].notna()]
    if df.empty:
        return (0, 0)
    expiries = sorted(df["expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    n_exp = len(expiries)
    if n_exp < 2:
        return (n_exp, 0)
    ok = 0
    for i, exp_near in enumerate(expiries):
        for exp_far in expiries[i + 1 :]:
            dte_n = _dte_single(as_of_date, exp_near)
            dte_f = _dte_single(as_of_date, exp_far)
            pair_ok = True
            if opt.dte_near_min is not None and dte_n < int(opt.dte_near_min):
                pair_ok = False
            if opt.dte_near_max is not None and dte_n > int(opt.dte_near_max):
                pair_ok = False
            if opt.dte_far_min is not None and dte_f < int(opt.dte_far_min):
                pair_ok = False
            if opt.dte_far_max is not None and dte_f > int(opt.dte_far_max):
                pair_ok = False
            if pair_ok:
                ok += 1
    return (n_exp, ok)


def first_dte_pair_within_bounds(
    ticker: str,
    as_of_date: str,
    strategy: StrategyId,
    opt: DiagonalSearchOptions,
) -> Optional[dict[str, Any]]:
    """
    Nájde prvú dvojicu expirácií, ktorá prejde len DTE pásmami z ``opt``.

    Vráti slovník s kľúčmi:
    ``expiry_near``, ``expiry_far``, ``dte_near``, ``dte_far``.
    Ak taká dvojica neexistuje, vráti ``None``.
    """
    spec = STRATEGIES.get(strategy)
    if not spec:
        return None
    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return None
    need = {"expiry", "strike", "option_type", "delta", "theta"}
    if not need <= set(raw.columns):
        return None
    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.loc[df["strike"].notna()]
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    df = df.loc[df["delta"].notna() & df["theta"].notna()]
    if df.empty:
        return None
    expiries = sorted(df["expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    if len(expiries) < 2:
        return None
    for i, exp_near in enumerate(expiries):
        for exp_far in expiries[i + 1 :]:
            dte_n = _dte_single(as_of_date, exp_near)
            dte_f = _dte_single(as_of_date, exp_far)
            if opt.dte_near_min is not None and dte_n < int(opt.dte_near_min):
                continue
            if opt.dte_near_max is not None and dte_n > int(opt.dte_near_max):
                continue
            if opt.dte_far_min is not None and dte_f < int(opt.dte_far_min):
                continue
            if opt.dte_far_max is not None and dte_f > int(opt.dte_far_max):
                continue
            return {
                "expiry_near": exp_near,
                "expiry_far": exp_far,
                "dte_near": dte_n,
                "dte_far": dte_f,
            }
    return None


def first_calendar_dte_pair(
    ticker: str,
    as_of_date: str,
    strategy: StrategyId,
) -> Optional[dict[str, Any]]:
    """
    Prvá dvojica expirácií v kalendárnom poradí (skorší, neskorší dátum) s dátami pre stratégiu,
    bez ohľadu na DTE filtre. Slúži na **návrh** po zlyhaní DTE brány, keď žiadne zadané
    pásma fyzicky nevyhovujú dátam v importe.
    """
    spec = STRATEGIES.get(strategy)
    if not spec:
        return None
    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return None
    need = {"expiry", "strike", "option_type", "delta", "theta"}
    if not need <= set(raw.columns):
        return None
    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.loc[df["strike"].notna()]
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    df = df.loc[df["delta"].notna() & df["theta"].notna()]
    if df.empty:
        return None
    expiries = sorted(df["expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    if len(expiries) < 2:
        return None
    exp_near, exp_far = expiries[0], expiries[1]
    dte_n = _dte_single(as_of_date, exp_near)
    dte_f = _dte_single(as_of_date, exp_far)
    return {
        "expiry_near": exp_near,
        "expiry_far": exp_far,
        "dte_near": dte_n,
        "dte_far": dte_f,
    }


def _dte_interval_penalty(x: int, lo: Optional[int], hi: Optional[int]) -> float:
    """Kvádrová penalizácia mimo [lo, hi]; ak hranica chýba, neberie sa."""
    w = 0.0
    if lo is not None and int(x) < int(lo):
        w += float(int(lo) - int(x)) ** 2
    if hi is not None and int(x) > int(hi):
        w += float(int(x) - int(hi)) ** 2
    return w


def suggest_dte_pair_closest_to_ui(
    ticker: str,
    as_of_date: str,
    strategy: StrategyId,
    opt: DiagonalSearchOptions,
) -> Optional[dict[str, Any]]:
    """
    Dvojica expirácií, ktorá je **najbližšia** (v zmysle L2 „vzdialenosti“) k **aktuálnym** DTE pásmam
    v ``opt`` (len zapnuté filtre, None = neohraničené strany). Pri remíze skoršia v kalendári.

    Ak DTE v ``opt`` nie sú vôbec zapnuté, vráti tú istú vec ako ``first_calendar_dte_pair``.
    """
    spec = STRATEGIES.get(strategy)
    if not spec:
        return None
    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return None
    need = {"expiry", "strike", "option_type", "delta", "theta"}
    if not need <= set(raw.columns):
        return None
    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.loc[df["strike"].notna()]
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    df = df.loc[df["delta"].notna() & df["theta"].notna()]
    if df.empty:
        return None
    expiries = sorted(df["expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    if len(expiries) < 2:
        return None
    has_any_dte = any(
        getattr(opt, f) is not None
        for f in ("dte_near_min", "dte_near_max", "dte_far_min", "dte_far_max")
    )
    if not has_any_dte:
        return first_calendar_dte_pair(ticker, as_of_date, strategy)
    best_score: float = float("inf")
    best_ij: tuple[int, int] = (10**9, 10**9)
    best: dict[str, Any] | None = None
    for i, exp_near in enumerate(expiries):
        for j in range(i + 1, len(expiries)):
            exp_far = expiries[j]
            dte_n = _dte_single(as_of_date, exp_near)
            dte_f = _dte_single(as_of_date, exp_far)
            score = 0.0
            score += _dte_interval_penalty(
                dte_n, opt.dte_near_min, opt.dte_near_max
            )
            score += _dte_interval_penalty(
                dte_f, opt.dte_far_min, opt.dte_far_max
            )
            t_ij = (i, j)
            if score < best_score - 1e-9 or (abs(float(score) - best_score) < 1e-9 and t_ij < best_ij):
                best_score = float(score)
                best_ij = t_ij
                best = {
                    "expiry_near": exp_near,
                    "expiry_far": exp_far,
                    "dte_near": dte_n,
                    "dte_far": dte_f,
                    "distance_score": float(score),
                }
    return best


DTE_VIOLATION_CODE_SK: dict[str, str] = {
    "skoršia_min": "skoršia DTE pod dolnou hranicou (páslo skoršia — min.)",
    "skoršia_max": "skoršia DTE nad hornou hranicou (páslo skoršia — max.)",
    "neskoršia_min": "neskoršia DTE pod dolnou hranicou (páslo neskoršia — min.)",
    "neskoršia_max": "neskoršia DTE nad hornou hranicou (páslo neskoršia — max.)",
}


def dte_pair_band_violation_codes(
    dte_n: int,
    dte_f: int,
    opt: DiagonalSearchOptions,
) -> list[str]:
    """Ktoré zadané pásma DTE dvojica porušuje (prázdne = vyhovuje). Rovnaká logika ako v ``_expiries_and_dte_pair_count``."""
    codes: list[str] = []
    if opt.dte_near_min is not None and dte_n < int(opt.dte_near_min):
        codes.append("skoršia_min")
    if opt.dte_near_max is not None and dte_n > int(opt.dte_near_max):
        codes.append("skoršia_max")
    if opt.dte_far_min is not None and dte_f < int(opt.dte_far_min):
        codes.append("neskoršia_min")
    if opt.dte_far_max is not None and dte_f > int(opt.dte_far_max):
        codes.append("neskoršia_max")
    return codes


def _dte_bands_caption_line(opt: DiagonalSearchOptions) -> str:
    def _one(lo: Optional[int], hi: Optional[int], label: str) -> str:
        if lo is None and hi is None:
            return f"**{label}:** (neobmedzené)"
        a = f"{int(lo)}" if lo is not None else "—"
        b = f"{int(hi)}" if hi is not None else "—"
        return f"**{label}:** {a}–{b} dní"

    return _one(opt.dte_near_min, opt.dte_near_max, "skoršia (skorší dátum v dvojici)") + "; " + _one(
        opt.dte_far_min, opt.dte_far_max, "neskoršia (neskorší dátum v dvojici)"
    )


def _calendar_pair_count(n_expiries: int) -> int:
    return n_expiries * (n_expiries - 1) // 2 if n_expiries >= 2 else 0


def dte_calendar_diagnostic_markdown(
    ticker: str,
    as_of_date: str,
    strategy: StrategyId,
    opt: DiagonalSearchOptions,
) -> str:
    """
    Markdown: zoznam expirácií s DTE a pre každú kalendárnu dvojicu, či vyhovuje pásmam alebo **ktoré** pásma režú.
    Prázdny reťazec, ak v ``opt`` nie sú žiadne DTE hranice.
    """
    if not any(
        getattr(opt, f) is not None
        for f in ("dte_near_min", "dte_near_max", "dte_far_min", "dte_far_max")
    ):
        return ""
    spec = STRATEGIES.get(strategy)
    if not spec:
        return ""
    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return "**DTE diagnostika:** v reťazci pre tento dátum snímky **nie sú dáta**."
    need = {"expiry", "strike", "option_type", "delta", "theta"}
    if not need <= set(raw.columns):
        return "**DTE diagnostika:** v importe chýbajú stĺpce pre túto kontrolu."
    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.loc[df["strike"].notna()]
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    df = df.loc[df["delta"].notna() & df["theta"].notna()]
    if df.empty:
        return "**DTE diagnostika:** žiadne riadky s delta+theta pre túto stratégiu."
    expiries = sorted(df["expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    if len(expiries) < 2:
        return "**DTE diagnostika:** v dátach je menej ako **dve** distinktné expirácie s delta+theta (diagonál nevznikne)."

    parts: list[str] = [
        f"Zadané pásma z hľadania: {_dte_bands_caption_line(opt)}.",
        "",
        "**Expirácie v dátach** (kalendárne poradie, DTE = dni od dátumu snímky k expirácii):",
        "",
        "| # | Expirácia | DTE |",
        "|---:|:---|---:|",
    ]
    for idx, e in enumerate(expiries, start=1):
        d = _dte_single(as_of_date, e)
        parts.append(f"| {idx} | {str(e)[:10]} | {d} |")
    parts.append("")
    parts.append(
        "**Kalendárne dvojice** (skorší dátum = *skoršia* noha, neskorší = *neskoršia*; v každom riadku DTE vľavo vždy ku skoršej expirácii, vpravo ku neskoršej):"
    )
    parts.append("")
    parts.append(
        "| Skoršia exp. | Neskoršia exp. | DTE skoršia | DTE neskoršia | Vyhovuje pásam? | Čo nevyhovuje |"
    )
    parts.append("|:---|:---|---:|---:|:---|:---|")

    max_show = 60
    type_counts: Counter[str] = Counter()
    real_ok = 0
    table_row = 0
    total_pairs = _calendar_pair_count(len(expiries))
    for i, exp_near in enumerate(expiries):
        for exp_far in expiries[i + 1 :]:
            dte_n = _dte_single(as_of_date, exp_near)
            dte_f = _dte_single(as_of_date, exp_far)
            vcodes = dte_pair_band_violation_codes(dte_n, dte_f, opt)
            if not vcodes:
                real_ok += 1
            else:
                for c in vcodes:
                    type_counts[c] += 1
            table_row += 1
            if table_row > max_show:
                continue
            if not vcodes:
                parts.append(
                    f"| {str(exp_near)[:10]} | {str(exp_far)[:10]} | {dte_n} | {dte_f} | áno | — |"
                )
            else:
                vtxt = ", ".join(DTE_VIOLATION_CODE_SK.get(c, c) for c in vcodes)
                parts.append(
                    f"| {str(exp_near)[:10]} | {str(exp_far)[:10]} | {dte_n} | {dte_f} | **nie** | {vtxt} |"
                )

    if total_pairs > max_show:
        parts.append(
            f"| … | … | — | — | *({total_pairs - max_show} ďalších dvojíc, tabuľka skrátená)* | |"
        )

    parts.append("")
    if real_ok > 0:
        parts.append(
            f"**Súčet:** z **{total_pairs}** kalendárnych dvojíc **{real_ok}** vyhovuje (aspoň) zadaným DTE pásam. "
            "Ak hľadanie pritom dalo 0 riadkov, pád je v **iných** filtroch (delta, theta, OTM, likvidita, …), nie v DTE pásmach."
        )
    else:
        if type_counts:
            top = ", ".join(
                f"**{DTE_VIOLATION_CODE_SK.get(k, k)}** ({n}×)" for k, n in type_counts.most_common(4)
            )
            parts.append(
                f"**Súčet:** z **{total_pairs}** dvojíc **žiadna** nesplní súčasne páslo skoršia aj páslo neskoršia. "
                f"Najčastejšie, čo reže (počet výskytov u dvojíc, jedna dvojica môže narušiť obe nohy): {top}."
            )
        else:
            parts.append("**Súčet:** žiadna dvojica; skontroluj, či majú pásma zmysel.")

    return "\n".join(parts)


def diagonal_search_why_empty_hint(
    ticker: str,
    *,
    as_of_date: str,
    strategy: StrategyId,
    opt: DiagonalSearchOptions,
) -> str:
    """
    Krátky text (Markdown) pre UI, keď ``search_diagonal_spreads`` vráti prázdno —
    najčastejšie príliš prísne **DTE** (najmä „ďaleká min“) alebo theta/vega/OTM pri kratších reťazcoch (GLD, CASY).
    """
    spec = STRATEGIES.get(strategy)
    if not spec:
        return ""
    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return "V **DB Grékov** nie sú riadky pre tento ticker a dátum snímky."
    need = {"expiry", "strike", "option_type", "delta", "theta"}
    miss = need - set(raw.columns)
    if miss:
        return f"V importe chýbajú stĺpce: **{', '.join(sorted(miss))}**."
    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    if df.empty:
        return (
            f"V tejto snímke nie sú riadky typu **{spec.option_type}**. Skús prepnúť stratégiu "
            "(Call/Put diagonál podľa importu)."
        )
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    both = df["delta"].notna() & df["theta"].notna()
    n_ok = int(both.sum())
    if n_ok == 0:
        return "Žiadny riadok nemá súčasne vyplnenú **delta** aj **theta** — skontroluj import volatility greeks."
    expiries = sorted(df.loc[both, "expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    if len(expiries) < 2:
        return (
            f"Po filtri delta+theta ostáva len **{len(expiries)}** expirácia(−e); diagonál potrebuje **aspoň dve**. "
            "Importuj ďalší reťazec z Barchartu (iná expirácia)."
        )
    dtes = [_dte_single(as_of_date, e) for e in expiries]
    parts = [
        f"Riadkov s delta+theta: **{n_ok}**, expirácií **{spec.option_type}**: **{len(expiries)}**, "
        f"DTE od snímky: **{min(dtes)}**–**{max(dtes)}** dní."
    ]
    ok_dte_pairs = 0
    for i, exp_near in enumerate(expiries):
        for exp_far in expiries[i + 1 :]:
            dte_n = _dte_single(as_of_date, exp_near)
            dte_f = _dte_single(as_of_date, exp_far)
            ok = True
            if opt.dte_near_min is not None and dte_n < int(opt.dte_near_min):
                ok = False
            if opt.dte_near_max is not None and dte_n > int(opt.dte_near_max):
                ok = False
            if opt.dte_far_min is not None and dte_f < int(opt.dte_far_min):
                ok = False
            if opt.dte_far_max is not None and dte_f > int(opt.dte_far_max):
                ok = False
            if ok:
                ok_dte_pairs += 1
    if ok_dte_pairs == 0:
        parts.append(
            "**Žiadna dvojica expirácií** (skorší dátum = *skoršia* exp., neskorší = *neskoršia*) nevyhovuje filtru DTE. "
            "Pri ETF a kratších reťazcoch je často vinné **min. DTE neskoršej** (napr. 90 dní pri striktných predvolbách): "
            "ak najdlhšia expirácia v DB má menej dní, zníž **Neskoršia min** (napr. 35–50) alebo DTE filtre vypni."
        )
    else:
        parts.append(
            f"Dvojíc vyhovujúcich DTE je **{ok_dte_pairs}** — zvyšok pravdepodobne vyhodili **theta / vega / gamma / OTM / debit** "
            "alebo rel. spready. V Pokročilých ich postupne vypni alebo rozšír pásma."
        )
    if opt.spot is not None and float(opt.spot) > 0 and opt.short_otm_min is not None:
        spot_f = float(opt.spot)
        otm_min = float(opt.short_otm_min)
        parts.append(
            f"**OTM short** (spot **{spot_f:.2f}**, min. **{otm_min:.2f}**) vie byť prísny — skús vypnúť alebo over spot v **Symboly**."
        )
        if spec.option_type == "Call":
            need_k = spot_f * (1.0 + otm_min)
        else:
            need_k = spot_f * (1.0 - otm_min)
        df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
        mx = float(df.loc[both, "strike"].max()) if both.any() else float("nan")
        if pd.notna(mx) and mx < need_k - 1e-6:
            parts.append(
                f"**Strike v DB** — max. **{mx:g}** pri **{spec.option_type}**, pri tomto OTM treba aspoň strike **≈ {need_k:.2f}** — "
                "v importe chýbajú ďaleké OTM strikey (Barchart: rozšír rozsah strike-ov)."
            )
    return " ".join(parts)


def diagonal_search_precheck_warnings_markdown(
    ticker: str,
    *,
    as_of_date: str,
    strategy: StrategyId,
    opt: DiagonalSearchOptions,
) -> str:
    """Krátke varovania pred hľadaním (dáta vs. OTM / vega / bid-ask). Prázdny reťazec = OK."""
    spec = STRATEGIES.get(strategy)
    if not spec:
        return ""
    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return ""
    need = {"expiry", "strike", "option_type", "delta", "theta"}
    if need - set(raw.columns):
        return ""
    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    if df.empty:
        return ""
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    both = df["delta"].notna() & df["theta"].notna()
    lines: list[str] = []
    if opt.spot is not None and float(opt.spot) > 0 and opt.short_otm_min is not None:
        spot_f = float(opt.spot)
        otm_min = float(opt.short_otm_min)
        if spec.option_type == "Call":
            need_k = spot_f * (1.0 + otm_min)
        else:
            need_k = spot_f * (1.0 - otm_min)
        df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
        mx = float(df.loc[both, "strike"].max()) if both.any() else float("nan")
        if pd.notna(mx) and mx < need_k - 1e-6:
            lines.append(
                f"- **OTM short:** v DB je max. strike **{mx:g}**, pri min. OTM **{otm_min:g}** a spote **{spot_f:.2f}** by bolo treba aspoň **≈ {need_k:.2f}** — "
                "doplniť import (širší rozsah strike-ov z Barchartu) alebo znížiť / vypnúť OTM filter."
            )
    if (
        opt.net_vega_max is not None
        and opt.spot is not None
        and float(opt.spot) > 200.0
        and float(opt.net_vega_max) < 0.4
    ):
        lines.append(
            "- **Vega max:** pri drahších podkladových (spot **> 200**) je čistá vega často **> 0,2** na akciu — "
            f"prah **{float(opt.net_vega_max):g}** môže vyhodiť všetko; zváž **0,35–0,60** alebo vypni horný limit."
        )
    if "bid" in df.columns and "ask" in df.columns:
        b = pd.to_numeric(df["bid"], errors="coerce")
        a = pd.to_numeric(df["ask"], errors="coerce")
        n = int(both.sum())
        if n > 0:
            miss = int((b.isna() | a.isna()).loc[both].sum())
            if miss / n >= 0.8 and (opt.max_debit_to_strike_width_ratio is not None or opt.max_rel_spread_short is not None):
                lines.append(
                    f"- **Bid/ask:** pri **{spec.option_type}** chýba bid alebo ask na **{miss}/{n}** riadkoch — debit a rel. spread môžu byť prázdne; "
                    "vypni **Debit/šírka** alebo doplniť CSV s cenami."
                )
    if not lines:
        return ""
    return "**Kontrola pred hľadaním**\n\n" + "\n".join(lines)


def diagonal_relax_suggestions_markdown(opt: DiagonalSearchOptions) -> str:
    """
    Stručné návrhy zjemnenia filtrov podľa toho, čo je v ``opt`` zapnuté / nastavené prísne.
    (Použije sa po prvom hľadaní s prázdym výsledkom.)
    """
    bullets: list[str] = []
    if opt.dte_far_min is not None and int(opt.dte_far_min) >= 60:
        bullets.append(
            f"**DTE ďaleká min** ({int(opt.dte_far_min)} dní) zníž napr. na **35–50**, ak najdlhšia expirácia v importe nemá dosť dní."
        )
    if opt.dte_near_max is not None and int(opt.dte_near_max) <= 55:
        bullets.append("**DTE skoršej exp. max** zvýš (napr. na **60**), ak skoršie expirácie padajú mimo pásmo.")
    if opt.dte_near_min is not None and int(opt.dte_near_min) >= 18:
        bullets.append("**DTE skoršej exp. min** zníž (napr. na **10**), ak máš len kratšie týždňové reťazce.")
    if opt.theta_scale_contracts:
        if opt.net_theta_min is not None and float(opt.net_theta_min) >= 2.0:
            bullets.append(
                "**Theta (×100):** spodný prah zníž (napr. **0,5**) a horný zvýš (napr. **15**) — ETF majú menšie čísla."
            )
    else:
        if opt.net_theta_min is not None:
            bullets.append("**Theta:** rozšír min./max. v jednotkách reťazca alebo zapni **×100** a nastav širšie prahy.")
    if opt.net_vega_min is not None and float(opt.net_vega_min) >= 0.08:
        bullets.append("**Vega:** min. zníž (napr. **0,05**), max. zvýš (napr. **0,35**).")
    if opt.net_vega_max is not None and float(opt.net_vega_max) <= 0.22:
        bullets.append("**Vega max** zvýš (napr. **0,35**), ak horná hranica reže príliš veľa.")
    if opt.short_otm_min is not None and opt.spot is not None and float(opt.spot) > 0:
        bullets.append("**Min. OTM short** dočasne vypni alebo over **spot** v Symboly.")
    if opt.max_debit_to_strike_width_ratio is not None:
        bullets.append("**Debit/šírka strike** alebo **rel. spready** dočasne vypni, ak likvidita v CSV nie je ideálna.")
    if opt.min_open_interest is not None and int(opt.min_open_interest) >= 100:
        bullets.append("**Min. OI** zníž alebo vypni (ak je OI v CSV, musí sedieť na oboch nohách).")
    if not bullets:
        bullets.append("V **Pokročilých** postupne vypínaj filtre od **theta/vega** cez **DTE** a znova **Hľadať**.")
    lines = ["**Navrhované zjemnenie:**"] + [f"- {b}" for b in bullets]
    lines.append(
        "- Alebo v Pokročilých klikni **„Širšie filtre (ETF / kratší reťazec)“** — nastaví širšie predvolby naraz."
    )
    return "\n".join(lines)


def _rel_spread(bid: pd.Series, ask: pd.Series, mid: pd.Series) -> pd.Series:
    b = pd.to_numeric(bid, errors="coerce")
    a = pd.to_numeric(ask, errors="coerce")
    m = pd.to_numeric(mid, errors="coerce")
    num = (a - b).abs()
    den = m.where(m > 1e-9, np.nan)
    return num / den


def _short_long_columns(spec: StrategySpec, cart: pd.DataFrame):
    """bid, ask, mid, iv, vega, gamma, oi, vol pre short a long nohu (podľa stratégie)."""
    if spec.w_near < 0:
        sb, sa, sm = cart["bid_near"], cart["ask_near"], cart["mid_near"]
        lb, la, lm = cart["bid_far"], cart["ask_far"], cart["mid_far"]
        iv_s, iv_l = cart["iv_near"], cart["iv_far"]
        vg_s, vg_l = cart["vega_near"], cart["vega_far"]
        gm_s, gm_l = cart["gamma_near"], cart["gamma_far"]
        oi_s, oi_l = cart["oi_near"], cart["oi_far"]
        vl_s, vl_l = cart["vol_near"], cart["vol_far"]
    else:
        sb, sa, sm = cart["bid_far"], cart["ask_far"], cart["mid_far"]
        lb, la, lm = cart["bid_near"], cart["ask_near"], cart["mid_near"]
        iv_s, iv_l = cart["iv_far"], cart["iv_near"]
        vg_s, vg_l = cart["vega_far"], cart["vega_near"]
        gm_s, gm_l = cart["gamma_far"], cart["gamma_near"]
        oi_s, oi_l = cart["oi_far"], cart["oi_near"]
        vl_s, vl_l = cart["vol_far"], cart["vol_near"]
    return sb, sa, sm, lb, la, lm, iv_s, iv_l, vg_s, vg_l, gm_s, gm_l, oi_s, oi_l, vl_s, vl_l


def _short_long_strikes(spec: StrategySpec, cart: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    if spec.w_near < 0:
        return cart["strike_near"], cart["strike_far"]
    return cart["strike_far"], cart["strike_near"]


def _otm_short_pct(option_type: str, spot: float, short_strike: pd.Series) -> pd.Series:
    sk = pd.to_numeric(short_strike, errors="coerce")
    if spot <= 0:
        return pd.Series(np.nan, index=short_strike.index)
    if option_type == "Call":
        return (sk - float(spot)) / float(spot)
    return (float(spot) - sk) / float(spot)


def _compute_score(
    net_delta: pd.Series,
    net_theta: pd.Series,
    net_vega: pd.Series,
    net_gamma: pd.Series,
    debit_per_share: pd.Series,
    strike_width: pd.Series,
) -> pd.Series:
    eps = 1e-6
    nd = pd.to_numeric(net_delta, errors="coerce")
    nt = pd.to_numeric(net_theta, errors="coerce")
    nv = pd.to_numeric(net_vega, errors="coerce")
    ng = pd.to_numeric(net_gamma, errors="coerce")
    dps = pd.to_numeric(debit_per_share, errors="coerce")
    sw = pd.to_numeric(strike_width, errors="coerce").abs().clip(lower=eps)
    ratio = dps.abs() / sw
    return (1.0 / (nd.abs() + eps)) * 30.0 + nt * 10.0 + nv * 2.0 - ng.abs() * 100.0 - ratio * 50.0


def _apply_row_filters(
    cart: pd.DataFrame,
    spec: StrategySpec,
    target_net_delta: float,
    opt: DiagonalSearchOptions,
) -> pd.DataFrame:
    if cart.empty:
        return cart
    m = pd.Series(True, index=cart.index)

    # Záporná čistá theta = v tomto modeli *strata* (decay proti pozícii) — nenaťahovať do výsledku.
    _nt = pd.to_numeric(cart["net_theta"], errors="coerce")
    m &= _nt.isna() | (_nt >= 0)

    if opt.delta_tolerance is not None:
        tol = float(opt.delta_tolerance)
        m &= (cart["net_delta"] - target_net_delta).abs() <= tol

    th = cart["net_theta"].astype(float)
    if opt.theta_scale_contracts:
        th = th * 100.0
    if opt.net_theta_min is not None:
        m &= th >= float(opt.net_theta_min)
    if opt.net_theta_max is not None:
        m &= th <= float(opt.net_theta_max)

    if opt.net_vega_min is not None:
        nv = cart["net_vega"]
        m &= nv.isna() | (nv >= float(opt.net_vega_min))
    if opt.net_vega_max is not None:
        nv = cart["net_vega"]
        m &= nv.isna() | (nv <= float(opt.net_vega_max))
    if opt.net_gamma_min is not None:
        ng = cart["net_gamma"]
        m &= ng.isna() | (ng >= float(opt.net_gamma_min))
    if opt.net_gamma_max is not None:
        ng = cart["net_gamma"]
        m &= ng.isna() | (ng <= float(opt.net_gamma_max))

    if opt.short_otm_min is not None and opt.spot is not None and float(opt.spot) > 0:
        sk_s, _ = _short_long_strikes(spec, cart)
        otm = _otm_short_pct(spec.option_type, float(opt.spot), sk_s)
        m &= otm >= float(opt.short_otm_min)

    if opt.max_debit_to_strike_width_ratio is not None:
        sw = cart["strike_width"].replace(0, np.nan)
        ratio = cart["debit_per_share"].abs() / sw
        m &= ratio <= float(opt.max_debit_to_strike_width_ratio)

    if opt.max_rel_spread_short is not None:
        m &= (cart["rel_spread_short"].isna()) | (cart["rel_spread_short"] <= float(opt.max_rel_spread_short))
    if opt.max_rel_spread_long is not None:
        m &= (cart["rel_spread_long"].isna()) | (cart["rel_spread_long"] <= float(opt.max_rel_spread_long))

    if opt.min_open_interest is not None:
        moi = int(opt.min_open_interest)
        ois = pd.to_numeric(cart["oi_short"], errors="coerce")
        oil = pd.to_numeric(cart["oi_long"], errors="coerce")
        # Chýbajúce OI v importe ≠ 0 — inak by prah 100 vyhodil všetko, keď CSV nemá stĺpec OI.
        m &= (ois.isna() | (ois >= moi)) & (oil.isna() | (oil >= moi))
    if opt.min_volume is not None:
        mv = int(opt.min_volume)
        vs = pd.to_numeric(cart["vol_short"], errors="coerce")
        vl = pd.to_numeric(cart["vol_long"], errors="coerce")
        m &= (vs.isna() | (vs >= mv)) & (vl.isna() | (vl >= mv))

    if opt.require_iv_short_ge_long:
        mar = float(opt.iv_short_ge_long_margin)
        m &= (cart["iv_short"].notna()) & (cart["iv_long"].notna()) & (cart["iv_short"] >= cart["iv_long"] + mar)

    if opt.strike_proximity_leg in ("long", "short") and opt.spot is not None and float(opt.spot) > 0:
        sk_s, lk_s = _short_long_strikes(spec, cart)
        spt = float(opt.spot)
        d_s = (pd.to_numeric(sk_s, errors="coerce") - spt).abs()
        d_l = (pd.to_numeric(lk_s, errors="coerce") - spt).abs()
        if opt.strike_proximity_leg == "long":
            m &= d_l <= d_s + 1e-9
        else:
            m &= d_s <= d_l + 1e-9

    return cart.loc[m.fillna(False)]


def search_diagonal_spreads(
    ticker: str,
    *,
    as_of_date: str,
    strategy: StrategyId,
    target_net_delta: float = 0.0,
    top_n: int = 40,
    max_strikes_per_expiry: int = 55,
    strike_min: Optional[float] = None,
    strike_max: Optional[float] = None,
    options: Optional[DiagonalSearchOptions] = None,
) -> pd.DataFrame:
    """
    Kombinácie blízka / ďaleká expirácia, kríž strike-ov.

    ``options``: pokročilé filtre a ``rank_mode`` ``legacy`` (delta_err, net_theta) alebo ``score``.
    """
    spec = STRATEGIES.get(strategy)
    if not spec:
        raise ValueError(f"Neznáma stratégia: {strategy!r}")
    opt = options or DiagonalSearchOptions()

    if strike_min is not None and strike_max is not None and float(strike_min) > float(strike_max):
        strike_min, strike_max = float(strike_max), float(strike_min)

    raw = odb.read_chain(ticker, as_of_date=as_of_date)
    if raw.empty:
        return pd.DataFrame()

    need = {"expiry", "strike", "option_type", "delta", "theta"}
    miss = need - set(raw.columns)
    if miss:
        return pd.DataFrame()

    df = raw.loc[raw["option_type"].astype(str) == spec.option_type].copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.loc[df["strike"].notna()]
    df["delta"] = pd.to_numeric(df["delta"], errors="coerce")
    df["theta"] = pd.to_numeric(df["theta"], errors="coerce")
    df = df.loc[df["delta"].notna() & df["theta"].notna()]
    for col in ("iv", "gamma", "vega", "volume", "open_interest"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = np.nan

    if strike_min is not None:
        df = df.loc[df["strike"] >= float(strike_min)]
    if strike_max is not None:
        df = df.loc[df["strike"] <= float(strike_max)]
    if df.empty:
        return pd.DataFrame()

    expiries = sorted(df["expiry"].astype(str).unique().tolist(), key=_expiry_sort_key)
    if len(expiries) < 2:
        return pd.DataFrame()

    extra_cols = ["iv", "gamma", "vega", "volume", "open_interest"]
    blocks: list[pd.DataFrame] = []

    for i, exp_near in enumerate(expiries):
        for exp_far in expiries[i + 1 :]:
            dte_n = _dte_single(as_of_date, exp_near)
            dte_f = _dte_single(as_of_date, exp_far)
            if opt.dte_near_min is not None and dte_n < int(opt.dte_near_min):
                continue
            if opt.dte_near_max is not None and dte_n > int(opt.dte_near_max):
                continue
            if opt.dte_far_min is not None and dte_f < int(opt.dte_far_min):
                continue
            if opt.dte_far_max is not None and dte_f > int(opt.dte_far_max):
                continue

            sub_n = df.loc[df["expiry"] == exp_near].copy()
            sub_f = df.loc[df["expiry"] == exp_far].copy()
            if sub_n.empty or sub_f.empty:
                continue

            strikes_n = _subsample_strikes(sub_n["strike"].tolist(), max_strikes_per_expiry)
            strikes_f = _subsample_strikes(sub_f["strike"].tolist(), max_strikes_per_expiry)
            sub_n = sub_n.loc[sub_n["strike"].isin(strikes_n)].copy()
            sub_f = sub_f.loc[sub_f["strike"].isin(strikes_f)].copy()

            for _sub in (sub_n, sub_f):
                for col in ("bid", "ask", "mid"):
                    if col not in _sub.columns:
                        _sub[col] = pd.NA

            cols_n = ["strike", "delta", "theta", "bid", "ask", "mid"] + [
                c for c in extra_cols if c in sub_n.columns
            ]
            cols_f = ["strike", "delta", "theta", "bid", "ask", "mid"] + [
                c for c in extra_cols if c in sub_f.columns
            ]

            ren_n = {
                "strike": "strike_near",
                "delta": "delta_near",
                "theta": "theta_near",
                "bid": "bid_near",
                "ask": "ask_near",
                "mid": "mid_near",
                "iv": "iv_near",
                "gamma": "gamma_near",
                "vega": "vega_near",
                "volume": "vol_near",
                "open_interest": "oi_near",
            }
            ren_f = {src: tgt.replace("_near", "_far") for src, tgt in ren_n.items()}

            near_leg = sub_n[[c for c in cols_n if c in sub_n.columns]].rename(
                columns={k: ren_n[k] for k in ren_n if k in cols_n}
            )
            far_leg = sub_f[[c for c in cols_f if c in sub_f.columns]].rename(
                columns={k: ren_f[k] for k in ren_f if k in cols_f}
            )
            for c in ("iv_near", "gamma_near", "vega_near", "vol_near", "oi_near"):
                if c not in near_leg.columns:
                    near_leg[c] = np.nan
            for c in ("iv_far", "gamma_far", "vega_far", "vol_far", "oi_far"):
                if c not in far_leg.columns:
                    far_leg[c] = np.nan

            near_leg["_k"] = 1
            far_leg["_k"] = 1

            cart = near_leg.merge(far_leg, on="_k").drop(columns="_k")
            if cart.empty:
                continue

            cart["net_delta"] = spec.w_near * cart["delta_near"] + spec.w_far * cart["delta_far"]
            cart["net_theta"] = spec.w_near * cart["theta_near"] + spec.w_far * cart["theta_far"]
            cart["net_vega"] = spec.w_near * cart["vega_near"] + spec.w_far * cart["vega_far"]
            cart["net_gamma"] = spec.w_near * cart["gamma_near"] + spec.w_far * cart["gamma_far"]
            cart["delta_err"] = (cart["net_delta"] - target_net_delta).abs()

            sb, sa, sm, lb, la, lm, iv_s, iv_l, vg_s, vg_l, gm_s, gm_l, oi_s, oi_l, vl_s, vl_l = _short_long_columns(spec, cart)
            cart["iv_short"] = pd.to_numeric(iv_s, errors="coerce")
            cart["iv_long"] = pd.to_numeric(iv_l, errors="coerce")
            cart["oi_short"] = pd.to_numeric(oi_s, errors="coerce")
            cart["oi_long"] = pd.to_numeric(oi_l, errors="coerce")
            cart["vol_short"] = pd.to_numeric(vl_s, errors="coerce")
            cart["vol_long"] = pd.to_numeric(vl_l, errors="coerce")
            cart["rel_spread_short"] = _rel_spread(sb, sa, sm)
            cart["rel_spread_long"] = _rel_spread(lb, la, lm)

            sk, lk = _short_long_strikes(spec, cart)
            cart["debit_per_share"] = pd.to_numeric(la, errors="coerce") - pd.to_numeric(sb, errors="coerce")
            cart["strike_width"] = (pd.to_numeric(lk, errors="coerce") - pd.to_numeric(sk, errors="coerce")).abs()

            if opt.spot is not None and float(opt.spot) > 0:
                cart["short_otm_pct"] = _otm_short_pct(spec.option_type, float(opt.spot), sk)
            else:
                cart["short_otm_pct"] = np.nan

            cart["score"] = _compute_score(
                cart["net_delta"], cart["net_theta"], cart["net_vega"], cart["net_gamma"],
                cart["debit_per_share"], cart["strike_width"],
            )

            cart["strategia"] = spec.label_sk
            cart["strategia_id"] = spec.id
            cart["expiracia_near"] = exp_near
            cart["expiracia_far"] = exp_far
            cart["typ"] = spec.option_type

            cart = _apply_row_filters(cart, spec, target_net_delta, opt)
            if not cart.empty:
                blocks.append(cart)

    if not blocks:
        return pd.DataFrame()

    out = pd.concat(blocks, ignore_index=True)
    if opt.rank_mode == "score":
        out = out.sort_values(by=["score", "delta_err"], ascending=[False, True], kind="mergesort")
    else:
        out = out.sort_values(by=["delta_err", "net_theta"], ascending=[True, False], kind="mergesort")
    out = out.head(int(max(1, top_n))).reset_index(drop=True)
    return _to_display_spread_table(out, spec, as_of_date)


def _to_display_spread_table(out: pd.DataFrame, spec: StrategySpec, as_of_date: str) -> pd.DataFrame:
    """
    Prehľadná tabuľka: noha short (DTE, exp, strike, bid), noha long (DTE, exp, strike, ask),
    čistá delta/theta/vega/gamma, debit/kredit na **1 lot (100 akcií)**, orientačné **APR %** z theta/debitu.

    **Čistá delta**, **čistá theta** a **čistá vega** sú v tabuľke ako ``hodnota_z_reťazca × 100``
    (lepšia čitateľnosť; filtre a skóre stále počítajú z pôvodných jednotiek v ``search_diagonal_spreads``).

    **APR % (rát.)** = ``(net_theta×100 $/deň) / |debit v $| × 365 × 100`` — hrubé porovnanie riadkov, nie účtovná výkonnosť.
    """
    if out.empty:
        return out

    for c in ("bid_near", "ask_near", "bid_far", "ask_far"):
        if c not in out.columns:
            out[c] = pd.NA

    if spec.w_near < 0:
        short_exp, long_exp = out["expiracia_near"], out["expiracia_far"]
        short_k, long_k = out["strike_near"], out["strike_far"]
        short_bid, long_ask = out["bid_near"], out["ask_far"]
    else:
        short_exp, long_exp = out["expiracia_far"], out["expiracia_near"]
        short_k, long_k = out["strike_far"], out["strike_near"]
        short_bid, long_ask = out["bid_far"], out["ask_near"]

    sb = pd.to_numeric(short_bid, errors="coerce")
    la = pd.to_numeric(long_ask, errors="coerce")
    debit_per_share = la - sb
    debit_lot = debit_per_share * 100.0
    net_theta = pd.to_numeric(out["net_theta"], errors="coerce")
    theta_usd_per_day = net_theta * 100.0
    d_abs = debit_lot.abs()
    safe_d = d_abs.where(d_abs > 1e-9)
    apr_s = (theta_usd_per_day / safe_d) * 365.0 * 100.0

    slim = pd.DataFrame(
        {
            "Typ": out["typ"].astype(str),
            "Short — DTE": _dte_days(as_of_date, short_exp),
            "Short — expirácia": short_exp.astype(str),
            "Short — strike": pd.to_numeric(short_k, errors="coerce"),
            "Short — bid": sb,
            "Long — DTE": _dte_days(as_of_date, long_exp),
            "Long — expirácia": long_exp.astype(str),
            "Long — strike": pd.to_numeric(long_k, errors="coerce"),
            "Long — ask": la,
            "Čistá delta ×100": pd.to_numeric(out["net_delta"], errors="coerce") * 100.0,
            "Čistá theta (+ príjem / − strata) ×100": net_theta * 100.0,
            "Čistá vega ×100": pd.to_numeric(out["net_vega"], errors="coerce") * 100.0,
            "Čistá gamma": pd.to_numeric(out["net_gamma"], errors="coerce"),
            "Debit/kredit ($/1 lot ×100)": debit_lot,
            "APR % (rát.)": apr_s,
        }
    )
    if "score" in out.columns:
        slim["Skóre"] = pd.to_numeric(out["score"], errors="coerce")
    return slim


# ---------------------------------------------------------------------------
# Postupné filtrovanie s automatickým zjemnením
# ---------------------------------------------------------------------------


@dataclass
class FilterStep:
    """Záznam o jednom filtri v protokole."""
    name: str
    passed: bool
    original: OptScalar
    relaxed_to: OptScalar
    rows_before: int
    rows_after: int


@dataclass
class FilterFailureStep:
    """Jeden filter v poradí — po zjemnení stále 0 výsledkov (alebo sa nedá ďalej zjemniť)."""
    label: str
    field: str
    start_value: OptScalar
    values_attempted: list[OptScalar]
    reason_sk: str


@dataclass
class FilterLog:
    """Protokol o postupnom filtrovaní."""
    steps: list[FilterStep]
    final_rows: int
    any_relaxed: bool
    failure_steps: list[FilterFailureStep] | None = None
    cumulative_relaxed: bool = False
    cumulative_attempted: bool = False

    def summary_sk(self) -> str:
        """Stručné slovenské zhrnutie zjemnených filtrov."""
        if self.cumulative_relaxed:
            return (
                "Zjemnené filtre: **kombinované uvoľnenie** ostatných filtrov naraz "
                "(**DTE min/max** sa nehýbu — ostanú podľa zadania)."
            )
        relaxed = [s for s in self.steps if not s.passed and s.relaxed_to is not None]
        if not relaxed:
            return ""
        parts = []
        for s in relaxed:
            orig = s.original
            new = s.relaxed_to
            if isinstance(orig, float) and isinstance(new, float):
                parts.append(f"**{s.name}**: {orig:.2f} → {new:.2f}")
            else:
                parts.append(f"**{s.name}**: {orig} → {new}")
        return "Zjemnené filtre: " + ", ".join(parts)

    def failure_report_markdown(
        self,
        *,
        initial_opt: DiagonalSearchOptions,
        last_tried_opt: DiagonalSearchOptions,
    ) -> str:
        """Text pre UI, keď ani postupné zjemnenie nenašlo kombinácie."""
        parts: list[str] = [
            "**0 výsledkov** aj po postupnom zjemnení filtrov v zadanom poradí.",
            "",
            "### Brány (kde sa to lámalo)",
            "",
            f"- **DTE brána:** skoršia **{_fmt_opt_val(initial_opt.dte_near_min)}–{_fmt_opt_val(initial_opt.dte_near_max)}**, "
            f"neskoršia **{_fmt_opt_val(initial_opt.dte_far_min)}–{_fmt_opt_val(initial_opt.dte_far_max)}** "
            f"→ {'prešla' if not (self.failure_steps and self.failure_steps[0].field.startswith('dte_')) else 'neprešla'}.",
            "",
            "### Kde sa to zastavilo",
        ]
        fs = self.failure_steps or []
        if not fs:
            parts.append(
                "- **Žiadny aktívny filter** nebolo možné ďalej uvoľňovať (všetky príslušné polia sú vypnuté alebo už voľné). "
                "Príčina môže byť v **dátach** (málo expirácií, chýbajúce delta/theta v importe) — pozri expander *Detail — DB* nižšie."
            )
        else:
            last = fs[-1]
            for i, row in enumerate(fs, start=1):
                tried = ", ".join(_fmt_opt_val(v) for v in row.values_attempted) if row.values_attempted else "—"
                parts.append(
                    f"{i}. **{row.label}** (`{row.field}`): počiatočná hodnota **{_fmt_opt_val(row.start_value)}**, "
                    f"skúšané: {tried}. {row.reason_sk}"
                )
            parts.append("")
            parts.append(
                f"**Posledný spracovaný filter (kde reťazec skončil neúspešne):** **{last.label}** (`{last.field}`)."
            )
        parts.extend(["", "### Tvoje vstupné kritériá (pred zjemnením)", _format_diagonal_options_markdown_sk(initial_opt)])
        parts.extend(["", "### Posledné vyskúšané kritériá (najhlbší pokus pred návratom filtrov)", _format_diagonal_options_markdown_sk(last_tried_opt)])
        return "\n".join(parts)


def build_delta_search_protocol_markdown(
    *,
    ticker: str,
    as_of_date: str,
    strategy: StrategyId,
    target_net_delta: float,
    top_n: int,
    max_strikes_per_expiry: int,
    strike_min: Optional[float],
    strike_max: Optional[float],
    initial_options: DiagonalSearchOptions,
    effective_options: DiagonalSearchOptions,
    filter_log: FilterLog,
    result: pd.DataFrame,
    preview_rows: int = 35,
) -> str:
    """
    Kompletný textový protokol jedného behu hľadania (Markdown) — na kopírovanie / uloženie pri ladení.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    spec = STRATEGIES.get(strategy)
    strat_label = spec.label_sk if spec else str(strategy)
    if strike_min is not None and strike_max is not None:
        strike_line = f"**Obmedzenie strike:** {strike_min:g} – {strike_max:g}"
    else:
        strike_line = "**Obmedzenie strike:** vypnuté"

    lines: list[str] = [
        f"# Protokol — hľadanie delty (diagonály)",
        "",
        f"- **Čas (UTC):** `{ts}`",
        f"- **Ticker:** `{ticker}`",
        f"- **Snímka (as-of):** `{as_of_date}`",
        f"- **Stratégia:** {strat_label} (`{strategy}`)",
        f"- **Cieľová čistá delta:** `{target_net_delta}`",
        f"- **Max. výsledkov:** `{top_n}`",
        f"- **Max. strike-ov / expiráciu:** `{max_strikes_per_expiry}`",
        f"- {strike_line}",
        "",
        "## Počiatočné filtre (UI → DiagonalSearchOptions)",
        "",
        _format_diagonal_options_markdown_sk(initial_options),
        "",
        "### Pôvodný DTE filter",
        "",
        f"- **DTE skoršej expirácie:** min **{_fmt_opt_val(initial_options.dte_near_min)}**, max **{_fmt_opt_val(initial_options.dte_near_max)}**",
        f"- **DTE neskoršej expirácie:** min **{_fmt_opt_val(initial_options.dte_far_min)}**, max **{_fmt_opt_val(initial_options.dte_far_max)}**",
        "",
        "## Počiatočné možnosti (JSON)",
        "",
        "```json",
        json.dumps(asdict(initial_options), ensure_ascii=False, indent=2),
        "```",
        "",
        "## Protokol postupného zjemnenia (FilterLog)",
        "",
        f"- **Počet krokov zjemnenia (úspešných):** {len(filter_log.steps)}",
        f"- **Bolo zjemnenie:** {'áno' if filter_log.any_relaxed else 'nie'}",
        f"- **Kombinované zjemnenie (2. fáza):** {'áno' if filter_log.cumulative_relaxed else 'nie'}"
        + (f" (pokus vykonaný: áno)" if filter_log.cumulative_attempted and not filter_log.cumulative_relaxed else ""),
        f"- **Finálny počet riadkov:** {filter_log.final_rows}",
        "",
    ]
    if filter_log.steps:
        lines.append("| # | Filter | Pôvodná | Zjemnená | Riadky po |")
        lines.append("|---|--------|---------|----------|-----------|")
        for i, s in enumerate(filter_log.steps, start=1):
            lines.append(
                f"| {i} | {s.name} | {_fmt_opt_val(s.original)} | {_fmt_opt_val(s.relaxed_to)} | {s.rows_after} |"
            )
        lines.append("")
    else:
        lines.append("_Žiadne záznamy zjemnenia (prvé hľadanie už vrátilo výsledky alebo sa zjemnenie neaplikovalo)._")
        lines.append("")
    lines.append("### Pôvodné vs. efektívne DTE")
    lines.append("")
    lines.append(
        f"- **Pôvodné DTE:** skoršia min/max **{_fmt_opt_val(initial_options.dte_near_min)} / {_fmt_opt_val(initial_options.dte_near_max)}**, "
        f"neskoršia min/max **{_fmt_opt_val(initial_options.dte_far_min)} / {_fmt_opt_val(initial_options.dte_far_max)}**."
    )
    lines.append(
        f"- **Efektívne DTE po hľadaní:** skoršia min/max **{_fmt_opt_val(effective_options.dte_near_min)} / {_fmt_opt_val(effective_options.dte_near_max)}**, "
        f"neskoršia min/max **{_fmt_opt_val(effective_options.dte_far_min)} / {_fmt_opt_val(effective_options.dte_far_max)}**."
    )
    lines.append("")

    fl_json = {
        "steps": [asdict(s) for s in filter_log.steps],
        "final_rows": filter_log.final_rows,
        "any_relaxed": filter_log.any_relaxed,
        "cumulative_relaxed": filter_log.cumulative_relaxed,
        "cumulative_attempted": filter_log.cumulative_attempted,
        "failure_steps": [asdict(s) for s in (filter_log.failure_steps or [])],
    }
    lines.append("## FilterLog (JSON)")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(fl_json, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")

    if result.empty:
        lines.append("## Výsledok: 0 riadkov")
        lines.append("")
        lines.append(filter_log.failure_report_markdown(initial_opt=initial_options, last_tried_opt=effective_options))
        lines.append("")
    else:
        lines.append(f"## Výsledok: {len(result)} riadkov")
        lines.append("")
        lines.append("### Efektívne kritériá po hľadaní")
        lines.append("")
        lines.append(_format_diagonal_options_markdown_sk(effective_options))
        lines.append("")
        lines.append("### Efektívne možnosti (JSON)")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(asdict(effective_options), ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")
        sub = result.head(max(1, int(preview_rows)))
        lines.append(f"### Ukážka tabuľky (prvých {len(sub)} z {len(result)}) — CSV")
        lines.append("")
        lines.append("```text")
        lines.append(sub.to_csv(index=False))
        lines.append("```")

    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("_TradeJournal — `core/diagonal_spread_search.py`_")
    return "\n".join(lines)


def write_delta_search_protocol_to_data_dir(markdown: str, ticker: str) -> Path:
    """Uloží protokol do ``data/delta_search_debug/*.md`` a vráti cestu."""
    root = Path(__file__).resolve().parents[1]
    out_dir = root / "data" / "delta_search_debug"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join((c if c.isalnum() else "_") for c in (ticker or "X").strip().upper())[:24]
    fn = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{safe}.md"
    path = out_dir / fn
    path.write_text(markdown, encoding="utf-8")
    return path


def _fmt_opt_val(v: OptScalar) -> str:
    if v is None:
        return "vypnuté"
    if isinstance(v, bool):
        return "áno" if v else "nie"
    if isinstance(v, str):
        return "Long" if v == "long" else ("Short" if v == "short" else v)
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _format_diagonal_options_markdown_sk(o: DiagonalSearchOptions) -> str:
    """Zostaví odrážkový zoznam zapnutých filtrov pre diagnostiku v UI."""
    lines: list[str] = []
    if o.delta_tolerance is not None:
        lines.append(f"- Delta: tolerancia **≤ {_fmt_opt_val(o.delta_tolerance)}** (|čistá delta − cieľ|)")
    th_note = " (×100)" if o.theta_scale_contracts else ""
    if o.net_theta_min is not None:
        lines.append(f"- Theta{th_note}: min **{_fmt_opt_val(o.net_theta_min)}**")
    if o.net_theta_max is not None:
        lines.append(f"- Theta{th_note}: max **{_fmt_opt_val(o.net_theta_max)}**")
    if o.net_vega_min is not None:
        lines.append(f"- Vega: min **{_fmt_opt_val(o.net_vega_min)}**")
    if o.net_vega_max is not None:
        lines.append(f"- Vega: max **{_fmt_opt_val(o.net_vega_max)}**")
    if o.net_gamma_min is not None:
        lines.append(f"- Gamma: min **{_fmt_opt_val(o.net_gamma_min)}**")
    if o.net_gamma_max is not None:
        lines.append(f"- Gamma: max **{_fmt_opt_val(o.net_gamma_max)}**")
    if o.dte_near_min is not None:
        lines.append(f"- DTE skoršej expirácie: min **{o.dte_near_min}** dní")
    if o.dte_near_max is not None:
        lines.append(f"- DTE skoršej expirácie: max **{o.dte_near_max}** dní")
    if o.dte_far_min is not None:
        lines.append(f"- DTE neskoršej expirácie: min **{o.dte_far_min}** dní")
    if o.dte_far_max is not None:
        lines.append(f"- DTE neskoršej expirácie: max **{o.dte_far_max}** dní")
    if o.short_otm_min is not None:
        lines.append(f"- OTM short: min **{_fmt_opt_val(o.short_otm_min)}** (potrebný spot)")
    if o.max_debit_to_strike_width_ratio is not None:
        lines.append(f"- Debit/šírka strike: max **{_fmt_opt_val(o.max_debit_to_strike_width_ratio)}**")
    if o.max_rel_spread_short is not None:
        lines.append(f"- Rel. spread short: max **{_fmt_opt_val(o.max_rel_spread_short)}**")
    if o.max_rel_spread_long is not None:
        lines.append(f"- Rel. spread long: max **{_fmt_opt_val(o.max_rel_spread_long)}**")
    if o.min_open_interest is not None:
        lines.append(f"- Min. open interest: **{o.min_open_interest}** (obe nohy)")
    if o.min_volume is not None:
        lines.append(f"- Min. volume: **{o.min_volume}**")
    if o.require_iv_short_ge_long:
        lines.append(f"- IV short ≥ IV long: **áno** (marža **{_fmt_opt_val(o.iv_short_ge_long_margin)}**)")
    if o.strike_proximity_leg in ("long", "short") and o.spot is not None and float(o.spot) > 0:
        _leg = "Long" if o.strike_proximity_leg == "long" else "Short"
        lines.append(
            f"- **Strike k spotu:** noha **{_leg}** má byť *bližšie* (menšie |strike − spot|) ako druhá noha"
        )
    lines.append(f"- Triedenie: **{o.rank_mode}**")
    if o.spot is not None and float(o.spot) > 0:
        lines.append(f"- Spot (pre OTM a strike): **{float(o.spot):.2f}**")
    if not lines:
        return "_Žiadne riadkové filtre — len dáta z reťazca._"
    return "\n".join(lines)


# Tieto polia sa pri postupnom / kumulatívnom zjemnení **nemenia** — DTE a výber nohy pri strike zostanú presne podľa UI.
# Inak by sa napr. „long bližšie k spotu“ pri 0 riadkoch ticho zmenilo na „short“ alebo sa filter vypol.
RELAX_EXCLUDE_FIELDS: frozenset[str] = frozenset(
    {
        "dte_near_min",
        "dte_near_max",
        "dte_far_min",
        "dte_far_max",
        "strike_proximity_leg",
    }
)

FILTER_PRIORITY: list[tuple[str, list[str]]] = [
    ("DTE skoršej exp. (kalendár)", ["dte_near_min", "dte_near_max"]),
    ("DTE neskoršej exp. (kalendár)", ["dte_far_min", "dte_far_max"]),
    ("Delta tolerancia", ["delta_tolerance"]),
    ("Theta", ["net_theta_min", "net_theta_max"]),
    ("OTM short", ["short_otm_min"]),
    ("Strike k spotu (|K−spot|)", ["strike_proximity_leg"]),
    ("Debit/šírka", ["max_debit_to_strike_width_ratio"]),
    ("Vega", ["net_vega_min", "net_vega_max"]),
    ("Gamma", ["net_gamma_min", "net_gamma_max"]),
    ("Rel. spread", ["max_rel_spread_short", "max_rel_spread_long"]),
    ("Open Interest", ["min_open_interest"]),
    ("Volume", ["min_volume"]),
    ("IV skew", ["require_iv_short_ge_long"]),
]

RELAX_STEPS: dict[str, list[Any]] = {
    "dte_near_min": [40, 30, 21, 14, 7, _RELAX_DISABLE],
    "dte_near_max": [55, 60, 75, 90, _RELAX_DISABLE],
    "dte_far_min": [90, 60, 35, _RELAX_DISABLE],
    "dte_far_max": [140, 200, 400, _RELAX_DISABLE],
    "delta_tolerance": [2.0, 3.0, 5.0, 8.0, _RELAX_DISABLE],
    "net_theta_min": [3.0, 1.5, 0.5, 0.0, _RELAX_DISABLE],
    "net_theta_max": [8.0, 12.0, 20.0, _RELAX_DISABLE],
    "short_otm_min": [0.10, 0.05, 0.02, _RELAX_DISABLE],
    "max_debit_to_strike_width_ratio": [0.25, 0.35, 0.50, _RELAX_DISABLE],
    "net_vega_min": [0.10, 0.05, 0.02, _RELAX_DISABLE],
    "net_vega_max": [0.20, 0.30, 0.50, _RELAX_DISABLE],
    "net_gamma_min": [-0.03, -0.05, -0.10, _RELAX_DISABLE],
    "net_gamma_max": [0.0, 0.02, 0.05, _RELAX_DISABLE],
    "max_rel_spread_short": [0.08, 0.12, 0.20, _RELAX_DISABLE],
    "max_rel_spread_long": [0.05, 0.10, 0.15, _RELAX_DISABLE],
    "min_open_interest": [100, 50, 20, _RELAX_DISABLE],
    "min_volume": [10, 5, 1, _RELAX_DISABLE],
    "require_iv_short_ge_long": [True, False],
    "strike_proximity_leg": ["long", "short", _RELAX_DISABLE],
}


def _get_opt_value(opt: DiagonalSearchOptions, field: str) -> OptScalar:
    """Získa hodnotu atribútu z DiagonalSearchOptions."""
    return getattr(opt, field, None)  # type: ignore[no-any-return]


def _set_opt_value(opt: DiagonalSearchOptions, field: str, value: OptScalar) -> None:
    """Nastaví hodnotu atribútu v DiagonalSearchOptions."""
    setattr(opt, field, value)


def _next_relax_value(field: str, current: OptScalar) -> Any:
    """Vráti nasledujúcu zjemnenú hodnotu, ``_RELAX_DISABLE`` (= vypnúť filter), alebo ``None`` ak už nie je krok."""
    steps = RELAX_STEPS.get(field, [])
    if not steps:
        return None
    if current is None:
        return None
    if isinstance(current, bool):
        try:
            idx = steps.index(current)
            if idx + 1 < len(steps):
                return steps[idx + 1]
        except ValueError:
            return None
        return None
    try:
        idx = steps.index(current)
        if idx + 1 < len(steps):
            return steps[idx + 1]
    except ValueError:
        pass
    nums = [s for s in steps if isinstance(s, (int, float))]
    if not nums:
        return None
    try:
        cur = float(current)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    min_style = field.startswith("min_") or field.endswith("_min")
    max_style = (
        field.startswith("max_")
        or field.endswith("_max")
        or field == "delta_tolerance"
    )
    if min_style and not max_style:
        cand = [s for s in nums if float(s) < cur - 1e-12]
        if cand:
            return max(cand, key=lambda x: float(x))
        if cur < min(float(x) for x in nums):
            return None
        return None
    if max_style:
        cand = [s for s in nums if float(s) > cur + 1e-12]
        if cand:
            return min(cand, key=lambda x: float(x))
        if cur > max(float(x) for x in nums):
            return None
        return None
    return None


def _opt_value_from_relax_token(field: str, token: Any) -> OptScalar:
    """Mapuje výstup z ``_next_relax_value`` na hodnotu v ``DiagonalSearchOptions``."""
    if token is _RELAX_DISABLE:
        return None
    return token  # type: ignore[no-any-return]


def _final_relaxed_opt_value(field: str, start: OptScalar) -> OptScalar:
    """Po prechode celou tabuľkou RELAX_STEPS od počiatočnej hodnoty — najvoľnejší stav (None = filter vypnutý)."""
    if start is None:
        return None
    if isinstance(start, bool) and field == "require_iv_short_ge_long":
        return False if start is True else start
    cur: OptScalar = start
    while cur is not None:
        nxt = _next_relax_value(field, cur)
        if nxt is None:
            return cur
        if nxt is _RELAX_DISABLE:
            return None
        cur = _opt_value_from_relax_token(field, nxt)
    return None


def progressive_filter_search(
    ticker: str,
    *,
    as_of_date: str,
    strategy: StrategyId,
    target_net_delta: float = 0.0,
    top_n: int = 40,
    max_strikes_per_expiry: int = 55,
    strike_min: Optional[float] = None,
    strike_max: Optional[float] = None,
    options: Optional[DiagonalSearchOptions] = None,
    max_relax_iterations: int = 3,
) -> tuple[pd.DataFrame, FilterLog, DiagonalSearchOptions]:
    """
    Postupné hľadanie s automatickým zjemnením filtrov.

    Ak prvé hľadanie s používateľskými filtrami vráti prázdno, prechádza polia v
    ``FILTER_PRIORITY`` a skúsi ich postupne zjemniť (max. ``max_relax_iterations``
    krokov na pole). **Výnimka:** polia DTE (``dte_near_*``, ``dte_far_*``) sa
    **nezjemňujú** — ostanú vždy podľa vstupu. Po neúspechu sa hodnota poľa vráti
    späť a pokračuje sa ďalším filtrom. Pri prvom neprázdnom výsledku sa skončí
    a do ``FilterLog`` sa zapíšu len skutočne účinné zjemnenia.

    Vráti tuple (DataFrame výsledkov, FilterLog, efektívne ``DiagonalSearchOptions``
    použité pri výslednom hľadaní — po zjemnení obsahuje uvoľnené prahy).
    """
    opt = options or DiagonalSearchOptions()
    work_opt = replace(opt)

    def _only_dte_opt(src: DiagonalSearchOptions) -> DiagonalSearchOptions:
        """Kópia opt s ponechanými iba DTE filtrami."""
        d = replace(src)
        for field in (
            "delta_tolerance",
            "net_theta_min",
            "net_theta_max",
            "net_vega_min",
            "net_vega_max",
            "net_gamma_min",
            "net_gamma_max",
            "short_otm_min",
            "max_debit_to_strike_width_ratio",
            "max_rel_spread_short",
            "max_rel_spread_long",
            "min_open_interest",
            "min_volume",
            "require_iv_short_ge_long",
            "iv_short_ge_long_margin",
            "strike_proximity_leg",
        ):
            _set_opt_value(d, field, None if field != "require_iv_short_ge_long" else False)
        return d

    log_steps: list[FilterStep] = []
    failure_trace: list[FilterFailureStep] = []
    any_relaxed = False
    last_tried_opt = replace(work_opt)

    result = search_diagonal_spreads(
        ticker,
        as_of_date=as_of_date,
        strategy=strategy,
        target_net_delta=target_net_delta,
        top_n=top_n,
        max_strikes_per_expiry=max_strikes_per_expiry,
        strike_min=strike_min,
        strike_max=strike_max,
        options=work_opt,
    )
    if result.empty:
        last_tried_opt = replace(work_opt)

    dte_only_opt = _only_dte_opt(opt)
    # Brána DTE musí ignorovať obmedzenie strike-ov: úzky pás inak vyprázdni
    # výsledok aj pri vyhovujúcich pároch expirácií a protokol by mylne hlásil „zlyhanie na DTE“.
    dte_only_result = search_diagonal_spreads(
        ticker,
        as_of_date=as_of_date,
        strategy=strategy,
        target_net_delta=target_net_delta,
        top_n=top_n,
        max_strikes_per_expiry=max_strikes_per_expiry,
        strike_min=None,
        strike_max=None,
        options=dte_only_opt,
    )
    n_exp, ok_dte_pairs = _expiries_and_dte_pair_count(ticker, as_of_date, strategy, opt)
    has_any_dte_filter = any(
        getattr(opt, f) is not None
        for f in ("dte_near_min", "dte_near_max", "dte_far_min", "dte_far_max")
    )
    dte_hits_filter_bounds = (
        dte_only_result.empty
        and has_any_dte_filter
        and n_exp >= 2
        and ok_dte_pairs == 0
    )
    if dte_hits_filter_bounds:
        dte_reason = (
            "V importe (aspoň dve expirácie s delta+theta) **neexistuje kalendárna dvojica** dátumov, kde by "
            "zároveň DTE k **skoršiemu** dátumu bolo v pásme skoršia *a* DTE k **neskoršiemu** bolo v pásme neskoršia. "
            "Typický prípad: páslo skoršia je OK (napr. 40–61), ale **min. neskoršia** ostal napr. **90** a v DB k najbližšej vhodnej "
            "dvojici pripadne len **80–86** dní k ďalšej expirácii — vtedy je nutné znížiť *Neskoršia min* alebo páslo vypnúť."
        )
        failure_trace.append(
            FilterFailureStep(
                label="DTE",
                field="dte_near_min/dte_near_max/dte_far_min/dte_far_max",
                start_value=None,
                values_attempted=[],
                reason_sk=dte_reason,
            )
        )
        return (
            pd.DataFrame(),
            FilterLog(
                steps=[],
                final_rows=0,
                any_relaxed=False,
                failure_steps=failure_trace,
                cumulative_relaxed=False,
                cumulative_attempted=False,
            ),
            replace(opt),
        )

    if not result.empty:
        return (
            result,
            FilterLog(steps=[], final_rows=len(result), any_relaxed=False, failure_steps=None),
            replace(work_opt),
        )

    for group_name, fields in FILTER_PRIORITY:
        for field in fields:
            if field in RELAX_EXCLUDE_FIELDS:
                continue
            field_start = _get_opt_value(work_opt, field)
            if field_start is None:
                continue
            if isinstance(field_start, bool) and not field_start:
                continue

            current_value = field_start
            found: pd.DataFrame | None = None
            relaxed_to: Optional[float | int | bool] = None
            values_attempted: list[Optional[float | int | bool]] = []

            for _ in range(max_relax_iterations):
                next_raw = _next_relax_value(field, current_value)
                if next_raw is None:
                    break
                if next_raw == current_value:
                    break
                actual = _opt_value_from_relax_token(field, next_raw)
                _set_opt_value(work_opt, field, actual)
                test_result = search_diagonal_spreads(
                    ticker,
                    as_of_date=as_of_date,
                    strategy=strategy,
                    target_net_delta=target_net_delta,
                    top_n=top_n,
                    max_strikes_per_expiry=max_strikes_per_expiry,
                    strike_min=strike_min,
                    strike_max=strike_max,
                    options=work_opt,
                )
                last_tried_opt = replace(work_opt)
                values_attempted.append(actual)
                current_value = actual
                if not test_result.empty:
                    found = test_result
                    relaxed_to = actual
                    break

            if found is not None and (relaxed_to != field_start or (relaxed_to is None and field_start is not None)):
                result = found
                log_steps.append(
                    FilterStep(
                        name=f"{group_name} ({field})",
                        passed=False,
                        original=field_start,
                        relaxed_to=relaxed_to,
                        rows_before=0,
                        rows_after=len(result),
                    )
                )
                any_relaxed = True
                return (
                    result,
                    FilterLog(steps=log_steps, final_rows=len(result), any_relaxed=any_relaxed, failure_steps=None),
                    replace(work_opt),
                )

            _set_opt_value(work_opt, field, field_start)

            if values_attempted:
                reason = (
                    f"Po **{len(values_attempted)}** krokoch zjemnenia stále **0** výsledkov; filter sa vrátil na počiatočnú hodnotu."
                )
            else:
                nxt = _next_relax_value(field, field_start)
                if nxt is None or nxt == field_start:
                    reason = (
                        "**Nepodarilo sa začať zjemnenie** — pre túto hodnotu už **nie je ďalší krok** v tabuľke zjemnení "
                        "(alebo je filter už čo najvoľnejší)."
                    )
                else:
                    reason = "Žiadny pokus sa nezaznamenal (vnútorný stav)."

            failure_trace.append(
                FilterFailureStep(
                    label=group_name,
                    field=field,
                    start_value=field_start,
                    values_attempted=list(values_attempted),
                    reason_sk=reason,
                )
            )

    cumulative_relaxed = False
    cumulative_attempted = False
    if result.empty:
        cumulative_attempted = True
        combo = replace(opt)
        for _group_name, fields in FILTER_PRIORITY:
            for field in fields:
                if field in RELAX_EXCLUDE_FIELDS:
                    continue
                start0 = _get_opt_value(opt, field)
                if start0 is None:
                    continue
                if isinstance(start0, bool) and not start0:
                    continue
                fv = _final_relaxed_opt_value(field, start0)
                _set_opt_value(combo, field, fv)
        last_tried_opt = replace(combo)
        result = search_diagonal_spreads(
            ticker,
            as_of_date=as_of_date,
            strategy=strategy,
            target_net_delta=target_net_delta,
            top_n=top_n,
            max_strikes_per_expiry=max_strikes_per_expiry,
            strike_min=strike_min,
            strike_max=strike_max,
            options=combo,
        )
        if not result.empty:
            cumulative_relaxed = True
            any_relaxed = True
            log_steps.append(
                FilterStep(
                    name="Kombinované zjemnenie (všetky filtre naraz)",
                    passed=False,
                    original=None,
                    relaxed_to=None,
                    rows_before=0,
                    rows_after=len(result),
                )
            )

    return (
        result,
        FilterLog(
            steps=log_steps,
            final_rows=len(result),
            any_relaxed=any_relaxed,
            failure_steps=None if cumulative_relaxed else failure_trace,
            cumulative_relaxed=cumulative_relaxed,
            cumulative_attempted=cumulative_attempted,
        ),
        last_tried_opt,
    )
