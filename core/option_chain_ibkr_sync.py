"""
Synchronizácia úzkeho opčného reťazca z IBKR do ``option_chain_db`` (SQLite).

Jeden ticker, jedna strana (Call alebo Put), explicitné expirácie, N strike-ov okolo ATM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, Sequence

import pandas as pd

from core import option_chain_db as odb
from core import ibkr

Right = Literal["C", "P"]


def _norm_right(right: str) -> Right:
    u = (right or "").strip().upper()
    if u in ("C", "CALL"):
        return "C"
    if u in ("P", "PUT"):
        return "P"
    raise ValueError(f"Neplatná strana opcie: {right!r} (očakávam call/c alebo put/p).")


def option_type_for_right(r: Right) -> str:
    return "Call" if r == "C" else "Put"


def normalize_user_expiry(s: str) -> str | None:
    """Vráti ``YYYY-MM-DD`` alebo ``None``."""
    t = (s or "").strip()
    if not t:
        return None
    t = t.replace("/", "-")
    if len(t) == 8 and t.isdigit():
        try:
            return datetime.strptime(t, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None
    return None


def expiry_any_to_yyyy_mm_dd(e: Any) -> str | None:
    """IB často vracia ``YYYYMMDD`` alebo ``YYYY-MM-DD``."""
    s = str(e or "").strip().replace("-", "")
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date().isoformat()
        except ValueError:
            return None
    if len(str(e or "")) >= 10 and str(e)[4] == "-" and str(e)[7] == "-":
        return normalize_user_expiry(str(e)[:10])
    return None


def available_expiries_from_secdef(chains: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for ch in chains or []:
        for ex in ch.get("expirations") or []:
            ymd = expiry_any_to_yyyy_mm_dd(ex)
            if ymd:
                out.add(ymd)
    return out


def strikes_from_secdef(chains: list[dict[str, Any]]) -> list[float]:
    merged = _merged_chain_row(chains)
    if not merged:
        return []
    raw = merged.get("strikes") or []
    out: list[float] = []
    for x in raw:
        try:
            out.append(float(x))
        except (TypeError, ValueError):
            continue
    return sorted(set(out))


def _merged_chain_row(chains: list[dict[str, Any]]) -> dict[str, Any] | None:
    for c in chains or []:
        if c.get("exchange") == "MERGED":
            return c
    return chains[0] if chains else None


def partition_expiries(
    requested_yyyy_mm_dd: Sequence[str],
    available_yyyy_mm_dd: set[str],
) -> tuple[list[str], list[str]]:
    """
    Zachová poradie požadovaných expirácií.
    Vráti ``(platné, chýbajúce)`` — obe zoznamy v ``YYYY-MM-DD``.
    """
    valid: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in requested_yyyy_mm_dd:
        ymd = normalize_user_expiry(raw) if raw else None
        if not ymd:
            missing.append(str(raw).strip() or "?")
            continue
        if ymd in seen:
            continue
        seen.add(ymd)
        if ymd in available_yyyy_mm_dd:
            valid.append(ymd)
        else:
            missing.append(ymd)
    return valid, missing


def dte_calendar_days(as_of: date, expiry_yyyy_mm_dd: str) -> int:
    exp = date.fromisoformat(expiry_yyyy_mm_dd)
    return max(0, (exp - as_of).days)


def pick_strikes_near_spot(strikes: Sequence[float], spot: float, n: int) -> list[float]:
    """
    Vyberie ``n`` strike-ov s najmenšou vzdialenosťou ``abs(K - spot)``;
    pri remíze viac strike-ov na rovnakej vzdialenosti berie všetky s touto vzdialenosťou,
    kým nedosiahne ``n`` (môže vrátiť viac ako ``n`` len ak je remíza na hranici — vtedy orezá).
    """
    if n < 1:
        n = 1
    uniq = sorted({float(x) for x in strikes if x == x})
    if not uniq:
        return []
    if len(uniq) <= n:
        return uniq
    scored = sorted(uniq, key=lambda k: (abs(k - spot), k))
    # berieme prvých n podľa (abs diff, strike)
    return sorted(scored[:n])


def parse_expiry_text(text: str) -> list[str]:
    """Rozparsuje expirácie z viacriadkového textu alebo čiarkami oddeleného zoznamu."""
    t = (text or "").strip()
    if not t:
        return []
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if not lines:
        lines = [t]
    out: list[str] = []
    for ln in lines:
        for part in ln.split(","):
            p = part.strip()
            if p:
                out.append(p)
    return out


def validate_expiries_against_secdef(
    ticker: str,
    requested: Sequence[str],
) -> tuple[list[str], list[str], str | None]:
    """
    Vráti ``(valid, missing, error)``.
    ``error`` nie je ``None`` ak secdef zlyhal (potom sú ``valid`` a ``missing`` prázdne/informatívne).
    """
    sym = (ticker or "").strip().upper()
    ch = ibkr.fetch_secdef_option_params(sym)
    if ch.get("error"):
        return [], [], str(ch["error"])
    avail = available_expiries_from_secdef(ch.get("chains") or [])
    valid, missing = partition_expiries(requested, avail)
    return valid, missing, None


def _iv_as_fraction(iv: float | None) -> float | None:
    if iv is None:
        return None
    try:
        x = float(iv)
    except (TypeError, ValueError):
        return None
    if x != x or x <= 0:
        return None
    return x / 100.0 if x > 1.0 else x


def metrics_to_import_row(
    metrics: dict[str, Any],
    *,
    option_type: str,
    dte: int,
    risk_free: float = 0.05,
) -> dict[str, Any]:
    """Jeden riadok v tvare zlučovacieho DataFrame pre ``import_merged_dataframe``."""
    from core.probability import calc_greeks, calc_iv_from_price

    strike = float(metrics.get("strike") or 0.0)
    right = str(metrics.get("right") or "C").upper()[:1]
    bid = metrics.get("bid")
    ask = metrics.get("ask")
    last = metrics.get("last")
    mid = metrics.get("mid")
    spot = metrics.get("und_price")
    iv_frac = _iv_as_fraction(metrics.get("iv"))

    delta = metrics.get("delta")
    gamma = metrics.get("gamma")
    theta = metrics.get("theta")
    vega = metrics.get("vega")

    if spot and mid and dte > 0 and (iv_frac is None or iv_frac <= 0):
        iv_frac = calc_iv_from_price(float(mid), float(spot), strike, int(dte), right, r=risk_free)

    if spot and iv_frac and iv_frac > 0 and dte > 0:
        g = calc_greeks(float(spot), strike, int(dte), float(iv_frac), right, r=risk_free)
        if delta is None:
            delta = g.get("delta")
        if gamma is None:
            gamma = g.get("gamma")
        if theta is None:
            theta = g.get("theta")
        if vega is None:
            vega = g.get("vega")

    return {
        "strike": strike,
        "option_type": option_type,
        "bid": bid,
        "mid": mid,
        "ask": ask,
        "last_price": last,
        "moneyness_pct": None,
        "iv": iv_frac,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "rho": None,
        "theor": None,
        "volume": None,
        "open_interest": metrics.get("open_interest"),
        "vol_oi_ratio": None,
        "itm_prob": None,
    }


@dataclass
class IbkrChainSyncResult:
    ok: bool
    ticker: str
    rows_written: int
    expiries_processed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def sync_chain_snapshot(
    ticker: str,
    *,
    right: str,
    expiries_yyyy_mm_dd: Sequence[str],
    strike_count: int,
    as_of_yyyy_mm_dd: str | None = None,
    pause_s: float = 0.2,
    risk_free: float = 0.05,
) -> IbkrChainSyncResult:
    """
    Načíta úzky reťazec z IBKR a zapíše do ``data/option_chains/<TICKER>.db``.

    ``expiries_yyyy_mm_dd`` musia byť už overené voči secdef (voliteľne po potvrdení používateľa).
    """
    import time

    sym = (ticker or "").strip().upper()
    rr = _norm_right(right)
    otype = option_type_for_right(rr)
    errs: list[str] = []
    warns: list[str] = []
    expiries = [normalize_user_expiry(e) for e in expiries_yyyy_mm_dd]
    expiries = [e for e in expiries if e]
    if not expiries:
        return IbkrChainSyncResult(False, sym, 0, errors=["Žiadne platné expirácie na spracovanie."])

    as_of = (
        date.fromisoformat(as_of_yyyy_mm_dd)
        if as_of_yyyy_mm_dd
        else date.today()
    )
    as_of_s = as_of.isoformat()
    source_tag = f"IBKR:{datetime.now().replace(microsecond=0).isoformat()}"

    ch = ibkr.fetch_secdef_option_params(sym)
    if ch.get("error"):
        return IbkrChainSyncResult(False, sym, 0, errors=[str(ch["error"])])

    all_strikes = strikes_from_secdef(ch.get("chains") or [])
    if not all_strikes:
        return IbkrChainSyncResult(False, sym, 0, errors=["SecDef nevrátil žiadne strikey."])

    und = ibkr.fetch_underlying(sym)
    spot = und.get("price")
    if not spot or (isinstance(spot, float) and (spot != spot or spot <= 0)):
        return IbkrChainSyncResult(
            False,
            sym,
            0,
            errors=[f"Spot nedostupný: {und.get('error') or spot}"],
        )
    spot_f = float(spot)

    strikes_pick = pick_strikes_near_spot(all_strikes, spot_f, int(strike_count))
    if not strikes_pick:
        return IbkrChainSyncResult(False, sym, 0, errors=["Po výbere strike-ov nezostal žiadny strike."])

    total_rows = 0
    processed: list[str] = []

    for exp in expiries:
        rows_out: list[dict[str, Any]] = []
        dte = dte_calendar_days(as_of, exp)
        for k in strikes_pick:
            time.sleep(max(0.0, float(pause_s)))
            m = ibkr.fetch_option_scan_metrics(sym, exp, float(k), rr)
            if m.get("error") and not (m.get("bid") or m.get("ask") or m.get("last")):
                errs.append(f"{exp} K{k:g}{rr}: {m.get('error')}")
                continue
            if m.get("error"):
                warns.append(f"{exp} K{k:g}{rr}: {m.get('error')} (čiastočné dáta)")
            if m.get("und_price") in (None, 0) and spot_f:
                m = {**m, "und_price": spot_f}
            rows_out.append(metrics_to_import_row(m, option_type=otype, dte=max(1, dte), risk_free=risk_free))

        if not rows_out:
            errs.append(f"{exp}: žiadny riadok s cenou — preskočené.")
            continue

        merged = pd.DataFrame(rows_out)
        conn = odb.get_connection(sym)
        try:
            n = odb.import_merged_dataframe(
                conn,
                expiry=exp,
                as_of_date=as_of_s,
                merged=merged,
                source_options_csv=source_tag,
                source_greeks_csv=source_tag,
            )
            total_rows += n
            processed.append(exp)
        except Exception as exc:
            errs.append(f"{exp}: import do DB zlyhal: {type(exc).__name__}: {exc}")
        finally:
            conn.close()

    if total_rows <= 0 and not processed:
        return IbkrChainSyncResult(
            False,
            sym,
            0,
            errors=errs or ["Nič sa neimportovalo."],
            warnings=warns,
        )
    return IbkrChainSyncResult(
        total_rows > 0,
        sym,
        total_rows,
        expiries_processed=processed,
        errors=errs,
        warnings=warns,
    )
