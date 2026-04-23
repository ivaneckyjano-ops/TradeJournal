"""
CSV variant kalendára / diagonály → nohy pre Spread Builder + krátke slovné zhrnutie top 1.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

import numpy as np
import pandas as pd

from core.expiration_catalog import get_catalog_expiries
from core.probability import bs_price, calc_greeks

_LEG_DELTA = "leg_delta_usd"
_LEG_THETA = "leg_theta_per_day_usd"
_LEG_VEGA = "leg_vega_usd"
_LEG_GAMMA = "leg_gamma"


def norm_header(col: str) -> str:
    s = str(col).strip().lower().replace("~", "")
    return re.sub(r"\s+", "_", s)


def parse_number(value: Any) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return float("nan")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip().replace("\u00a0", " ").replace("%", "").replace(" ", "")
    if not s:
        return float("nan")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def parse_expiry_to_yyyymmdd(raw: Any) -> Optional[str]:
    if raw is None or (isinstance(raw, float) and np.isnan(raw)):
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("/", "-").replace(".", "-")
    digits = re.sub(r"\D", "", s)
    if len(digits) == 8:
        try:
            datetime.strptime(digits, "%Y%m%d")
            return digits
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m-%d-%Y"):
        try:
            d = datetime.strptime(s.replace("/", "-"), fmt).date()
            return d.strftime("%Y%m%d")
        except ValueError:
            continue
    return None


def series_to_norm_dict(row: pd.Series) -> dict[str, str]:
    out: dict[str, str] = {}
    for k in row.index:
        nk = norm_header(str(k))
        v = row[k]
        if pd.isna(v):
            out[nk] = ""
        else:
            out[nk] = str(v).strip()
    return out


def underlying_ticker_from_norm(norm: dict[str, str]) -> str:
    """
    Ticker podkladu z bežných názvov stĳpcov v CSV/Exceli (po norm_header).
    Neberie prvý ticker z DB — ak nič nenájde, vráti prázdny reťazec.
    """
    for key in (
        "ticker",
        "symbol",
        "underlying",
        "underlying_symbol",
        "stock_ticker",
        "equity_symbol",
        "equity",
        "root",
        "root_symbol",
        "stock",
        "ul",
        "sym",
        "base_symbol",
    ):
        raw = (norm.get(key) or "").strip()
        if not raw:
            continue
        first = raw.upper().split()[0]
        tok = "".join(ch for ch in first if ch.isalnum())
        if not tok or tok.isdigit():
            continue
        if len(tok) > 12:
            continue
        return tok
    return ""


def _dte_yyyymmdd(expiry_str: str) -> int:
    try:
        e = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
        return max(0, (e - date.today()).days)
    except Exception:
        return 0


def _infer_right(norm: dict[str, str]) -> str:
    for key in ("right", "cp", "call_put", "option_type", "typ", "type"):
        v = (norm.get(key) or "").strip().lower()
        if not v:
            continue
        if v in ("c", "call", "calls"):
            return "C"
        if v in ("p", "put", "puts"):
            return "P"
        if v.startswith("call"):
            return "C"
        if v.startswith("put"):
            return "P"
    return "C"


def _infer_strike(norm: dict[str, str]) -> Optional[float]:
    for key in (
        "leg1_strike",
        "leg_1_strike",
        "strike",
        "long_strike",
        "short_strike",
        "leg2_strike",
        "leg_2_strike",
        "k",
        "atm_strike",
        "strike_price",
    ):
        v = norm.get(key, "").strip()
        if not v:
            continue
        x = parse_number(v)
        if not np.isnan(x) and x > 0:
            return float(x)
    return None


def _strike_first_in_keys(norm: dict[str, str], keys: tuple[str, ...]) -> Optional[float]:
    for key in keys:
        v = norm.get(key, "").strip()
        if not v:
            continue
        x = parse_number(v)
        if not np.isnan(x) and float(x) > 0:
            return float(x)
    return None


def _strikes_short_long_from_row(norm: dict[str, str]) -> tuple[Optional[float], Optional[float]]:
    """
    Striky pre krátku (Leg1 / front) a dlhú (Leg2 / back) nohu.
    Kalendár: často len Leg1 Strike → oba rovnaké. Diagonál: Leg1 + Leg2 (alebo short/long strike).
    """
    s1 = _strike_first_in_keys(
        norm,
        (
            "leg1_strike",
            "leg_1_strike",
            "short_strike",
            "shortstrike",
            "strike1",
            "k1",
            "near_strike",
            "front_strike",
            "strike_short",
        ),
    )
    s2 = _strike_first_in_keys(
        norm,
        (
            "leg2_strike",
            "leg_2_strike",
            "long_strike",
            "longstrike",
            "strike2",
            "k2",
            "far_strike",
            "back_strike",
            "strike_long",
        ),
    )
    return s1, s2


def _pick_first_expiry(norm: dict[str, str], keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        cell = norm.get(k, "").strip()
        if not cell:
            continue
        y = parse_expiry_to_yyyymmdd(cell)
        if y:
            return y
    return None


def resolve_calendar_expiries(
    norm: dict[str, str],
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Vráti (near, far, err, notice).
    Near = skrátená noha (bližšia expirácia), far = dlhá.
    notice = neblokujúca pripomienka (napr. dosadené predvolené dátumy).
    """
    e1 = _pick_first_expiry(
        norm,
        (
            "exp_leg1",
            "exp_leg_1",
            "expleg1",
            "exp1",
            "exp_short",
            "short_exp",
            "short_expiry",
            "near_exp",
            "front_exp",
            "predna_exp",
            "leg1_exp",
            "leg_1_exp",
            "expiration_leg1",
            "expiration_leg_1",
            "expiry1",
            "expiry_leg1",
            "first_expiry",
            "front_expiry",
        ),
    )
    e2 = _pick_first_expiry(
        norm,
        (
            "exp_leg2",
            "exp_leg_2",
            "expleg2",
            "exp2",
            "exp_long",
            "long_exp",
            "long_expiry",
            "far_exp",
            "back_exp",
            "zadna_exp",
            "leg2_exp",
            "leg_2_exp",
            "expiration_leg2",
            "expiration_leg_2",
            "expiry2",
            "expiry_leg2",
            "second_expiry",
            "back_expiry",
        ),
    )

    if e1 and e2:
        d1, d2 = _dte_yyyymmdd(e1), _dte_yyyymmdd(e2)
        if d1 <= d2:
            return e1, e2, None, None
        return e2, e1, None, None

    only = e1 or e2
    if not only:
        td = date.today()
        short_exp = (td + timedelta(days=21)).strftime("%Y%m%d")
        long_exp = (td + timedelta(days=63)).strftime("%Y%m%d")
        return (
            short_exp,
            long_exp,
            None,
            "V CSV sa nenašli rozpoznateľné expirácie — dosadené orientačné dátumy (+21 / +63 dní od dnes). "
            "Uprav ich **kalendárom** v Spread Builderi.",
        )

    long_exp = only
    exps = get_catalog_expiries(months=18)
    candidates: list[str] = []
    ld = _dte_yyyymmdd(long_exp)
    for e in exps:
        de = _dte_yyyymmdd(e)
        if 0 < de < ld and ld - de >= 7:
            candidates.append(e)
    if candidates:
        short_exp = max(candidates, key=_dte_yyyymmdd)
        return short_exp, long_exp, None, None

    try:
        ld_dt = datetime.strptime(long_exp, "%Y%m%d").date()
        sd_dt = ld_dt - timedelta(days=35)
        if sd_dt <= date.today():
            sd_dt = date.today() + timedelta(days=14)
        short_exp = sd_dt.strftime("%Y%m%d")
        return short_exp, long_exp, None, None
    except Exception:
        return None, None, "Nepodarilo sa dopočítať druhú expiráciu — doplň **Exp Leg1** a **Exp Leg2** v CSV.", None


