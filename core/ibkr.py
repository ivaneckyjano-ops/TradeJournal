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
import math
import re
import socket
import threading
import time
from datetime import date as _date

import streamlit as st
from typing import Optional
from datetime import datetime, date, timedelta


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7496
DEFAULT_CLIENT_ID = 10

# Predvoľby pre UI (Dashboard): rovnaký TWS API socket pre TWS, IB Gateway aj IBKR Desktop —
# port musí presne sedieť s Edit → Global Configuration → API → Socket port v danej aplikácii.
# Ak bežia TWS a IBKR Desktop naraz, v každej nastav iný port a tu zvoľ zodpovedajúcu predvoľbu.
IB_CONNECTION_PRESETS: tuple[tuple[str, int], ...] = (
    ("TWS — live", 7496),
    ("TWS — paper", 7497),
    ("IBKR Desktop", 7498),
    ("IB Gateway — live", 4001),
    ("IB Gateway — paper", 4002),
)


def current_connection_scope() -> str:
    """
    Stabilný identifikátor aktívneho IB pripojenia.

    Používame ho na oddelenie cache v ``st.session_state``, aby sa live a paper
    údaje navzájom neprepisovali pri prepnutí portu / klienta.
    """
    host = str(st.session_state.get("ib_host") or DEFAULT_HOST).strip().lower()
    try:
        port = int(st.session_state.get("ib_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        client_id = int(st.session_state.get("ib_cid") or DEFAULT_CLIENT_ID)
    except (TypeError, ValueError):
        client_id = DEFAULT_CLIENT_ID
    return f"{host}:{port}:cid{client_id}"


def scoped_session_key(base_key: str) -> str:
    """Vráti názov session_state kľúča pre aktuálny IB scope."""
    return f"{base_key}__{current_connection_scope()}"


def current_connection_label() -> str:
    """Ľudsky čitateľný popis aktívneho IB pripojenia."""
    host = str(st.session_state.get("ib_host") or DEFAULT_HOST).strip() or DEFAULT_HOST
    try:
        port = int(st.session_state.get("ib_port") or DEFAULT_PORT)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    try:
        client_id = int(st.session_state.get("ib_cid") or DEFAULT_CLIENT_ID)
    except (TypeError, ValueError):
        client_id = DEFAULT_CLIENT_ID
    preset = next((lbl for lbl, prt in IB_CONNECTION_PRESETS if int(prt) == port), "Vlastné")
    return f"{preset} · {host}:{port} · cid {client_id}"


def get_scoped_session_value(base_key: str, default=None):
    """Načíta hodnotu zo scope-kľúča pre aktuálne IB pripojenie."""
    return st.session_state.get(scoped_session_key(base_key), default)


def set_scoped_session_value(base_key: str, value) -> None:
    """Zapíše hodnotu do scope-kľúča pre aktuálne IB pripojenie."""
    st.session_state[scoped_session_key(base_key)] = value

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
    """
    Vráti IB inštanciu.

    Priorita: ``st.session_state['ib']`` (zdroj pravdy pri Streamlit rerunoch),
    potom module-level cache. Opačné poradie vedie k tomu, že sidebar v
    ``streamlit_app.py`` beží pred ``pg.run()`` a mohol čítať zastaralý
    singleton skôr, než sa obnovil odkaz zo session.
    """
    global _IB_INSTANCE
    ss_ib = st.session_state.get("ib")
    if ss_ib is not None:
        _IB_INSTANCE = ss_ib
        return ss_ib
    if _IB_INSTANCE is not None:
        try:
            st.session_state["ib"] = _IB_INSTANCE
        except Exception:
            pass
        return _IB_INSTANCE
    return None


def is_connected() -> bool:
    ib = get_ib()
    connected = False
    if ib is not None:
        try:
            connected = bool(ib.isConnected())
        except Exception:
            connected = False

    # Udržuj jeden stabilný zdroj pravdy pre UI medzi rerunmi Streamlitu.
    try:
        st.session_state["ib_connected"] = connected
    except Exception:
        pass
    return connected


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
        hint_7498 = ""
        if int(port) == 7498:
            hint_7498 = (
                "Číslo 7498 je len odporúčaná predvoľba v TradeJournal — v IBKR Desktop (alebo TWS) "
                "musíš v Global Configuration → API nastaviť rovnaký „Socket port“ a reštartovať klienta. "
                "Ak tam API ešte nemáš zmenené, skús predvolené TWS: live 7496 alebo paper 7497. "
            )
        return (
            False,
            f"Na {host}:{port} sa nedá pripojiť (TCP). Spusti TWS, IB Gateway alebo IBKR Desktop "
            f"a skontroluj, že v jeho API nastaveniach je rovnaký socket port ako tu. "
            f"{hint_7498}"
            f"Bežné porty: TWS live 7496, paper 7497, Gateway 4001/4002. "
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
            st.session_state["ib_connected"] = True
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
    st.session_state["ib_connected"] = False


# ─── Market data ──────────────────────────────────────────────────────────────

def fetch_underlying(ticker: str, timeout: float = 10.0) -> dict:
    """
    Vráti aktuálnu cenu podkladového aktíva.
    Preferuje snapshot/stream z IB; fallback je historický posledný bar.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"price": None, "ticker": ticker, "error": "Nie je pripojenie na IBKR"}

    ib_loop = getattr(ib, "_loop", None) or _MAIN_LOOP
    if ib_loop is None or getattr(ib_loop, "is_closed", lambda: False)():
        return {"price": None, "ticker": ticker, "error": "Chýba IB event loop (odpoj/pripoj TWS)"}

    prev_loop = None
    try:
        prev_loop = asyncio.get_event_loop()
    except RuntimeError:
        prev_loop = None

    asyncio.set_event_loop(ib_loop)
    try:
        _, Stock, _ = _ib_ready()
        stock = Stock(ticker, "SMART", "USD")
        q = ib.qualifyContracts(stock)
        if not q:
            return {"price": None, "ticker": ticker, "error": f"Underlying {ticker} nenájdený"}
        stock = q[0]

        def _valid(v) -> float | None:
            try:
                f = float(v)
                return f if f and not math.isnan(f) and f > 0 else None
            except Exception:
                return None

        # 1) Snapshot/live pokusy (live/delayed/frozen)
        per_try = max(1.5, float(timeout) / 4.0)
        for mdt in (1, 3, 2, 4):
            try:
                ib.reqMarketDataType(mdt)
                tkr = ib.reqMktData(stock, "106", True, False)
                deadline = time.time() + per_try
                found = None
                while time.time() < deadline and not found:
                    found = _valid(getattr(tkr, "last", None))
                    if not found:
                        found = _valid(getattr(tkr, "close", None))
                    if not found:
                        bid = _valid(getattr(tkr, "bid", None))
                        ask = _valid(getattr(tkr, "ask", None))
                        if bid and ask:
                            found = round((bid + ask) / 2.0, 2)
                    if not found:
                        found = _valid(tkr.marketPrice())
                    if not found:
                        ib.sleep(0.1)
                ib.cancelMktData(stock)
                if found:
                    return {"price": float(found), "ticker": ticker, "error": None, "source": f"snapshot mdt={mdt}"}
            except Exception:
                continue

        # 2) Historický fallback (často funguje aj keď snapshot nie)
        for dur, what, bar_size in [
            ("300 S", "TRADES", "1 min"),
            ("300 S", "MIDPOINT", "1 min"),
            ("1 D", "TRADES", "1 min"),
            ("1 D", "MIDPOINT", "1 min"),
            ("2 D", "TRADES", "1 day"),
            ("2 D", "MIDPOINT", "1 day"),
            ("5 D", "TRADES", "1 day"),
            ("5 D", "MIDPOINT", "1 day"),
        ]:
            try:
                bars = ib.reqHistoricalData(
                    stock,
                    endDateTime="",
                    durationStr=dur,
                    barSizeSetting=bar_size,
                    whatToShow=what,
                    useRTH=False,
                    formatDate=1,
                    timeout=min(max(float(timeout), 6.0), 20.0),
                )
                if bars:
                    px = _valid(getattr(bars[-1], "close", None))
                    if px:
                        return {"price": float(px), "ticker": ticker, "error": None, "source": f"hist {what.lower()}"}
            except Exception:
                continue

        # 3) Posledné záchranné lano – portfólio cena
        try:
            for item in ib.portfolio():
                if item.contract.symbol == ticker and item.contract.secType == "STK":
                    p = _valid(item.marketPrice)
                    if p:
                        return {"price": float(p), "ticker": ticker, "error": None, "source": "portfolio fallback"}
        except Exception:
            pass

        return {"price": None, "ticker": ticker, "error": "Spot nedostupný (snapshot aj historical bez dát)"}
    finally:
        if prev_loop is not None and not getattr(prev_loop, "is_closed", lambda: False)():
            try:
                asyncio.set_event_loop(prev_loop)
            except Exception:
                pass


def fetch_underlying_previous_close(ticker: str, timeout: float = 10.0) -> dict:
    """
    Vráti uzatváraciu cenu podkladu z posledného ukončeného dňa.
    Používa historické dáta, nie live snapshot.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"price": None, "ticker": ticker, "error": "Nie je pripojenie na IBKR"}

    ib_loop = getattr(ib, "_loop", None) or _MAIN_LOOP
    if ib_loop is None or getattr(ib_loop, "is_closed", lambda: False)():
        return {"price": None, "ticker": ticker, "error": "Chýba IB event loop (odpoj/pripoj TWS)"}

    prev_loop = None
    try:
        prev_loop = asyncio.get_event_loop()
    except RuntimeError:
        prev_loop = None

    asyncio.set_event_loop(ib_loop)
    try:
        _, Stock, _ = _ib_ready()
        stock = Stock((ticker or "").strip().upper(), "SMART", "USD")
        q = ib.qualifyContracts(stock)
        if not q:
            return {"price": None, "ticker": ticker, "error": f"Underlying {ticker} nenájdený"}
        stock = q[0]

        def _valid(v) -> float | None:
            try:
                f = float(v)
                return f if f and not math.isnan(f) and f > 0 else None
            except Exception:
                return None

        for dur, what, bar_size in [
            ("2 D", "TRADES", "1 day"),
            ("2 D", "MIDPOINT", "1 day"),
            ("5 D", "TRADES", "1 day"),
            ("5 D", "MIDPOINT", "1 day"),
            ("10 D", "TRADES", "1 day"),
            ("10 D", "MIDPOINT", "1 day"),
            ("2 D", "TRADES", "1 hour"),
            ("2 D", "MIDPOINT", "1 hour"),
        ]:
            try:
                bars = ib.reqHistoricalData(
                    stock,
                    endDateTime="",
                    durationStr=dur,
                    barSizeSetting=bar_size,
                    whatToShow=what,
                    useRTH=True,
                    formatDate=1,
                    timeout=min(max(float(timeout), 6.0), 20.0),
                )
                if bars:
                    px = _valid(getattr(bars[-1], "close", None))
                    if px:
                        return {"price": float(px), "ticker": ticker, "error": None, "source": f"prev_close {what.lower()}"}
            except Exception:
                continue
        return {"price": None, "ticker": ticker, "error": "Historický close podkladu nedostupný"}
    finally:
        if prev_loop is not None and not getattr(prev_loop, "is_closed", lambda: False)():
            try:
                asyncio.set_event_loop(prev_loop)
            except Exception:
                pass


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


def _ib_expiry_compact(expiry: str) -> str:
    """IB Option kontrakt očakáva YYYYMMDD; ak príde YYYY-MM-DD, znormalizuje."""
    s = str(expiry or "").strip().split()[0]
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        return s[:4] + s[5:7] + s[8:10]
    if len(s) == 8 and s.isdigit():
        return s
    s2 = s.replace("-", "")
    return s2[:8] if len(s2) >= 8 else s2


def fetch_iv(ticker: str, expiry: str, strike: float, right: str = "C") -> dict:
    """Načíta IV pre konkrétny opčný kontrakt (na ``ib._loop`` + skúška live/delayed)."""
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"iv": None, "und_price": None, "error": "Nie je pripojenie na IBKR"}

    sym = (ticker or "").strip().upper()
    rr = (right or "C").upper()[:1]
    if rr not in ("C", "P"):
        rr = "C"
    expiry_ib = _ib_expiry_compact(expiry)

    ib_loop = getattr(ib, "_loop", None) or _MAIN_LOOP
    if ib_loop is None or getattr(ib_loop, "is_closed", lambda: False)():
        return {"iv": None, "und_price": None, "error": "Chýba IB event loop"}

    prev_loop = None
    try:
        prev_loop = asyncio.get_event_loop()
    except RuntimeError:
        prev_loop = None

    asyncio.set_event_loop(ib_loop)
    try:
        _ib_ready()
        from ib_insync import Option as IBOption

        opt = IBOption(sym, expiry_ib, float(strike), rr, "SMART", currency="USD")
        q = ib.qualifyContracts(opt)
        if not q:
            return {"iv": None, "und_price": None, "error": f"Kontrakt {sym} nenájdený"}
        opt = q[0]
        last_err = "Greeks nedostupné"
        for mdt in (1, 3, 2, 4):
            ib.reqMarketDataType(mdt)
            try:
                [tk] = ib.reqTickers(opt)
            except Exception as e:
                last_err = str(e)
                continue
            for _wait in range(5):
                ib.sleep(0.22)
                g = tk.modelGreeks or tk.bidGreeks or tk.askGreeks
                if g is not None and getattr(g, "impliedVol", None) is not None:
                    try:
                        ivv = float(g.impliedVol)
                        if ivv == ivv and ivv > 0:
                            return {
                                "iv": g.impliedVol,
                                "und_price": g.undPrice,
                                "error": None,
                            }
                    except (TypeError, ValueError):
                        pass
        return {"iv": None, "und_price": None, "error": last_err}
    except Exception as e:
        return {"iv": None, "und_price": None, "error": str(e)}
    finally:
        if prev_loop is not None and not getattr(prev_loop, "is_closed", lambda: False)():
            try:
                asyncio.set_event_loop(prev_loop)
            except Exception:
                pass


def fetch_secdef_option_params(ticker: str, timeout: float = 14.0) -> dict:
    """
    Zoznam opčných reťazcov (expirácie, striky) pre akciu — ``reqSecDefOptParams``.

    ``ib_insync`` používa ``asyncio.get_event_loop()`` (``util.getLoop()``). Ak Streamlit /
    Python 3.12 nastaví iný loop ako ``ib._loop`` z ``connect()``, ``reqSecDefOptParams``
    skončí s prázdnym výsledkom. Preto tu dočasne viažeme aktuálne vlákno na ``ib._loop``.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"chains": [], "error": "Nie je pripojenie na IBKR"}
    sym = (ticker or "").strip().upper()
    result: dict = {"chains": [], "error": None}

    ib_loop = getattr(ib, "_loop", None) or _MAIN_LOOP
    if ib_loop is None or getattr(ib_loop, "is_closed", lambda: False)():
        return {
            "chains": [],
            "error": "Chýba IB event loop (ib._loop) — odpoj sa a znova pripoj TWS.",
        }

    prev_loop = None
    try:
        prev_loop = asyncio.get_event_loop()
    except RuntimeError:
        prev_loop = None

    asyncio.set_event_loop(ib_loop)
    old_timeout = None
    try:
        _ib_ready()
        from ib_insync import Stock as IBStock

        old_timeout = getattr(ib, "RequestTimeout", None)
        try:
            ib.RequestTimeout = int(max(8, min(float(timeout), 60.0)))
        except Exception:
            pass
        try:
            stc = IBStock(sym, "SMART", "USD")
            qualified = ib.qualifyContracts(stc)
            if not qualified:
                result["error"] = f"Underlying {sym} nenájdený"
                return result
            und = qualified[0]
            uid = und.conId
            sec_type = getattr(und, "secType", None) or "STK"
            chains = ib.reqSecDefOptParams(sym, "", sec_type, uid)
            out = []
            for c in chains or []:
                exps = sorted(getattr(c, "expirations", []) or [])
                strikes = sorted({float(x) for x in (getattr(c, "strikes", []) or [])})
                out.append({
                    "exchange": getattr(c, "exchange", ""),
                    "trading_class": getattr(c, "tradingClass", ""),
                    "expirations": exps,
                    "strikes": strikes,
                })
            merged_exp: set[str] = set()
            merged_str: set[float] = set()
            for row in out:
                merged_exp.update(row.get("expirations") or [])
                merged_str.update(row.get("strikes") or [])
            if merged_exp:
                out.insert(0, {
                    "exchange": "MERGED",
                    "trading_class": "",
                    "expirations": sorted(merged_exp),
                    "strikes": sorted(merged_str),
                })
            result["chains"] = out
        except Exception as e:
            result["error"] = str(e)
        finally:
            if old_timeout is not None:
                try:
                    ib.RequestTimeout = old_timeout
                except Exception:
                    pass
    finally:
        if prev_loop is not None and not getattr(prev_loop, "is_closed", lambda: False)():
            try:
                asyncio.set_event_loop(prev_loop)
            except Exception:
                pass

    return result


def fetch_option_scan_metrics(ticker: str, expiry: str, strike: float, right: str, timeout: float = 12.0) -> dict:
    """
    Bid/ask/mid, spread % mid, open interest, IV a theta z modelGreeks (tick 101) pre jeden kontrakt.
    Beží na ``ib._loop`` (rovnako ako ``fetch_secdef_option_params``) — vlákno + cudzí loop
    často nevráti ticky ani pri otvorenom trhu.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"error": "Nie je pripojenie na IBKR"}
    sym = (ticker or "").strip().upper()
    r = (right or "C").upper()[:1]
    if r not in ("C", "P"):
        r = "C"

    def _sf(v):
        try:
            f = float(v)
            return f if not math.isnan(f) and f > 0 else None
        except Exception:
            return None

    def _tick_ok(t_obj) -> bool:
        return bool(_sf(getattr(t_obj, "bid", None)) or _sf(getattr(t_obj, "ask", None)) or _sf(getattr(t_obj, "last", None)))

    def _fill_from_ticker(t_obj, out: dict) -> None:
        bid = _sf(t_obj.bid)
        ask = _sf(t_obj.ask)
        last = _sf(t_obj.last)
        greeks = t_obj.modelGreeks or t_obj.bidGreeks or t_obj.askGreeks
        iv_raw = getattr(greeks, "impliedVol", None) if greeks is not None else None
        und_price = getattr(greeks, "undPrice", None) if greeks is not None else None
        theta_raw = getattr(greeks, "theta", None) if greeks is not None else None
        delta_raw = getattr(greeks, "delta", None) if greeks is not None else None
        oi = getattr(t_obj, "openInterest", None)
        if oi is not None:
            try:
                oi = int(oi)
            except (TypeError, ValueError):
                oi = None
        mid = None
        if bid and ask:
            mid = round((bid + ask) / 2.0, 4)
        elif last:
            mid = round(last, 4)
        spread_pct = None
        if bid and ask and mid and mid > 0:
            spread_pct = round((ask - bid) / mid * 100.0, 3)
        th = None
        if theta_raw is not None:
            try:
                tf = float(theta_raw)
                if not math.isnan(tf):
                    th = round(tf, 5)
            except (TypeError, ValueError):
                pass
        dl = None
        if delta_raw is not None:
            try:
                df = float(delta_raw)
                if not math.isnan(df):
                    dl = round(df, 6)
            except (TypeError, ValueError):
                pass
        ga_raw = getattr(greeks, "gamma", None) if greeks is not None else None
        ve_raw = getattr(greeks, "vega", None) if greeks is not None else None
        gam = None
        if ga_raw is not None:
            try:
                gf = float(ga_raw)
                if not math.isnan(gf):
                    gam = round(gf, 6)
            except (TypeError, ValueError):
                pass
        veg = None
        if ve_raw is not None:
            try:
                vf = float(ve_raw)
                if not math.isnan(vf):
                    veg = round(vf, 5)
            except (TypeError, ValueError):
                pass
        out.update({
            "ticker": sym,
            "expiry": expiry,
            "strike": float(strike),
            "right": r,
            "bid": bid,
            "ask": ask,
            "last": last,
            "mid": mid,
            "realized_fill_price": mid or last,
            "spread_pct_mid": spread_pct,
            "open_interest": oi,
            "iv": _sf(iv_raw),
            "delta": dl,
            "theta": th,
            "gamma": gam,
            "vega": veg,
            "und_price": _sf(und_price),
            "error": None if (bid or ask or last) else "Žiadna cena (trh/market data)",
        })

    ib_loop = getattr(ib, "_loop", None) or _MAIN_LOOP
    if ib_loop is None or getattr(ib_loop, "is_closed", lambda: False)():
        return {"error": "Chýba IB event loop (odpoj/pripoj TWS)"}

    prev_loop = None
    try:
        prev_loop = asyncio.get_event_loop()
    except RuntimeError:
        prev_loop = None

    result: dict = {"error": None}
    asyncio.set_event_loop(ib_loop)
    old_timeout = None
    try:
        _ib_ready()
        from ib_insync import Option as IBOption

        old_timeout = getattr(ib, "RequestTimeout", None)
        try:
            ib.RequestTimeout = int(max(8, min(float(timeout), 30.0)))
        except Exception:
            pass

        expiry_ib = _ib_expiry_compact(expiry)
        opt = IBOption(sym, expiry_ib, float(strike), r, "SMART", currency="USD")
        qualified = ib.qualifyContracts(opt)
        if not qualified:
            return {"error": f"Kontrakt {sym} {expiry} ${strike} {r} nenájdený"}
        opt = qualified[0]

        per_pass = max(2.5, float(timeout) / 4.0)
        got = False
        for snapshot in (True, False):
            for mdt in (1, 3, 2, 4):
                ib.reqMarketDataType(mdt)
                t_obj = ib.reqMktData(opt, "101", snapshot, False)
                deadline = time.time() + per_pass
                while time.time() < deadline and not _tick_ok(t_obj):
                    ib.sleep(0.12)
                ib.cancelMktData(opt)
                if _tick_ok(t_obj):
                    _fill_from_ticker(t_obj, result)
                    got = True
                    break
            if got:
                break

        if not got:
            result.update({
                "ticker": sym,
                "expiry": expiry,
                "strike": float(strike),
                "right": r,
                "bid": None,
                "ask": None,
                "last": None,
                "mid": None,
                "realized_fill_price": None,
                "spread_pct_mid": None,
                "open_interest": None,
                "iv": None,
                "delta": None,
                "theta": None,
                "gamma": None,
                "vega": None,
                "und_price": None,
                "error": "Žiadna cena (bid/ask/last) — skús iný typ dát v TWS alebo kontrakt",
            })
    except Exception as e:
        return {"error": str(e)}
    finally:
        if old_timeout is not None:
            try:
                ib.RequestTimeout = old_timeout
            except Exception:
                pass
        if prev_loop is not None and not getattr(prev_loop, "is_closed", lambda: False)():
            try:
                asyncio.set_event_loop(prev_loop)
            except Exception:
                pass

    return result


