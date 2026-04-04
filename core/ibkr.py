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
import socket
import threading
import time
import math
from datetime import date as _date

import streamlit as st
from typing import Optional
from datetime import datetime, date, timedelta


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7496
DEFAULT_CLIENT_ID = 10

# ─── Background fetch job state (perzistentný medzi Streamlit rerunmi) ────────
# Uložené na úrovni modulu – modul sa pri rerun NEreimportuje, stav zostáva.
FETCH_JOB: dict = {
    "status": "idle",   # idle | running | done | cancelled | error
    "positions": None,
    "orders": None,
    "error": None,
    "stop_event": None,
    "thread": None,
}


# ─── Black-Scholes (delegované na core.greeks) ───────────────────────────────
from core.greeks import bs_price as _bs_price, calc_iv as _calc_iv, bs_greeks as _bs_greeks

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


# ─── IB singleton + hlavný event loop (module-level) ────────────────────────
# Module-level premenné sú dostupné aj z background vlákien (kde session_state
# nie je k dispozícii).
_IB_INSTANCE: object = None  # type: ignore[assignment]
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None  # loop hlavného vlákna pri connect()


async def _await_ib_open_orders(ib):
    """
    ib.reqAllOpenOrdersAsync() vracia asyncio.Future, nie coroutine.
    run_coroutine_threadsafe vyžaduje coroutine → musíme obaliť do async def.
    """
    return await ib.reqAllOpenOrdersAsync()


async def _await_ib_contract_details(ib, contract):
    """Rovnaký dôvod ako pri open orders – Future musí byť awaitnutý v coroutine na ib._loop."""
    return await ib.reqContractDetailsAsync(contract)


def _ib_api_loop(ib) -> asyncio.AbstractEventLoop | None:
    """Event loop kde beží IB socket (vždy ib._loop po connect)."""
    return getattr(ib, "_loop", None) or _MAIN_LOOP


# Cache conId → čitateľný popis (naplní sa pri connect cez _populate_contract_cache)
_LEG_LABEL_CACHE: dict[int, str] = {}


def clear_leg_label_cache() -> None:
    _LEG_LABEL_CACHE.clear()


def _populate_contract_cache(ib) -> None:
    """
    Naplní _LEG_LABEL_CACHE kontraktmi z IB.
    Volaj HNEĎ po connect() kým async ešte funguje.
    """
    global _LEG_LABEL_CACHE
    
    # Zbieraj conId z portfolio, positions, fills
    for item in (ib.portfolio() or []):
        c = item.contract
        if c.conId and c.localSymbol:
            _LEG_LABEL_CACHE[c.conId] = _contract_label_from_details(c)
    
    for pos in (ib.positions() or []):
        c = pos.contract
        if c.conId and c.localSymbol:
            _LEG_LABEL_CACHE[c.conId] = _contract_label_from_details(c)
    
    for fill in (ib.fills() or []):
        c = fill.contract
        if c.conId and c.localSymbol:
            _LEG_LABEL_CACHE[c.conId] = _contract_label_from_details(c)
    
    # Zbieraj conId z otvorených objednávok (vrátane BAG combo legs)
    try:
        trades = ib.openTrades() or []
        con_ids_to_resolve: set[int] = set()
        
        for trade in trades:
            c = trade.contract
            if c.secType == "BAG":
                for leg in (getattr(c, "comboLegs", []) or []):
                    if leg.conId and leg.conId not in _LEG_LABEL_CACHE:
                        con_ids_to_resolve.add(leg.conId)
        
        # Resolve všetky naraz cez qualifyContracts
        if con_ids_to_resolve:
            from ib_insync import Contract
            contracts = [Contract(conId=cid) for cid in con_ids_to_resolve]
            try:
                ib.qualifyContracts(*contracts)
                for c in contracts:
                    if c.conId and c.localSymbol:
                        _LEG_LABEL_CACHE[c.conId] = _contract_label_from_details(c)
            except Exception:
                pass
    except Exception:
        pass


