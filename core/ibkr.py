"""
IBKR konektor cez ib_insync.
Udržiava singleton IB inštanciu v st.session_state['ib'].

ib_insync/eventkit vyžadujú asyncio event loop pri importe.
Python 3.12 event loop v non-main vlákne automaticky nevytvorí.
Riešenie: lazy import — ib_insync sa importuje až pri prvom volaní
po zaistení event loop pomocou _ensure_event_loop().
"""
from __future__ import annotations

import asyncio
import threading
import time
import math

import streamlit as st
from typing import Optional
from datetime import datetime, date, timedelta


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7496
DEFAULT_CLIENT_ID = 10


# ─── Event loop helper ────────────────────────────────────────────────────────

def _ensure_event_loop():
    """Zaistí, že v aktuálnom vlákne existuje otvorený asyncio event loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def _ib_ready():
    """
    Zaistí event loop + aplikuje nest_asyncio.
    Importuje a vráti (IB, Stock, Option) z ib_insync.
    Volaj toto VŽDY pred akýmkoľvek použitím ib_insync.
    """
    _ensure_event_loop()
    import nest_asyncio
    nest_asyncio.apply()
    from ib_insync import IB, Stock, Option
    return IB, Stock, Option


# ─── Session state helpers ────────────────────────────────────────────────────

def get_ib():
    return st.session_state.get("ib")


def is_connected() -> bool:
    ib = get_ib()
    return ib is not None and ib.isConnected()


# ─── Connect / Disconnect ─────────────────────────────────────────────────────

def connect(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    client_id: int = DEFAULT_CLIENT_ID,
) -> tuple[bool, str]:
    """
    Pripoj sa na IBKR. Ak je clientId obsadený, automaticky skúsi ďalšie ID (až +5).
    Vráti (úspech, správa).
    """
    IB, _, _ = _ib_ready()

    old = get_ib()
    if old:
        try:
            old.disconnect()
        except Exception:
            pass
    st.session_state.pop("ib", None)

    last_err = ""
    for offset in range(6):
        cid = client_id + offset
        try:
            ib = IB()
            ib.connect(host, port, clientId=cid, timeout=10, readonly=False)
            st.session_state["ib"] = ib
            return True, f"Pripojený na {host}:{port}  (clientId={cid})"
        except Exception as e:
            last_err = str(e)
            if "already in use" not in last_err and "326" not in last_err:
                break
    return False, f"Chyba pripojenia: {last_err}"


def disconnect() -> None:
    ib = get_ib()
    if ib and ib.isConnected():
        ib.disconnect()
    st.session_state.pop("ib", None)


# ─── Market data ──────────────────────────────────────────────────────────────

def fetch_underlying(ticker: str, timeout: float = 10.0) -> dict:
    """
    Vráti aktuálnu cenu podkladového aktíva.
    1. Portfólio (okamžité), 2. reqMktData (~10s).
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"price": None, "ticker": ticker, "error": "Nie je pripojenie na IBKR"}

    _, Stock, _ = _ib_ready()

    # 1. Portfólio — okamžité
    try:
        for item in ib.portfolio():
            if item.contract.symbol == ticker and item.contract.secType == "STK":
                p = item.marketPrice
                if p and not math.isnan(p) and p > 0:
                    return {"price": float(p), "ticker": ticker, "error": None, "source": "portfolio"}
    except Exception:
        pass

    # 2. reqTickers v separátnom vlákne (neblokuje Streamlit UI)
    price_result: dict = {}
    done2 = threading.Event()

    def _spot_worker():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            import nest_asyncio
            nest_asyncio.apply(loop)
            from ib_insync import Stock as IBStock
            stock = IBStock(ticker, "SMART", "USD")
            ib.qualifyContracts(stock)
            ib.reqMarketDataType(4)
            tickers = ib.reqTickers(stock)
            if tickers:
                t_obj = tickers[0]
                p = t_obj.marketPrice()
                if p and not math.isnan(p) and p > 0:
                    price_result["price"] = float(p)
                elif t_obj.close and not math.isnan(t_obj.close) and t_obj.close > 0:
                    price_result["price"] = float(t_obj.close)
        except Exception as e:
            price_result["error"] = str(e)
        finally:
            done2.set()

    t2 = threading.Thread(target=_spot_worker, daemon=True)
    t2.start()
    finished2 = done2.wait(timeout=timeout)

    if not finished2:
        return {"price": None, "ticker": ticker, "error": f"Timeout {timeout}s — cena nedostupná"}
    if price_result.get("price"):
        return {"price": price_result["price"], "ticker": ticker, "error": None, "source": "mktdata"}
    return {"price": None, "ticker": ticker,
            "error": price_result.get("error", "Cena nedostupná — zadaj manuálne")}


