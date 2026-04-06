"""
Pravidlá monitorovania (semafor) a textové odporúčania k rolovaniu.
Neexekuuje obchody — len logika a odhady.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.steady_yields.constants import (
    DELTA_GREEN_MAX,
    DELTA_RED_MIN,
    DEFAULT_SLIPPAGE_USD_PER_CONTRACT,
    ROLL_DTE_TRIGGER,
)


@dataclass
class TrafficState:
    level: str  # "green" | "orange" | "red"
    reasons: list[str] = field(default_factory=list)


def traffic_light(
    *,
    abs_delta: float | None,
    dte: int | None,
    delta_green_max: float | None = None,
    delta_red_min: float | None = None,
    roll_dte_trigger: int | None = None,
) -> TrafficState:
    """
    abs_delta: absolútna hodnota delty shortu (0…1).
    dte: dni do expirácie shortu.
    Červená: |Δ| > delta_red_min. Oranžová: |Δ| ≥ delta_green_max alebo DTE < roll_dte. Zelená: inak.
    Voliteľné prahy (napr. z ``st.session_state``) prepíšu konštanty z ``constants``.
    """
    dg = float(delta_green_max) if delta_green_max is not None else DELTA_GREEN_MAX
    dr = float(delta_red_min) if delta_red_min is not None else DELTA_RED_MIN
    rd = int(roll_dte_trigger) if roll_dte_trigger is not None else ROLL_DTE_TRIGGER

    reasons: list[str] = []
    ad = float(abs_delta) if abs_delta is not None else None
    d = int(dte) if dte is not None else None

    if ad is not None and ad > dr:
        reasons.append(f"Delta |Δ|={ad:.2f} > {dr} — okamžitá akcia / ochrana LEAPS")
        return TrafficState(level="red", reasons=reasons)

    time_orange = d is not None and d < rd
    delta_orange = ad is not None and ad >= dg
    if delta_orange:
        reasons.append(
            f"Delta |Δ|={ad:.2f} v rozsahu pripravenosti ({dg:.2f}–{dr:.2f})"
        )
    if time_orange:
        reasons.append(f"DTE={d} < {rd} — časový trigger na roll")

    if delta_orange or time_orange:
        if not reasons:
            reasons.append("Oranžový stav")
        return TrafficState(level="orange", reasons=reasons)

    if ad is not None:
        reasons.append(f"Delta |Δ|={ad:.2f} < {dg:.2f} — v poriadku")
    else:
        reasons.append("Delta neznáma — over v TWS / Portfolio")

    return TrafficState(level="green", reasons=reasons)


@dataclass
class RollAdvice:
    ok: bool
    messages: list[str]
    est_net_credit_per_contract: float | None = None
    warnings: list[str] = field(default_factory=list)
    suggested_contracts: list[dict] = field(default_factory=list)


def estimate_roll_net_credit(
    *,
    close_short_bid: float | None,
    close_short_ask: float | None,
    open_short_bid: float | None,
    open_short_ask: float | None,
    contracts: int = 1,
    slippage_per_contract: float = DEFAULT_SLIPPAGE_USD_PER_CONTRACT,
    suggested_contracts: list[dict] | None = None,
) -> RollAdvice:
    """
    Konzervatívny odhad: zatvoríš short nákupom za ask, otvoríš nový predajom za bid.
    Netto na kontrakt (v $ prémií): bid_new - ask_old - slippage.
    """
    msgs: list[str] = []
    warns: list[str] = []
    sug = list(suggested_contracts or [])
    if close_short_ask is None or open_short_bid is None:
        return RollAdvice(
            ok=False,
            messages=["Chýbajú bid/ask pre konzervatívny odhad (potrebuj ask starého a bid nového shortu)."],
            warnings=warns,
            suggested_contracts=sug,
        )

    per = float(open_short_bid) - float(close_short_ask) - float(slippage_per_contract)
    total = per * 100 * max(1, int(contracts))
    if per > 0:
        msgs.append(f"Odhad čistého kreditu: ~${per:.3f}/kontrakt prémií (~${total:,.0f} na pozíciu ×100×kontrakty).")
        return RollAdvice(
            ok=True,
            messages=msgs,
            est_net_credit_per_contract=round(per, 4),
            warnings=warns,
            suggested_contracts=sug,
        )
    msgs.append(
        f"Konzervatívne netto ≤ 0 (${per:.3f}/kontr.) — podľa pravidla uprednostni roll ďalej v čase alebo iný strike."
    )
    warns.append("Net credit ≤ 0 pri konzervatívnom bid/ask — zváž ďalší mesiac alebo iný strike.")
    return RollAdvice(
        ok=False,
        messages=msgs,
        est_net_credit_per_contract=round(per, 4),
        warnings=warns,
        suggested_contracts=sug,
    )