def enrich_open_orders_legs(orders: list[dict]) -> None:
    """
    Doplní BAG combo nohy čitateľnými popismi (strike, exp, C/P).
    Volaj výhradne z hlavného Streamlit vlákna (rovnaké ako connect).
    Upravuje orders in-place.
    Používa ib.qualifyContracts() ktorý je spoľahlivejší než reqContractDetails.
    """
    ib = get_ib()
    if not ib or not ib.isConnected() or not orders:
        return
    _ib_ready()
    from ib_insync import Contract

    # Zbieraj všetky conId z BAG objednávok, ktoré ešte nie sú v cache
    needed_cids: set[int] = set()
    for o in orders:
        if o.get("sec_type") != "BAG":
            continue
        for lg in o.get("legs") or []:
            cid = int(lg.get("con_id") or 0)
            if cid and cid not in _LEG_LABEL_CACHE:
                needed_cids.add(cid)

    # Batch qualify všetkých nových conId naraz
    if needed_cids:
        contracts = [Contract(conId=cid) for cid in needed_cids]
        try:
            ib.qualifyContracts(*contracts)
        except Exception:
            pass  # niektoré môžu zlyhať, pokračuj s tým čo prišlo
        for c in contracts:
            if c.conId and c.localSymbol:
                # Máme qualified kontrakt → extrahuj label
                lab = _contract_label_from_details(c)
                _LEG_LABEL_CACHE[c.conId] = lab
            elif c.conId:
                _LEG_LABEL_CACHE[c.conId] = str(c.conId)

    # Doplň labels do orders
    for o in orders:
        if o.get("sec_type") != "BAG":
            continue
        legs = o.get("legs") or []
        parts = []
        for lg in legs:
            cid = int(lg.get("con_id") or 0)
            lab = _LEG_LABEL_CACHE.get(cid, str(cid))
            sign = "+" if lg.get("action") == "BUY" else "-"
            parts.append(f"{sign}{lg.get('ratio', 1)} {lab}")
            lg["label"] = lab
        o["legs_descr"] = ", ".join(parts)


def get_ib():
    """Vráti IB inštanciu – z module-level cache alebo session_state."""
    global _IB_INSTANCE
    if _IB_INSTANCE is not None:
        return _IB_INSTANCE
    ib = st.session_state.get("ib")
    if ib is not None:
        _IB_INSTANCE = ib
    return ib


def is_connected() -> bool:
    ib = get_ib()
    if ib is None:
        return False
    try:
        return ib.isConnected()
    except Exception:
        return False


# ─── Connect / Disconnect ─────────────────────────────────────────────────────