def fetch_option_data(ticker: str, expiry: str, strike: float, right: str) -> dict:
    """
    Načíta bid/ask cenu opcie z IBKR.
    IV a Greeks vypočíta lokálne cez Black-Scholes (funguje aj bez live dát).
    expiry: 'YYYYMMDD', right: 'C' alebo 'P'
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"error": "Nie je pripojenie na IBKR"}

    # Vypočítaj DTE z expiry stringu
    try:
        from datetime import date as _date
        exp_date = _date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:]))
        dte = max(1, (exp_date - _date.today()).days)
    except Exception:
        dte = 30

    price_result: dict = {}
    done = threading.Event()

    def _worker():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            import nest_asyncio
            nest_asyncio.apply(loop)
            from ib_insync import Option as IBOption

            opt = IBOption(ticker, expiry, strike, right, "SMART", currency="USD")
            qualified = ib.qualifyContracts(opt)
            if not qualified:
                price_result["error"] = f"Kontrakt {ticker} {expiry} ${strike} {right} nenájdený"
                return

            ib.reqMarketDataType(4)
            # Požiadaj o snapshot (snapshot=True = jednorázové dáta, nečaká na stream)
            t_obj = ib.reqMktData(opt, "", True, False)
            # Krátke čakanie na snapshot
            deadline = time.time() + 8
            while time.time() < deadline:
                if t_obj.bid is not None or t_obj.ask is not None or t_obj.last is not None:
                    break
                time.sleep(0.2)
            ib.cancelMktData(opt)

            def _safe(v):
                try:
                    f = float(v)
                    return f if not math.isnan(f) and f > 0 else None
                except Exception:
                    return None

            price_result["bid"] = _safe(t_obj.bid)
            price_result["ask"] = _safe(t_obj.ask)
            price_result["last"] = _safe(t_obj.last)
            # Skús získať underlying cenu z Greeks ak dostupné
            g = t_obj.modelGreeks or t_obj.bidGreeks or t_obj.askGreeks
            if g and g.undPrice:
                price_result["und_price_ibkr"] = g.undPrice
            if g and g.impliedVol:
                price_result["iv_ibkr"] = g.impliedVol
        except Exception as e:
            price_result["error"] = str(e)
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    done.wait(timeout=12)

    if price_result.get("error"):
        return {"error": price_result["error"]}

    bid = price_result.get("bid")
    ask = price_result.get("ask")
    last = price_result.get("last")
    mid = round((bid + ask) / 2, 3) if bid and ask else (last or None)

    # Underlying cena: z IBKR Greeks alebo z portfólia
    und_price = price_result.get("und_price_ibkr")
    if not und_price:
        try:
            for item in ib.portfolio():
                if item.contract.symbol == ticker and item.contract.secType == "STK":
                    p = item.marketPrice
                    if p and not math.isnan(p) and p > 0:
                        und_price = float(p)
                        break
        except Exception:
            pass

    # IV z IBKR ak dostupná, inak vypočítaj z mid ceny (BS bisekcia)
    from core.probability import calc_iv_from_price, calc_greeks
    iv = price_result.get("iv_ibkr")
    if not iv and mid and und_price:
        iv = calc_iv_from_price(mid, und_price, strike, dte, right)

    # Greeks vždy vypočítame lokálne (BS)
    greeks = {}
    if iv and und_price:
        greeks = calc_greeks(und_price, strike, dte, iv, right)

    result = {
        "ticker": ticker, "expiry": expiry, "strike": strike, "right": right,
        "bid": bid, "ask": ask, "last": last,
        "mid": mid,
        "iv": iv,
        "delta": greeks.get("delta"),
        "gamma": greeks.get("gamma"),
        "theta": greeks.get("theta"),
        "vega": greeks.get("vega"),
        "und_price": und_price,
        "iv_source": "IBKR" if price_result.get("iv_ibkr") else ("BS kalkulácia" if iv else None),
        "error": None if (bid or ask or last) else "Cena nedostupná z IBKR (trh zatvorený alebo chýba predplatné)",
    }
    return result


def fetch_iv(ticker: str, expiry: str, strike: float, right: str = "C") -> dict:
    """Načíta IV pre konkrétny opčný kontrakt."""
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"iv": None, "und_price": None, "error": "Nie je pripojenie na IBKR"}

    _, _, Option = _ib_ready()

    try:
        opt = Option(ticker, expiry, strike, right, "SMART")
        ib.qualifyContracts(opt)
        ib.reqMarketDataType(4)
        [t] = ib.reqTickers(opt)
        greeks = t.modelGreeks or t.bidGreeks or t.askGreeks
        if greeks is None:
            return {"iv": None, "und_price": None, "error": "Greeks nedostupné"}
        return {"iv": greeks.impliedVol, "und_price": greeks.undPrice, "error": None}
    except Exception as e:
        return {"iv": None, "und_price": None, "error": str(e)}


# ─── Portfolio / Fills ────────────────────────────────────────────────────────

def fetch_positions() -> dict:
    """Načíta všetky aktuálne pozície z IBKR portfólia vrátane IV a grekov pre opcie."""
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"positions": [], "error": "Nie je pripojenie na IBKR"}

    _ib_ready()

    try:
        ib.reqMarketDataType(4)
        raw = ib.portfolio()

        # Požiadaj o market data pre všetky opčné kontrakty naraz
        opt_tickers: dict = {}  # contract → ticker
        for item in raw:
            c = item.contract
            if c.secType == "OPT":
                tkr = ib.reqMktData(c, "106", snapshot=False, regulatorySnapshot=False)
                opt_tickers[id(c)] = (c, tkr)

        # Počkaj na dáta (max 3 s) — TWS ich má hneď pre portfóliové pozície
        if opt_tickers:
            ib.sleep(3)

        positions = []
        for item in raw:
            c = item.contract
            if c.secType not in ("OPT", "STK"):
                continue
            pos_size   = item.position
            leg_type   = "Short" if pos_size < 0 else "Long"
            contracts  = int(abs(pos_size))
            base = {
                "sec_type":       c.secType,
                "ticker":         c.symbol,
                "contracts":      contracts,
                "leg_type":       leg_type,
                "avg_cost":       item.averageCost,
                "market_price":   item.marketPrice,
                "market_value":   item.marketValue,
                "unrealized_pnl": item.unrealizedPNL,
                "realized_pnl":   item.realizedPNL,
                "account":        item.account,
                "iv":             None,
                "delta":          None,
                "gamma":          None,
                "theta":          None,
                "vega":           None,
            }
            if c.secType == "OPT":
                base.update({
                    "option_type": "Call" if c.right == "C" else "Put",
                    "strike":      float(c.strike),
                    "expiry":      c.lastTradeDateOrContractMonth,
                })
                # Prečítaj modelGreeks z TWS (IV, delta, theta, vega, gamma)
                _tkr_entry = opt_tickers.get(id(c))
                if _tkr_entry:
                    _, _tkr = _tkr_entry
                    mg = getattr(_tkr, "modelGreeks", None)
                    if mg:
                        iv_raw = getattr(mg, "impliedVol", None)
                        if iv_raw and 0 < iv_raw < 50:  # sanity check
                            base["iv"]    = round(float(iv_raw), 4)
                        _d = getattr(mg, "delta", None)
                        _g = getattr(mg, "gamma", None)
                        _t = getattr(mg, "theta", None)
                        _v = getattr(mg, "vega",  None)
                        if _d is not None and abs(_d) <= 1:
                            base["delta"] = round(float(_d), 4)
                        if _g is not None:
                            base["gamma"] = round(float(_g), 6)
                        if _t is not None:
                            base["theta"] = round(float(_t), 4)
                        if _v is not None:
                            base["vega"]  = round(float(_v), 4)
            else:
                base.update({"option_type": None, "strike": None, "expiry": None})
            positions.append(base)

        # Zruš market data subscriptions
        for _, (c, _) in opt_tickers.items():
            try:
                ib.cancelMktData(c)
            except Exception:
                pass

        return {"positions": positions, "error": None}
    except Exception as e:
        return {"positions": [], "error": str(e)}


def _pos_key(ticker, strike, expiry, leg_type, option_type) -> str:
    """Unikátny kľúč pre porovnanie pozícií."""
    return f"{ticker}|{strike}|{expiry}|{leg_type}|{option_type}"


def sync_positions_to_db(positions: list[dict], db_module) -> dict:
    """
    Porovná IBKR pozície s DB:
    1. Pridá nové pozície.
    2. Aktualizuje contracts + avg_cost pre existujúce.
    3. Detekuje pozície, ktoré sú v DB ako Open ale v IBKR chýbajú
       (pravdepodobne uzavreté) → uloží do zoznamu 'possibly_closed'.
    """
    existing_open = db_module.get_open_trades()
    ibkr_opts = [p for p in positions if p["sec_type"] == "OPT"]

    # Mapa IBKR pozícií podľa kľúča
    ibkr_map: dict[str, dict] = {}
    for pos in ibkr_opts:
        k = _pos_key(pos["ticker"], pos["strike"], pos["expiry"],
                     pos["leg_type"], pos["option_type"])
        ibkr_map[k] = pos

    # Mapa DB open trades podľa kľúča
    db_map: dict[str, dict] = {}
    for t in existing_open:
        k = _pos_key(t["ticker"], t.get("strike"), t.get("expiry"),
                     t.get("leg_type"), t.get("option_type"))
        db_map[k] = t

    added = updated = skipped = 0
    possibly_closed: list[dict] = []

    # 1. IBKR → DB: pridaj nové, aktualizuj existujúce
    for k, pos in ibkr_map.items():
        if k in db_map:
            t = db_map[k]
            changes = {}
            # Aktualizuj počet kontraktov
            if pos["contracts"] != t.get("contracts"):
                changes["contracts"] = pos["contracts"]
            # Aktualizuj priemerné náklady (entry price)
            new_ep = round(pos["avg_cost"] / 100, 4) if pos.get("avg_cost") else None
            old_ep = t.get("entry_price") or 0.0
            if new_ep is not None and abs(new_ep - old_ep) > 0.01:
                changes["entry_price"] = new_ep
            if changes:
                db_module.update_trade(t["id"], **changes)
                updated += 1
            else:
                skipped += 1
        else:
            # Nová pozícia — pridaj do DB
            db_module.add_trade(
                ticker=pos["ticker"],
                strategy="Import IBKR",
                leg_type=pos["leg_type"],
                option_type=pos["option_type"],
                strike=pos["strike"],
                expiry=pos["expiry"],
                contracts=pos["contracts"],
                entry_price=round(pos["avg_cost"] / 100, 4) if pos.get("avg_cost") else 0.0,
                entry_date=datetime.today().strftime("%Y-%m-%d"),
                group_id=None, iv_at_entry=None, pop_at_entry=None,
            )
            added += 1

    # 2. DB → IBKR: zisti pozície, ktoré zmizli z IBKR (možno uzavreté)
    for k, t in db_map.items():
        if k not in ibkr_map:
            possibly_closed.append({
                "id": t["id"],
                "ticker": t["ticker"],
                "leg_type": t.get("leg_type"),
                "option_type": t.get("option_type"),
                "strike": t.get("strike"),
                "expiry": t.get("expiry"),
                "entry_price": t.get("entry_price"),
            })

    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "possibly_closed": possibly_closed,
    }


def fetch_fills() -> dict:
    """Načíta vykonané obchody (fills) z aktuálnej TWS session vrátane komisií."""
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"fills": [], "error": "Nie je pripojenie na IBKR"}
    _ib_ready()
    try:
        # Zostavíme mapu execId → komisia z commissionReports
        commission_map: dict[str, float] = {}
        for cr in ib.fills():
            if hasattr(cr, "commissionReport") and cr.commissionReport:
                rpt = cr.commissionReport
                eid = getattr(rpt, "execId", None) or getattr(cr.execution, "execId", None)
                if eid and rpt.commission not in (None, 1.7976931348623157e+308):
                    commission_map[eid] = float(rpt.commission)

        result = []
        for f in ib.fills():
            c = f.contract
            if c.secType != "OPT":
                continue
            ex = f.execution
            side = ex.side.upper()  # "BOT" alebo "SLD"
            comm = commission_map.get(ex.execId, 0.0)
            result.append({
                "ticker": c.symbol,
                "option_type": "Call" if c.right == "C" else "Put",
                "strike": float(c.strike),
                "expiry": c.lastTradeDateOrContractMonth,
                "contracts": int(abs(ex.shares)),
                "leg_type": "Long" if side == "BOT" else "Short",
                "entry_price": ex.price,
                "entry_date": (ex.time.strftime("%Y-%m-%d") if hasattr(ex.time, "strftime") else str(ex.time)[:10]) if ex.time else datetime.today().strftime("%Y-%m-%d"),
                "exec_id": ex.execId,
                "side": side,
                "account": ex.acctNumber,
                "commission": comm,
            })
        return {"fills": result, "error": None}
    except Exception as e:
        return {"fills": [], "error": str(e)}


def sync_fills_to_db(fills: list[dict], db_module) -> dict:
    """
    Importuje fills do DB.
    - BOT fill + existujúca Open Short pozícia → uzavrie ju (close).
    - SLD fill + existujúca Open Long  pozícia → uzavrie ju (close).
    - Ostatné fills pridá ako nové obchody (ak ešte neexistujú).

    Poznámka: ex.shares je vždy kladné, preto sa leg_type nedá odvodiť zo znamienka.
    Namiesto toho porovnáme fill priamo s otvorenými pozíciami v DB.
    """
    existing = db_module.get_all_trades()
    open_trades = [t for t in existing if t.get("status") == "Open"]
    added = skipped = closed = 0

    for fill in fills:
        side = fill.get("side", "").upper()   # "BOT" alebo "SLD"

        # Určíme, aký typ otvorenej pozície by tento fill UZATVÁRAL
        # BOT uzatvára Short; SLD uzatvára Long
        close_leg = "Short" if side == "BOT" else "Long"

        # Pokús sa nájsť zodpovedajúcu Open pozíciu na uzavretie
        target = next(
            (
                t for t in open_trades
                if t["ticker"] == fill["ticker"]
                and str(t.get("strike", "")) == str(fill["strike"])
                and str(t.get("expiry", "")) == str(fill["expiry"])
                and t.get("option_type") == fill["option_type"]
                and t.get("leg_type") == close_leg
                and t.get("status") == "Open"
            ),
            None,
        )

        if target:
            # Celková komisia = entry komisia (uložená) + exit komisia (z tohto fillu)
            existing_comm = float(target.get("commission") or 0.0)
            exit_comm     = float(fill.get("commission") or 0.0)
            total_comm    = existing_comm + exit_comm
            db_module.update_trade(
                target["id"],
                exit_price=fill["entry_price"],
                exit_date=fill["entry_date"],
                status="Closed",
                commission=total_comm if total_comm > 0 else None,
            )
            open_trades = [t for t in open_trades if t["id"] != target["id"]]
            closed += 1
            continue

        # Otváracie plnenie — leg_type podľa smeru (BOT=Long, SLD=Short)
        open_leg = "Long" if side == "BOT" else "Short"
        duplicate = any(
            t["ticker"] == fill["ticker"]
            and str(t.get("strike", "")) == str(fill["strike"])
            and str(t.get("expiry", "")) == str(fill["expiry"])
            and t.get("leg_type") == open_leg
            and t.get("option_type") == fill["option_type"]
            and t.get("entry_date", "") == fill["entry_date"]
            for t in existing
        )
        if duplicate:
            skipped += 1
            continue
        db_module.add_trade(
            ticker=fill["ticker"],
            strategy="Import Fills",
            leg_type=open_leg,
            option_type=fill["option_type"],
            strike=fill["strike"],
            expiry=fill["expiry"],
            contracts=fill["contracts"],
            entry_price=fill["entry_price"],
            entry_date=fill["entry_date"],
            group_id=None, iv_at_entry=None, pop_at_entry=None,
            commission=fill.get("commission") or 0.0,
        )
        added += 1
    return {"added": added, "skipped": skipped, "closed": closed}


# ─── Expirácie ─────────────────────────────────────────────────────────────────

def generate_expirations_local(months: int = 12) -> dict:
    """
    Generuje štandardné expirácie LOKÁLNE bez IBKR (okamžité):
    - Týždenné piatky na 8 týždňov
    - Mesačné (3. piatok) na months mesiacov dopredu
    """
    today = date.today()
    expirations = set()

    # Weeklies — každý piatok 8 týždňov
    d = today
    while d.weekday() != 4:
        d += timedelta(days=1)
    for _ in range(8):
        expirations.add(d.strftime("%Y%m%d"))
        d += timedelta(weeks=1)

    # Monthlies — 3. piatok každého mesiaca
    for m_offset in range(months + 1):
        year = today.year + (today.month + m_offset - 1) // 12
        month = (today.month + m_offset - 1) % 12 + 1
        first_day = date(year, month, 1)
        days_to_friday = (4 - first_day.weekday()) % 7
        third_friday = first_day + timedelta(days=days_to_friday) + timedelta(weeks=2)
        if third_friday > today:
            expirations.add(third_friday.strftime("%Y%m%d"))

    return {"expirations": sorted(expirations), "source": "local", "error": None}


def fetch_expirations_for_ticker(ticker: str, max_months: int = 12) -> dict:
    """Vráti lokálne generované expirácie (okamžité, bez IBKR)."""
    local = generate_expirations_local(max_months)
    return {
        "expirations": local["expirations"],
        "strikes": [],
        "source": "local",
        "error": None,
    }
