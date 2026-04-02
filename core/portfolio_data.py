"""
Vrstva na prípravu portfóliových dát – bez závislosti na Streamlit.

Obsahuje:
  - calc_dte / normalize_expiry  – pomocné utility
  - greek_for_trade              – získa Greeks z IBKR cache alebo Black-Scholes
  - build_group_data             – obohatí skupiny o Greeks, DTE, net metriky
  - build_alerts                 – textové upozornenia (DTE, theta, IV rank)
  - match_greeks                 – nájde Greeks v IBKR cache pre nohu z DB (groups.py)
"""
from __future__ import annotations

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
            mult    = int(leg.get("contracts", 1)) * 100
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
                "contracts":   int(leg.get("contracts", 1)),
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

def match_greeks(trade: dict, ibkr_positions: list) -> dict:
    """
    Nájde Greeks v IBKR portfóliu pre danú nohu z DB.
    Porovnáva ticker, strike, expiry (normalizované), option_type, leg_type.
    Vráti dict s delta/gamma/theta/vega alebo {}.
    """
    if not ibkr_positions:
        return {}
    for p in ibkr_positions:
        if p.get("sec_type") != "OPT":
            continue
        if (p.get("ticker") == trade.get("ticker") and
                float(p.get("strike", 0)) == float(trade.get("strike", 0) or 0) and
                p.get("option_type") == trade.get("option_type") and
                p.get("leg_type") == trade.get("leg_type")):
            if str(p.get("expiry")).replace("-", "") == str(trade.get("expiry")).replace("-", ""):
                return {
                    "delta": p.get("delta"),
                    "gamma": p.get("gamma"),
                    "theta": p.get("theta"),
                    "vega":  p.get("vega"),
                }
    return {}
