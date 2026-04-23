"""
AI Agent pre analýzu opčných pozícií pomocou Claude (Anthropic).
Volá sa len na požiadanie – nie automaticky.
"""
import os
from datetime import date, datetime
from typing import Optional, Callable


MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 900
MAX_TOKENS_PORTFOLIO = 2400

AVAILABLE_MODELS: dict[str, dict] = {
    "claude-haiku-4-5":   {"label": "⚡ Haiku 4.5  – rýchly, lacný",         "max_tokens": 1200},
    "claude-sonnet-4-6":  {"label": "🎯 Sonnet 4.6 – vyvážený (odporúčaný)", "max_tokens": 2400},
    "claude-opus-4-5":    {"label": "🧠 Opus 4.5   – najpresnejší, pomalší", "max_tokens": 3200},
}

# Beta hodnoty voči SPY (aproximácia, 2024-2025)
TICKER_BETA = {
    "AMZN": 1.20, "AAPL": 1.10, "GOOGL": 1.20, "MSFT": 1.10,
    "TSLA": 2.00, "NVDA": 1.80, "META": 1.30,
}

# Aproximácia korelácií medzi tickermi (symetrická matica)
TICKER_CORRELATIONS = {
    ("AMZN", "AAPL"):  0.65, ("AMZN", "GOOGL"): 0.72, ("AMZN", "MSFT"):  0.68,
    ("AMZN", "TSLA"):  0.45, ("AMZN", "NVDA"):   0.60, ("AMZN", "META"):  0.70,
    ("AAPL", "GOOGL"):  0.68, ("AAPL", "MSFT"):  0.72, ("AAPL", "TSLA"):  0.42,
    ("AAPL", "NVDA"):   0.58, ("AAPL", "META"):  0.65,
    ("GOOGL", "MSFT"):  0.70, ("GOOGL", "TSLA"): 0.44, ("GOOGL", "NVDA"): 0.62,
    ("GOOGL", "META"):  0.75,
    ("MSFT", "TSLA"):   0.40, ("MSFT", "NVDA"):  0.65, ("MSFT", "META"):  0.68,
    ("TSLA", "NVDA"):   0.50, ("TSLA", "META"):  0.45,
    ("NVDA", "META"):   0.60,
}

WATCHED_TICKERS = ["AMZN", "AAPL", "GOOGL", "MSFT", "TSLA", "NVDA", "META"]


def _load_client():
    """Lazy inicializácia Anthropic klienta s načítaním .env."""
    try:
        import anthropic
        from dotenv import load_dotenv
    except ImportError as e:
        raise ImportError(
            "Chýbajú balíčky. Spusti: pip install anthropic python-dotenv"
        ) from e

    load_dotenv()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if not api_key or api_key == "tu_vloz_svuj_novy_api_kluc":
        raise ValueError(
            "ANTHROPIC_API_KEY nie je nastavený. "
            "Otvor súbor .env a vlož svoj Anthropic API kľúč."
        )

    return anthropic.Anthropic(api_key=api_key)


def _calc_dte(expiry_str: Optional[str]) -> Optional[int]:
    if not expiry_str:
        return None
    try:
        exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        return (exp - date.today()).days
    except ValueError:
        return None


def _format_leg(trade: dict, compute_pnl: Callable = None) -> str:
    dte = _calc_dte(trade.get("expiry"))
    dte_str = f"{dte} dní do exp." if dte is not None else "DTE neznámy"
    iv = trade.get("iv_at_entry")
    th_e = trade.get("theta_at_entry")
    leg_type = trade.get("leg_type", "")
    role = "PREDANÁ (prémium inkasované, chceme aby stratila hodnotu)" if leg_type == "Short" else "KÚPENÁ (prémium zaplatené, chceme aby získala hodnotu)"
    
    greeks_text = ""
    if "_greeks" in trade and trade["_greeks"]:
        g = trade["_greeks"]
        if g.get("theta") is not None and g.get("gamma") is not None:
            greeks_text = f" | Aktuálna Theta: {g['theta']:+.4f} | Aktuálna Gamma: {g['gamma']:+.4f}"

    return (
        f"  - {leg_type} {trade.get('option_type','')} "
        f"strike {trade.get('strike',0):.0f} USD | "
        f"expiry {trade.get('expiry','')} ({dte_str}) | "
        f"{trade.get('contracts',1)} kontrakt(y) | "
        f"entry {trade.get('entry_price',0):.2f} USD | "
        f"rola: {role}"
        f"{greeks_text} | "
        f"IV pri vstupe: {iv if iv else 'N/A'} | "
        f"Theta pri vstupe ($/deň): {f'{th_e:+.3f}' if th_e is not None else 'N/A'}"
    )