def _first_numeric(norm: dict[str, str], *keys: str) -> Optional[float]:
    """Prvá kladná hodnota z normovaných kľúčov (CSV čísla s čiarkou / %)."""
    for k in keys:
        cell = norm.get(k, "").strip()
        if not cell:
            continue
        x = parse_number(cell)
        if not np.isnan(x) and float(x) > 0:
            return float(x)
    return None


def _iv_fraction_from_norm(norm: dict[str, str], *keys: str) -> Optional[float]:
    """IV ako zlomok (0.30); vstup môže byť percentá (62.35 alebo 62.35%)."""
    for k in keys:
        cell = norm.get(k, "").strip()
        if not cell:
            continue
        x = parse_number(cell)
        if np.isnan(x) or x <= 0:
            continue
        if x > 1.0:
            x = x / 100.0
        return float(min(max(x, 0.01), 5.0))
    return None


def _apply_net_greek_scales(legs: list[dict], norm: dict[str, str], contracts: int) -> None:
    """
    Ak CSV má Net Delta / Net Vega / Net Theta (typicky „na akciu“ ako v Exceli),
    preškálujeme uložené pozičné USD na nohách tak, aby ich súčet sedel s cieľom.

    |hodnota| pod prahom → považujeme za agregát „na akciu“ × 100 × kontrakty.
    """
    n = max(1, int(contracts))
    # theta z TWS býva malé číslo (napr. 0.07); delta/vega často < ~5 na akciu
    specs: tuple[tuple[str, str, float], ...] = (
        ("net_delta", _LEG_DELTA, 12.0),
        ("net_vega", _LEG_VEGA, 12.0),
        ("net_theta", _LEG_THETA, 1.5),
    )
    for nk, attr, thresh in specs:
        cell = norm.get(nk, "").strip()
        if not cell:
            continue
        raw = parse_number(cell)
        if np.isnan(raw):
            continue
        if abs(float(raw)) < thresh:
            target = float(raw) * 100.0 * n
        else:
            target = float(raw)
        cur = sum(float(lg.get(attr) or 0) for lg in legs)
        if abs(cur) < 1e-12:
            continue
        k = target / cur
        for lg in legs:
            lg[attr] = float(lg.get(attr) or 0) * k


