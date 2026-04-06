"""
Po úprave jednej nohy v Spread Builderi: zosúladenie párovej nohy (kalendár / diagonál / vertikál)
a jemné posunutie expirácií kalendára smerom k oknám z spread_mentor (orientačné).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from core.expiration_catalog import get_catalog_expiries
from core.spread_mentor import (
    CAL_LONG_DTE_MAX,
    CAL_LONG_DTE_MIN,
    CAL_SHORT_DTE_MAX,
    CAL_SHORT_DTE_MIN,
    CAL_SPREAD_MONTHS_MAX,
    CAL_SPREAD_MONTHS_MIN,
    dte_from_expiry,
)


def _sorted_future_expiries(today: Optional[date] = None) -> list[str]:
    td = today or date.today()
    ex = get_catalog_expiries(months=18)
    out = [e for e in sorted(ex) if dte_from_expiry(str(e), td) > 0]
    return out if out else sorted(ex)


def _round_strike(x: float) -> float:
    return max(0.5, round(float(x) * 2.0) / 2.0)


def sync_pair_after_edit(
    legs: list[dict],
    edited_idx: int,
    old_edited: dict,
    old_other: dict,
    today: Optional[date] = None,
) -> list[str]:
    """
    Upraví ``legs[1-edited_idx]`` ak je to zmysluplný pár s práve uloženou nohou.
    ``old_edited`` / ``old_other`` = kópie nôh pred uložením (strike, expiry, right, leg_type).
    """
    msgs: list[str] = []
    if len(legs) != 2:
        return msgs
    i, j = edited_idx, 1 - edited_idx
    a, b = legs[i], legs[j]
    oa, ob = old_edited, old_other

    same_right = str(a.get("right")) == str(b.get("right"))
    same_exp = str(a.get("expiry")) == str(b.get("expiry"))
    diff_exp = not same_exp

    td = today or date.today()

    # ── Kalendár: rovnaký typ opcie, rôzna expirácia, pred úpravou rovnaký strike (mriežka 0,5 $)
    if same_right and diff_exp and abs(
        _round_strike(float(oa.get("strike", 0))) - _round_strike(float(ob.get("strike", 0)))
    ) < 1e-6:
        new_k = _round_strike(float(a.get("strike", 0)))
        b["strike"] = new_k
        msgs.append(f"Kalendár: noha #{j + 1} — strike zosúladený na **${new_k:g}**.")

        si = next((idx for idx in (i, j) if str(legs[idx].get("leg_type")) == "Short"), None)
        li = next((idx for idx in (i, j) if str(legs[idx].get("leg_type")) == "Long"), None)
        if si is not None and li is not None:
            msgs.extend(_nudge_calendar_expiries(legs, si, li, td, frozen_idx=i))

    # ── Diagonál: rovnaký typ opcie, rôzna expirácia, pred úpravou rôzne striky, Long + Short
    elif (
        same_right
        and diff_exp
        and abs(
            _round_strike(float(oa.get("strike", 0))) - _round_strike(float(ob.get("strike", 0)))
        )
        >= 1e-6
        and oa.get("leg_type") != ob.get("leg_type")
    ):
        w = abs(float(oa["strike"]) - float(ob["strike"]))
        if w > 0:
            old_ot_st = float(ob["strike"])
            direction = 1.0 if old_ot_st > float(oa["strike"]) else -1.0
            new_ed = float(a["strike"])
            new_ot = _round_strike(new_ed + direction * w)
            b["strike"] = new_ot
            msgs.append(
                f"Diagonál: noha #{j + 1} — strike **${new_ot:g}** "
                f"(zachovaný rozostup strikov **${w:g}** voči upravenej nohe)."
            )

    # ── Vertikál: rovnaká expirácia a typ opcie, opačné L/S, pred úpravou mala dvojica strikov zmysel
    elif same_right and same_exp and oa.get("leg_type") != ob.get("leg_type"):
        w = abs(float(oa.get("strike", 0)) - float(ob.get("strike", 0)))
        if w > 0:
            old_ed_st = float(oa["strike"])
            old_ot_st = float(ob["strike"])
            direction = 1.0 if old_ot_st > old_ed_st else -1.0
            new_ed = float(a["strike"])
            new_ot = _round_strike(new_ed + direction * w)
            b["strike"] = new_ot
            msgs.append(
                f"Vertikál: noha #{j + 1} — strike **${new_ot:g}** (zachovaná šírka ${w:g} od upravenej nohy)."
            )

    return msgs


def _nudge_calendar_expiries(
    legs: list[dict],
    short_idx: int,
    long_idx: int,
    td: date,
    frozen_idx: int,
) -> list[str]:
    """
    ``frozen_idx`` = noha, ktorú používateľ práve uložil — jej expiráciu program nikdy neprepisuje
    (predchádza „kruhu“ úprav medzi nohami).
    """
    msgs: list[str] = []
    exps = _sorted_future_expiries(td)
    if not exps:
        return msgs

    min_gap = max(7, int(CAL_SPREAD_MONTHS_MIN * 30))
    max_gap = int(CAL_SPREAD_MONTHS_MAX * 30)

    def _d(idx: int) -> int:
        return dte_from_expiry(str(legs[idx].get("expiry", "")), td)

    def _set(idx: int, exp: str, label: str) -> None:
        if idx == frozen_idx:
            msgs.append(
                f"⚠️ Noha **#{frozen_idx + 1}** (práve upravená): pre „{label}“ by bolo treba expiráciu **{exp}** — "
                "automaticky sa **neprepisuje**. Uprav dátum sám, alebo vypni zosúladenie druhej nohy."
            )
            return
        if str(legs[idx].get("expiry")) == exp:
            return
        legs[idx]["expiry"] = exp
        role = "predná (Short)" if idx == short_idx else "zadná (Long)"
        msgs.append(f"Kalendár — {role}: expirácia **{exp}** ({label}).")

    # Opakujeme max 4 kroky (inverzia, medzera, short DTE, long DTE)
    for _ in range(4):
        ds = _d(short_idx)
        dl = _d(long_idx)
        if ds <= 0 or dl <= 0:
            break

        changed = False

        if dl <= ds:
            for e in exps:
                if dte_from_expiry(e, td) > ds:
                    _set(long_idx, e, "dlhšia ako Short — odstránenie inverzie")
                    changed = True
                    break
            if changed:
                continue

        ds = _d(short_idx)
        dl = _d(long_idx)
        gap = dl - ds

        if gap < min_gap:
            target = ds + min_gap
            pick: Optional[str] = None
            for e in exps:
                if dte_from_expiry(e, td) >= target:
                    pick = e
                    break
            if pick:
                _set(long_idx, pick, f"rozstup ≥ {min_gap} dní (mentor kalendár)")
                continue

        if gap > max_gap:
            cap = ds + max_gap
            best: Optional[str] = None
            best_d = -1
            for e in exps:
                de = dte_from_expiry(e, td)
                if ds < de <= cap and de > best_d:
                    best, best_d = e, de
            if best:
                _set(long_idx, best, f"rozstup ≤ {max_gap} dní (horný limit mentora)")
                continue

        if ds < CAL_SHORT_DTE_MIN:
            cand = [
                e
                for e in exps
                if CAL_SHORT_DTE_MIN <= dte_from_expiry(e, td) <= CAL_SHORT_DTE_MAX
            ]
            if cand:
                pick = min(cand, key=lambda e: abs(dte_from_expiry(e, td) - ds))
                _set(short_idx, pick, "Short DTE v okne mentora (kalendár)")
                continue

        if ds > CAL_SHORT_DTE_MAX:
            cand = [
                e
                for e in exps
                if CAL_SHORT_DTE_MIN <= dte_from_expiry(e, td) <= CAL_SHORT_DTE_MAX
            ]
            if cand:
                pick = min(cand, key=lambda e: abs(dte_from_expiry(e, td) - ds))
                _set(short_idx, pick, "Short DTE v okne mentora (kalendár)")
                continue

        if dl < CAL_LONG_DTE_MIN:
            cand = [
                e
                for e in exps
                if dte_from_expiry(e, td) >= _d(short_idx) + min_gap
                and CAL_LONG_DTE_MIN <= dte_from_expiry(e, td) <= CAL_LONG_DTE_MAX
            ]
            if cand:
                pick = min(cand, key=lambda e: abs(dte_from_expiry(e, td) - dl))
                _set(long_idx, pick, "Long DTE v okne mentora (kalendár)")
                continue

        if dl > CAL_LONG_DTE_MAX:
            candidates = [
                e
                for e in exps
                if _d(short_idx) < dte_from_expiry(e, td) <= CAL_LONG_DTE_MAX
                and dte_from_expiry(e, td) - _d(short_idx) >= min_gap
            ]
            if candidates:
                best2 = min(candidates, key=lambda e: abs(dte_from_expiry(e, td) - CAL_LONG_DTE_MAX))
                _set(long_idx, best2, "Long DTE v okne mentora (kalendár)")
                continue

        break

    return msgs