def build_prompt(group: dict, trades: list[dict], compute_pnl: Callable = None,
                 question: str = "", notes: list[dict] = None, events: list[dict] = None, orders: list[dict] = None) -> str:
    """Zostaví prompt pre Claude – len otvorené nohy, s voliteľnou otázkou a kontextom."""
    open_legs = [t for t in trades if t.get("status") == "Open"]

    if not open_legs:
        raise ValueError("Skupina nemá žiadne otvorené nohy na analýzu.")

    open_text = "\n".join(_format_leg(t, compute_pnl) for t in open_legs)
    today_str = date.today().strftime("%d.%m.%Y")
    
    net_greeks_text = ""
    if "net_theta" in group and "net_gamma" in group:
        net_greeks_text = (
            f"\n## Celkové metriky skupiny:\n"
            f"- Net Theta: {group['net_theta']:+.2f} USD / deň\n"
            f"- Net Gamma: {group['net_gamma']:+.4f}\n"
        )

    # Poznámky / kontext obchodníka
    notes_text = ""
    if notes:
        entries = []
        for n in notes[:5]:  # max 5 posledných poznámok
            ts = (n.get("created_at") or "")[:10]
            entries.append(f"  [{ts}] {n.get('title','')}: {(n.get('content') or '')[:300]}")
        if entries:
            notes_text = "\n## Kontext a poznámky obchodníka:\n" + "\n".join(entries) + "\n"
            
    # Alerty a udalosti
    events_text = ""
    if events:
        entries = []
        for e in sorted(events, key=lambda x: x.get("date", ""))[:10]:
            entries.append(f"  [{e.get('date', '')}] {e.get('type', '').upper()}: {e.get('title', '')} - {e.get('description') or ''}")
        if entries:
            events_text = "\n## Alerty a Udalosti z kalendára:\n" + "\n".join(entries) + "\n"

    custom_section = ""
    if question:
        custom_section = f"\n## Špeciálna otázka od obchodníka:\n{question}\n"
        output_format = """## Odpoveď na otázku
(konkrétna, číselná odpoveď na špeciálnu otázku)

## Stav pozície
(1–2 vety)

## Odporúčanie
**Akcia: HOLD / ROLL / CLOSE / ADJUST**
(2–3 vety prečo)

## Sledovať
- (čo monitorovať)"""
    else:
        output_format = """## Stav pozície
(1–2 vety – aká je štruktúra, čo vidíš)

## Kľúčové riziká
- (riziko 1)
- (riziko 2)
- (riziko 3, max)

## Odporúčanie
**Akcia: HOLD / ROLL / CLOSE / ADJUST**
(2–3 vety prečo)

## Sledovať
- (čo monitorovať)"""

    # Otvorené objednávky v TWS
    orders_text = ""
    if orders:
        entries = []
        for o in orders:
            sec  = o.get("sec_type", "")
            price_info = []
            if o.get("limit_price"):
                price_info.append(f"Limit: {o['limit_price']} USD")
            if o.get("aux_price"):
                price_info.append(f"Stop: {o['aux_price']} USD")
            p_str = " | ".join(price_info) if price_info else "MKT"

            if sec in ("OPT", "FOP"):
                desc = f"Opcia {o.get('option_type')} Strike {o.get('strike',0):.0f} USD exp {o.get('expiry')}"
            elif sec == "BAG":
                legs_descr = o.get("legs_descr") or ""
                if not legs_descr:
                    legs = o.get("legs") or []
                    parts = []
                    for lg in legs:
                        sign = "+" if lg.get("action") == "BUY" else "-"
                        parts.append(f"{sign}{lg.get('ratio',1)} conId={lg.get('con_id')}")
                    legs_descr = ", ".join(parts) or "neznáme nohy"
                desc = f"Combo/Spread nohy: {legs_descr}"
            elif sec == "STK":
                desc = "Akcia"
            elif sec == "FUT":
                desc = "Futures"
            else:
                desc = sec or "?"

            # Podmienky objednávky
            cond_parts = []
            for cond in (o.get("conditions") or []):
                ctype = cond.get("type", "?")
                conj  = "A" if cond.get("conjunction", "a") == "a" else "ALEBO"
                if ctype == "PriceCondition":
                    direction = ">" if cond.get("isMore") else "<"
                    cond_parts.append(
                        f"Cena {direction} {cond.get('price')} USD"
                        + (f" (conId={cond.get('conId')})" if cond.get("conId") else "")
                    )
                elif ctype == "TimeCondition":
                    direction = "po" if cond.get("isMore") else "pred"
                    cond_parts.append(f"Čas {direction} {cond.get('time')}")
                elif ctype == "MarginCondition":
                    direction = ">" if cond.get("isMore") else "<"
                    cond_parts.append(f"Margin cushion {direction} {cond.get('percent')}%")
                elif ctype == "PercentChangeCondition":
                    direction = "+" if cond.get("isMore") else "-"
                    cond_parts.append(f"Zmena {direction}{cond.get('changePercent')}%")
                elif ctype == "ExecutionCondition":
                    cond_parts.append(f"Po vyplnení {cond.get('symbol')} {cond.get('secType')}")
                elif ctype == "VolumeCondition":
                    direction = ">" if cond.get("isMore") else "<"
                    cond_parts.append(f"Objem {direction} {cond.get('volume')}")
                else:
                    cond_parts.append(ctype)
                if len(cond_parts) > 1:
                    cond_parts[-1] = f"{conj} {cond_parts[-1]}"

            cond_str = ""
            if cond_parts:
                cond_str = f" ⟦Podmienka: {'; '.join(cond_parts)}⟧"

            entries.append(
                f"  - {o.get('action')} {o.get('total_qty')}x {desc}"
                f" | Typ: {o.get('order_type')} {p_str}"
                f" | Stav: {o.get('status')}{cond_str}"
            )

        if entries:
            orders_text = "\n## Otvorené objednávky v TWS (čakajúce na vyplnenie):\n" + "\n".join(entries) + "\n"

    return f"""Si skúsený obchodník s opciami. Analyzuj nasledujúcu otvorenú pozíciu.

DÔLEŽITÉ PRAVIDLÁ FORMÁTOVANIA:
- Píš v slovenčine
- Ceny píš ako napr. "190 USD", NIKDY nepoužívaj LaTeX ani matematické symboly
- Buď stručný a konkrétny – max 250 slov celkovo
- Vždy ber do úvahy DÔVOD VSTUPU a KONTEXT z poznámok obchodníka ak sú k dispozícii

## Pozícia: {group.get('name', '?')}
- Ticker: {group.get('ticker', '?')}
- Stratégia: {group.get('strategy', '?')}
- Dôvod vstupu / tézis: {group.get('description') or '(nevyplnené – obchodník by mal doplniť)'}
- Dátum analýzy: {today_str}

## Otvorené nohy ({len(open_legs)}):
{open_text}
{net_greeks_text}
{orders_text}
{notes_text}{events_text}{custom_section}
---
Odpovedaj PRESNE v tomto formáte:

{output_format}"""