def _set_leg_greeks(leg: dict, spot: float) -> None:
    iv = float(leg.get("iv") or 0.3)
    dte = max(1, _dte_yyyymmdd(str(leg.get("expiry") or "")))
    sign = -1 if leg.get("leg_type") == "Short" else 1
    n = int(leg.get("contracts", 1) or 1)
    if dte <= 0 or spot <= 0 or iv <= 0:
        leg[_LEG_DELTA] = leg[_LEG_THETA] = leg[_LEG_VEGA] = leg[_LEG_GAMMA] = 0.0
        return
    g = calc_greeks(spot, float(leg["strike"]), dte, iv, str(leg.get("right") or "C"))
    leg[_LEG_DELTA] = (g.get("delta") or 0) * sign * n * 100
    leg[_LEG_THETA] = (g.get("theta") or 0) * sign * n * 100
    leg[_LEG_VEGA] = (g.get("vega") or 0) * sign * n * 100
    leg[_LEG_GAMMA] = (g.get("gamma") or 0) * sign * n * 100


def _make_leg(
    leg_id: int,
    leg_type: str,
    right: str,
    strike: float,
    expiry: str,
    contracts: int,
    spot: float,
    iv: float,
    *,
    entry_override: Optional[float] = None,
) -> dict:
    dte = max(1, _dte_yyyymmdd(expiry))
    if entry_override is not None and not np.isnan(entry_override) and float(entry_override) > 0:
        ep = round(float(entry_override), 2)
    else:
        ep = bs_price(spot, strike, dte, iv, right)
        ep = round(max(0.01, ep or 0.5), 2)
    leg = {
        "id": leg_id,
        "leg_type": leg_type,
        "right": right,
        "strike": float(strike),
        "expiry": expiry,
        "contracts": int(contracts),
        "entry_price": ep,
        "iv": float(iv),
    }
    _set_leg_greeks(leg, spot)
    return leg