def _tcp_reachable(host: str, port: int, timeout: float = 3.0) -> tuple[bool, str]:
    """Rýchla kontrola či TWS/Gateway počúva na host:port (pred ib.connect)."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
        return True, ""
    except OSError as e:
        return False, str(e)


def connect(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    client_id: int = DEFAULT_CLIENT_ID,
) -> tuple[bool, str]:
    """
    Pripoj sa na IBKR. Vždy v hlavnom Streamlit vlákne (ib_insync to vyžaduje).
    Najprv TCP test (3s) – ak TWS nepočúva, okamžitá chyba namiesto visenia.
    """
    global _IB_INSTANCE, _MAIN_LOOP

    ok_tcp, tcp_err = _tcp_reachable(host, port, timeout=3.0)
    if not ok_tcp:
        return (
            False,
            f"Na {host}:{port} sa nedá pripojiť (TCP). Spusti TWS alebo IB Gateway "
            f"a skontroluj API port (TWS live 7496, paper 7497, Gateway 4001/4002). "
            f"Detail: {tcp_err}",
        )

    old = _IB_INSTANCE or st.session_state.get("ib")
    if old:
        try:
            old.disconnect()
        except Exception:
            pass
    _IB_INSTANCE = None
    _MAIN_LOOP   = None
    st.session_state.pop("ib", None)

    IB, _, _ = _ib_ready()

    last_err = ""
    for offset in range(6):
        cid = client_id + offset
        try:
            ib = IB()
            try:
                ib.RequestTimeout = 8
            except AttributeError:
                pass
            ib.connect(host, port, clientId=cid, timeout=8, readonly=False)
            _IB_INSTANCE = ib
            # ib._loop by mal byť nastavený po connect, ale nest_asyncio ho môže zničiť
            # Explicitne nastavíme ak chýba
            if not getattr(ib, "_loop", None):
                from ib_insync import util
                ib._loop = util.getLoop()
            _MAIN_LOOP = ib._loop or asyncio.get_event_loop()
            st.session_state["ib"] = ib
            try:
                ib.reqAccountUpdates(True)
            except Exception:
                pass
            try:
                ib.reqAllOpenOrders()   # pre-populate cache objednávok
            except Exception:
                pass
            # Naplň contract cache pre BAG combo legs (kým async funguje)
            try:
                _populate_contract_cache(ib)
            except Exception:
                pass
            return True, f"Pripojený na {host}:{port}  (clientId={cid})"
        except Exception as e:
            last_err = str(e)
            if "already in use" not in last_err and "326" not in last_err:
                break
    return False, f"Chyba pripojenia: {last_err}"


def disconnect() -> None:
    global _IB_INSTANCE, _MAIN_LOOP
    ib = get_ib()
    if ib:
        try:
            ib.disconnect()
        except Exception:
            pass
    _IB_INSTANCE = None
    _MAIN_LOOP   = None
    clear_leg_label_cache()
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

    # 1. Portfólio — okamžité (ale len ak sa zdá byť aktuálne, tu sa často skrýva stará 'marketPrice')
    # Ak nemáš STK, ale len OPT, nechceme brať cenu z portfólia lebo býva neaktuálna.
    # Urobíme to tak, že najprv skúsime načítat streamovanú cenu.
    
    # 2. Streaming reqMktData — čerstvá cena v separátnom vlákne
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

            def _valid(v) -> float | None:
                try:
                    f = float(v)
                    return f if f and not math.isnan(f) and f > 0 else None
                except Exception:
                    return None

            # Skúsi live (1) → delayed (3) → frozen live (2) → delayed frozen (4)
            for mdt in (1, 3, 2, 4):
                ib.reqMarketDataType(mdt)
                # DÔLEŽITÉ: Niekedy stock stream nepríde, ak nedržíš akcie, ale chce to reqMktData priamo na smart.
                # Snapshot request s False čaká na plný stream. 
                tkr = ib.reqMktData(stock, "106", False, False)
                deadline = time.time() + 3
                found = None
                while time.time() < deadline and not found:
            # TWS posiela tickPrice eventy. 'last' a 'close' sa updatujú.
                    if getattr(tkr, "last", None) and not math.isnan(tkr.last) and tkr.last > 0:
                        found = float(tkr.last)
                    elif getattr(tkr, "close", None) and not math.isnan(tkr.close) and tkr.close > 0:
                        found = float(tkr.close)
                    elif getattr(tkr, "bid", None) and getattr(tkr, "ask", None) and not math.isnan(tkr.bid) and not math.isnan(tkr.ask) and tkr.bid > 0 and tkr.ask > 0:
                        found = float(round((tkr.bid + tkr.ask) / 2, 2))
                    
                    if not found:
                        val = tkr.marketPrice()
                        if val and not math.isnan(val) and val > 0:
                            found = float(val)

                    if not found:
                        ib.sleep(0.1)
                        
                ib.cancelMktData(stock)
                if found:
                    price_result["price"] = found
                    price_result["source"] = f"live-stream mdt={mdt}"
                    break


        except Exception as e:
            price_result["error"] = str(e)
        finally:
            done2.set()

    t2 = threading.Thread(target=_spot_worker, daemon=True)
    t2.start()
    finished2 = done2.wait(timeout=timeout)

    # 3. Záchranné lano – ak reqMktData nenašiel nič, ale máme niečo v portfóliu
    if not price_result.get("price"):
        try:
            for item in ib.portfolio():
                if item.contract.symbol == ticker and item.contract.secType == "STK":
                    p = item.marketPrice
                    if p and not math.isnan(p) and p > 0:
                        return {"price": float(p), "ticker": ticker, "error": None, "source": "portfolio fallback"}
        except Exception:
            pass

    if not finished2:
        return {"price": None, "ticker": ticker, "error": f"Timeout {timeout}s — cena nedostupná"}
    if price_result.get("price"):
        return {"price": price_result["price"], "ticker": ticker, "error": None,
                "source": price_result.get("source", "mktdata")}
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


def _contract_label_from_details(ct) -> str:
    if ct.secType in ("OPT", "FOP"):
        exp = ct.lastTradeDateOrContractMonth or ""
        if len(exp) == 8:
            try:
                from datetime import datetime as _dt
                d = _dt.strptime(exp, "%Y%m%d")
                exp = d.strftime("%b'%y")
            except Exception:
                pass
        right = "C" if ct.right in ("C", "CALL") else "P"
        return f"{exp} {ct.strike:.0f} {right}"
    if ct.secType == "FUT":
        exp = ct.lastTradeDateOrContractMonth or ""
        return f"{ct.symbol} {exp} FUT"
    return ct.localSymbol or ct.symbol or str(getattr(ct, "conId", ""))


def fetch_open_orders(use_cache: bool = False) -> dict:
    """
    Načíta aktívne objednávky z TWS (vrátane objednávok zadaných priamo v TWS).

    use_cache=False (default, hlavné Streamlit vlákno):
        Volá reqAllOpenOrders() priamo. Vyžaduje nest_asyncio.
    use_cache=True (background vlákno):
        Bezpečne plánuje reqAllOpenOrdersAsync() na IB event loope cez
        run_coroutine_threadsafe – žiadny asyncio konflikt, dostane VŠETKY
        objednávky vrátane TWS-zadaných.
    """
    global _MAIN_LOOP
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"orders": [], "error": "Nie je pripojenie na IBKR"}

    try:
        if use_cache:
            # Z background vlákna: na ib._loop musí ísť skutočný coroutine
            loop = _ib_api_loop(ib)
            if loop and loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    _await_ib_open_orders(ib), loop
                )
                all_trades = future.result(timeout=15)
            else:
                all_trades = ib.openTrades()
        else:
            # Živé volanie z hlavného Streamlit vlákna
            _ib_ready()
            try:
                _MAIN_LOOP = asyncio.get_event_loop()
            except Exception:
                pass
            # Najprv skús openTrades (cache), potom reqAllOpenOrders
            all_trades = ib.openTrades()
            if not all_trades:
                try:
                    all_trades = ib.reqAllOpenOrders()
                except Exception as e:
                    return {"orders": [], "error": f"reqAllOpenOrders failed: {type(e).__name__}: {e}", "total_raw": 0, "_openTrades_count": 0}

        # DEBUG
        _debug_statuses = []
        for t in (all_trades or []):
            _debug_statuses.append(f"{t.contract.symbol}:{t.orderStatus.status}")

        ACTIVE_STATUSES = {
            "PendingSubmit", "PendingCancel", "PreSubmitted",
            "Submitted", "Inactive",
        }
        orders = []
        skipped = []
        for trade in (all_trades or []):
            status = trade.orderStatus.status
            if status not in ACTIVE_STATUSES:
                continue
            c = trade.contract
            o = trade.order
            base = {
                "ticker":      c.symbol,
                "sec_type":    c.secType,
                "action":      o.action,
                "total_qty":   o.totalQuantity,
                "order_type":  o.orderType,
                "limit_price": o.lmtPrice if o.orderType in ("LMT", "STP LMT") else None,
                "aux_price":   o.auxPrice if o.orderType in ("STP", "STP LMT", "TRAIL") else None,
                "status":      status,
                "filled_qty":  o.filledQuantity if hasattr(o, "filledQuantity") else trade.orderStatus.filled,
                "remaining":   trade.orderStatus.remaining,
            }
            if c.secType in ("OPT", "FOP"):
                base.update({
                    "option_type": "Call" if c.right == "C" else "Put",
                    "strike":      float(c.strike),
                    "expiry":      c.lastTradeDateOrContractMonth,
                    "legs_descr":  None,
                    "legs":        [],
                })
            elif c.secType == "BAG":
                raw_legs = getattr(c, "comboLegs", []) or []
                legs_list = []
                legs_parts = []
                
                for leg in raw_legs:
                    # Použij module-level cache (naplnenú pri connect)
                    lab = _LEG_LABEL_CACHE.get(leg.conId, str(leg.conId))
                    
                    sign = "+" if leg.action == "BUY" else "-"
                    legs_parts.append(f"{sign}{leg.ratio} {lab}")
                    legs_list.append({
                        "con_id":   leg.conId,
                        "ratio":    leg.ratio,
                        "action":   leg.action,
                        "exchange": getattr(leg, "exchange", ""),
                        "label":    lab,
                    })

                legs_descr = ", ".join(legs_parts) if legs_parts else (getattr(c, "comboLegsDescrip", "") or None)

                base.update({
                    "option_type": None,
                    "strike":      None,
                    "expiry":      None,
                    "legs_descr":  legs_descr,
                    "legs":        legs_list,
                })
            else:
                base.update({
                    "option_type": None,
                    "strike":      None,
                    "expiry":      None,
                    "legs_descr":  None,
                    "legs":        [],
                })

            # ── Podmienky objednávky (PriceCondition, TimeCondition, …) ──────
            conditions_list = []
            for cond in (getattr(o, "conditions", None) or []):
                ctype = type(cond).__name__
                cdict: dict = {"type": ctype}
                # PriceCondition
                if hasattr(cond, "price"):
                    cdict["price"]   = cond.price
                    cdict["isMore"]  = cond.isMore   # True = price > X
                    cdict["conId"]   = getattr(cond, "conId", None)
                    cdict["exch"]    = getattr(cond, "exch", None)
                # PercentChangeCondition / VolumeCondition
                if hasattr(cond, "changePercent"):
                    cdict["changePercent"] = cond.changePercent
                    cdict["isMore"]        = cond.isMore
                if hasattr(cond, "volume"):
                    cdict["volume"]  = cond.volume
                    cdict["isMore"]  = cond.isMore
                # TimeCondition
                if hasattr(cond, "time"):
                    cdict["time"]    = cond.time
                    cdict["isMore"]  = cond.isMore
                # MarginCondition
                if hasattr(cond, "percent"):
                    cdict["percent"] = cond.percent
                    cdict["isMore"]  = cond.isMore
                # ExecutionCondition
                if hasattr(cond, "symbol"):
                    cdict["symbol"]  = getattr(cond, "symbol", None)
                    cdict["secType"] = getattr(cond, "secType", None)
                # AND/OR spájanie podmienok
                cdict["conjunction"] = getattr(cond, "conjunctionConnection", "a")
                conditions_list.append(cdict)

            base["conditions"] = conditions_list
            orders.append(base)

        return {"orders": orders, "error": None, "total_raw": len(all_trades or []), "_debug_statuses": _debug_statuses, "_src": "openTrades" if not use_cache else "cache"}
    except Exception as e:
        return {"orders": [], "error": str(e)}

# ─── Portfolio / Fills ────────────────────────────────────────────────────────

def _price_from_mkt_data(md) -> tuple[float | None, str]:
    """
    Cena čo najbližšie stĺpcu „Last“ v TWS (nie iba portfolio mark).
    Poradie: last → marketPrice() → mid bid/ask → close.
    """
    if md is None:
        return None, ""
    try:
        last = getattr(md, "last", None)
        if last is not None and not math.isnan(float(last)) and float(last) > 0:
            return float(last), "last"
        mp = md.marketPrice()
        if mp and not math.isnan(mp) and mp > 0:
            return float(mp), "mark"
        bid, ask = getattr(md, "bid", None), getattr(md, "ask", None)
        if (bid is not None and ask is not None
                and not math.isnan(float(bid)) and not math.isnan(float(ask))
                and float(bid) > 0 and float(ask) > 0):
            return float(round((float(bid) + float(ask)) / 2, 4)), "mid"
        cl = getattr(md, "close", None)
        if cl is not None and not math.isnan(float(cl)) and float(cl) > 0:
            return float(cl), "close"
    except Exception:
        pass
    return None, ""


def _snapshot_enrich_position_prices(ib, positions: list[dict], snap_rows: list[tuple[int, object]]) -> None:
    """
    snap_rows: (index do positions, Contract z portfolio).
    reqMktData snapshot – zarovnanie k Last v TWS; krátky limit aby UI neviselo.
    """
    if not snap_rows:
        return
    mkt_rows: list[tuple[int, object, object | None]] = []
    for pidx, c in snap_rows:
        md = None
        try:
            ib.qualifyContracts(c)
            md = ib.reqMktData(c, "", snapshot=True, regulatorySnapshot=False)
        except Exception:
            pass
        mkt_rows.append((pidx, c, md))

    deadline = time.time() + min(4.0, 1.0 + 0.35 * len(mkt_rows))
    done: set[int] = set()
    while time.time() < deadline and len(done) < len(mkt_rows):
        for pidx, c, md in mkt_rows:
            if md is None or pidx in done:
                if md is None:
                    done.add(pidx)
                continue
            px, src = _price_from_mkt_data(md)
            if px:
                positions[pidx]["market_price"] = px
                positions[pidx]["price_source"] = src
                done.add(pidx)
        if len(done) == len(mkt_rows):
            break
        try:
            ib.sleep(0.08)
        except Exception:
            time.sleep(0.08)

    for _pidx, c, md in mkt_rows:
        if md is not None:
            try:
                ib.cancelMktData(c)
            except Exception:
                pass


def _apply_upnl_from_price(positions: list[dict], pidx: int, c, px: float) -> None:
    """
    Prepočíta unrealized_pnl a market_value z danej ceny identicky ako TWS.
    avg_cost z IB API = celková cena kontraktu (pre OPT 100× cena per share).
    """
    avg_cost  = float(positions[pidx].get("avg_cost") or 0)
    contracts = float(positions[pidx].get("contracts") or 0)
    if avg_cost <= 0 or contracts <= 0:
        return
    mult         = 100 if c.secType in ("OPT", "FOP") else 1
    avg_per_unit = avg_cost / mult
    leg_type     = positions[pidx].get("leg_type", "Long")
    sign         = -1 if leg_type == "Short" else 1
    positions[pidx]["unrealized_pnl"] = round(
        (px - avg_per_unit) * contracts * mult * sign, 2
    )
    positions[pidx]["market_value"] = round(
        px * contracts * mult * sign, 2
    )


def _historical_enrich_position_prices(
    ib, positions: list[dict], snap_rows: list[tuple[int, object]]
) -> None:
    """
    Obohatí pozície o settlement/close cenu cez reqMktData streaming (ticker.close).
    ticker.close = denná záverečná (settlement) cena — presne tá, z ktorej TWS počíta UNRL.

    Ak ticker.close nie je dostupný (NaN), padne na reqHistoricalData MIDPOINT.
    """
    if not snap_rows:
        return

    # ── Krok 1: reqMktData streaming pre všetky pozície naraz ──────────────────
    mkt_rows: list[tuple[int, object, object]] = []
    for pidx, c in snap_rows:
        try:
            ib.qualifyContracts(c)
            tk = ib.reqMktData(c, "", snapshot=False, regulatorySnapshot=False)
            mkt_rows.append((pidx, c, tk))
        except Exception:
            mkt_rows.append((pidx, c, None))

    # Čakaj max 5 s na ticker.close
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if all(
            (tk is not None and tk.close is not None and not math.isnan(tk.close) and tk.close > 0)
            for _, _, tk in mkt_rows
        ):
            break
        try:
            ib.sleep(0.1)
        except Exception:
            time.sleep(0.1)

    # Spracuj výsledky a zruš subscripcie
    fallback_rows: list[tuple[int, object]] = []
    for pidx, c, tk in mkt_rows:
        if tk is not None:
            try:
                ib.cancelMktData(c)
            except Exception:
                pass
        close_px = None
        if tk is not None and tk.close is not None:
            try:
                v = float(tk.close)
                if v > 0:
                    close_px = v
            except Exception:
                pass
        if close_px is not None:
            positions[pidx]["market_price"] = close_px
            positions[pidx]["price_source"] = "settlement_close"
            _apply_upnl_from_price(positions, pidx, c, close_px)
        else:
            fallback_rows.append((pidx, c))

    # ── Krok 2: fallback – reqHistoricalData MIDPOINT pre neúspešné ────────────
    for pidx, c in fallback_rows:
        for dur, what in [("300 S", "TRADES"), ("300 S", "MIDPOINT")]:
            try:
                bars = ib.reqHistoricalData(
                    c,
                    endDateTime="",
                    durationStr=dur,
                    barSizeSetting="1 min",
                    whatToShow=what,
                    useRTH=False,
                    formatDate=1,
                    timeout=8,
                )
                if bars:
                    px = float(bars[-1].close)
                    if px > 0:
                        positions[pidx]["market_price"] = px
                        positions[pidx]["price_source"] = (
                            "hist_trades" if what == "TRADES" else "hist_midpoint"
                        )
                        _apply_upnl_from_price(positions, pidx, c, px)
                        break
            except Exception:
                continue


def fetch_positions(
    with_greeks: bool = False,
    use_mkt_snapshot: bool = False,
    use_historical_last: bool = False,
) -> dict:
    """
    Načíta všetky aktuálne pozície z IBKR portfólia.

    use_mkt_snapshot=False (default): ib.portfolio() – rýchle (vhodné pre auto-sync).
    use_mkt_snapshot=True: reqMktData snapshot – len ak máš streaming MD subscription.
    use_historical_last=True: reqHistoricalData (posledný 1-min bar) – Last cena ako v TWS,
        funguje aj bez streaming subscription. Pomalšie (1–2 s na pozíciu).

    with_greeks=True: IV + Greeks z BS (market_price po prípadnom obohaténí).
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"positions": [], "error": "Nie je pripojenie na IBKR"}

    try:
        raw = ib.portfolio()
        if not raw:
            return {"positions": [], "error": None}

        positions: list[dict] = []
        snap_rows: list[tuple[int, object]] = []

        for item in raw:
            c = item.contract
            pos_size  = float(item.position)
            leg_type  = "Short" if pos_size < 0 else "Long"
            mp0 = item.marketPrice
            if mp0 is not None and (math.isnan(float(mp0)) or float(mp0) <= 0):
                mp0 = None
            base = {
                "sec_type":       c.secType,
                "ticker":         c.symbol,
                "contracts":      abs(pos_size),
                "leg_type":       leg_type,
                "avg_cost":       item.averageCost,
                "market_price":   float(mp0) if mp0 is not None else None,
                # keep original portfolio marketPrice for debugging (may be overwritten by snapshot)
                "market_price_portfolio": float(mp0) if mp0 is not None else None,
                "market_value":   item.marketValue,
                "unrealized_pnl": item.unrealizedPNL,
                "realized_pnl":   item.realizedPNL,
                "account":        item.account,
                "price_source":   "portfolio_mark",
                "iv":             None,
                "delta":          None,
                "gamma":          None,
                "theta":          None,
                "vega":           None,
            }
            if c.secType in ("OPT", "FOP"):
                base.update({
                    "option_type": "Call" if c.right == "C" else "Put",
                    "strike":      float(c.strike),
                    "expiry":      c.lastTradeDateOrContractMonth,
                })
            elif c.secType == "FUT":
                base.update({
                    "option_type": None,
                    "strike":      None,
                    "expiry":      c.lastTradeDateOrContractMonth,
                })
            else:
                base.update({"option_type": None, "strike": None, "expiry": None})

            pidx = len(positions)
            positions.append(base)
            snap_rows.append((pidx, c))

        if use_historical_last and snap_rows:
            try:
                _historical_enrich_position_prices(ib, positions, snap_rows)
            except Exception:
                pass
        elif use_mkt_snapshot and snap_rows:
            try:
                _snapshot_enrich_position_prices(ib, positions, snap_rows)
            except Exception:
                pass

        # Podklad pre Greeks: prvý STK (po snapshot)
        under_price: float | None = None
        for p in positions:
            if p.get("sec_type") == "STK":
                mp = p.get("market_price")
                if mp is not None and not math.isnan(float(mp)) and float(mp) > 0:
                    under_price = float(mp)
                    break

        if with_greeks and under_price:
            for p in positions:
                if p.get("sec_type") not in ("OPT", "FOP"):
                    continue
                opt_price = p.get("market_price")
                if opt_price is None or math.isnan(float(opt_price)) or float(opt_price) <= 0:
                    continue
                exp_str = p.get("expiry") or ""
                try:
                    T = max(0.001, (_date(int(exp_str[:4]), int(exp_str[4:6]), int(exp_str[6:])) - _date.today()).days / 365.0)
                except Exception:
                    T = 30 / 365.0
                right = "C" if p.get("option_type") == "Call" else "P"
                iv = _calc_iv(under_price, float(p["strike"]), T, float(opt_price), right)
                if iv:
                    p.update(_bs_greeks(under_price, float(p["strike"]), T, iv, right))

        return {"positions": positions, "error": None}
    except Exception as e:
        return {"positions": [], "error": str(e)}