def get_correlation(t1: str, t2: str) -> float:
    """Vráti koreláciu medzi dvoma tickermi (0-1)."""
    if t1 == t2:
        return 1.0
    key = (min(t1, t2), max(t1, t2))
    return TICKER_CORRELATIONS.get(key, 0.5)


def build_portfolio_prompt(
    portfolio_data: dict,
    question: str = "",
) -> str:
    """
    Zostaví prompt pre portfoliovú analýzu.
    portfolio_data obsahuje:
      - groups: list skupín s Greeks a metadátami
      - total_theta, total_delta_bw, total_vega
      - alerts: list upozornení
      - tickers_without_position: list tickerov bez pozície
      - spot_prices: dict {ticker: price}
      - iv_data: dict {ticker: iv_value}
      - iv_ranks: dict {ticker: rank} (manuálne zadané)
      - open_orders: list otvorených objednávok z TWS (vrátane BAG combo nôh a podmienok)
      - ibkr_market_data_notes: str — predplatné / dostupnosť dát (voliteľné)
    """
    today_str    = date.today().strftime("%d.%m.%Y")
    groups       = portfolio_data.get("groups", [])
    spots        = portfolio_data.get("spot_prices", {})
    ivs          = portfolio_data.get("iv_data", {})
    iv_ranks     = portfolio_data.get("iv_ranks", {})
    alerts       = portfolio_data.get("alerts", [])
    no_pos       = portfolio_data.get("tickers_without_position", [])
    acct         = portfolio_data.get("account", {})
    strat        = portfolio_data.get("strategy_params", {})
    open_orders  = portfolio_data.get("open_orders", [])

    _md_raw = (portfolio_data.get("ibkr_market_data_notes") or "").strip()
    _md = _md_raw[:6000] if _md_raw else ""
    market_data_text = ""
    if _md:
        market_data_text = (
            f"\n## Predplatné trhových dát IBKR (od obchodníka)\n{_md}\n"
            "Pri otázkach na live IV, opčné reťazce, Greeks alebo „či mám dostatok dát“ "
            "tomuto textu dôveruj viac než všeobecným predpokladom. "
            "TWS môže zobrazovať IV Rank 13t a 52t — to sú rozdielne okná (percentil IV voči min/max v danom horizonte); "
            "nie je to automaticky to isté číslo ako stĺpec IV Rank v tejto aplikácii (tam môže byť manuálne alebo z vlastnej histórie).\n"
        )

    # Sekcia: otvorené skupiny
    groups_text = ""
    for g in groups:
        legs_text = ""
        for leg in g.get("open_legs", []):
            dte_v = leg.get("dte", "?")
            greeks = leg.get("greeks", {})
            legs_text += (
                f"    • {leg.get('leg_type','')} {leg.get('option_type','')} "
                f"${float(leg.get('strike',0)):.0f} exp {leg.get('expiry','')} "
                f"(DTE {dte_v}) | "
                f"Theta {greeks.get('theta', 0):+.2f} | Delta ${greeks.get('delta', 0):+.0f}\n"
            )
        groups_text += (
            f"\n### {g['name']} ({g.get('ticker','?')})\n"
            f"  Stratégia: {g.get('strategy','?')} | "
            f"Net Theta: ${g.get('net_theta', 0):+.2f}/deň | "
            f"Net Delta: ${g.get('net_delta', 0):+.0f}\n"
            f"  Spot: ${spots.get(g.get('ticker',''), 0):.2f} | "
            f"IV: {ivs.get(g.get('ticker',''), 0)*100:.1f}%"
            + (f" | IV Rank: {iv_ranks[g.get('ticker','')]}%" if g.get('ticker','') in iv_ranks else "") + "\n"
            f"{legs_text}"
        )

    # Sekcia: portfolio metriky
    total_theta = portfolio_data.get("total_theta", 0)
    total_bw_delta = portfolio_data.get("total_delta_bw", 0)
    total_vega  = portfolio_data.get("total_vega", 0)

    metrics_text = (
        f"\n## Portfóliové metriky\n"
        f"- Celková Net Theta: ${total_theta:+.2f}/deň (cieľ ≥ +$10/deň)\n"
        f"- Beta-weighted Delta (SPY): ${total_bw_delta:+.0f}\n"
        f"- Celková Vega: ${total_vega:+.2f}\n"
    )

    # Sekcia: alerty
    alerts_text = ""
    if alerts:
        alerts_text = "\n## Aktívne upozornenia\n" + "\n".join(f"- {a}" for a in alerts) + "\n"

    # Sekcia: tickery bez pozície
    no_pos_text = ""
    if no_pos:
        no_pos_entries = []
        for t in no_pos:
            iv_r = iv_ranks.get(t)
            iv_v = ivs.get(t, 0)
            sp   = spots.get(t, 0)
            iv_r_str = f" | IV Rank: {iv_r}%" if iv_r is not None else ""
            no_pos_entries.append(
                f"  • {t}: Spot ${sp:.2f} | IV {iv_v*100:.1f}%{iv_r_str}"
            )
        no_pos_text = "\n## Tickery bez otvorenej pozície (príležitosti)\n" + "\n".join(no_pos_entries) + "\n"

    custom_section = f"\n## Otázka obchodníka:\n{question}\n" if question else ""

    # Sekcia: margin účtu
    account_text = ""
    if acct:
        nlv = acct.get("net_liquidation", 0)
        avail = acct.get("available_funds", 0)
        bp    = acct.get("buying_power", 0)
        maint = acct.get("maintenance_margin", 0)
        account_text = (
            f"\n## Stav účtu\n"
            f"- Čistá hodnota portfólia (NLV): ${nlv:,.0f}\n"
            f"- Voľný margin (Available Funds): ${avail:,.0f}\n"
            f"- Kúpna sila: ${bp:,.0f}\n"
            f"- Udržiavací margin (použitý): ${maint:,.0f}\n"
        )

    # Sekcia: parametre stratégie obchodníka
    params_text = ""
    if strat:
        params_text = (
            f"\n## Parametre stratégie obchodníka\n"
            f"- Max. debet na nový spread: ${strat.get('max_debet', 1500):,.0f}\n"
            f"- Max. nových pozícií naraz: {strat.get('max_positions', 2)}\n"
            f"- Preferovaný mesiac expirácie SHORT nohy: {strat.get('pref_short_month', 'Najbližší štandardný')}\n"
            f"- Cieľové DTE LONG nohy: {strat.get('pref_long_dte', 90)} dní\n"
            f"- Max. riziko na spread: {strat.get('max_risk_pct', 5)}% NLV"
            + (f" (= ${strat.get('max_risk_pct',5)/100 * acct.get('net_liquidation',0):,.0f})"
               if acct.get('net_liquidation') else "") + "\n"
            "DÔLEŽITÉ: Navrhuj len spready ktoré zmestia do týchto limitov!\n"
        )

    # Sekcia: otvorené objednávky z TWS
    orders_text = ""
    if open_orders:
        entries = []
        for o in open_orders:
            sec = o.get("sec_type", "")
            p_str = f"Limit: {o['limit_price']} USD" if o.get("limit_price") else "MKT"
            if sec in ("OPT", "FOP"):
                desc = f"Opcia {o.get('option_type')} {o.get('strike', 0):.0f} USD exp {o.get('expiry')}"
            elif sec == "BAG":
                desc = f"Combo: {o.get('legs_descr') or 'neznáme nohy'}"
            elif sec == "STK":
                desc = "Akcia"
            else:
                desc = sec or "?"
            cond_parts = []
            for cond in (o.get("conditions") or []):
                ctype = cond.get("type", "")
                if ctype == "PriceCondition":
                    direction = ">" if cond.get("isMore") else "<"
                    cond_parts.append(f"Cena {direction} {cond.get('price')} USD")
                elif ctype == "TimeCondition":
                    cond_parts.append(f"Čas {'po' if cond.get('isMore') else 'pred'} {cond.get('time')}")
                elif ctype == "MarginCondition":
                    cond_parts.append(f"Margin {'>' if cond.get('isMore') else '<'} {cond.get('percent')}%")
            cond_str = f" ⟦{'; '.join(cond_parts)}⟧" if cond_parts else ""
            entries.append(
                f"  - {o.get('ticker')} {o.get('action')} {o.get('total_qty')}x {desc}"
                f" | {p_str} | Stav: {o.get('status')}{cond_str}"
            )
        orders_text = "\n## Otvorené objednávky v TWS:\n" + "\n".join(entries) + "\n"

    return f"""Si skúsený portfóliový manažér pre opčné stratégie (call diagonaly). Vykonaj komplexnú analýzu portfólia vrátane ochranných pozícií.

DÔLEŽITÉ PRAVIDLÁ:
- Píš v slovenčine
- Ceny píš ako "190 USD", NIKDY nepoužívaj LaTeX ani matematické symboly
- Buď konkrétny a číselný – vždy uveď strike, expiry, odhadovanú cenu
- Štýl obchodníka: mesačné call diagonaly, Short ~30 DTE delta 0.25-0.35, Long ~90 DTE delta 0.50-0.65, cieľ Net Theta ≥ +$10/deň
- VŽDY zohľadni voľný margin a limity obchodníka pri návrhoch
- Ber do úvahy otvorené objednávky – neopakuj čo je už zadané do TWS
- Pri ochrane: posudzuj každú skupinu samostatne aj portfólio ako celok

## Dátum analýzy: {today_str}
{market_data_text}
## Otvorené skupiny ({len(groups)}):
{groups_text}
{metrics_text}
{account_text}
{params_text}
{orders_text}
{alerts_text}
{no_pos_text}
{custom_section}
---
Odpovedaj PRESNE v tomto formáte (max 550 slov):

## Celkový stav portfólia
(2-3 vety – súhrn expozície, Theta, Delta, hlavné riziká)

## Analýza ochrany portfólia
Pre každú skupinu vyhodnoť:
- Aktuálna ochrana: (áno/čiastočná/žiadna – čím je chránená)
- Riziko bez ochrany: (konkrétny scenár – čo sa stane ak podklad klesne/vzrastie o X%)
- Odporúčanie: (nič nerob / pridaj hedge / uprav stop)

Celkové portfólio:
- Net Delta expozícia: (či je portfólio príliš directional)
- Ochrana voči poklesu trhu: (či je Beta-weighted delta v norme)

## Návrhy ochranných opatrení
Pre každý návrh uveď PRESNE:
TICKER | Typ hedgu (napr. Put spread, Collar, Stop-loss podmienka) | Strike(y) | Expiry | Odh. cena | Podmienka vstupu (ak relevantná, napr. "ak AMZN klesne pod 180 USD") | Dôvod

## Skupiny vyžadujúce inú akciu (roll, úprava, uzavretie)
- (skupina: konkrétna akcia s číslami)

## Nové spready na zváženie
Pre každý navrhovaný spread uveď PRESNE:
TICKER | Short Call $STRIKE exp DÁTUM (DTE) delta ~0.XX | Long Call $STRIKE exp DÁTUM (DTE) delta ~0.XX | Net debet ~$XXX | Theta ~+$X.XX/deň | Podmienka vstupu (ak relevantná) | Dôvod

## Prioritné akcie (zoradené podľa naliehavosti)
1. ...
2. ...
3. ...
"""