def calendar_legs_from_variant_row(
    row: pd.Series,
    *,
    spot: float,
    iv: float,
    contracts: int = 1,
) -> tuple[list[dict], Optional[str], Optional[str]]:
    """
    Vráti (legs, err, notice). Dve nohy: short pri **near** expirácii, long pri **far**.
    Ak sa **Leg1 Strike** a **Leg2 Strike** (alebo long/short strike) líšia → **diagonála**;
    inak rovnaký strike ako **kalendár**.
    """
    norm = series_to_norm_dict(row)
    near, far, err, notice = resolve_calendar_expiries(norm)
    if err or not near or not far:
        return [], err or "Chýbajú expirácie.", notice

    s1, s2 = _strikes_short_long_from_row(norm)
    shared = _infer_strike(norm)
    if s1 is not None and s2 is not None:
        k_short, k_long = float(s1), float(s2)
    elif s1 is not None:
        k_short = k_long = float(s1)
    elif s2 is not None:
        k_short = k_long = float(s2)
    elif shared is not None and float(shared) > 0:
        k_short = k_long = float(shared)
    else:
        return (
            [],
            "V riadku chýba strike — skús **Leg1 Strike** / **Leg2 Strike**, **Short/Long strike** alebo **Strike**.",
            notice,
        )

    notice_parts: list[str] = []
    if notice:
        notice_parts.append(str(notice))
    if abs(k_short - k_long) > 1e-6:
        notice_parts.append(
            "Striky z **Leg1** a **Leg2** sa líšia — v Buildery ide o **diagonálu** (kalendár má rovnaký strike na oboch expiráciách)."
        )

    right = _infer_right(norm)
    sp = float(spot) if float(spot) > 0 else float(k_short)
    sp_csv = _first_numeric(norm, "price", "underlying_price", "spot_px", "spot", "last")
    if sp_csv is not None:
        sp = sp_csv
    iv_use = float(iv) if float(iv) > 0 else 0.30

    iv_short = _iv_fraction_from_norm(
        norm,
        "leg1_iv",
        "short_iv",
        "iv_leg1",
        "iv1",
        "front_iv",
        "near_iv",
    )
    iv_long = _iv_fraction_from_norm(
        norm,
        "leg2_iv",
        "long_iv",
        "iv_leg2",
        "iv2",
        "back_iv",
        "far_iv",
    )
    iv_s = iv_short if iv_short is not None else iv_use
    iv_l = iv_long if iv_long is not None else iv_use

    bid1 = _first_numeric(norm, "bid1", "bid_leg1", "bid_1", "short_bid", "leg1_bid")
    ask2 = _first_numeric(norm, "ask2", "ask_leg2", "ask_2", "long_ask", "leg2_ask")

    legs = [
        _make_leg(
            1,
            "Short",
            right,
            k_short,
            near,
            contracts,
            sp,
            iv_s,
            entry_override=bid1,
        ),
        _make_leg(
            2,
            "Long",
            right,
            k_long,
            far,
            contracts,
            sp,
            iv_l,
            entry_override=ask2,
        ),
    ]
    if bid1 is not None:
        legs[0]["tws_bid"] = float(bid1)
    if ask2 is not None:
        legs[1]["tws_ask"] = float(ask2)

    _apply_net_greek_scales(legs, norm, contracts)
    notice_out = " ".join(notice_parts) if notice_parts else None
    return legs, None, notice_out


def ticker_spot_iv_for_diagonal_send(
    ticker: str,
    *,
    strike_hint: Optional[float] = None,
) -> tuple[str, float, float]:
    """
    Ticker, spot a IV (zlomok 0–1) z tabuľky Symboly — rovnaká logika ako pri odoslaní z CSV variantov
    (bez ceny z CSV riadku). Spot 200 len ak nič iné.
    """
    from core import database as db

    tk = (ticker or "").strip().upper()
    if not tk:
        return "", max(1.0, float(strike_hint or 200.0)), 0.30
    sym = db.get_symbol(tk)
    spot = float(sym.get("spot") or 0) if sym else 0.0
    iv = 0.30
    if sym and sym.get("iv_pct") is not None:
        try:
            fv = float(sym["iv_pct"])
        except (TypeError, ValueError):
            fv = 0.30
        if fv > 1.0:
            fv = fv / 100.0
        iv = float(min(max(fv, 0.01), 5.0))
    if spot <= 0 and strike_hint is not None:
        sh = float(strike_hint)
        if sh > 0:
            spot = sh
    if spot <= 0:
        spot = 200.0
    return tk, spot, iv