def fetch_option_historical_last(ticker: str, expiry: str, strike: float, right: str, timeout: float = 10.0) -> dict:
    """
    Fallback: historický bar pre opčný kontrakt.
    Skúša viac kombinácií (intraday aj denné bary), aby fungoval aj tam,
    kde IBKR nevracia 1-min históriu.
    Vracia {"last": float} alebo {"error": str}.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"error": "Nie je pripojenie na IBKR"}
    sym = (ticker or "").strip().upper()
    r = (right or "C").upper()[:1]
    if r not in ("C", "P"):
        r = "C"

    ib_loop = getattr(ib, "_loop", None) or _MAIN_LOOP
    if ib_loop is None or getattr(ib_loop, "is_closed", lambda: False)():
        return {"error": "Chýba IB event loop"}

    prev_loop = None
    try:
        prev_loop = asyncio.get_event_loop()
    except RuntimeError:
        prev_loop = None

    asyncio.set_event_loop(ib_loop)
    try:
        _ib_ready()
        from ib_insync import Option as IBOption

        expiry_ib = _ib_expiry_compact(expiry)
        opt = IBOption(sym, expiry_ib, float(strike), r, "SMART", currency="USD")
        qualified = ib.qualifyContracts(opt)
        if not qualified:
            return {"error": f"Kontrakt {sym} {expiry} ${strike} {r} nenájdený"}
        opt = qualified[0]

        for dur, what, bar_size in [
            ("300 S", "TRADES", "1 min"),
            ("300 S", "MIDPOINT", "1 min"),
            ("300 S", "BID_ASK", "1 min"),
            ("1 D", "TRADES", "1 min"),
            ("1 D", "MIDPOINT", "1 min"),
            ("1 D", "BID_ASK", "1 min"),
            ("2 D", "TRADES", "1 day"),
            ("2 D", "MIDPOINT", "1 day"),
            ("2 D", "BID_ASK", "1 day"),
            ("5 D", "TRADES", "1 day"),
            ("5 D", "MIDPOINT", "1 day"),
            ("5 D", "BID_ASK", "1 day"),
        ]:
            try:
                bars = ib.reqHistoricalData(
                    opt,
                    endDateTime="",
                    durationStr=dur,
                    barSizeSetting=bar_size,
                    whatToShow=what,
                    useRTH=False,
                    formatDate=1,
                    timeout=min(max(float(timeout), 5.0), 15.0),
                )
                if bars:
                    last_bar = bars[-1]
                    close_px = getattr(last_bar, "close", None)
                    if close_px is not None and not math.isnan(close_px) and close_px > 0:
                        return {"last": round(float(close_px), 4)}
            except Exception:
                continue
        return {"error": "Historické dáta nedostupné"}
    except Exception as e:
        return {"error": str(e)}
    finally:
        if prev_loop is not None and not getattr(prev_loop, "is_closed", lambda: False)():
            try:
                asyncio.set_event_loop(prev_loop)
            except Exception:
                pass


def _parse_ib_margin_value(v) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
        if not math.isnan(f):
            return f
    except (TypeError, ValueError):
        pass
    s = str(v).replace(",", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if m:
        try:
            return float(m.group(0))
        except ValueError:
            return None
    return None


def fetch_spread_whatif_margin(ticker: str, legs: list[dict]) -> dict:
    """
    Požiada IB o what-if maržu pre BAG combo z nôh Spread Builderu (otvorenie pozície).

    Vráti initial_margin, maintenance_margin (USD), prípadne error.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"error": "Nie je pripojenie na IBKR", "initial_margin": None, "maintenance_margin": None}

    if not legs:
        return {"error": "Žiadne nohy", "initial_margin": None, "maintenance_margin": None}
    if len(legs) < 2:
        return {
            "error": "What-if combo vyžaduje aspoň 2 nohy (BAG). Pri jednej nohe použij TWS.",
            "initial_margin": None,
            "maintenance_margin": None,
        }

    sym = (ticker or "").strip().upper()
    if not sym:
        return {"error": "Prázdny ticker", "initial_margin": None, "maintenance_margin": None}

    result: dict = {
        "error": None,
        "initial_margin": None,
        "maintenance_margin": None,
        "order_action": None,
        "combo_quantity": None,
    }
    done = threading.Event()

    def _worker():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            import nest_asyncio

            nest_asyncio.apply(loop)
            from ib_insync import ComboLeg, Contract, MarketOrder, Option as IBOption

            ratios = [max(1, int(leg.get("contracts", 1) or 1)) for leg in legs]
            from functools import reduce

            g_all = reduce(math.gcd, ratios)
            combo_qty = max(1, g_all)
            norm_ratios = [r // g_all for r in ratios]

            combo_legs = []
            for leg, ratio in zip(legs, norm_ratios):
                exp = str(leg.get("expiry") or "")
                strike = float(leg.get("strike") or 0)
                right = str(leg.get("right") or "C")[:1]
                if len(exp) < 8 or strike <= 0 or right not in ("C", "P"):
                    result["error"] = f"Neplatná noha: {leg}"
                    return
                opt = IBOption(sym, exp, strike, right, "SMART", currency="USD")
                ib.qualifyContracts(opt)
                if not getattr(opt, "conId", None):
                    result["error"] = f"Kontrakt nenájdený: {sym} {exp} {strike} {right}"
                    return
                action = "BUY" if str(leg.get("leg_type")) == "Long" else "SELL"
                combo_legs.append(
                    ComboLeg(
                        conId=opt.conId,
                        ratio=int(ratio),
                        action=action,
                        exchange="SMART",
                        openClose=0,
                        shortSaleSlot=0,
                        designatedLocation="",
                        exemptCode=-1,
                    )
                )

            bag = Contract(symbol=sym, secType="BAG", currency="USD", exchange="SMART", comboLegs=combo_legs)
            ib.qualifyContracts(bag)

            net = sum(
                (
                    -float(leg.get("entry_price") or 0)
                    if str(leg.get("leg_type")) == "Long"
                    else float(leg.get("entry_price") or 0)
                )
                * max(1, int(leg.get("contracts", 1) or 1))
                * 100
                for leg in legs
            )
            order_action = "SELL" if net >= 0 else "BUY"
            result["order_action"] = order_action
            result["combo_quantity"] = int(combo_qty)

            order = MarketOrder(order_action, int(combo_qty))
            order.whatIf = True
            order.transmit = False

            trade = ib.placeOrder(bag, order)
            for _ in range(60):
                time.sleep(0.25)
                stt = trade.orderStatus
                if stt:
                    ini = _parse_ib_margin_value(getattr(stt, "initMarginBefore", None))
                    mai = _parse_ib_margin_value(getattr(stt, "maintMarginBefore", None))
                    if ini is not None or mai is not None:
                        result["initial_margin"] = ini
                        result["maintenance_margin"] = mai
                        break
                    if getattr(stt, "status", None) in ("Cancelled", "Inactive") and getattr(
                        stt, "whyHeld", ""
                    ):
                        break
            try:
                ib.cancelOrder(order)
            except Exception:
                pass

            if result["initial_margin"] is None and result["maintenance_margin"] is None:
                msg = getattr(trade.orderStatus, "warningText", None) or getattr(
                    trade.orderStatus, "whyHeld", None
                )
                result["error"] = (
                    msg
                    or "IB nevrátil maržu (what-if). Skús znova alebo TWS Margin Impact."
                )
        except Exception as e:
            result["error"] = str(e)
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    done.wait(timeout=25)

    if not result.get("error") and result["initial_margin"] is None and result["maintenance_margin"] is None:
        result["error"] = result.get("error") or "Timeout alebo prázdna odpoveď z IB."
    elif result.get("initial_margin") is not None or result.get("maintenance_margin") is not None:
        result["error"] = None

    return result


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
        # Nepoužívaj ib.reqPositions() pred portfolio(): v niektorých verziách API to vyprázdni cache
        # a portfolio() vráti [] do ďalšieho update cyklu → používateľ „nemá nič“.
        try:
            ib.sleep(0.15)
        except Exception:
            time.sleep(0.15)

        raw = ib.portfolio()
        if not raw:
            try:
                ib.sleep(0.65)
            except Exception:
                time.sleep(0.65)
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

        # Podklad pre Greeks: prvý STK (po snapshot), alebo spot z prvého podkladového tickeru OPT (bez STK v účte)
        under_price: float | None = None
        for p in positions:
            if p.get("sec_type") == "STK":
                mp = p.get("market_price")
                if mp is not None and not math.isnan(float(mp)) and float(mp) > 0:
                    under_price = float(mp)
                    break

        if with_greeks and under_price is None:
            seen_syms: list[str] = []
            for p in positions:
                if p.get("sec_type") not in ("OPT", "FOP"):
                    continue
                sym = str(p.get("ticker") or "").strip().upper()
                if sym and sym not in seen_syms:
                    seen_syms.append(sym)
            # Len prvý ticker — vyhneme sa dlhému blokovaniu pri každom načítaní stránky.
            for sym in seen_syms[:1]:
                try:
                    sp = fetch_underlying(sym, timeout=5.0)
                    px = sp.get("price")
                    if px is not None and float(px) > 0:
                        under_price = float(px)
                        break
                except Exception:
                    continue

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
    """
    Rovnaká normalizácia ako ``journal_position_key`` (poradie: ticker, strike, exp, typ opcie, noha).
    Denník ↔ TWS ↔ sync musia používať jeden kľúč.
    """
    from core.portfolio_data import journal_position_key

    t = journal_position_key(ticker, strike, expiry, option_type, leg_type)
    return "|".join(str(x) for x in t)


def sync_positions_to_db(positions: list[dict], db_module, *, close_missing: bool = False) -> dict:
    """
    Porovná IBKR pozície s DB.

    1. Pridá nové pozície.
    2. Aktualizuje contracts + avg_cost pre existujúce.
    3. Voliteľne uzavrie Open pozície, ktoré v IBKR chýbajú.

    `close_missing=True` je vhodné pri ručnom importe z Dashboardu.
    `close_missing=False` necháva chýbajúce riadky otvorené.
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

    added = updated = skipped = closed = 0
    closed_trades: list[dict] = []

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
            closed_trades.append({
                "id": t["id"],
                "ticker": t["ticker"],
                "leg_type": t.get("leg_type"),
                "option_type": t.get("option_type"),
                "strike": t.get("strike"),
                "expiry": t.get("expiry"),
                "entry_price": t.get("entry_price"),
            })
            if close_missing:
                db_module.update_trade(
                    t["id"],
                    status="Closed",
                    exit_date=date.today().isoformat(),
                )
                closed += 1

    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "closed": closed,
        "closed_trades": closed_trades,
    }


def fetch_fills() -> dict:
    """
    Načíta vykonané obchady (fills) z IBKR vrátane komisií.

    Kombinuje ``reqExecutions()`` (dvakrát s krátkym ``ib.sleep`` medzi volaniami — pri prvom
    pripojení často pomôže „druhý pokus“) so zlúčením ``ib.fills()`` z aktuálnej relácie.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {
            "fills": [],
            "error": "Nie je pripojenie na IBKR",
            "raw_fill_count": 0,
            "non_option_fill_count": 0,
        }
    _ib_ready()
    try:
        from ib_insync import ExecutionFilter

        seen_exec: set[str] = set()
        raw: list = []

        def _add_fill_rows(rows):
            for f in rows or []:
                ex = getattr(f, "execution", None)
                eid = str(getattr(ex, "execId", None) or "")
                key = eid if eid else f"_id_{id(f)}"
                if key in seen_exec:
                    continue
                seen_exec.add(key)
                raw.append(f)

        def _req_exec_once():
            try:
                _add_fill_rows(ib.reqExecutions(ExecutionFilter()))
            except Exception:
                try:
                    _add_fill_rows(ib.reqExecutions())
                except Exception:
                    pass

        _req_exec_once()
        try:
            ib.sleep(0.4)
        except Exception:
            time.sleep(0.4)
        _req_exec_once()
        _add_fill_rows(list(ib.fills() or []))

        non_opt = sum(
            1
            for f in raw
            if getattr(getattr(f, "contract", None), "secType", None) != "OPT"
        )

        commission_map: dict[str, float] = {}
        for f in raw:
            rpt = getattr(f, "commissionReport", None)
            if rpt:
                eid = getattr(rpt, "execId", None) or getattr(f.execution, "execId", None)
                if eid and rpt.commission not in (None, 1.7976931348623157e+308, float("inf")):
                    try:
                        commission_map[str(eid)] = float(rpt.commission)
                    except (TypeError, ValueError):
                        pass

        result = []
        for f in raw:
            c = f.contract
            if c.secType != "OPT":
                continue
            ex = f.execution
            side = ex.side.upper()
            comm = commission_map.get(str(ex.execId), 0.0)
            exp_raw = str(getattr(c, "lastTradeDateOrContractMonth", "") or "").strip()
            if len(exp_raw) >= 8 and exp_raw[:8].isdigit():
                exp_norm = exp_raw[:8]
            else:
                from core.portfolio_data import normalize_expiry

                exp_norm = normalize_expiry(exp_raw).replace("-", "")[:8]
            result.append(
                {
                    "ticker": c.symbol,
                    "option_type": "Call" if c.right == "C" else "Put",
                    "strike": float(c.strike),
                    "expiry": exp_norm,
                    "contracts": int(abs(ex.shares)),
                    "leg_type": "Long" if side == "BOT" else "Short",
                    "entry_price": float(ex.price),
                    "realized_fill_price": float(ex.price),
                    "entry_date": (
                        (ex.time.strftime("%Y-%m-%d") if hasattr(ex.time, "strftime") else str(ex.time)[:10])
                        if ex.time
                        else datetime.today().strftime("%Y-%m-%d")
                    ),
                    "exec_id": ex.execId,
                    "side": side,
                    "account": ex.acctNumber,
                    "commission": comm,
                }
            )
        return {
            "fills": result,
            "error": None,
            "raw_fill_count": len(raw),
            "non_option_fill_count": non_opt,
        }
    except Exception as e:
        return {
            "fills": [],
            "error": str(e),
            "raw_fill_count": 0,
            "non_option_fill_count": 0,
        }


def sync_fills_to_db(fills: list[dict], db_module) -> dict:
    """
    Importuje fills do DB.
    - BOT fill + existujúca Open Short pozícia → uzavrie ju (close).
    - SLD fill + existujúca Open Long  pozícia → uzavrie ju (close).
    - Ostatné fills pridá ako nové obchody (ak ešte neexistujú).

    Poznámka: ex.shares je vždy kladné, preto sa leg_type nedá odvodiť zo znamienka.
    Namiesto toho porovnáme fill priamo s otvorenými pozíciami v DB.
    """
    from core.portfolio_data import normalize_expiry

    def _canon_expiry(exp) -> str:
        s = str(exp or "").strip().split()[0]
        if len(s) >= 8 and s[:8].isdigit():
            return s[:8]
        try:
            return normalize_expiry(s).replace("-", "")[:8]
        except Exception:
            return s.replace("-", "")[:8]

    def _same_opt_type(a, b) -> bool:
        ca = str(a or "").strip().lower().startswith("c")
        cb = str(b or "").strip().lower().startswith("c")
        return ca == cb

    def _strike_eq(a, b) -> bool:
        try:
            return abs(float(a or 0) - float(b or 0)) < 1e-4
        except (TypeError, ValueError):
            return str(a) == str(b)

    def _fill_matches_leg(t: dict, fill: dict, leg: str, *, require_open: bool = True) -> bool:
        st_ok = str(t.get("status") or "") == "Open" if require_open else True
        return (
            str(t.get("ticker") or "").upper() == str(fill.get("ticker") or "").upper()
            and _strike_eq(t.get("strike"), fill.get("strike"))
            and _canon_expiry(t.get("expiry")) == _canon_expiry(fill.get("expiry"))
            and _same_opt_type(t.get("option_type"), fill.get("option_type"))
            and str(t.get("leg_type") or "") == leg
            and st_ok
        )

    existing = db_module.get_all_trades()
    open_trades = [t for t in existing if t.get("status") == "Open"]
    added = skipped = closed = 0

    for fill in fills:
        side = fill.get("side", "").upper()   # "BOT" alebo "SLD"

        # Určíme, aký typ otvorenej pozície by tento fill UZATVÁRAL
        # BOT uzatvára Short; SLD uzatvára Long
        close_leg = "Short" if side == "BOT" else "Long"

        # Pokús sa nájsť zodpovedajúcu Open pozíciu na uzavretie
        target = next((t for t in open_trades if _fill_matches_leg(t, fill, close_leg, require_open=True)), None)

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
            _fill_matches_leg(t, fill, open_leg, require_open=False)
            and str(t.get("entry_date", "")) == str(fill.get("entry_date", ""))
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


def _resolve_order_contract(ib, contract):
    """
    Získa plný kontrakt s ``conId`` pre ``placeOrder``.

    1) ``qualifyContracts`` (3 pokusy, dlhší timeout — paper TWS často mešká).
    2) Ak stále nič, záložne ``reqContractDetails`` (niekedy prebehne, keď qualify visí).
    """
    old_to = getattr(ib, "RequestTimeout", 8)
    last_timeout: BaseException | None = None
    try:
        ib.RequestTimeout = 75
        for _attempt in range(3):
            try:
                qc = ib.qualifyContracts(contract)
                if qc and getattr(qc[0], "conId", None):
                    return qc[0], None
            except (TimeoutError, asyncio.TimeoutError) as e:
                last_timeout = e
                try:
                    ib.sleep(0.6)
                except Exception:
                    time.sleep(0.6)
            except Exception as e:
                return None, e

        try:
            ib.RequestTimeout = 60
            dets = ib.reqContractDetails(contract)
            if dets:
                cand = dets[0].contract
                if getattr(cand, "conId", None):
                    return cand, None
        except (TimeoutError, asyncio.TimeoutError) as e:
            last_timeout = last_timeout or e
        except Exception as e:
            if last_timeout:
                return None, last_timeout
            return None, e

        if last_timeout:
            return None, last_timeout
        return None, RuntimeError("IB nevrátil platný kontrakt (prázdny qualify aj contractDetails).")
    finally:
        try:
            ib.RequestTimeout = old_to
        except Exception:
            pass


def submit_trading_command_order(cmd: dict) -> dict:
    """
    Odošle jeden príkaz z uloženého záznamu (``trading_commands``) do TWS s ``transmit=True``.

    Očakáva polia: ``ticker``, ``action`` (buy/sell), ``order_kind``, ``quantity``,
    voliteľne ``limit_price``, ``stop_price``, ``close_sec_type`` (STK|OPT),
    pre OPT: ``close_expiry`` (YYYYMMDD), ``close_strike``, ``close_right`` (C|P).

    ``order_kind``: ``market`` (trh), ``mtl`` (trhový limit v TWS, API ``MTL``), ``limit``, ``stop``.
    """
    ib = get_ib()
    if not ib or not ib.isConnected():
        return {"error": "Nie je pripojenie na IBKR.", "perm_id": None, "order_id": None}

    _ib_ready()
    from ib_insync import LimitOrder, MarketOrder, Option as IBOption, Order, StopOrder, Stock

    sym = (cmd.get("ticker") or "").strip().upper()
    if not sym:
        return {"error": "Chýba ticker.", "perm_id": None, "order_id": None}

    action_raw = (cmd.get("action") or "").strip().lower()
    if action_raw not in ("buy", "sell"):
        return {"error": "Vyber smer príkazu (nákup / predaj).", "perm_id": None, "order_id": None}
    ib_action = "BUY" if action_raw == "buy" else "SELL"

    okind = (cmd.get("order_kind") or "").strip().lower()
    if okind == "bracket":
        return {
            "error": "Typ „Bracket / combo“ zatiaľ nie je podporovaný pre priame odoslanie — zvoľ Trh, Trhový limit (MTL), Limit alebo Stop.",
            "perm_id": None,
            "order_id": None,
        }
    if okind not in ("market", "mtl", "limit", "stop"):
        return {
            "error": "Vyber typ príkazu (Trh / Trhový limit (MTL) / Limit / Stop).",
            "perm_id": None,
            "order_id": None,
        }

    try:
        qty_f = float(cmd.get("quantity") or 0)
    except (TypeError, ValueError):
        qty_f = 0.0
    if qty_f <= 0:
        return {"error": "Množstvo musí byť väčšie ako 0.", "perm_id": None, "order_id": None}

    sec = (cmd.get("close_sec_type") or "").strip().upper()
    if sec not in ("STK", "OPT"):
        return {"error": "Vyber kontrakt na zatvorenie: Akcia (STK) alebo Opčný kontrakt (OPT).", "perm_id": None, "order_id": None}

    try:
        if sec == "STK":
            contract = Stock(sym, "SMART", "USD")
        else:
            exp = str(cmd.get("close_expiry") or "").strip().replace("-", "")[:8]
            if len(exp) != 8 or not exp.isdigit():
                return {"error": "Pre OPT vyplň expiráciu YYYYMMDD.", "perm_id": None, "order_id": None}
            try:
                strike = float(cmd.get("close_strike") or 0)
            except (TypeError, ValueError):
                strike = 0.0
            if strike <= 0:
                return {"error": "Pre OPT vyplň platný strike.", "perm_id": None, "order_id": None}
            right = (cmd.get("close_right") or "").strip().upper()[:1]
            if right not in ("C", "P"):
                return {"error": "Pre OPT vyber Call alebo Put.", "perm_id": None, "order_id": None}
            contract = IBOption(sym, exp, strike, right, "SMART", currency="USD")
    except Exception as e:
        return {"error": f"Kontrakt: {type(e).__name__}: {e}", "perm_id": None, "order_id": None}

    if sec == "STK":
        # Pre jednoduché US akcie je SMART/USD dostatočný kontrakt pre TWS placeOrder.
        # Povinné qualifyContracts tu zbytočne blokovalo skúšobné akciové príkazy.
        c = contract
    else:
        try:
            c, qerr = _resolve_order_contract(ib, contract)
            if qerr is not None:
                if isinstance(qerr, (TimeoutError, asyncio.TimeoutError)):
                    detail = str(qerr).strip() or type(qerr).__name__
                    return {
                        "error": (
                            "Kontrakt: časový limit (qualifyContracts / reqContractDetails). "
                            "Skús znova o chvíľu; skontroluj sieť, nepreťažené TWS a platnosť kontraktu "
                            "(expirácia YYYYMMDD, SMART, paper vs live). "
                            f"Detail: {detail}"
                        ),
                        "perm_id": None,
                        "order_id": None,
                    }
                return {
                    "error": f"Kontrakt: {type(qerr).__name__}: {qerr}",
                    "perm_id": None,
                    "order_id": None,
                }
            if not c or not getattr(c, "conId", None):
                return {"error": "Kontrakt sa nepodarilo rozlíšiť v IB (žiadny conId).", "perm_id": None, "order_id": None}
        except Exception as e:
            return {"error": f"Kontrakt: {type(e).__name__}: {e}", "perm_id": None, "order_id": None}

    try:
        lp = cmd.get("limit_price")
        sp = cmd.get("stop_price")
        lf = float(lp) if lp is not None else None
        sf = float(sp) if sp is not None else None
    except (TypeError, ValueError):
        lf = sf = None

    if okind == "market":
        order = MarketOrder(ib_action, qty_f)
    elif okind == "mtl":
        if lf is None or lf <= 0:
            return {
                "error": "Pri trhovom limite (MTL) vyplň kladnú limitnú cenu (lmtPrice v TWS).",
                "perm_id": None,
                "order_id": None,
            }
        order = Order(action=ib_action, totalQuantity=qty_f, orderType="MTL", lmtPrice=lf)
    elif okind == "limit":
        if lf is None or lf <= 0:
            return {"error": "Pri limite vyplň kladnú cenu limitu.", "perm_id": None, "order_id": None}
        order = LimitOrder(ib_action, qty_f, lf)
    else:
        if sf is None or sf <= 0:
            return {"error": "Pri stop príkaze vyplň kladnú spúšťaciu stop cenu.", "perm_id": None, "order_id": None}
        order = StopOrder(ib_action, qty_f, sf)

    order.transmit = True

    try:
        trade = ib.placeOrder(c, order)
        perm_out = None
        oid_out = None
        status_out = ""
        warning_out = ""
        for _ in range(80):
            ib.sleep(0.15)
            po = getattr(trade, "order", None)
            ps = getattr(trade, "orderStatus", None)
            if po is not None:
                oid = getattr(po, "orderId", None)
                if oid not in (None, 0):
                    oid_out = str(int(oid))
                pid = getattr(po, "permId", 0) or 0
                if pid and int(pid) > 0:
                    perm_out = str(int(pid))
                    break
            if ps is not None:
                status_out = str(getattr(ps, "status", "") or status_out)
                warning_out = (
                    getattr(ps, "whyHeld", "")
                    or getattr(ps, "warningText", "")
                    or warning_out
                )
                oid = getattr(ps, "orderId", None)
                if oid not in (None, 0):
                    oid_out = str(int(oid))
                pid = getattr(ps, "permId", 0) or 0
                if pid and int(pid) > 0:
                    perm_out = str(int(pid))
                    break
        if not perm_out:
            bad_states = {"Cancelled", "ApiCancelled", "Inactive"}
            if oid_out and status_out not in bad_states:
                return {
                    "error": None,
                    "perm_id": None,
                    "order_id": oid_out,
                    "warning": (
                        "TWS príkaz prijal, ale Perm ID ešte neprišlo. "
                        "Skontroluj stav príkazu v TWS; v denníku ukladám Order ID."
                        + (f" Stav: {status_out}." if status_out else "")
                        + (f" Poznámka: {warning_out}" if warning_out else "")
                    ),
                }
            return {
                "error": "IB nepotvrdil prijatie príkazu včas."
                + (f" Stav: {status_out}." if status_out else "")
                + (f" Poznámka: {warning_out}" if warning_out else ""),
                "perm_id": None,
                "order_id": oid_out,
            }
        return {"error": None, "perm_id": perm_out, "order_id": oid_out}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}", "perm_id": None, "order_id": None}


