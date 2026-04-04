"""
Vrstva na prípravu portfóliových dát – bez závislosti na Streamlit.

Obsahuje:
  - calc_dte / normalize_expiry  – pomocné utility
  - greek_for_trade              – získa Greeks z IBKR cache alebo Black-Scholes
  - build_group_data             – obohatí skupiny o Greeks, DTE, net metriky
  - build_alerts                 – textové upozornenia (DTE, theta, IV rank)
  - match_greeks                 – nájde Greeks v IBKR cache pre nohu z DB (groups.py)
  - compute_simple_apr           – orientačná ročná výnosová miera (%) pre skupinu / portfólio
  - ibkr_aggregates_by_underlying / ibkr_summary_by_journal_group – súčty P/L podľa tickeru / skupiny
  - group_ibkr_positions_for_dashboard – IB pozície podľa group_id z denníka
  - compute_group_apr_on_maint_margin – APR skupiny voči ručnej udržiavacej marži
  - compute_theta_annualized_yield_pct – ročný výnos z Theta: (Θ×365 / (net debet + marža)) × 100
  - compute_portfolio_theta_aptr – rovnaká logika APTR (Θ) agregovaná cez denníkové skupiny
  - compute_spread_model_theta_aptr_pct – APTR pre model spreadu (Tvorba spreadov): Θ×365/(net debet+marža)
  - dashboard_group_margin_widget_key – kľúč widgetu marže skupiny (Streamlit)
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from typing import Optional

from core.greeks import bs_greeks

# ── Prahy (rovnaké ako v portfolio_agent.py) ─────────────────────────────────
SHORT_DTE_ALERT = 14
MIN_THETA_GROUP = 0.0
LOW_IV_RANK     = 25
DEFAULT_IV      = 0.30


# ─────────────────────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────────────────────

def calc_dte(expiry_str: Optional[str]) -> Optional[int]:
    """Vráti počet dní do expirácie z reťazca YYYY-MM-DD. None ak chýba / chyba."""
    if not expiry_str:
        return None
    try:
        return (datetime.strptime(expiry_str, "%Y-%m-%d").date() - date.today()).days
    except ValueError:
        return None


def normalize_expiry(exp: str) -> str:
    """Prevedie YYYYMMDD → YYYY-MM-DD. Ak je už YYYY-MM-DD, vráti bez zmeny."""
    exp = str(exp).strip()
    if len(exp) == 8 and "-" not in exp:
        return f"{exp[:4]}-{exp[4:6]}-{exp[6:]}"
    return exp


def _right(option_type: str) -> str:
    return "C" if str(option_type).upper().startswith("C") else "P"


# ─────────────────────────────────────────────────────────────────────────────
# Greeks – live IBKR alebo Black-Scholes fallback
# ─────────────────────────────────────────────────────────────────────────────

def greek_for_trade(
    trade: dict,
    positions_cache: list,
    manual_spots: dict,
    manual_ivs: dict,
) -> tuple[dict, str]:
    """
    Vráti (greeks_dict, source) kde source je 'live', 'bs' alebo 'none'.

    Priorita:
      1. IBKR cache (positions_cache) – hľadá podľa ticker/strike/expiry/option_type
      2. Black-Scholes – manual_spots[ticker] + manual_ivs[ticker]
      3. prázdny dict, 'none'

    IBKR cache (fetch_positions) ukladá:
      ticker="AMZN", strike=float, expiry="YYYYMMDD" alebo "YYYY-MM-DD",
      option_type="Call"/"Put", delta/theta/gamma/vega na vrchnej úrovni.
    """
    ticker = trade.get("ticker", "")
    strike = float(trade.get("strike", 0))
    expiry = normalize_expiry(trade.get("expiry", ""))
    opt    = trade.get("option_type", "")

    for p in positions_cache:
        if p.get("sec_type") != "OPT":
            continue
        exp_cache = normalize_expiry(p.get("expiry", ""))
        if (p.get("ticker") == ticker and
                abs(float(p.get("strike") or -1) - strike) < 0.01 and
                exp_cache == expiry and
                p.get("option_type", "") == opt):
            g = {
                "delta": p.get("delta"),
                "theta": p.get("theta"),
                "gamma": p.get("gamma"),
                "vega":  p.get("vega"),
                "iv":    p.get("iv"),
            }
            if any(v is not None and v != 0 for v in g.values()):
                return g, "live"

    spot = manual_spots.get(ticker, 0)
    iv   = manual_ivs.get(ticker, DEFAULT_IV)
    dte  = calc_dte(expiry)
    if spot > 0 and strike > 0 and dte and dte > 0:
        T = dte / 365.0
        g = bs_greeks(spot, strike, T, iv, _right(opt))
        if g:
            return g, "bs"

    return {}, "none"


# ─────────────────────────────────────────────────────────────────────────────
# Skupiny – obohatenie o Greeks a net metriky
# ─────────────────────────────────────────────────────────────────────────────

def build_group_data(
    groups: list,
    all_trades: list,
    pos_cache: list,
    manual_spots: dict,
    manual_ivs: dict,
) -> list:
    """
    Pre každú skupinu s aspoň jednou otvorenou nohou vráti obohatenú dict:
      open_legs, net_theta, net_delta, net_gamma, net_vega, data_source.
    """
    enriched = []
    for g in groups:
        ticker = g.get("ticker", "")
        g_name = g.get("name", "")
        legs   = [t for t in all_trades
                  if t.get("group_id") == g_name and t.get("status") == "Open"]
        if not legs:
            continue

        open_legs = []
        net_theta = net_delta = net_gamma = net_vega = 0.0
        any_live  = False
        any_bs    = False

        for leg in legs:
            greeks, src = greek_for_trade(leg, pos_cache, manual_spots, manual_ivs)
            dte_val = calc_dte(leg.get("expiry"))
            mult    = float(leg.get("contracts", 1) or 1) * 100
            sign    = -1 if leg.get("leg_type") == "Short" else 1

            theta = (greeks.get("theta", 0) or 0)
            delta = (greeks.get("delta", 0) or 0)
            gamma = (greeks.get("gamma", 0) or 0)
            vega  = (greeks.get("vega",  0) or 0)

            if src == "live":
                any_live = True
            elif src == "bs":
                any_bs = True

            leg_out = {
                "leg_type":    leg.get("leg_type", ""),
                "option_type": leg.get("option_type", ""),
                "strike":      float(leg.get("strike", 0)),
                "expiry":      leg.get("expiry", ""),
                "dte":         dte_val,
                "contracts":   float(leg.get("contracts", 1) or 1),
                "entry_price": float(leg.get("entry_price", 0)),
                "source":      src,
                "greeks": {
                    "theta": theta * mult * sign,
                    "delta": delta * mult * sign,
                    "gamma": gamma * mult * sign,
                    "vega":  vega  * mult * sign,
                },
            }
            open_legs.append(leg_out)
            net_theta += leg_out["greeks"]["theta"]
            net_delta += leg_out["greeks"]["delta"]
            net_gamma += leg_out["greeks"]["gamma"]
            net_vega  += leg_out["greeks"]["vega"]

        data_source = "live" if any_live else ("bs" if any_bs else "none")
        enriched.append({
            **g,
            "open_legs":   open_legs,
            "net_theta":   net_theta,
            "net_delta":   net_delta,
            "net_gamma":   net_gamma,
            "net_vega":    net_vega,
            "ticker":      ticker,
            "data_source": data_source,
        })
    return enriched


# ─────────────────────────────────────────────────────────────────────────────
# Alerty
# ─────────────────────────────────────────────────────────────────────────────

def build_alerts(group_data: list, iv_ranks: dict) -> list[str]:
    """
    Vráti zoznam textových upozornení:
      - Short noga s DTE < SHORT_DTE_ALERT
      - Net Theta skupiny záporná
      - IV Rank tickera < LOW_IV_RANK
    """
    alerts = []
    for g in group_data:
        name   = g["name"]
        ticker = g.get("ticker", "")
        for leg in g["open_legs"]:
            if (leg["leg_type"] == "Short"
                    and leg["dte"] is not None
                    and leg["dte"] < SHORT_DTE_ALERT):
                alerts.append(
                    f"⏰ **{name}** – Short {leg['option_type']} ${leg['strike']:.0f} "
                    f"exp {leg['expiry']} má len **{leg['dte']} DTE** – čas na roll!"
                )
        if g["net_theta"] < MIN_THETA_GROUP and g["data_source"] != "none":
            alerts.append(
                f"📉 **{name}** – Net Theta ${g['net_theta']:+.2f}/deň je záporná."
            )
        if ticker in iv_ranks and 0 < iv_ranks[ticker] < LOW_IV_RANK:
            alerts.append(
                f"☁️ **{ticker}** – IV Rank {iv_ranks[ticker]}% < {LOW_IV_RANK}% "
                "– nevhodné prostredie pre nový diagonal (radšej doma ako zmoknúť)."
            )
    return alerts


# ─────────────────────────────────────────────────────────────────────────────
# Match Greeks – pre groups.py (páruje nohu z DB s IBKR cache)
# ─────────────────────────────────────────────────────────────────────────────

def dashboard_group_margin_widget_key(group_id: str) -> str:
    """Stabilný kľúč ``st.session_state`` / ``number_input`` pre maržu skupiny na dashboarde."""
    return "pf_grp_maint_" + hashlib.md5(str(group_id).encode("utf-8")).hexdigest()[:16]


def journal_group_id(trade: dict) -> str:
    """Kľúč skupiny ako v Trade Logu."""
    return (trade.get("group_id") or "").strip() or "— (bez skupiny)"


def ibkr_option_position_matches_trade(p: dict, trade: dict) -> bool:
    """Či IBKR OPT riadok zodpovedá otvorenej nohe z denníka."""
    if p.get("sec_type") != "OPT":
        return False
    if str(p.get("ticker", "")).upper() != str(trade.get("ticker", "")).upper():
        return False
    if float(p.get("strike", 0) or 0) != float(trade.get("strike", 0) or 0):
        return False
    if p.get("option_type") != trade.get("option_type"):
        return False
    if p.get("leg_type") != trade.get("leg_type"):
        return False
    e_t = normalize_expiry(str(trade.get("expiry") or "")).replace("-", "")
    e_p = normalize_expiry(str(p.get("expiry") or "")).replace("-", "")
    return e_t == e_p


def match_greeks(trade: dict, ibkr_positions: list) -> dict:
    """
    Nájde Greeks v IBKR portfóliu pre danú nohu z DB.
    Porovnáva ticker, strike, expiry (normalizované), option_type, leg_type.
    Vráti dict s delta/gamma/theta/vega alebo {}.
    """
    if not ibkr_positions:
        return {}
    for p in ibkr_positions:
        if ibkr_option_position_matches_trade(p, trade):
            return {
                "delta": p.get("delta"),
                "gamma": p.get("gamma"),
                "theta": p.get("theta"),
                "vega":  p.get("vega"),
            }
    return {}


def find_ibkr_option_for_trade(trade: dict, ib_positions: list) -> Optional[dict]:
    """Prvá IBKR OPT pozícia zodpovedajúca otvorenej nohe z denníka."""
    if not ib_positions:
        return None
    for p in ib_positions:
        if ibkr_option_position_matches_trade(p, trade):
            return p
    return None


def unrealized_by_journal_ids_for_ib_legs(
    legs_in_group: list[dict],
    ib_positions_for_group: list[dict],
) -> dict[int, float]:
    """
    Mapa ``trade_id`` → IB unrealized P/L pre otvorené nohy, ktoré sa dajú spárovať s danými IB riadkami.
    Použitie: orientačné APR na báze prémie na dashboarde, keď ešte nie je zadaná udržiavacia marža.
    """
    out: dict[int, float] = {}
    for t in legs_in_group:
        if t.get("status") != "Open":
            continue
        tid = t.get("id")
        if tid is None:
            continue
        for p in ib_positions_for_group:
            if ibkr_option_position_matches_trade(p, t):
                out[int(tid)] = float(p.get("unrealized_pnl") or 0)
                break
    return out


def group_ibkr_positions_for_dashboard(
    open_trades: list[dict],
    ib_positions: list[dict],
) -> tuple[list[tuple[str, list[dict]]], list[dict]]:
    """
    Zoradí IB pozície podľa ``group_id`` z otvorených nôh v denníku.
    Každý IB OPT riadok max. jednej nohe; STK bez opčného páru ide do syntetickej skupiny ``— STK · TICKER``.
    Nevyužité riadky (napr. OPT mimo denníka) vráti v ``unmatched``.
    """
    from collections import defaultdict

    used = [False] * len(ib_positions)
    grouped: dict[str, list[dict]] = defaultdict(list)

    for t in open_trades:
        if t.get("status") != "Open":
            continue
        gid = journal_group_id(t)
        for i, p in enumerate(ib_positions):
            if used[i]:
                continue
            if ibkr_option_position_matches_trade(p, t):
                grouped[gid].append(p)
                used[i] = True
                break

    for i, p in enumerate(ib_positions):
        if used[i]:
            continue
        if p.get("sec_type") == "STK":
            sym = str(p.get("ticker") or "?")
            gid = f"— STK · {sym} (IB)"
            grouped[gid].append(p)
            used[i] = True

    unmatched = [p for i, p in enumerate(ib_positions) if not used[i]]

    journal_gids_ordered: list[str] = []
    seen_j: set[str] = set()
    for t in open_trades:
        if t.get("status") != "Open":
            continue
        gid = journal_group_id(t)
        if gid not in seen_j:
            journal_gids_ordered.append(gid)
            seen_j.add(gid)

    ordered: list[tuple[str, list[dict]]] = []
    seen_g: set[str] = set()
    for gid in journal_gids_ordered:
        if gid in grouped:
            ordered.append((gid, grouped[gid]))
            seen_g.add(gid)
    for gid in sorted(grouped.keys()):
        if gid not in seen_g:
            ordered.append((gid, grouped[gid]))
            seen_g.add(gid)
    return ordered, unmatched


def compute_group_apr_on_maint_margin(
    legs_all_in_group: list[dict],
    ib_unrealized_sum: float,
    maint_margin_usd: float,
) -> Optional[dict]:
    """
    APR % skupiny: ``(realizovaný P&L z DB + súčet IB unrealized pre zosúladené nohy) / udržiavacia_marža × (365/dní) × 100``.
    ``legs_all_in_group`` = všetky obchody v skupine (Open + Closed) z denníka.

    Ak v denníku chýba ``entry_date`` ale aspoň jedna noha je Open, za začiatok horizontu sa berie **dnešný dátum**
    (aby šiel APR zobraziť hneď po otvorení).

    Návratový dict obsahuje aj ``basis_kind`` / ``basis_value`` (marža) pre jednotné ukladanie histórie na dashboarde.
    """
    from core import database as _db

    if maint_margin_usd < 1.0:
        return None
    realized = 0.0
    for t in legs_all_in_group:
        if t.get("status") != "Closed":
            continue
        pnl = _db.compute_pnl(t)
        if pnl is not None:
            realized += float(pnl)
    pnl_total = realized + float(ib_unrealized_sum)

    entry_dates = [parse_trade_date(t.get("entry_date")) for t in legs_all_in_group]
    entry_dates = [d for d in entry_dates if d is not None]
    if not entry_dates:
        if any(t.get("status") == "Open" for t in legs_all_in_group):
            start = date.today()
        else:
            return None
    else:
        start = min(entry_dates)
    has_open = any(t.get("status") == "Open" for t in legs_all_in_group)
    if has_open:
        end = date.today()
    else:
        exs = [
            parse_trade_date(t.get("exit_date"))
            for t in legs_all_in_group
            if t.get("status") == "Closed"
        ]
        exs = [d for d in exs if d is not None]
        end = max(exs) if exs else date.today()
    days = max(1, (end - start).days)
    apr_pct = (pnl_total / maint_margin_usd) * (365.0 / float(days)) * 100.0
    return {
        "apr_pct": apr_pct,
        "pnl": pnl_total,
        "days": days,
        "realized": realized,
        "unreal_ib": float(ib_unrealized_sum),
        "basis_kind": "maint",
        "basis_value": float(maint_margin_usd),
    }


def _journal_leg_contract_multiplier(trade: dict) -> float:
    """USD násobiteľ pri vstupnej cene: 100 × kontrakty pre opcie, inak 1× (akcie)."""
    try:
        st = float(trade.get("strike") or 0)
    except (TypeError, ValueError):
        st = 0.0
    if st > 0 and trade.get("option_type"):
        return 100.0
    return 1.0


def net_open_debit_capital_usd(open_legs: list[dict]) -> Optional[float]:
    """
    Kapitál v riziku pri otvorení (net debet): súčet zaplatených vstupných prémií (long)
    mínus prijaté (short) — napr. PMCC: cena long LEAPS − kredit zo shortu.

    Počítajú sa len nohy so stavom Open a vyplnenou ``entry_price``.
    Ak niektorá otvorená noha nemá vstupnú cenu, vráti ``None``.
    """
    net = 0.0
    n_open = 0
    for t in open_legs:
        if t.get("status") != "Open":
            continue
        n_open += 1
        ep = t.get("entry_price")
        if ep is None:
            return None
        c = float(t.get("contracts", 1) or 1)
        mult = _journal_leg_contract_multiplier(t)
        amt = float(ep) * c * mult
        if t.get("leg_type") == "Short":
            net -= amt
        else:
            net += amt
    if n_open == 0:
        return None
    return net


def group_net_theta_usd_per_day(ib_positions: list[dict]) -> tuple[float, bool]:
    """
    Súčet denného Theta v USD pre dané IB riadky (rovnaká znamienková logika ako na dashboarde).

    Vráti ``(súčet, incomplete)`` kde ``incomplete`` je True, ak aspoň jedna OPT noha nemá theta.
    """
    incomplete = False
    tot = 0.0
    for p in ib_positions:
        if p.get("sec_type") != "OPT":
            continue
        q = float(p.get("contracts") or 1)
        th = p.get("theta")
        if th is None:
            incomplete = True
            continue
        sign = -1 if p.get("leg_type") == "Short" else 1
        tot += float(th) * q * 100.0 * sign
    return tot, incomplete


def compute_theta_annualized_yield_pct(
    open_journal_legs: list[dict],
    ib_positions_for_group: list[dict],
    maintenance_margin_usd: float = 0.0,
    theta_override_usd: float = 0.0,
) -> Optional[dict]:
    """
    Ročný výnos (%) podľa časového rozpadu: ``(Θ × 365 / báza) × 100``.

    - **Θ** — aktuálne denné Theta celého spreadu (súčet z IB pre zadané pozície).
    - **Báza (náklad)** — vstupný **net debet** z denníka (long − short prémiá) **+** voliteľná **udržiavacia marža**
      (USD), ak je zadaná (> 0). Obe položky súviažu s tým, čo pozícia „stojí“ (hotovosť + viazanie u brokera).

    Pri **net kredite** (záporný net debet) sa nevracia nič — vzorec je určený pre štruktúry typu net debet (PMCC atď.).
    Ak v skupine sú opcie z IB ale žiadna nemá Theta, vráti ``None``.
    """
    net_debit = net_open_debit_capital_usd(open_journal_legs)
    if net_debit is None or net_debit <= 0:
        return None

    mm = max(0.0, float(maintenance_margin_usd or 0.0))
    capital_basis = net_debit + mm
    if capital_basis <= 0:
        return None

    journal_has_open_opt = any(
        t.get("status") == "Open"
        and t.get("option_type")
        and float(t.get("strike") or 0) > 0
        for t in open_journal_legs
    )
    ib_has_opt = any(p.get("sec_type") == "OPT" for p in ib_positions_for_group)
    if journal_has_open_opt and not ib_has_opt:
        return None

    has_opt = ib_has_opt
    if has_opt:
        any_th = any(
            p.get("sec_type") == "OPT" and p.get("theta") is not None
            for p in ib_positions_for_group
        )
        if not any_th:
            return None

    theta_day_ib, inc = group_net_theta_usd_per_day(ib_positions_for_group)
    # Ručný override z TWS má prednosť pred IB API hodnotou
    theta_day = float(theta_override_usd) if theta_override_usd != 0.0 else theta_day_ib
    theta_source = "manual" if theta_override_usd != 0.0 else "ib"
    yld = (theta_day * 365.0 / capital_basis) * 100.0
    return {
        "yield_pct": yld,
        "theta_per_day": theta_day,
        "theta_per_day_ib": theta_day_ib,
        "theta_source": theta_source,
        "net_debit_usd": net_debit,
        "maintenance_margin_usd": mm,
        "capital_basis_usd": capital_basis,
        "incomplete_theta": inc,
    }


def compute_portfolio_theta_aptr(
    ordered_groups: list[tuple[str, list[dict]]],
    all_trades: list[dict],
    group_id_to_margin_usd: dict[str, float],
    unmatched_ib_positions: list[dict],
) -> Optional[dict]:
    """
    Portfóliový **APTR z Theta** (zosúladené skupiny denník ↔ IB).

    ``Σ Θ (USD/deň)`` po skupinách, ktoré majú aspoň jednu opciu v IB a kladný vstupný net debet v denníku,
    deleno ``Σ (net debet + marža)`` za tie isté skupiny. Rovnaká logika ako pri jednej skupine.

    Riadky IB **bez** páru v denníku prispievajú do ``unmatched_theta_per_day`` (informačne), nie do čitateľa.
    """
    total_theta = 0.0
    total_basis = 0.0
    incomplete = False
    groups_used = 0

    for gid, plist in ordered_groups:
        if not any(p.get("sec_type") == "OPT" for p in plist):
            continue
        open_legs = [
            t
            for t in all_trades
            if journal_group_id(t) == gid and t.get("status") == "Open"
        ]
        nd = net_open_debit_capital_usd(open_legs)
        if nd is None or nd <= 0:
            continue
        mm = max(0.0, float(group_id_to_margin_usd.get(gid, 0.0) or 0.0))
        basis = nd + mm
        th, inc = group_net_theta_usd_per_day(plist)
        if inc:
            incomplete = True
        total_theta += th
        total_basis += basis
        groups_used += 1

    unmatched_theta = 0.0
    un_opt = 0
    for p in unmatched_ib_positions:
        if p.get("sec_type") != "OPT":
            continue
        un_opt += 1
        th_u, inc_u = group_net_theta_usd_per_day([p])
        unmatched_theta += th_u
        if inc_u:
            incomplete = True

    if total_basis <= 0 or groups_used == 0:
        return None

    yld = (total_theta * 365.0 / total_basis) * 100.0
    return {
        "yield_pct": yld,
        "theta_per_day": total_theta,
        "capital_basis_usd": total_basis,
        "groups_in_basis": groups_used,
        "incomplete_theta": incomplete,
        "unmatched_opt_count": un_opt,
        "unmatched_theta_per_day": unmatched_theta,
    }


def compute_spread_model_theta_aptr_pct(
    net_debit_usd: float,
    net_theta_per_day_usd: float,
    maintenance_margin_usd: float = 0.0,
) -> Optional[dict]:
    """
    Ročný výnos z Θ pre **modelovaný** spread (BS Theta, bez IB).

    **Net debet** = zaplatené prémie − prijaté (kladné číslo pri klasickom net debet spreade).
    **Báza** = net debet + modelová udržiavacia marža (≥ 0). Ak je báza ≤ 0, vráti ``None``.
    """
    mm = max(0.0, float(maintenance_margin_usd or 0.0))
    nd = float(net_debit_usd)
    basis = nd + mm
    if basis <= 0:
        return None
    th = float(net_theta_per_day_usd)
    yld = (th * 365.0 / basis) * 100.0
    return {
        "yield_pct": yld,
        "capital_basis_usd": basis,
        "net_debit_usd": nd,
        "maintenance_margin_usd": mm,
    }


def ibkr_aggregates_by_underlying(ib_positions: list[dict]) -> list[dict]:
    """Súčty Unrealized P/L a trhovej hodnoty podľa symbolu (všetky riadky z fetch_positions)."""
    from collections import defaultdict

    agg: dict = defaultdict(lambda: {"rows": 0, "unreal": 0.0, "mkt": 0.0, "abs_mkt": 0.0})
    for p in ib_positions:
        sym = str(p.get("ticker") or "")
        mv = float(p.get("market_value") or 0)
        a = agg[sym]
        a["rows"] += 1
        a["unreal"] += float(p.get("unrealized_pnl") or 0)
        a["mkt"] += mv
        a["abs_mkt"] += abs(mv)
    out = []
    for sym in sorted(agg.keys()):
        v = agg[sym]
        out.append({
            "Podklad": sym,
            "Riadkov": v["rows"],
            "Unreal. P/L $": round(v["unreal"], 2),
            "Trh. hodnota $": round(v["mkt"], 2),
            "Σ abs trh.hodn. $": round(v["abs_mkt"], 2),
        })
    return out


def ibkr_summary_by_journal_group(open_trades: list[dict], ib_positions: list[dict]) -> list[dict]:
    """
    Otvorené nohy z DB zoskupené podľa ``group_id``; pre každú sa sčíta IBKR unrealized a market value,
    ak noha sedí s OPT pozíciou z TWS.
    """
    from collections import defaultdict

    groups: dict = defaultdict(
        lambda: {"legs_db": 0, "matched": 0, "unreal": 0.0, "mkt": 0.0, "abs_mkt": 0.0}
    )
    for t in open_trades:
        if t.get("status") != "Open":
            continue
        gid = journal_group_id(t)
        g = groups[gid]
        g["legs_db"] += 1
        p = find_ibkr_option_for_trade(t, ib_positions)
        if p:
            g["matched"] += 1
            mv = float(p.get("market_value") or 0)
            g["unreal"] += float(p.get("unrealized_pnl") or 0)
            g["mkt"] += mv
            g["abs_mkt"] += abs(mv)
    rows = []
    for gid in sorted(groups.keys()):
        v = groups[gid]
        rows.append({
            "Skupina": gid,
            "Nôh (DB)": v["legs_db"],
            "Zhoda IB": f"{v['matched']}/{v['legs_db']}",
            "Unreal. P/L $": round(v["unreal"], 2),
            "Trh. hodnota $": round(v["mkt"], 2),
            "Σ abs trh.hodn. $": round(v["abs_mkt"], 2),
        })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# APR (jednoduchá ročná miera) – portfólio / skupina
# ─────────────────────────────────────────────────────────────────────────────

def parse_trade_date(s: Optional[str]) -> Optional[date]:
    """YYYY-MM-DD z DB; toleruje aj kratší prefix."""
    if not s:
        return None
    s = str(s).strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def premium_notional_usd(trade: dict) -> float:
    """Absolútna vstupná prémia v USD: |entry_price| × kontrakty × 100."""
    c = float(trade.get("contracts", 1) or 1)
    ep = float(trade.get("entry_price", 0) or 0)
    return abs(ep) * c * 100.0


def compute_simple_apr(
    legs: list[dict],
    unrealized_by_id: dict[int, float],
) -> Optional[dict]:
    """
    Jednoduchá **ročná výnosová miera v % (APR)** pre zadané nohy (skupina alebo celé portfólio).

    **Vzorec:** ``(P&L / základ) × (365 / dní) × 100``, kde
    - **P&L** = súčet realizovaného (z uzavretých nôh) + nerealizovaného (mapa podľa ``id`` otvorených).
    - **Základ** = súčet ``|prémia| × kontrakty × 100`` za **všetky** nohy v rozsahu (otvorené aj uzavreté).
    - **Dni** = kalendárne dni od najstaršieho ``entry_date`` do **dneška**, ak existuje aspoň jedna otvorená
      noha; inak od najstaršieho vstupu do **najnovšieho** ``exit_date`` (iba uzavreté nohy).

    Ide o orientačnú metriku: pri rollu, viacerých uzavretiach v jednej skupine alebo bez záznamu max. rizika
    nemusí zodpovedať broker ROC. Pre veľmi krátky horizont (< 7 dní) je annualizácia volatilná.
    """
    if not legs:
        return None

    open_l = [t for t in legs if t.get("status") == "Open"]
    closed_l = [t for t in legs if t.get("status") == "Closed"]

    from core import database as _db

    realized = 0.0
    for t in closed_l:
        p = _db.compute_pnl(t)
        if p is not None:
            realized += float(p)

    unrealized = sum(float(unrealized_by_id.get(int(t["id"]), 0)) for t in open_l)
    pnl = realized + unrealized

    basis = sum(premium_notional_usd(t) for t in legs)
    if basis < 1.0:
        basis = 1.0

    entry_dates = [parse_trade_date(t.get("entry_date")) for t in legs]
    entry_dates = [d for d in entry_dates if d is not None]
    if not entry_dates:
        if open_l:
            start = date.today()
        else:
            return None
    else:
        start = min(entry_dates)

    if open_l:
        end = date.today()
    else:
        exit_dates = [parse_trade_date(t.get("exit_date")) for t in closed_l]
        exit_dates = [d for d in exit_dates if d is not None]
        if not exit_dates:
            return None
        end = max(exit_dates)

    days = max(1, (end - start).days)
    apr_pct = (pnl / basis) * (365.0 / float(days)) * 100.0

    return {
        "apr_pct": apr_pct,
        "pnl": pnl,
        "basis": basis,
        "days": days,
        "short_horizon": days < 7,
        "basis_kind": "premium",
    }