def diagonal_legs_from_saved_display_row(
    row: pd.Series,
    *,
    spot: float,
    iv: float,
    contracts: int = 1,
) -> tuple[list[dict], Optional[str], Optional[str]]:
    """
    Riadok z uložených / náhľadových diagonál (stĺpce ``Short — …`` / ``Long — …`` / ``Typ``)
    → dve nohy (Short + Long) s expiráciami a strikmi presne ako v tabuľke — vhodné pre ``csv_calendar_variant`` patch.
    """
    skip = {"ID", "Uložené", "Zmazať", "Do Buildera", "Uložiť", "Ticker", "Snímka uloženia", "Stratégia ID"}
    clean_idx = [c for c in row.index if c not in skip]
    sub = row[clean_idx]
    norm = series_to_norm_dict(sub)
    for col in row.index:
        if col in skip or pd.isna(row[col]) or not str(row[col]).strip():
            continue
        lc = str(col).lower()
        cstr = str(col)
        cista = ("čistá" in lc) or ("cista" in lc)
        if cista and "delta" in lc and "theta" not in lc and "vega" not in lc and "gamma" not in lc:
            x = parse_number(row[col])
            if not np.isnan(x):
                if "×100" in cstr or "x100" in lc:
                    x = x / 100.0
                norm["net_delta"] = str(x)
        elif cista and "theta" in lc and "vega" not in lc:
            x = parse_number(row[col])
            if not np.isnan(x):
                if "×100" in cstr or "x100" in lc:
                    x = x / 100.0
                norm["net_theta"] = str(x)
        elif cista and "vega" in lc and "theta" not in lc and "gamma" not in lc:
            x = parse_number(row[col])
            if not np.isnan(x):
                if "×100" in cstr or "x100" in lc:
                    x = x / 100.0
                norm["net_vega"] = str(x)

    def _cell(*names) -> Any:
        for n in names:
            if n in row.index and pd.notna(row[n]) and str(row[n]).strip():
                return row[n]
        return None

    sx = _cell("Short — expirácia")
    lx = _cell("Long — expirácia")
    if sx is None or lx is None:
        return [], "Chýba Short alebo Long expirácia.", None

    near = parse_expiry_to_yyyymmdd(sx)
    far = parse_expiry_to_yyyymmdd(lx)
    if not near or not far:
        return [], "Nepodarilo sa rozparsovať dátum expirácie (očakávam YYYY-MM-DD).", None

    sk = parse_number(_cell("Short — strike"))
    lk = parse_number(_cell("Long — strike"))
    if np.isnan(sk) or np.isnan(lk) or sk <= 0 or lk <= 0:
        return [], "Chýbajú alebo sú neplatné striky Short / Long.", None

    typ = str(_cell("Typ") or "Call").strip().lower()
    if typ in ("p", "put", "puts") or typ.startswith("put"):
        right = "P"
    else:
        right = "C"

    bid1 = parse_number(_cell("Short — bid", "short_bid"))
    ask2 = parse_number(_cell("Long — ask", "long_ask"))

    iv_use = float(iv) if float(iv) > 0 else 0.30
    sp = float(spot) if float(spot) > 0 else float(max(sk, lk))

    legs = [
        _make_leg(1, "Short", right, float(sk), near, contracts, sp, iv_use, entry_override=bid1 if not np.isnan(bid1) else None),
        _make_leg(2, "Long", right, float(lk), far, contracts, sp, iv_use, entry_override=ask2 if not np.isnan(ask2) else None),
    ]
    if not np.isnan(bid1) and float(bid1) > 0:
        legs[0]["tws_bid"] = float(bid1)
    if not np.isnan(ask2) and float(ask2) > 0:
        legs[1]["tws_ask"] = float(ask2)

    _apply_net_greek_scales(legs, norm, contracts)

    strat = str(_cell("Stratégia") or "").strip()
    notice = f"Z uložených diagonál: {strat}" if strat else "Z uložených diagonál."
    return legs, None, notice


