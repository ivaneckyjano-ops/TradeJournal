"""
Orientačný výpočet P&L skupiny opčných nôh vs. cena podkladu (Black–Scholes).

Nie je identický s TWS Performance Graph (iné IV, úroky, dividendy, viac expirácií),
ale dáva podobný tvar krivky „dnes“ vs. pri najbližšej expirácii v skupine.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from typing import Any, Optional

from core.greeks import bs_price, iv_display_to_bs_fraction
from core.portfolio_data import calc_dte, normalize_expiry


def _expiry_to_date(expiry_raw: str) -> Optional[date]:
    if not expiry_raw:
        return None
    try:
        s = normalize_expiry(str(expiry_raw).strip().split()[0])
        if len(s) >= 10:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return None


def _dte_leg_asof(expiry_raw: str | None, asof: date) -> int | None:
    """Kalendárne dni do expirácie nohy vzhľadom na deň ``asof`` (0 = expiračný deň alebo po exp.)."""
    exp_d = _expiry_to_date(str(expiry_raw or ""))
    if exp_d is None:
        return None
    return (exp_d - asof).days


def _right_bs(option_type: str | None) -> str:
    u = str(option_type or "").strip().upper()
    return "C" if u.startswith("C") else "P"


def _iv_fraction_for_leg(leg: dict) -> float:
    # Preferujeme iv_current/iv_at_entry (DB journal), ale niektoré miesta v kóde
    # používajú len "iv" (napr. Spread Builder). Keď nič nie je, padneme na 0.30.
    for key in ("iv_current", "iv_at_entry", "iv"):
        raw = leg.get(key)
        if raw is None:
            continue
        try:
            f = iv_display_to_bs_fraction(float(raw))
        except (TypeError, ValueError):
            continue
        if f is not None and f > 0:
            return float(f)
    return 0.30


def _entry_price_per_share_for_bs(leg: dict) -> float:
    """
    Vstupná prémia $/akcia pre BS P&L.

    V DB môže `entry_price` byť niekde uložené so znamienkom (IB kredit/kost).
    V našom vzorci však znamienko P&L určuje výhradne `leg_type`
    (long = +, short = -), preto prémie normalizujeme na kladnú veľkosť cez `abs()`.
    """
    try:
        ep = float(leg.get("entry_price") or 0)
    except (TypeError, ValueError):
        return 0.0
    return abs(ep)


def _leg_pl_usd(
    S: float,
    leg: dict,
    T_years: float,
    iv: float,
    r: float,
) -> float:
    """P&L nohy v USD vs. vstupná prémia (entry_price × kontrakty × 100)."""
    K = float(leg.get("strike") or 0)
    if K <= 0 or S <= 0:
        return 0.0
    ep = _entry_price_per_share_for_bs(leg)
    try:
        c_raw = float(leg.get("contracts") or 1)
    except (TypeError, ValueError):
        c_raw = 1.0
    c_abs = abs(c_raw)
    if c_abs < 1e-12:
        c_abs = 1.0
    mult = c_abs * 100.0
    sgn = 1.0 if str(leg.get("leg_type") or "").strip().capitalize() == "Long" else -1.0
    right = _right_bs(leg.get("option_type"))
    Ty = max(0.0, float(T_years))
    if Ty <= 0.0 or iv <= 0.0:
        px = bs_price(S, K, 0.0, max(iv, 1e-6), right, r)
    else:
        px = bs_price(S, K, Ty, iv, right, r)
    return sgn * (float(px) - ep) * mult


_DEFAULT_FORWARD_DAYS: tuple[int, ...] = (2, 3, 5)


def _normalize_forward_days(forward_days: tuple[int, ...] | None) -> tuple[int, ...]:
    """1–10 dní; ``()`` = vypnúť dopredné krivky; ``None`` = predvolené (2, 3, 5)."""
    if forward_days is not None and len(forward_days) == 0:
        return ()
    src = forward_days if forward_days is not None else _DEFAULT_FORWARD_DAYS
    xs = sorted({int(x) for x in src if 1 <= int(x) <= 10})
    return tuple(xs)


def _compute_pl_curves_on_spots(
    legs: list[dict],
    spots: list[float],
    today: date,
    horizon: date,
    r: float,
    fwd_sorted: tuple[int, ...],
    *,
    with_horizon: bool = True,
) -> tuple[list[float], list[float], dict[int, list[float]]]:
    """P&L vs. zoznam spotov S: dnes, voliteľne horizont najbližšej exp., a dopredné dni."""
    pl_now: list[float] = []
    pl_hz: list[float] = []
    for S in spots:
        tot_n = 0.0
        tot_h = 0.0
        for leg in legs:
            dte = calc_dte(leg.get("expiry"))
            if dte is None:
                continue
            iv = _iv_fraction_for_leg(leg)
            Tn = max(1.0 / 365.0, dte / 365.0)
            tot_n += _leg_pl_usd(S, leg, Tn, iv, r)

            if with_horizon:
                exp_d = _expiry_to_date(str(leg.get("expiry") or ""))
                if exp_d is None:
                    continue
                days_left = (exp_d - horizon).days
                if days_left <= 0:
                    Th = 0.0
                else:
                    Th = max(1.0 / 365.0, days_left / 365.0)
                tot_h += _leg_pl_usd(S, leg, Th, iv, r)
        pl_now.append(round(tot_n, 2))
        if with_horizon:
            pl_hz.append(round(tot_h, 2))

    pl_fwd_by_day: dict[int, list[float]] = {}
    for k in fwd_sorted:
        asof_k = today + timedelta(days=int(k))
        pl_k: list[float] = []
        for S in spots:
            tot_k = 0.0
            for leg in legs:
                dte_k = _dte_leg_asof(leg.get("expiry"), asof_k)
                if dte_k is None:
                    continue
                iv = _iv_fraction_for_leg(leg)
                if dte_k <= 0:
                    Tn = 0.0
                else:
                    Tn = max(1.0 / 365.0, float(dte_k) / 365.0)
                tot_k += _leg_pl_usd(S, leg, Tn, iv, r)
            pl_k.append(round(tot_k, 2))
        pl_fwd_by_day[int(k)] = pl_k

    return pl_now, pl_hz, pl_fwd_by_day


def _interp_y_on_grid(xs: list[float], ys: list[float], x: float) -> float:
    if not xs or not ys:
        return 0.0
    if x <= xs[0]:
        return float(ys[0])
    if x >= xs[-1]:
        return float(ys[-1])
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            denom = xs[i + 1] - xs[i]
            if abs(denom) < 1e-15:
                return float(ys[i])
            w = (x - xs[i]) / denom
            return float(ys[i] + w * (ys[i + 1] - ys[i]))
    return float(ys[-1])


def _pick_short_anchor_leg(
    legs: list[dict],
    *,
    spot_hint: float | None,
) -> tuple[dict | None, float | None, date | None]:
    """
    Short noha pre „strike shortu“: najbližšia budúca (alebo najbližšia) expirácia medzi shortami;
    pri viacerých strikoch v ten istý deň — strike najbližší k ``spot_hint``.
    """
    candidates: list[tuple[dict, float, date]] = []
    for leg in legs:
        if str(leg.get("leg_type") or "").strip().capitalize() != "Short":
            continue
        try:
            K = float(leg.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if K <= 0:
            continue
        ed = _expiry_to_date(str(leg.get("expiry") or ""))
        if ed is None:
            continue
        candidates.append((leg, K, ed))
    if not candidates:
        return None, None, None
    today = date.today()
    fut = [(L, K, e) for L, K, e in candidates if e >= today]
    pool = fut if fut else list(candidates)
    min_e = min(e for _, _, e in pool)
    same = [(L, K, e) for L, K, e in pool if e == min_e]
    hint = float(spot_hint) if spot_hint is not None and spot_hint > 0 else None
    if len(same) == 1:
        L, K, e = same[0]
        return L, float(K), e
    if hint is not None and not math.isnan(hint):
        same.sort(key=lambda t: abs(t[1] - hint))
        L, K, e = same[0]
        return L, float(K), e
    same.sort(key=lambda t: t[1])
    L, K, e = same[len(same) // 2]
    return L, float(K), e


def _stop_spot_window_from_short_entry(
    entry_per_share: float | None,
    below_override: float | None,
    above_override: float | None,
) -> tuple[float, float]:
    """
    Šírka okna podkladu okolo ``K_short``: väčšinou dole (pokles), málo hore.
    Ak nie je override, škáluje sa podľa |vstupná prémia| short nohy (USD/akciu).
    """
    ae = abs(float(entry_per_share)) if entry_per_share is not None else 0.0
    if ae < 1e-12:
        bw0, aw0 = 48.0, 4.0
    else:
        bw0 = max(18.0, min(140.0, 18.0 + ae * 52.0))
        aw0 = max(2.5, min(24.0, 1.2 + ae * 6.5))
    bw = float(below_override) if below_override is not None else bw0
    aw = float(above_override) if above_override is not None else aw0
    return max(1.0, bw), max(0.5, aw)


def journal_group_pl_stoploss_short_window(
    legs: list[dict],
    *,
    spot_center: float | None = None,
    marker_source: str | None = None,
    forward_days: tuple[int, ...] | None = None,
    below_usd: float | None = None,
    above_usd: float | None = None,
    n_points: int = 72,
    r: float = 0.045,
) -> Optional[dict[str, Any]]:
    """
    Samostatný pohľad na **pokles podkladu** okolo striku **short** nohy.

    Os **X = cena podkladu** (USD); rozsah ``[K_short − Δ↓, K_short + Δ↑]`` sa predvolene odvodí od
    **vstupnej prémie** short nohy (``entry_price`` v journali, USD na akciu). Voliteľné ``below_usd`` / ``above_usd``
    prepíšu šírku (USD pod / nad strike).

    Krivky: **dnes** + **+2 / +3 / +5 dní** (bez horizontu najbližšej exp.).
    """
    if not legs:
        return None
    tickers = {str(t.get("ticker") or "").strip().upper() for t in legs}
    tickers.discard("")
    if len(tickers) != 1:
        return None
    ticker = next(iter(tickers))

    exp_dates: list[date] = []
    for t in legs:
        ed = _expiry_to_date(str(t.get("expiry") or ""))
        if ed is not None:
            exp_dates.append(ed)
    if not exp_dates:
        return None
    today = date.today()
    future = [d for d in exp_dates if d >= today]
    horizon = min(future) if future else max(exp_dates)

    leg_s, K_short, short_exp = _pick_short_anchor_leg(legs, spot_hint=spot_center)
    if K_short is None or K_short <= 0 or short_exp is None:
        return None

    ep_raw = leg_s.get("entry_price") if leg_s else None
    try:
        ep_share = float(ep_raw) if ep_raw is not None else None
    except (TypeError, ValueError):
        ep_share = None
    bw, aw = _stop_spot_window_from_short_entry(ep_share, below_usd, above_usd)

    marker_spot: float | None = None
    if spot_center is not None and float(spot_center) > 0:
        marker_spot = float(spot_center)

    s_lo = float(K_short) - bw
    s_hi = float(K_short) + aw
    if marker_spot is not None:
        s_lo = min(s_lo, marker_spot - max(1.0, aw * 0.35))
        s_hi = max(s_hi, marker_spot + max(0.5, aw * 0.2))
    s_lo = max(0.5, s_lo)
    if s_hi <= s_lo:
        s_hi = s_lo + max(5.0, float(K_short) * 0.02)

    n = max(20, min(int(n_points), 200))
    spots = [s_lo + (s_hi - s_lo) * i / (n - 1) for i in range(n)]
    x_rel = [round(s - float(K_short), 6) for s in spots]

    fwd_sorted = _normalize_forward_days(forward_days)
    pl_now, _, pl_fwd_by_day = _compute_pl_curves_on_spots(
        legs, spots, today, horizon, r, fwd_sorted, with_horizon=False
    )

    y_now_m = _interp_y_on_grid(spots, pl_now, marker_spot) if marker_spot is not None else None
    marker_x_rel = (marker_spot - K_short) if marker_spot is not None else None
    pl_fwd_at_marker: dict[int, float | None] = {}
    for k in fwd_sorted:
        ys = pl_fwd_by_day.get(int(k))
        pl_fwd_at_marker[int(k)] = (
            _interp_y_on_grid(spots, ys, marker_spot)
            if marker_spot is not None and ys is not None
            else None
        )

    ep_lbl = f"{abs(ep_share):.2f} $/akcia" if ep_share is not None and abs(ep_share) >= 1e-9 else "—"
    note = (
        f"**Stop-loss pohľad** — short **{K_short:g}** (exp. {short_exp.isoformat()}), podklad **{ticker}**. "
        f"Os X: **cena podkladu** {s_lo:.2f} … {s_hi:.2f} USD (okolo striku; rozsah z **vstupnej prémie** short nohy "
        f"**{ep_lbl}** → šírka **{bw:.1f}** USD pod K a **{aw:.1f}** USD nad K). BS, IV z journalu."
    )
    if marker_spot is not None:
        src = (marker_source or "").strip()
        tag = f" ({src})" if src else ""
        note += f" **Spot**{tag}: {marker_spot:.2f} → **spot−K**: {marker_x_rel:+.2f}."
    if fwd_sorted:
        note += f" Tenšie čiary: +{', +'.join(str(d) for d in fwd_sorted)} dní."

    return {
        "x_spot_minus_short": x_rel,
        "spots": spots,
        "k_short": float(K_short),
        "short_expiry": short_exp,
        "ticker": ticker,
        "marker_spot": marker_spot,
        "marker_x_rel": marker_x_rel,
        "marker_source": (marker_source or "").strip() or None,
        "pl_now": pl_now,
        "pl_now_at_marker": y_now_m,
        "spot_axis_lo": s_lo,
        "spot_axis_hi": s_hi,
        "short_entry_per_share": ep_share,
        "window_below_usd": bw,
        "window_above_usd": aw,
        "forward_days": list(fwd_sorted),
        "pl_fwd_by_day": pl_fwd_by_day,
        "pl_fwd_at_marker": pl_fwd_at_marker,
        "note": note,
    }


def journal_spot_levels_descending(high: float, low: float, step: float) -> list[float]:
    """
    Úroveň spotu od ``high`` smerom nadol po ``low`` (vrátane), krok ``step`` USD.
    Ak posledný krok preskočí ``low``, doplní sa presná hodnota ``low``.
    """
    hi = float(high)
    lo = float(low)
    if hi < lo:
        hi, lo = lo, hi
    st = max(0.01, float(step))
    out: list[float] = [round(hi, 4)]
    x = hi
    while x > lo + 1e-9:
        x = round(x - st, 4)
        if x < lo:
            x = lo
        if abs(x - out[-1]) < 1e-8:
            break
        out.append(round(x, 4))
        if abs(x - lo) < 1e-8:
            break
    if out and out[-1] > lo + 1e-6:
        out.append(round(lo, 4))
    seen: set[float] = set()
    dedup: list[float] = []
    for v in out:
        if v in seen:
            continue
        seen.add(v)
        dedup.append(v)
    return dedup


def journal_spot_levels_band(
    center: float,
    above_usd: float,
    below_usd: float,
    step: float,
) -> list[float]:
    """
    Spotové úrovne od ``center + above_usd`` smerom nadol po ``max(1, center − below_usd)``, krok ``step``.

    Oproti ``journal_spot_levels_descending(center, low, …)`` pridáva aj **úrovne nad** referenčným spotom.
    Ak presný ``center`` nepadne na mriežku kroku, doplní sa do zoznamu.
    """
    c = float(center)
    if c <= 0 or math.isnan(c):
        return []
    ab = max(0.0, float(above_usd))
    be = max(0.0, float(below_usd))
    hi = c + ab
    lo = max(1.0, c - be)
    if hi < lo:
        hi, lo = lo, hi
    st = max(0.01, float(step))
    out = journal_spot_levels_descending(hi, lo, st)
    eps = max(1e-6, st * 1e-4)
    if not any(abs(x - c) < eps for x in out):
        out = out + [round(c, 4)]
    out.sort(reverse=True)
    seen: set[float] = set()
    dedup: list[float] = []
    for v in out:
        if v in seen:
            continue
        seen.add(v)
        dedup.append(v)
    return dedup


def _pl_long_short_net_at_spot_today(legs: list[dict], S: float, r: float) -> tuple[float, float, float]:
    """Súčet modelového P&L „dnes“ (USD) pre long nohy, pre short nohy a čistý súčet."""
    long_tot = 0.0
    short_tot = 0.0
    for leg in legs:
        dte = calc_dte(leg.get("expiry"))
        if dte is None:
            continue
        iv = _iv_fraction_for_leg(leg)
        Tn = max(1.0 / 365.0, float(dte) / 365.0)
        pl = _leg_pl_usd(S, leg, Tn, iv, r)
        lt = str(leg.get("leg_type") or "").strip().capitalize()
        if lt == "Short":
            short_tot += pl
        else:
            long_tot += pl
    return long_tot, short_tot, long_tot + short_tot


def _single_leg_pl_now_usd(leg: dict, S: float, r: float) -> float | None:
    dte = calc_dte(leg.get("expiry"))
    if dte is None:
        return None
    iv = _iv_fraction_for_leg(leg)
    Tn = max(1.0 / 365.0, float(dte) / 365.0)
    return _leg_pl_usd(S, leg, Tn, iv, r)


def _journal_leg_instrument_label(leg: dict) -> str:
    """Krátky text kontraktu (ticker dátum strike CALL/PUT) — podobne ako v TWS."""
    tk = str(leg.get("ticker") or "").strip().upper()
    raw_ot = str(leg.get("option_type") or "").strip().upper()
    ot = "CALL" if raw_ot.startswith("C") else "PUT"
    try:
        k = float(leg.get("strike") or 0)
    except (TypeError, ValueError):
        k = 0.0
    ex = str(leg.get("expiry") or "").strip()
    ed = _expiry_to_date(ex.split()[0] if ex else "")
    if ed is not None:
        exs = ed.strftime("%d.%m.%y")
    else:
        exs = (ex[:10] if len(ex) >= 10 else ex) or "?"
    if abs(k - round(k)) < 1e-9:
        ks = f"{int(round(k))}"
    else:
        ks = f"{k:.2f}".rstrip("0").rstrip(".")
    return f"{tk} {exs} {ks} {ot}"


def _leg_contracts_signed_display(leg: dict) -> int:
    try:
        c = abs(int(round(float(leg.get("contracts") or 1))))
    except (TypeError, ValueError):
        c = 1
    c = max(1, c)
    if str(leg.get("leg_type") or "").strip().capitalize() == "Short":
        return -c
    return c


def _legs_display_order(legs: list[dict]) -> list[dict]:
    """Long nohy prvé, potom short; v rámci skupiny podľa expirácie."""

    def _key(leg: dict) -> tuple[int, int]:
        lt = 0 if str(leg.get("leg_type") or "").strip().capitalize() == "Long" else 1
        ed = _expiry_to_date(str(leg.get("expiry") or "").strip().split()[0])
        return lt, ed.toordinal() if ed else 0

    return sorted(legs, key=_key)


def journal_group_pl_ladder_tws_style_rows(
    legs: list[dict],
    spot_levels: list[float],
    *,
    r: float = 0.045,
) -> Optional[list[dict[str, Any]]]:
    """
    Riadky v štýle TWS: pre každý **spot** najprv každá **noha** (kontrakt, strana, ks., P&L v USD),
    potom riadok **Σ NET** (celá skupina). P&L = BS model „dnes“, IV z journalu (nie presná kópia TWS).
    """
    if not legs or not spot_levels:
        return None
    tickers = {str(t.get("ticker") or "").strip().upper() for t in legs}
    tickers.discard("")
    if len(tickers) != 1:
        return None
    exp_dates: list[date] = []
    for t in legs:
        ed = _expiry_to_date(str(t.get("expiry") or ""))
        if ed is not None:
            exp_dates.append(ed)
    if not exp_dates:
        return None
    legs_sorted = _legs_display_order(list(legs))
    out: list[dict[str, Any]] = []
    for s in spot_levels:
        sf = max(0.5, float(s))
        spot_r = round(sf, 2)
        for leg in legs_sorted:
            plv = _single_leg_pl_now_usd(leg, sf, r)
            if plv is None:
                continue
            out.append(
                {
                    "spot": spot_r,
                    "kontrakt": _journal_leg_instrument_label(leg),
                    "noha": str(leg.get("leg_type") or "").strip().capitalize() or "—",
                    "ks": f"{_leg_contracts_signed_display(leg):+d}",
                    "pl_usd": int(round(float(plv))),
                    "_typ": "noha",
                }
            )
        lo, sh, net = _pl_long_short_net_at_spot_today(legs, sf, r)
        out.append(
            {
                "spot": spot_r,
                "kontrakt": "Σ NET",
                "noha": "",
                "ks": "",
                "pl_usd": int(round(float(net))),
                "_typ": "net",
            }
        )
    return out


def journal_group_pl_rows_at_spots(
    legs: list[dict],
    spot_levels: list[float],
    *,
    r: float = 0.045,
) -> Optional[list[dict[str, float]]]:
    """
    Riadky pre každý spot: ``spot``, ``pl_long_usd``, ``pl_short_usd``, ``pl_net_usd`` (Long+Short),
    voliteľne ``spot_minus_k`` (voči striku anchora short nohy).
    """
    if not legs or not spot_levels:
        return None
    tickers = {str(t.get("ticker") or "").strip().upper() for t in legs}
    tickers.discard("")
    if len(tickers) != 1:
        return None
    exp_dates: list[date] = []
    for t in legs:
        ed = _expiry_to_date(str(t.get("expiry") or ""))
        if ed is not None:
            exp_dates.append(ed)
    if not exp_dates:
        return None
    spots = [max(0.5, float(s)) for s in spot_levels]
    _, K_short, _ = _pick_short_anchor_leg(legs, spot_hint=spots[0])
    k = float(K_short) if K_short is not None else None
    rows: list[dict[str, float]] = []
    for s in spots:
        lo, sh, net = _pl_long_short_net_at_spot_today(legs, s, r)
        row: dict[str, float] = {
            "spot": round(float(s), 4),
            "pl_long_usd": round(lo, 2),
            "pl_short_usd": round(sh, 2),
            "pl_net_usd": round(net, 2),
        }
        if k is not None:
            row["spot_minus_k"] = round(float(s) - k, 4)
        rows.append(row)
    return rows


def journal_group_pl_vs_spot(
    legs: list[dict],
    *,
    spot_center: float | None = None,
    marker_source: str | None = None,
    forward_days: tuple[int, ...] | None = None,
    spot_min: float | None = None,
    spot_max: float | None = None,
    n_points: int = 72,
    r: float = 0.045,
) -> Optional[dict[str, Any]]:
    """
    Vráti ``{spots, pl_now, pl_horizon, pl_fwd_by_day?, …}`` alebo ``None``.

    - **pl_now**: P&L pri dnešnom zostávajúcom čase do expirácie každej nohy (DTE z ``calc_dte``).
    - **pl_horizon**: P&L na kalendárny deň **najbližšej expirácie** v skupine: pre každú nohu
      zostávajúci čas = expirácia nohy − tento deň (0 = iba intrinsic).
    - **pl_fwd_by_day**: voliteľne P&L vs. ``S`` pri posune „dnes“ o **k** kalendárnych dní (BS, rovnaká IV);
      zvýrazní, ako sa pri rozdielnych Δ nohách mení kompenzácia v čase (theta + skrátený čas).
    """
    if not legs:
        return None
    tickers = {str(t.get("ticker") or "").strip().upper() for t in legs}
    tickers.discard("")
    if len(tickers) != 1:
        return None
    ticker = next(iter(tickers))

    exp_dates: list[date] = []
    for t in legs:
        ed = _expiry_to_date(str(t.get("expiry") or ""))
        if ed is not None:
            exp_dates.append(ed)
    if not exp_dates:
        return None
    today = date.today()
    future = [d for d in exp_dates if d >= today]
    horizon = min(future) if future else max(exp_dates)

    strikes = [float(t.get("strike") or 0) for t in legs]
    ks = [x for x in strikes if x > 0]
    if not ks:
        return None

    if spot_center is not None and spot_center > 0:
        s0 = float(spot_center)
    else:
        s0 = sum(ks) / len(ks)

    if spot_min is not None and spot_max is not None and spot_max > spot_min:
        lo, hi = float(spot_min), float(spot_max)
    else:
        lo = max(1.0, min(ks) * 0.72)
        hi = max(lo + 1.0, max(ks) * 1.28)
        mid = s0
        span = hi - lo
        lo = max(1.0, mid - span * 0.55)
        hi = max(lo + 1.0, mid + span * 0.55)

    marker_spot: float | None = None
    if spot_center is not None and float(spot_center) > 0:
        marker_spot = float(spot_center)
        lo = min(lo, marker_spot * 0.96)
        hi = max(hi, marker_spot * 1.04)
        if hi <= lo:
            hi = lo * 1.01

    n = max(16, min(int(n_points), 200))
    spots = [lo + (hi - lo) * i / (n - 1) for i in range(n)]

    fwd_sorted = _normalize_forward_days(forward_days)
    pl_now, pl_hz, pl_fwd_by_day = _compute_pl_curves_on_spots(
        legs, spots, today, horizon, r, fwd_sorted, with_horizon=True
    )

    y_now_m = _interp_y_on_grid(spots, pl_now, marker_spot) if marker_spot is not None else None
    y_hz_m = _interp_y_on_grid(spots, pl_hz, marker_spot) if marker_spot is not None else None
    pl_fwd_at_marker: dict[int, float | None] = {}
    for k in fwd_sorted:
        ys = pl_fwd_by_day.get(int(k))
        pl_fwd_at_marker[int(k)] = (
            _interp_y_on_grid(spots, ys, marker_spot)
            if marker_spot is not None and ys is not None
            else None
        )

    note = (
        f"Podklad **{ticker}**, horizont **{horizon.isoformat()}** (najbližšia expirácia v skupine). "
        "Model: BS, **r = 4,5 %**, IV z journalu (aktuálna / vstup / 30 %). Nie je to presná kópia TWS."
    )
    if len({str(t.get("expiry")) for t in legs}) > 1:
        note += " **Diagonála / viac expirácií:** čiarkovaná čiara je zjednodušená (čas do vlastnej expirácie oproti dňu najbližšej expirácie v skupine)."
    if fwd_sorted:
        note += (
            f" **Čas dopredu (+{', +'.join(str(d) for d in fwd_sorted)} dní):** tenšie čiary — P&L vs. spot pri posune dátumu "
            "(BS, rovnaká IV z journalu); pri **rozdielnej Δ** nohách sa krivky pod spotom v čase **rozbiehajú** (nekompenzujú rovnomerne)."
        )

    if marker_spot is not None:
        src = (marker_source or "").strip()
        tag = f" ({src})" if src else ""
        note += f" **Aktuálny spot**{tag}: {marker_spot:.2f} — v grafe zvýraznený."

    return {
        "spots": spots,
        "pl_now": pl_now,
        "pl_horizon": pl_hz,
        "s0": s0,
        "marker_spot": marker_spot,
        "marker_source": (marker_source or "").strip() or None,
        "pl_now_at_marker": y_now_m,
        "pl_horizon_at_marker": y_hz_m,
        "horizon_date": horizon,
        "ticker": ticker,
        "note": note,
        "forward_days": list(fwd_sorted),
        "pl_fwd_by_day": pl_fwd_by_day,
        "pl_fwd_at_marker": pl_fwd_at_marker,
    }