# ─── Spot fetch job (background, podobne ako FETCH_JOB) ──────────────────────
SPOT_FETCH_JOB: dict = {
    "status": "idle",   # idle | running | done | error
    "result": None,     # dict {ticker: price}
    "error":  None,
}


def fetch_spot_prices_bg(tickers: list[str]) -> None:
    """
    Spustí fetching spot cien v separátnom vlákne.
    Výsledok sa uloží do SPOT_FETCH_JOB["result"].
    Volá sa z UI – vlákno samo beží mimo Streamlit loop.
    """
    import threading

    def _worker():
        SPOT_FETCH_JOB["status"] = "running"
        SPOT_FETCH_JOB["error"]  = None
        SPOT_FETCH_JOB["result"] = None
        try:
            result = _fetch_spot_prices_sync(tickers)
            SPOT_FETCH_JOB["result"] = result
            SPOT_FETCH_JOB["status"] = "done"
        except Exception as exc:
            SPOT_FETCH_JOB["error"]  = str(exc)
            SPOT_FETCH_JOB["status"] = "error"

    t = threading.Thread(target=_worker, daemon=True)
    t.start()


def _fetch_spot_prices_sync(tickers: list[str], timeout: float = 10.0) -> dict[str, float]:
    """
    Interná implementácia – beží v background vlákne.
    Používa reqMktData (snapshot) s manuálnym čakaním namiesto reqTickers
    aby sa predišlo deadlocku v hlavnom Streamlit vlákne.
    """
    ib = _IB_INSTANCE
    if not ib or not ib.isConnected():
        return {}

    try:
        from ib_insync import Stock, util
    except ImportError:
        return {}

    result: dict[str, float] = {}
    contracts = [Stock(tk, "SMART", "USD") for tk in tickers]

    # Prihlás sa na snapshot market data pre každý ticker
    mkt_data_list = []
    for c in contracts:
        try:
            md = ib.reqMktData(c, "", snapshot=True, regulatorySnapshot=False)
            mkt_data_list.append((c.symbol, md))
        except Exception:
            pass

    # Počkaj max timeout sekúnd na príchod dát
    deadline = time.time() + timeout
    while time.time() < deadline:
        all_done = True
        for sym, md in mkt_data_list:
            if sym in result:
                continue
            price = None
            try:
                # Skús marketPrice(), potom last, potom close
                mp = md.marketPrice()
                if mp and not math.isnan(mp) and mp > 0:
                    price = mp
                elif md.last and not math.isnan(float(md.last)) and float(md.last) > 0:
                    price = float(md.last)
                elif md.close and not math.isnan(float(md.close)) and float(md.close) > 0:
                    price = float(md.close)
            except Exception:
                pass
            if price:
                result[sym] = price
            else:
                all_done = False
        if all_done:
            break
        time.sleep(0.5)

    # Zruš market data subscriptions
    for _, md in mkt_data_list:
        try:
            ib.cancelMktData(md.contract)
        except Exception:
            pass

    return result