def csv_row_on_flag(norm: dict[str, str]) -> bool:
    for k in ("on", "active", "pick", "trade", "selected"):
        v = (norm.get(k) or "").strip().lower()
        if v in ("on", "yes", "áno", "ano", "1", "true", "x", "y", "✓", "ok"):
            return True
    return False


def verbally_assess_top1(
    top_row: pd.Series,
    full_ranked: pd.DataFrame,
    strategy: str,
) -> str:
    """Slovné zhrnutie prečo je prvý variant „on“ / na prvom mieste."""
    parts: list[str] = []
    norm = series_to_norm_dict(top_row)
    strat_labels = {
        "balanced": "Balanced (debit + skew + theta + delta)",
        "cheap": "Najnižší debit",
        "skew": "Najvyšší IV skew",
        "theta": "Najvyššia net theta",
    }
    sl = strat_labels.get(strategy, strategy)

    if csv_row_on_flag(norm):
        parts.append(
            "Stĺpec **On** (alebo ekvivalent) v CSV je pre tento riadok zapnutý — v tvojom výstupe to znamená „ber do úvahy / preferuj“ oproti riadkom bez On."
        )

    nd = top_row.get("_net_debit")
    try:
        debit_f = float(nd) if nd is not None and str(nd) != "" and not (isinstance(nd, float) and np.isnan(nd)) else float("nan")
    except (TypeError, ValueError):
        debit_f = float("nan")

    if full_ranked.empty:
        parts.append(f"**Top 1** podľa režimu *{sl}* — v dátach je len jeden použiteľný variant.")
        return " ".join(parts)

    if strategy == "cheap" and not np.isnan(debit_f):
        pool = pd.to_numeric(full_ranked["_net_debit"], errors="coerce").dropna()
        if len(pool) > 0:
            lo, hi = float(pool.min()), float(pool.max())
            parts.append(
                f"Medzi {len(pool)} variantmi má **najnižší Net debit** ({debit_f:g} $; v súbore od {lo:g} do {hi:g})."
            )
    elif strategy == "skew":
        parts.append(
            "Vyhráva **najvyšší IV skew** — predpoklad: lepší tvar volatility medzi nohami (krátka vs. dlhá expirácia) podľa tvojho exportu."
        )
    elif strategy == "theta":
        parts.append(
            "Vyhráva **najvyššia net theta** — variant s najsilnejším časovým rozpadom v prospech pozície podľa CSV."
        )
    elif strategy == "balanced":
        sc = top_row.get("_score")
        try:
            sc_f = float(sc) if sc is not None and str(sc) != "" else float("nan")
        except (TypeError, ValueError):
            sc_f = float("nan")
        if not np.isnan(sc_f):
            parts.append(f"Interné **balanced** skóre je **{sc_f:.4f}** (váhy: debit 40 %, skew 30 %, theta 20 %, |delta| 10 %).")
        if not np.isnan(debit_f):
            pool = pd.to_numeric(full_ranked["_net_debit"], errors="coerce").dropna()
            if len(pool) > 1:
                worse = (pool > debit_f).sum()
                parts.append(
                    f"Net debit **{debit_f:g}** $ je nižší než u **{worse}** z **{len(pool)}** variantov (nižší debit = lepší)."
                )
        parts.append(
            "Ide o kompromis: nečistí len jednu metriku, ale kombinuje lacnejší vstup, skew, theta a rozumnú vzdialenosť delty od nuly."
        )
    else:
        parts.append(f"Vybraný režim: **{sl}**.")

    parts.append(
        "Po odoslaní do Spread Buildera skontroluj striky (kalendár = rovnaký K, diagonál = Leg1/Leg2 K), expirácie a ceny."
    )
    return " ".join(parts)