def _resolve_model(model: str | None) -> tuple[str, int]:
    """Vráti (model_id, max_tokens) pre daný model. Fallback na globálny MODEL."""
    m = model or MODEL
    info = AVAILABLE_MODELS.get(m)
    if info:
        return m, info["max_tokens"]
    return m, MAX_TOKENS_PORTFOLIO


def analyze_portfolio(portfolio_data: dict, question: str = "", model: str | None = None) -> str:
    """Spustí AI portfoliovú analýzu."""
    client  = _load_client()
    prompt  = build_portfolio_prompt(portfolio_data, question=question)
    m, max_tok = _resolve_model(model)
    message = client.messages.create(
        model=m,
        max_tokens=max_tok,
        messages=[{"role": "user", "content": prompt}],
    )
    return message.content[0].text


def chat_portfolio(history: list[dict], model: str | None = None) -> str:
    """
    Pokračuje v konverzácii s agentom – zachováva celý kontext.

    history: list of {"role": "user"|"assistant", "content": str}
    Prvý záznam je zvyčajne odpoveď agenta (analýza).
    Claude API vyžaduje aby prvá správa bola od "user" – preto
    prvú assistant správu (analýzu) vložíme ako systémový kontext.
    """
    client = _load_client()
    m, max_tok = _resolve_model(model)

    try:
        from core import database as _db

        _ibkr_ctx = (_db.get_setting(_db.AGENT_IBKR_MARKET_DATA_KEY, "") or "").strip()[:4000]
    except Exception:
        _ibkr_ctx = ""
    _sys_ibkr = (
        f" Predplatné / dostupnosť trhových dát IBKR (od používateľa): {_ibkr_ctx}"
        if _ibkr_ctx
        else ""
    )

    api_messages = []
    for i, msg in enumerate(history):
        role    = msg["role"]
        content = msg["content"]
        if i == 0 and role == "assistant":
            api_messages.append({
                "role": "user",
                "content": (
                    "Práve si dokončil túto portfóliovú analýzu:\n\n"
                    f"{content}\n\n"
                    "Buď pripravený odpovedať na doplňujúce otázky. "
                    "Zachovaj kontext celého portfólia."
                ),
            })
            api_messages.append({
                "role": "assistant",
                "content": "Rozumiem. Som pripravený odpovedať na doplňujúce otázky k portfóliu.",
            })
        else:
            api_messages.append({"role": role, "content": content})

    message = client.messages.create(
        model=m,
        max_tokens=max_tok,
        system=(
            "Si skúsený portfóliový manažér pre opčné stratégie (call diagonaly). "
            "Píš v slovenčine. Buď konkrétny a číselný. "
            "Ceny píš ako '190 USD', nikdy nepoužívaj LaTeX."
            + _sys_ibkr
        ),
        messages=api_messages,
    )
    return message.content[0].text