def journal_short_opt_matches_ib_position(trade: dict, p: dict) -> bool:
    """Či IB pozícia zodpovedá journal short opčnej nohe (OPT/FOP, Short)."""
    from core.portfolio_data import normalize_expiry, _canon_option_type_for_key

    if str(p.get("sec_type") or "").upper() not in ("OPT", "FOP"):
        return False
    if str(p.get("leg_type") or "").strip() != "Short":
        return False
    if str(trade.get("leg_type") or "").strip() != "Short":
        return False
    if str(trade.get("ticker") or "").strip().upper() != str(p.get("ticker") or "").strip().upper():
        return False
    try:
        ts = float(trade.get("strike") or 0)
        ps = float(p.get("strike") or 0)
    except (TypeError, ValueError):
        return False
    if abs(ts - ps) > 1e-4:
        return False

    def _ex_norm(x: Any) -> str:
        raw = str(x or "").strip().split()[0]
        if not raw:
            return ""
        return normalize_expiry(raw).replace("-", "")[:8]

    if _ex_norm(trade.get("expiry")) != _ex_norm(p.get("expiry")):
        return False
    tt = _canon_option_type_for_key(trade.get("option_type"))
    pt = _canon_option_type_for_key(p.get("option_type"))
    return tt == pt and tt in ("Call", "Put")