# ─── Dashboard fetch job (TWS Portfolio stránka; načítanie je sync pod spinnerom) ─
DASHBOARD_FETCH_JOB: dict = {
    "status":    "idle",   # idle | done | error (running sa už nepoužíva)
    "positions": None,
    "orders":    None,
    "account":   None,
    "error":     None,
}

# ─── Account fetch job (background thread, time.sleep – neblokuje UI) ────────
ACCOUNT_FETCH_JOB: dict = {
    "status": "idle",   # idle | running | done | error
    "result": None,
    "error":  None,
}

_ACCOUNT_KEYS_MAP = {
    "AvailableFunds":  "available_funds",
    "NetLiquidation":  "net_liquidation",
    "BuyingPower":     "buying_power",
    "MaintMarginReq":  "maintenance_margin",
    "InitMarginReq":   "initial_margin",
}


def _parse_account_values(values) -> dict:
    """
    Parsuje account values z IBKR do slovníka.
    Priorita meny: BASE > USD > EUR > ostatné (prvý nájdený).
    Funguje pre EUR aj USD účty.
    """
    # Zbierame všetky varianty pre každý tag
    candidates: dict[str, list[tuple[str, float]]] = {}
    for item in (values or []):
        tag = getattr(item, "tag", None)
        cur = getattr(item, "currency", "") or ""
        val = getattr(item, "value", None)
        if tag not in _ACCOUNT_KEYS_MAP:
            continue
        try:
            candidates.setdefault(tag, []).append((cur, float(val)))
        except (ValueError, TypeError):
            pass

    result = {}
    priority = ("BASE", "USD", "EUR")
    detected_currency = "USD"
    for tag, entries in candidates.items():
        chosen_val = None
        chosen_cur = None
        for pref in priority:
            match = next(((c, v) for c, v in entries if c == pref), None)
            if match is not None:
                chosen_cur, chosen_val = match
                break
        if chosen_val is None and entries:
            chosen_cur, chosen_val = entries[0]
        if chosen_val is not None:
            result[_ACCOUNT_KEYS_MAP[tag]] = chosen_val
            if chosen_cur and chosen_cur not in ("BASE", ""):
                detected_currency = chosen_cur
    result["_currency"] = detected_currency
    return result


