"""
AI Agent pre analýzu opčných pozícií pomocou Claude (Anthropic).
Volá sa len na požiadanie – nie automaticky.
"""
import os
from datetime import date, datetime
from typing import Optional, Callable


MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 900


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


def _format_leg(trade: dict, compute_pnl: Callable) -> str:
    dte = _calc_dte(trade.get("expiry"))
    dte_str = f"{dte} dní do exp." if dte is not None else "DTE neznámy"
    iv = trade.get("iv_at_entry")
    pop = trade.get("pop_at_entry")
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
        f"PoP pri vstupe: {f'{pop:.0f}%' if pop else 'N/A'}"
    )


def build_prompt(group: dict, trades: list[dict], compute_pnl: Callable,
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
            price_info = []
            if o.get("limit_price"):
                price_info.append(f"Limit: {o['limit_price']} USD")
            if o.get("aux_price"):
                price_info.append(f"Stop: {o['aux_price']} USD")
            p_str = " | ".join(price_info)
            
            desc = ""
            if o.get("sec_type") == "OPT":
                desc = f"Opcia: {o.get('option_type')} {o.get('strike',0):.0f} USD (exp: {o.get('expiry')})"
            else:
                desc = "Akcia"
                
            entries.append(f"  - {o.get('action')} {o.get('total_qty')}x {desc} | Typ: {o.get('order_type')} | {p_str}")
            
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


def analyze_group(
    group: dict,
    trades: list[dict],
    compute_pnl: Callable,
    question: str = "",
    notes: list[dict] = None,
    events: list[dict] = None,
    orders: list[dict] = None,
) -> str:
    """
    Spustí AI analýzu skupiny pozícií.

    Args:
        group: slovník skupiny (name, ticker, strategy, description)
        trades: zoznam nôh (trade dict) patriacich do skupiny
        compute_pnl: funkcia compute_pnl z core.database

    Returns:
        Markdown text s analýzou od Claude

    Raises:
        ValueError: ak chýba API kľúč
        ImportError: ak chýbajú balíčky
        Exception: ostatné API chyby
    """
    client = _load_client()
    prompt = build_prompt(group, trades, compute_pnl, question=question, notes=notes, events=events, orders=orders)

    message = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )

    return message.content[0].text