def short_block_still_open_vs_ib(trade: dict, positions: list[dict]) -> dict:
    """
    ``blocked`` = True ak zhodná Short OPT pozícia je v snímku IB (typické blokovanie predaja long nohy).
    """
    matches = [p for p in positions if journal_short_opt_matches_ib_position(trade, p)]
    qty = sum(float(p.get("contracts") or 0) for p in matches)
    if qty > 1e-9:
        return {
            "blocked": True,
            "visible_qty": qty,
            "detail_sk": "Short kontrakt je v IB snímku stále otvorený — účet často nedovolí uzavrieť long nohu.",
        }
    return {
        "blocked": False,
        "visible_qty": 0.0,
        "detail_sk": "Zhodná Short OPT pozícia v tomto snímku nie je (assignment / uzavretie / iný účet). Pred odoslaním long close over v TWS.",
    }


def check_assignment_watch_vs_ib(watch_trade: dict) -> dict:
    """Načíta ``fetch_positions()`` a vyhodnotí, či sledovaná short noha ešte „drží“ blokovanie."""
    res = fetch_positions()
    if res.get("error"):
        return {"error": res["error"], "blocked": None, "visible_qty": None, "detail_sk": ""}
    inner = short_block_still_open_vs_ib(watch_trade, list(res.get("positions") or []))
    return {"error": None, **inner}


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