def chat_spread_builder(history: list[dict], model: str | None = None) -> str:
    """
    Pokračovanie konverzácie o spreadi z Spread Buildera (prvá správa = analýza asistenta).
    history: [{"role": "user"|"assistant", "content": str}, ...]
    """
    client = _load_client()
    m, max_tok = _resolve_model(model)

    api_messages: list[dict] = []
    for i, msg in enumerate(history):
        role = msg.get("role")
        content = msg.get("content") or ""
        if i == 0 and role == "assistant":
            api_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Práve si dokončil túto analýzu spreadu v Spread Builderi:\n\n"
                        f"{content}\n\n"
                        "Buď pripravený odpovedať na doplňujúce otázky k tomuto spreadu, "
                        "Greeks a rizikám. Ak používateľ zmení striky v Builderi, ber do úvahy len "
                        "text konverzácie — nemáš live dáta z UI."
                    ),
                }
            )
            api_messages.append(
                {
                    "role": "assistant",
                    "content": "Rozumiem. Som pripravený na doplňujúce otázky k tomuto spreadu.",
                }
            )
        else:
            api_messages.append({"role": role, "content": content})

    message = client.messages.create(
        model=m,
        max_tokens=max_tok,
        system=(
            "Si skúsený obchodník s opciami. Pokračuješ v konverzácii o jednom spreadi "
            "(Spread Builder). Píš v slovenčine. Buď konkrétny a číselný. "
            "Ceny ako '190 USD', bez LaTeX."
        ),
        messages=api_messages,
    )
    return message.content[0].text