def fetch_account_summary_bg() -> None:
    """
    Spustí fetch account summary v background vlákne.
    Používa time.sleep() (nie ib.sleep()) – neblokuje Streamlit UI.
    reqAccountUpdates() z background vlákna queuje request na hlavný event loop.
    """
    import threading

    def _worker():
        ACCOUNT_FETCH_JOB["status"] = "running"
        ACCOUNT_FETCH_JOB["error"]  = None
        ACCOUNT_FETCH_JOB["result"] = None
        ib = _IB_INSTANCE
        if not ib or not ib.isConnected():
            ACCOUNT_FETCH_JOB["error"]  = "IBKR nie je pripojené"
            ACCOUNT_FETCH_JOB["status"] = "error"
            return
        try:
            # Skús najprv priamo z cache (ak bola subscription aktívna skôr)
            cached = _parse_account_values(ib.accountValues())
            if cached:
                ACCOUNT_FETCH_JOB["result"] = cached
                ACCOUNT_FETCH_JOB["status"] = "done"
                return

            # Pošli reqAccountUpdates – event loop hlavného vlákna to spracuje
            ib.reqAccountUpdates(True)

            # Čakaj na dáta pomocou time.sleep (neblokuje Streamlit main thread)
            deadline = time.time() + 8.0
            result: dict = {}
            while time.time() < deadline:
                result = _parse_account_values(ib.accountValues())
                if result:
                    break
                time.sleep(0.3)

            ib.reqAccountUpdates(False)
            ACCOUNT_FETCH_JOB["result"] = result
            ACCOUNT_FETCH_JOB["status"] = "done" if result else "error"
            if not result:
                _all_vals = ib.accountValues()
                ACCOUNT_FETCH_JOB["error"] = (
                    f"Žiadne dáta po 8s. accountValues() vrátilo "
                    f"{len(_all_vals)} položiek, "
                    f"tagy: {[getattr(v,'tag','?') for v in _all_vals[:6]]}"
                )
        except Exception as exc:
            ACCOUNT_FETCH_JOB["error"]  = str(exc)
            ACCOUNT_FETCH_JOB["status"] = "error"

    threading.Thread(target=_worker, daemon=True).start()


def fetch_account_summary() -> dict:
    """Synchrónna verzia – len pre kompatibilitu. Použi fetch_account_summary_bg()."""
    ib = _IB_INSTANCE or get_ib()
    if not ib:
        return {}
    return _parse_account_values(ib.accountValues())


def _pos_key(ticker, strike, expiry, leg_type, option_type) -> str:
    """Unikátny kľúč pre porovnanie pozícií (exp vždy YYYYMMDD – rovnako ako v denníku)."""
    from core.portfolio_data import normalize_expiry
    e = normalize_expiry(str(expiry or "")).replace("-", "")
    sk = round(float(strike or 0), 4)
    return f"{ticker}|{sk}|{e}|{leg_type}|{option_type}"


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
            ib_c = float(pos["contracts"])
            db_c = float(t.get("contracts") or 1)
            if abs(ib_c - db_c) > 1e-6:
                # Opčné kontrakty sú vždy celé čísla v DB (INTEGER)
                changes["contracts"] = int(round(ib_c))
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
                contracts=int(round(float(pos["contracts"]))),
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
