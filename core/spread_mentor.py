"""
Konzervatívne pravidlá pre „mentora“ v Spread Builderi (diagonál / PMCC / podobné štruktúry).

Orientačné rozsahy — nie investičná rada; používateľ si ich môže prispôsobiť v UI neskôr.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional


# Konzervatívny „stred“ (odporúčané okná z tvojej tabuľky)
SHORT_DTE_MIN = 30
SHORT_DTE_MAX = 45
LONG_DTE_MIN = 60
LONG_DTE_MAX = 120
SPREAD_MONTHS_MIN = 1.0
SPREAD_MONTHS_MAX = 3.0

# Kalendárny spread (rovnaký strike, rovnaký call/put, rôzne expirácie)
CAL_SHORT_DTE_MIN = 25
CAL_SHORT_DTE_MAX = 45
CAL_LONG_DTE_MIN = 50
CAL_LONG_DTE_MAX = 150
CAL_SPREAD_MONTHS_MIN = 0.5
CAL_SPREAD_MONTHS_MAX = 2.5


def _parse_expiry_yyyymmdd(expiry_str: str) -> Optional[date]:
    s = (expiry_str or "").strip().replace("-", "")
    if len(s) < 8:
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except (TypeError, ValueError):
        return None


def dte_from_expiry(expiry_str: str, today: Optional[date] = None) -> int:
    td = today or date.today()
    e = _parse_expiry_yyyymmdd(str(expiry_str))
    if e is None:
        return 0
    return max(0, (e - td).days)


@dataclass
class DiagonalMentorResult:
    short_dte: int
    long_dte: int
    spread_days: int
    spread_months: float
    short_ok: bool
    long_ok: bool
    spread_ok: bool
    inverted: bool
    summary_lines: list[str]


def analyze_diagonal_mentor(legs: list[dict], today: Optional[date] = None) -> Optional[DiagonalMentorResult]:
    """
    Z nôh vytiahne „najbližšiu“ short expiráciu (min DTE medzi Short) a „najďalejšiu“ long (max DTE medzi Long).
    Rozptyl = long_dte − short_dte (dni).
    """
    td = today or date.today()
    short_dtes: list[int] = []
    long_dtes: list[int] = []
    for leg in legs:
        lt = str(leg.get("leg_type") or "")
        exp = leg.get("expiry")
        if not exp:
            continue
        d = dte_from_expiry(str(exp), td)
        if d <= 0:
            continue
        if lt == "Short":
            short_dtes.append(d)
        elif lt == "Long":
            long_dtes.append(d)
    if not short_dtes or not long_dtes:
        return None
    short_dte = min(short_dtes)
    long_dte = max(long_dtes)
    inverted = long_dte < short_dte
    spread_days = max(0, long_dte - short_dte)
    spread_months = spread_days / 30.0

    short_ok = SHORT_DTE_MIN <= short_dte <= SHORT_DTE_MAX
    long_ok = LONG_DTE_MIN <= long_dte <= LONG_DTE_MAX
    spread_ok = (not inverted) and (SPREAD_MONTHS_MIN <= spread_months <= SPREAD_MONTHS_MAX)

    lines: list[str] = []
    if inverted:
        lines.append(
            "Dlhá noha má kratšie DTE než skrátená — skontroluj označenie Long/Short a dátumy expirácie; "
            "klasický diagonál má dlhšiu long expiráciu."
        )
    if short_ok and long_ok and spread_ok and not inverted:
        lines.append(
            "Parametre sú blízko konzervatívnemu stredu: rýchlejší prínos z času (theta) na skrátenej nohe "
            "a rozumný horizont dlhej nohy."
        )
    else:
        if short_dte < SHORT_DTE_MIN:
            lines.append(
                f"Skrátená noha má len {short_dte} DTE — pod odporúčaným minimom ({SHORT_DTE_MIN}). "
                "Vyšší gamma, rýchlejšie rozhodnutie (roll / uzavretie)."
            )
        elif short_dte > SHORT_DTE_MAX:
            lines.append(
                f"Skrátená noha má {short_dte} DTE — nad odporúčaným horným oknom ({SHORT_DTE_MAX}). "
                "Časový rozpad býva pomalší; obchod môže byť citlivejší na volatilitu skôr než na theta."
            )
        if long_dte < LONG_DTE_MIN:
            lines.append(
                f"Dlhá noha má {long_dte} DTE — pod odporúčaným minimom ({LONG_DTE_MIN}). "
                "Kratší „chránič“ môže znamenať vyšší roll pressure."
            )
        elif long_dte > LONG_DTE_MAX:
            lines.append(
                f"Dlhá noha má {long_dte} DTE — výrazne nad odporúčaným oknom ({LONG_DTE_MAX}). "
                "Vega býva vyššia — spread je pomalší a citlivejší na zmeny implied volatility."
            )
        if spread_months < SPREAD_MONTHS_MIN:
            lines.append(
                f"Rozptyl expirácií je len cca {spread_months:.1f} mes. — pod odporúčaným minimom ({SPREAD_MONTHS_MIN}–{SPREAD_MONTHS_MAX} mes.). "
                "Diagonál je „tesný“; menej času medzi expiráciami na plán B."
            )
        elif spread_months > SPREAD_MONTHS_MAX:
            lines.append(
                f"Rozptyl je cca {spread_months:.1f} mes. — nad odporúčaným oknom ({SPREAD_MONTHS_MIN}–{SPREAD_MONTHS_MAX} mes.). "
                "Pomalší obchod, viac času do rollu, väčší vplyv volatility na P/L."
            )

    if not lines and not inverted:
        lines.append("Skontroluj DTE a rozptyl ručne — niektoré nohy môžu mať neštandardné expirácie.")

    return DiagonalMentorResult(
        short_dte=short_dte,
        long_dte=long_dte,
        spread_days=spread_days,
        spread_months=spread_months,
        short_ok=short_ok,
        long_ok=long_ok,
        spread_ok=spread_ok,
        inverted=inverted,
        summary_lines=lines,
    )


@dataclass
class CalendarMentorResult:
    strike: float
    right: str
    short_dte: int
    long_dte: int
    spread_days: int
    spread_months: float
    short_ok: bool
    long_ok: bool
    spread_ok: bool
    inverted: bool
    summary_lines: list[str]


def analyze_calendar_mentor(legs: list[dict], today: Optional[date] = None) -> Optional[CalendarMentorResult]:
    """
    Kalendárny spread: aspoň jedna Short a jedna Long s rovnakým strike a typom opcie (C/P), rôzne expirácie.
    Ak je takých párov viac, berie sa pár s najbližšou short expiráciou (najmenšie short DTE).
    """
    td = today or date.today()
    short_by_key: dict[tuple[float, str], list[int]] = {}
    long_by_key: dict[tuple[float, str], list[int]] = {}
    for leg in legs:
        lt = str(leg.get("leg_type") or "")
        exp = leg.get("expiry")
        if not exp:
            continue
        d = dte_from_expiry(str(exp), td)
        if d <= 0:
            continue
        r = str(leg.get("right") or "").strip().upper()[:1]
        if r not in ("C", "P"):
            continue
        try:
            strike = round(float(leg.get("strike")), 4)
        except (TypeError, ValueError):
            continue
        key = (strike, r)
        if lt == "Short":
            short_by_key.setdefault(key, []).append(d)
        elif lt == "Long":
            long_by_key.setdefault(key, []).append(d)

    common = [k for k in short_by_key if k in long_by_key and short_by_key[k] and long_by_key[k]]
    if not common:
        return None

    def _pick_key(k: tuple[float, str]) -> tuple[int, float, str]:
        return (min(short_by_key[k]), k[0], k[1])

    key = min(common, key=_pick_key)
    strike, right = key
    short_dte = min(short_by_key[key])
    long_dte = max(long_by_key[key])
    inverted = long_dte < short_dte
    spread_days = max(0, long_dte - short_dte)
    spread_months = spread_days / 30.0

    short_ok = CAL_SHORT_DTE_MIN <= short_dte <= CAL_SHORT_DTE_MAX
    long_ok = CAL_LONG_DTE_MIN <= long_dte <= CAL_LONG_DTE_MAX
    spread_ok = (not inverted) and (CAL_SPREAD_MONTHS_MIN <= spread_months <= CAL_SPREAD_MONTHS_MAX)

    lines: list[str] = []
    cp = "Call" if right == "C" else "Put"
    lines.append(f"Detekovaný kalendár na strike **${strike:g}** ({cp}) — predná noha (short) vs. zadná (long).")
    if inverted:
        lines.append(
            "Zadná expirácia je kratšia než predná — skontroluj Long/Short a dátumy; "
            "klasický kalendár má dlhšiu long expiráciu než short."
        )
    if short_ok and long_ok and spread_ok and not inverted:
        lines.append(
            "DTE a rozstup expirácií sú blízko konzervatívnemu oknu pre kalendár: theta z prednej nohy "
            "a vega z dlhšej nohy sú v rozumnom pomere."
        )
    else:
        if short_dte < CAL_SHORT_DTE_MIN:
            lines.append(
                f"Predná (short) noha má len {short_dte} DTE — pod odporúčaným minimom ({CAL_SHORT_DTE_MIN}). "
                "Vyšší gamma a rýchlejší rozhodovací bod pri expirácii v strende (pin risk pri rovnakom strike)."
            )
        elif short_dte > CAL_SHORT_DTE_MAX:
            lines.append(
                f"Predná noha má {short_dte} DTE — nad horným oknom ({CAL_SHORT_DTE_MAX}). "
                "Časový rozpad býva pomalší; kalendár dlhšie čaká na pohyb ceny alebo IV."
            )
        if long_dte < CAL_LONG_DTE_MIN:
            lines.append(
                f"Zadná (long) noha má {long_dte} DTE — pod odporúčaným minimom ({CAL_LONG_DTE_MIN}). "
                "Kratší „chránič“ môže znížiť vega a zhoršiť odolnosť voči IV crush na prednej nohe."
            )
        elif long_dte > CAL_LONG_DTE_MAX:
            lines.append(
                f"Zadná noha má {long_dte} DTE — výrazne nad odporúčaným oknom ({CAL_LONG_DTE_MAX}). "
                "Vega na dlhej nohe býva vysoká; P/L je citlivejší na zmeny implied volatility."
            )
        if spread_months < CAL_SPREAD_MONTHS_MIN:
            lines.append(
                f"Rozstup expirácií je len cca {spread_months:.1f} mes. — pod odporúčaným minimom "
                f"({CAL_SPREAD_MONTHS_MIN}–{CAL_SPREAD_MONTHS_MAX} mes.). Kalendár je veľmi „tesný“."
            )
        elif spread_months > CAL_SPREAD_MONTHS_MAX:
            lines.append(
                f"Rozstup je cca {spread_months:.1f} mes. — nad odporúčaným oknom. "
                "Viac času medzi expiráciami znamená pomalší obchod a iný tvar krivky P/L."
            )
    if not inverted:
        lines.append(
            "Pri rovnakom strike sleduj **pin** okolo expirácie prednej nohy a rozdiel IV medzi mesiacmi "
            "(front-month IV často klesá rýchlejšie než back month)."
        )

    return CalendarMentorResult(
        strike=strike,
        right=right,
        short_dte=short_dte,
        long_dte=long_dte,
        spread_days=spread_days,
        spread_months=spread_months,
        short_ok=short_ok,
        long_ok=long_ok,
        spread_ok=spread_ok,
        inverted=inverted,
        summary_lines=lines,
    )


def mentor_comparison_rows(res: DiagonalMentorResult) -> list[dict[str, Any]]:
    """Riadky pre tabuľku v Streamlite."""
    return [
        {
            "Parameter": "Short DTE",
            "Konzervatívny stred": f"{SHORT_DTE_MIN} – {SHORT_DTE_MAX} dní",
            "Tvoj setup": f"{res.short_dte} dní",
            "Stav": "OK" if res.short_ok else "Mimo odporúčania",
        },
        {
            "Parameter": "Long DTE",
            "Konzervatívny stred": f"{LONG_DTE_MIN} – {LONG_DTE_MAX} dní",
            "Tvoj setup": f"{res.long_dte} dní",
            "Stav": "OK" if res.long_ok else "Mimo odporúčania",
        },
        {
            "Parameter": "Rozptyl (Long − Short)",
            "Konzervatívny stred": f"{SPREAD_MONTHS_MIN:.0f} – {SPREAD_MONTHS_MAX:.0f} mesiace",
            "Tvoj setup": f"{res.spread_months:.1f} mes. ({res.spread_days} dní)",
            "Stav": "OK" if res.spread_ok else "Mimo odporúčania",
        },
    ]


def mentor_calendar_rows(res: CalendarMentorResult) -> list[dict[str, Any]]:
    """Riadky pre tabuľku kalendárneho spreadu v Streamlite."""
    return [
        {
            "Parameter": "Short DTE (predná noha)",
            "Konzervatívny stred": f"{CAL_SHORT_DTE_MIN} – {CAL_SHORT_DTE_MAX} dní",
            "Tvoj setup": f"{res.short_dte} dní",
            "Stav": "OK" if res.short_ok else "Mimo odporúčania",
        },
        {
            "Parameter": "Long DTE (zadná noha)",
            "Konzervatívny stred": f"{CAL_LONG_DTE_MIN} – {CAL_LONG_DTE_MAX} dní",
            "Tvoj setup": f"{res.long_dte} dní",
            "Stav": "OK" if res.long_ok else "Mimo odporúčania",
        },
        {
            "Parameter": "Rozstup expirácií",
            "Konzervatívny stred": f"{CAL_SPREAD_MONTHS_MIN:.1f} – {CAL_SPREAD_MONTHS_MAX:.1f} mes.",
            "Tvoj setup": f"{res.spread_months:.1f} mes. ({res.spread_days} dní)",
            "Stav": "OK" if res.spread_ok else "Mimo odporúčania",
        },
    ]