def build_diagonal_compare_analysis_prompt(
    protocol_md: str, *, user_note: str = ""
) -> str:
    """
    Prvá analýza pre sekcii „Konzultácia s agentom“ po **porovnaní 2+** uložených diagonál
    (celý Markdown protokol, nie Jeden spread z Buildera).
    """
    u = (user_note or "").strip()
    extra = f"\n## Doplňujúce pokyny od obchodníka:\n{u}\n" if u else ""
    body = (protocol_md or "").strip() or "*(Protokol bol prázdny.)*"
    return f"""Si skúsený obchodník s opciami (long call/put diagonaly). Dostal si **protokol** z TradeJournal: porovnanie **viacerých** uložených kandidátov (kompozit: bázové skóre + short bid), **nie** jediný rozvinutý spread z Spread Buildera.
{extra}
## Protokol (celý):
{body}

Odpovedaj v slovenčine, bez LaTeX, ceny ako 190 USD. Do 450 slov. Formát:

## Ako čítať poradie a stĺpec Skóre
(že **B** je min–max v sade, nie absolútna 0–100; pri veľmi malej |delta| môžu byť čísla v tabuľke vysoké; či 1. miesto vždy dáva zmysel reálne obchodovať)

## Prakticky medzi týmito kandidátmi
(vrstva likvidity / bid vs. theta, čo ešte overiť u brokera alebo v platforme)

## Hlavné riziká
(krátke, konkrétne)

## Záver
(žiadne investičné poradenstvo; 2–3 vety čo ešte skontrolovať pred prípadným výberom 1. vs. 2. miesta)
"""


def chat_diagonal_compare(history: list[dict], model: str | None = None) -> str:
    """
    Pokračovanie chatu po prvej analýze porovnania diagonál (2+ záznamy, protokol v kontexte).
    """
    client = _load_client()
    m, max_tok = _resolve_model(model)

    api_messages: list[dict] = []
    for i, msg in enumerate(history):
        role = msg.get("role")
        content = msg.get("content") or ""
        if i == 0 and role == "assistant":
            api_messages.append(
                {
                    "role": "user",
                    "content": (
                        "Práve si dokončil túto analýzu z porovnania uložených diagonál (protokol v prvej správe z užívateľského kontextu):\n\n"
                        f"{content}\n\n"
                        "Buď pripravený na doplňujúce otázky k uvedenému porovnaniu, poradiu, likvidite a rizikám. "
                        "Nemáš živé dáta z platformy, len text protokolu a konverzácie."
                    ),
                }
            )
            api_messages.append(
                {
                    "role": "assistant",
                    "content": "Rozumiem. Som pripravený odpovedať na doplňujúce otázky k tomuto porovnaniu diagonál.",
                }
            )
        else:
            api_messages.append({"role": role, "content": content})

    message = client.messages.create(
        model=m,
        max_tokens=max_tok,
        system=(
            "Si skúsený obchodník s opciami. Pokračuješ v konverzácii o **porovnaní viacerých uložených diagonál** (TradeJournal). "
            "Píš v slovenčine. Buď konkrétny. Ceny ako '190 USD', bez LaTeX."
        ),
        messages=api_messages,
    )
    return message.content[0].text


def analyze_group(
    group: dict,
    trades: list[dict],
    compute_pnl: Callable = None,
    question: str = "",
    notes: list[dict] = None,
    events: list[dict] = None,
    orders: list[dict] = None,
    model: str | None = None,
) -> str:
    """
    Spustí AI analýzu skupiny pozícií.

    Args:
        group: slovník skupiny (name, ticker, strategy, description)
        trades: zoznam nôh (trade dict) patriacich do skupiny
        compute_pnl: funkcia compute_pnl z core.database
        model: voliteľný model (None = globálny MODEL)

    Returns:
        Markdown text s analýzou od Claude

    Raises:
        ValueError: ak chýba API kľúč
        ImportError: ak chýbajú balíčky
        Exception: ostatné API chyby
    """
    client = _load_client()
    prompt = build_prompt(group, trades, compute_pnl, question=question, notes=notes, events=events, orders=orders)
    m, _ = _resolve_model(model)

    message = client.messages.create(
        model=m,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text
