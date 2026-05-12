"""
Obchodné príkazy — plán v aktuálnej DB podľa režimu LIVE/PAPER; voliteľné **odoslanie** jedného príkazu do TWS (transmit),
ak vyplníš kontrakt na zatvorenie a potvrdíš riziká.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import streamlit as st

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page
from core.portfolio_data import normalize_expiry

db.init_db()
set_tradejournal_page("trading_commands")

_TC_MANUAL_PATH = Path(__file__).resolve().parent.parent / "docs" / "Obchodné príkazy" / "manual.md"

_STATUS_OPT: list[tuple[str, str]] = [
    ("draft", "Koncept"),
    ("ready", "Pripravené"),
    ("submitted", "Odoslané / v platforme"),
    ("filled", "Vykonané"),
    ("cancelled", "Zrušené"),
]
_ACTION_OPT: list[tuple[str, str]] = [
    ("", "—"),
    ("buy", "Nákup"),
    ("sell", "Predaj"),
]
_ORDER_OPT: list[tuple[str, str]] = [
    ("", "—"),
    ("market", "Trh"),
    ("mtl", "Trhový limit (MTL)"),
    ("limit", "Limit"),
    ("stop", "Stop"),
    ("bracket", "Bracket / combo"),
]

_ORDER_KIND_HELP_TWS = (
    "**Trh** — čistý market. **MTL** — ako trh s možnosťou nastaviť limitnú hranicu (marketable limit). "
    "**Stop** — po dosiahnutí úrovne trhový výkon. Podrobnejší rozdiel je v šedej poznámke pod výberom typu."
)

_TC_MARKET_FAMILY_CAPTION = (
    "**Trh** = čistý market. **MTL / Limit** dopĺň do **Limit ($)**; **Stop** do **Stop ($)**. "
    "Tieto polia sú predvolene **skryté** — zapni ich checkboxom nižšie len keď treba."
)
_SORT_OPT: list[tuple[str, str]] = [
    ("updated", "Posledná úprava"),
    ("plan", "Postupnosť (skupina + krok)"),
]
_COND_UNDER_OPT: list[tuple[str, str]] = [
    ("", "— žiadna podmienka na cenu podkladu —"),
    ("gt", "Cena podkladu väčšia ako"),
    ("lt", "Cena podkladu menšia ako"),
    ("gte", "Cena podkladu väčšia alebo rovná ako (≥)"),
    ("lte", "Cena podkladu menšia alebo rovná ako (≤)"),
]
_COND_FILL_OPT: list[tuple[str, str]] = [
    ("", "— žiadna podmienka na predchádzajúci obchod —"),
    ("option", "Až po (vlastnom) obchode s opciou"),
    ("underlying", "Až po (vlastnom) obchode s podkladom"),
    ("option_or_underlying", "Až po obchode s opciou alebo s podkladom"),
    ("custom", "Iné — dopíš do poznámky k podmienkam"),
]
_TRIGGER_OPT: list[tuple[str, str]] = [
    ("manual", "Manuálne — odoslanie po kontrole"),
    ("short_leg_assignment", "Po uplatnení / priradení short nohy"),
]
_CLOSE_SEC_OPT: list[tuple[str, str]] = [
    ("", "— iba plán v texte (bez IB kontraktu) —"),
    ("STK", "Akcia (STK)"),
    ("OPT", "Opčný kontrakt (OPT)"),
]
_CLOSE_RIGHT_OPT: list[tuple[str, str]] = [
    ("", "—"),
    ("C", "Call"),
    ("P", "Put"),
]

_TC_HELP_LIMIT_PRICE = (
    "**0** pri uložení znamená „bez limitu“ — v DB ostane prázdne (vhodné pre **Trh** alebo rozpracovaný plán). "
    "Pri **odoslaní Limit** alebo **Trhový limit (MTL)** do TWS musí byť vyplnená **kladná limitná cena** "
    "(MTL = korekcia „takmer market“ výplne; Limit = čistý limit)."
)
_TC_HELP_STOP_PRICE = (
    "**0** pri uložení znamená „bez stopu“ — v DB ostane prázdne. "
    "Pri **odoslaní Stop** príkazu do TWS musí byť vyplnená **kladná spúšťacia** stop cena (ako Stop v TWS)."
)


def _order_kind_code(sel: Any) -> str:
    """Prvá položka z (kód, popis) vo výsledku selectboxu."""
    if sel is None:
        return ""
    if isinstance(sel, (list, tuple)) and len(sel) >= 1:
        return str(sel[0] or "").strip().lower()
    return str(sel).strip().lower()


def _tc_sync_show_prices_from_order_kind(*, form: str, rid: int | None = None, cur_k_seed: str = "") -> None:
    """
    Vo ``st.form`` nie je povolený ``on_change`` na selectboxoch — rozbalenie Limit/Stop riešime cez session_state.
    Pri **zmene** typu na MTL/Limit/Stop raz zapneme checkbox; pri tom istom type po skrytí znova nenútime.
    """
    if form == "new":
        k = _order_kind_code(st.session_state.get("tc_new_k"))
        prev = str(st.session_state.get("_tc_new_kind_lsp_prev") or "").strip().lower()
        if k in ("mtl", "limit", "stop") and k != prev:
            st.session_state["tc_new_show_lsp"] = True
        st.session_state["_tc_new_kind_lsp_prev"] = k
    elif form == "edit" and rid is not None:
        k = _order_kind_code(st.session_state.get(f"t_{rid}_k")) or str(cur_k_seed or "").strip().lower()
        prev = str(st.session_state.get(f"_tc_edit_{rid}_kind_lsp_prev") or "").strip().lower()
        if k in ("mtl", "limit", "stop") and k != prev:
            st.session_state[f"t_{rid}_show_lsp"] = True
        st.session_state[f"_tc_edit_{rid}_kind_lsp_prev"] = k


def _sk_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in pairs}


_STATUS_SK = _sk_map(_STATUS_OPT)
_TRIGGER_SK = _sk_map(_TRIGGER_OPT)


def _is_journal_option_leg(t: dict) -> bool:
    """Opčná noha v denníku — nie čistý podkladový STK bez kontraktu."""
    ot = str(t.get("option_type") or "").strip().lower()
    if ot.startswith("c") or ot.startswith("p"):
        return True
    try:
        k = float(t.get("strike") or 0)
    except (TypeError, ValueError):
        k = 0.0
    ex = str(t.get("expiry") or "").strip()
    return bool(ex and k > 0)


def _journal_option_trade_full_label(t: dict, *, status_note: str | None = None) -> str:
    """Ľudsky čitateľný celý popis opčnej nohy pre výberové polia."""
    tid = t.get("id")
    if tid is None:
        return ""
    strat = str(t.get("strategy") or "").strip()
    tk = str(t.get("ticker") or "").strip().upper()
    lt = str(t.get("leg_type") or "").strip()
    ot = str(t.get("option_type") or "").strip()
    k = t.get("strike")
    ex = str(t.get("expiry") or "").strip()
    try:
        n = int(t.get("contracts") or 1)
    except (TypeError, ValueError):
        n = 1
    ed = str(t.get("entry_date") or "").strip()
    gid = str(t.get("group_id") or "").strip()
    bits: list[str] = [f"#{int(tid)}"]
    if strat:
        bits.append(strat)
    bits.extend([tk, f"{lt} {ot}".strip(), f"K={k}", f"exp {ex}", f"{n} ks"])
    if gid:
        bits.append(f"skupina {gid}")
    if ed:
        bits.append(f"vstup {ed}")
    if status_note:
        bits.append(status_note)
    return " · ".join(bits)


def _trade_link_choices(*, include_trade_id: int | None = None) -> list[tuple[str, str]]:
    """Len aktuálne (Open) opčné nohy; dlhý popis. Voliteľne doplní konkrétne ID (napr. uzavretý starý väzba)."""
    out: list[tuple[str, str]] = [("", "— bez väzby na obchod v denníku —")]
    keys: set[str] = set()
    try:
        trades = db.get_open_trades()
    except Exception:
        trades = []
    for t in trades:
        if not _is_journal_option_leg(t):
            continue
        tid = t.get("id")
        if tid is None:
            continue
        sid = str(int(tid))
        keys.add(sid)
        out.append((sid, _journal_option_trade_full_label(t)))
    if include_trade_id is not None and include_trade_id > 0:
        sid = str(int(include_trade_id))
        if sid not in keys:
            t2 = db.get_trade_by_id(int(include_trade_id))
            if t2 is not None:
                stt = str(t2.get("status") or "").strip()
                note = None if stt == "Open" else f"stav {stt}"
                lab = _journal_option_trade_full_label(t2, status_note=note)
                out.append((sid, lab))
                keys.add(sid)
    return out


def _choice_index_val(choices: list[tuple[str, str]], current: str | None) -> int:
    cur = str(current or "").strip()
    for i, (k, _) in enumerate(choices):
        if k == cur:
            return i
    return 0


def _ticker_choices_from_symbols(*, ensure_ticker: str | None = None) -> list[tuple[str, str]]:
    """(hodnota_do_db, popis) — hodnoty z tabuľky Symboly; voliteľne doplní aktuálny ticker záznamu, ak v Symboloch chýba."""
    raw = db.get_symbol_tickers()
    syms = sorted({str(t).strip().upper() for t in raw if str(t).strip()})
    choices: list[tuple[str, str]] = [("", "— bez tickeru —")]
    ex = (ensure_ticker or "").strip().upper()
    if ex and ex not in syms:
        choices.append((ex, f"{ex} (nie je v Symboly)"))
    for s in syms:
        choices.append((s, s))
    return choices


def _ticker_choice_index(choices: list[tuple[str, str]], current: str | None) -> int:
    cur = (current or "").strip().upper()
    for i, (val, _) in enumerate(choices):
        if val == cur:
            return i
    return 0


def _opt_index(options: list[tuple[str, str]], value: str | None) -> int:
    v = (value or "").strip().lower()
    for i, (k, _) in enumerate(options):
        if k == v:
            return i
    return 0


def _expiry_yyyymmdd_from_trade(exp: Any) -> str:
    raw = str(exp or "").strip().split()[0]
    if not raw:
        return ""
    try:
        return normalize_expiry(raw).replace("-", "")[:8]
    except Exception:
        return raw.replace("-", "")[:8] if raw else ""


def _open_position_choices() -> list[tuple[str, str]]:
    """Otvorené nohy z aktuálnej DB podľa režimu (stav Open)."""
    out: list[tuple[str, str]] = [("", "— žiadna / zadať ručne —")]
    try:
        for t in db.get_open_trades():
            tid = t.get("id")
            if tid is None:
                continue
            tk = str(t.get("ticker") or "").strip().upper()
            lt = str(t.get("leg_type") or "").strip()
            ot = str(t.get("option_type") or "").strip()
            k = t.get("strike")
            ex = str(t.get("expiry") or "").strip()
            n = int(t.get("contracts") or 1)
            out.append((str(int(tid)), f"#{tid} · {tk} · {lt} {ot} · K={k} · exp {ex} · {n} ks"))
    except Exception:
        pass
    return out


def _short_watch_choices() -> list[tuple[str, str]]:
    """Open + Short nohy z aktuálnej DB podľa režimu — na párovanie s IB (blokovanie long close)."""
    out: list[tuple[str, str]] = [("", "— bez sledovania short nohy —")]
    try:
        for t in db.get_open_trades():
            if str(t.get("leg_type") or "").strip() != "Short":
                continue
            tid = t.get("id")
            if tid is None:
                continue
            tk = str(t.get("ticker") or "").strip().upper()
            ot = str(t.get("option_type") or "").strip()
            k = t.get("strike")
            ex = str(t.get("expiry") or "").strip()
            n = int(t.get("contracts") or 1)
            out.append((str(int(tid)), f"#{tid} · SHORT {tk} · {ot} · K={k} · exp {ex} · {n} ks"))
    except Exception:
        pass
    return out


def _prefill_from_open_trade(t: dict) -> dict[str, Any]:
    """Predvyplnenie príkazu na zatvorenie vybranej nohy z denníka."""
    tid = int(t["id"])
    tk = str(t.get("ticker") or "").strip().upper()
    leg = str(t.get("leg_type") or "").strip()
    ot_raw = str(t.get("option_type") or "").strip().lower()
    right = "C" if ot_raw.startswith("c") else "P" if ot_raw.startswith("p") else ""
    exp = _expiry_yyyymmdd_from_trade(t.get("expiry"))
    try:
        strike = float(t.get("strike") or 0)
    except (TypeError, ValueError):
        strike = 0.0
    try:
        qty = float(t.get("contracts") or 1)
    except (TypeError, ValueError):
        qty = 1.0
    if leg == "Short":
        close_action = "buy"
    elif leg == "Long":
        close_action = "sell"
    else:
        close_action = ""
    has_opt = bool(exp and strike > 0 and right in ("C", "P"))
    return {
        "title": f"Zatvoriť nohu #{tid} ({tk})",
        "ticker": tk,
        "action": close_action,
        "quantity": qty,
        "close_sec_type": "OPT" if has_opt else "",
        "close_expiry": exp if has_opt else "",
        "close_strike": strike if has_opt else 0.0,
        "close_right": right if has_opt else "",
        "linked_trade_id": tid,
    }


def _apply_prefill_new_session(pref: dict[str, Any]) -> None:
    st.session_state["tc_new_title"] = pref.get("title") or ""
    sym = (pref.get("ticker") or "").strip().upper()
    ch = _ticker_choices_from_symbols(ensure_ticker=sym or None)
    st.session_state["tc_new_sym"] = ch[_ticker_choice_index(ch, sym)]
    ai = next((i for i, x in enumerate(_ACTION_OPT) if x[0] == pref.get("action")), 0)
    st.session_state["tc_new_a"] = _ACTION_OPT[ai]
    st.session_state["tc_new_q"] = float(pref.get("quantity") or 0)
    csi = _choice_index_val(_CLOSE_SEC_OPT, pref.get("close_sec_type") or "")
    st.session_state["tc_new_cls"] = _CLOSE_SEC_OPT[csi]
    st.session_state["tc_new_cex"] = pref.get("close_expiry") or ""
    st.session_state["tc_new_cst"] = float(pref.get("close_strike") or 0)
    cri = _choice_index_val(_CLOSE_RIGHT_OPT, pref.get("close_right") or "")
    st.session_state["tc_new_crt"] = _CLOSE_RIGHT_OPT[cri]
    _lid_raw = str(pref.get("linked_trade_id") or "").strip()
    _lid_i = int(_lid_raw) if _lid_raw.isdigit() else None
    lnk = _trade_link_choices(include_trade_id=_lid_i)
    lid = str(pref.get("linked_trade_id") or "")
    st.session_state["tc_new_link"] = lnk[_choice_index_val(lnk, lid)]
    wch = _short_watch_choices()
    wid = str(pref.get("assignment_watch_trade_id") or "")
    st.session_state["tc_new_watch"] = wch[_choice_index_val(wch, wid)]


def _apply_prefill_edit_session(rid: int, pref: dict[str, Any]) -> None:
    p = f"t_{rid}_"
    st.session_state[f"{p}title"] = pref.get("title") or ""
    sym = (pref.get("ticker") or "").strip().upper()
    ch = _ticker_choices_from_symbols(ensure_ticker=sym or None)
    st.session_state[f"{p}sym"] = ch[_ticker_choice_index(ch, sym)]
    ai = next((i for i, x in enumerate(_ACTION_OPT) if x[0] == pref.get("action")), 0)
    st.session_state[f"{p}a"] = _ACTION_OPT[ai]
    st.session_state[f"{p}q"] = float(pref.get("quantity") or 0)
    csi = _choice_index_val(_CLOSE_SEC_OPT, pref.get("close_sec_type") or "")
    st.session_state[f"{p}cls"] = _CLOSE_SEC_OPT[csi]
    st.session_state[f"{p}cex"] = pref.get("close_expiry") or ""
    st.session_state[f"{p}cstk"] = float(pref.get("close_strike") or 0)
    cri = _choice_index_val(_CLOSE_RIGHT_OPT, pref.get("close_right") or "")
    st.session_state[f"{p}crt"] = _CLOSE_RIGHT_OPT[cri]
    _lid_raw = str(pref.get("linked_trade_id") or "").strip()
    _lid_i = int(_lid_raw) if _lid_raw.isdigit() else None
    lnk = _trade_link_choices(include_trade_id=_lid_i)
    lid = str(pref.get("linked_trade_id") or "")
    st.session_state[f"{p}lnk"] = lnk[_choice_index_val(lnk, lid)]
    wch = _short_watch_choices()
    wid = str(pref.get("assignment_watch_trade_id") or "")
    st.session_state[f"{p}watch"] = wch[_choice_index_val(wch, wid)]


def _tc_ib_pos_cache_key() -> str:
    return ibkr.scoped_session_key("tc_ib_portfolio_cache")


def _tc_ib_pos_err_key() -> str:
    return ibkr.scoped_session_key("tc_ib_portfolio_err")


TC_RESTRICT_DRAFT_READY = "tc_restrict_draft_ready"


def _ib_position_label(p: dict, idx: int) -> str:
    tk = str(p.get("ticker") or "").strip().upper()
    sec = str(p.get("sec_type") or "").strip()
    lt = str(p.get("leg_type") or "").strip()
    try:
        q = float(p.get("contracts") or 0)
    except (TypeError, ValueError):
        q = 0.0
    if sec == "STK":
        return f"[{idx}] {tk} STK · {lt} · {q:g} ks"
    if sec in ("OPT", "FOP"):
        ot = str(p.get("option_type") or "").strip()
        k = p.get("strike")
        ex = str(p.get("expiry") or "").strip()
        return f"[{idx}] {tk} {sec} · {lt} {ot} · K={k} · {ex} · {q:g} ks"
    return f"[{idx}] {tk} {sec} · {lt} · {q:g} ks"


def _ib_position_select_options(cache: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("", "— vyber po stiahnutí z IB —")]
    for i, p in enumerate(cache):
        out.append((f"ib:{i}", _ib_position_label(p, i)))
    return out


def _prefill_from_ib_position(p: dict) -> dict[str, Any]:
    """Predvyplnenie z riadku ``fetch_positions()`` (živé IB portfólio)."""
    tk = str(p.get("ticker") or "").strip().upper()
    sec = str(p.get("sec_type") or "").strip().upper()
    leg = str(p.get("leg_type") or "").strip()
    try:
        qty = float(p.get("contracts") or 0)
    except (TypeError, ValueError):
        qty = 0.0
    if leg == "Short":
        close_action = "buy"
    elif leg == "Long":
        close_action = "sell"
    else:
        close_action = ""

    if sec == "STK":
        return {
            "title": f"IB · {tk} · zatvoriť akciu ({leg})",
            "ticker": tk,
            "action": close_action,
            "quantity": qty,
            "close_sec_type": "STK",
            "close_expiry": "",
            "close_strike": 0.0,
            "close_right": "",
            "linked_trade_id": None,
        }
    if sec in ("OPT", "FOP"):
        ot = str(p.get("option_type") or "").strip().lower()
        right = "C" if ot.startswith("c") else "P" if ot.startswith("p") else ""
        exp = _expiry_yyyymmdd_from_trade(p.get("expiry"))
        try:
            strike = float(p.get("strike") or 0)
        except (TypeError, ValueError):
            strike = 0.0
        has_opt = bool(exp and strike > 0 and right in ("C", "P"))
        return {
            "title": f"IB · {tk} OPT {right} {strike:g} {exp}",
            "ticker": tk,
            "action": close_action,
            "quantity": qty,
            "close_sec_type": "OPT" if has_opt else "",
            "close_expiry": exp if has_opt else "",
            "close_strike": strike if has_opt else 0.0,
            "close_right": right if has_opt else "",
            "linked_trade_id": None,
        }
    return {
        "title": f"IB · {tk} {sec} · doplniť kontrakt ručne",
        "ticker": tk,
        "action": close_action,
        "quantity": qty,
        "close_sec_type": "",
        "close_expiry": "",
        "close_strike": 0.0,
        "close_right": "",
        "linked_trade_id": None,
    }


def _fetch_ib_positions_into_session() -> None:
    res = ibkr.fetch_positions()
    if res.get("error"):
        st.session_state[_tc_ib_pos_err_key()] = str(res["error"])
        st.session_state[_tc_ib_pos_cache_key()] = []
    else:
        st.session_state[_tc_ib_pos_err_key()] = None
        st.session_state[_tc_ib_pos_cache_key()] = list(res.get("positions") or [])
_manual_md = ""
if _TC_MANUAL_PATH.is_file():
    try:
        _manual_md = _TC_MANUAL_PATH.read_text(encoding="utf-8")
    except OSError:
        _manual_md = ""
_mc1, _mc2 = st.columns([4, 2])
with _mc1:
    if _manual_md:
        with st.expander("📖 Manuál — Obchodné príkazy (náhľad)", expanded=False):
            st.markdown(_manual_md)
    else:
        st.caption(
            "Manuál sa nenašiel. V repozitári by mal byť súbor `docs/Obchodné príkazy/manual.md`."
        )
with _mc2:
    if _manual_md:
        st.download_button(
            "Stiahnuť manuál (.md)",
            data=_manual_md.encode("utf-8"),
            file_name="obchodne-prikazy-manual.md",
            mime="text/markdown",
            key="tc_manual_download",
            help="Rovnaký obsah ako súbor docs/Obchodné príkazy/manual.md",
        )
st.caption(
    "**Odporúčaný postup:** pripoj **IB** v sidebar, klikni **Stiahnuť pozície z IB**, vyber riadok z aktuálneho "
    "účtu a **Načítať do formulára** — ticker, smer na uzavretie (Long→predaj / Short→nákup), množstvo a STK alebo OPT "
    "podľa dát z IB. Údaje uložíš do **aktuálnej DB podľa režimu**; odoslanie do TWS je voliteľné (potvrdenie ASSIGN). "
    "Ak **short** ešte drží blokovanie, ulož pri príkaze **Sledovanú short nohu** a občas **Skontroluj** voči IB. "
    "**Alternatíva:** predvyplnenie z **dneníka** (Open), ak nemáš práve IB."
)

_sym_raw = db.get_symbol_tickers()
if not _sym_raw or not any(str(t).strip() for t in _sym_raw):
    st.info("V **Symboly** zatiaľ nemáš žiadny ticker — pridaj symboly, aby sa tu dali vyberať. ")
    st.page_link("pages/symbols.py", label="Otvoriť Symboly", icon=":material/bookmarks:")


def _fmt_cmd_line(r: dict) -> str:
    t = str(r.get("title") or "")
    tk = str(r.get("ticker") or "").strip()
    stt = _STATUS_SK.get(str(r.get("status") or ""), r.get("status"))
    bits = [f"**{r['id']}** · {t}"]
    if tk:
        bits.append(tk)
    pg = (str(r.get("plan_group") or "")).strip()
    si = r.get("step_index")
    if pg and si is not None:
        try:
            bits.append(f"〔{pg}〕 #{int(si)}")
        except (TypeError, ValueError):
            bits.append(f"〔{pg}〕")
    elif pg:
        bits.append(f"〔{pg}〕")
    if (str(r.get("tws_perm_id") or "")).strip() or (str(r.get("tws_order_id") or "")).strip():
        bits.append("TWS✎")
    tg = str(r.get("trigger_kind") or "").strip().lower()
    if tg == "short_leg_assignment":
        bits.append("Po assign.")
    if (str(r.get("assignment_watch_trade_id") or "")).strip().isdigit():
        bits.append("Watch short")
    if (str(r.get("close_sec_type") or "")).strip():
        bits.append(str(r.get("close_sec_type")).strip())
    if (
        (str(r.get("cond_under_cmp") or "")).strip()
        or (str(r.get("cond_after_fill") or "")).strip()
        or (str(r.get("cond_detail") or "")).strip()
    ):
        bits.append("Podm.")
    bits.append(f"_{stt}_")
    return " · ".join(bits)


def _render_tc_new_command_expander_body() -> None:
    _ib_cache = st.session_state.get(_tc_ib_pos_cache_key()) or []
    _ib_err = st.session_state.get(_tc_ib_pos_err_key())
    st.markdown("##### Z Interactive Brokers (účet)")
    if ibkr.is_connected():
        _fb1, _fb2 = st.columns([2, 4])
        with _fb1:
            if st.button("Stiahnuť pozície z IB", key="tc_new_ib_fetch", type="primary"):
                _fetch_ib_positions_into_session()
                st.rerun()
        with _fb2:
            if _ib_err:
                st.error(_ib_err)
            else:
                st.caption(f"Posledný snapshot: **{len(_ib_cache)}** pozícií (zdielané s úpravami záznamov nižšie).")
        _ib_opts = _ib_position_select_options(_ib_cache)
        _ibc1, _ibc2 = st.columns([5, 1])
        with _ibc1:
            pick_ib_new = st.selectbox(
                "Pozícia z IB",
                options=_ib_opts,
                format_func=lambda x: x[1],
                key="tc_new_ib_pick",
            )
        with _ibc2:
            st.write("")
            st.write("")
            if st.button("Načítať z IB", key="tc_new_ib_load", type="secondary"):
                val = pick_ib_new[0] if pick_ib_new else ""
                if val and str(val).startswith("ib:"):
                    try:
                        ix = int(str(val).split(":", 1)[1])
                    except (IndexError, ValueError):
                        ix = -1
                    if 0 <= ix < len(_ib_cache):
                        _apply_prefill_new_session(_prefill_from_ib_position(_ib_cache[ix]))
                        st.rerun()
                    else:
                        st.error("Neplatná položka — znova stiahni pozície z IB.")
                else:
                    st.warning("Vyber riadok zo zoznamu (najprv **Stiahnuť pozície z IB**).")
    else:
        st.info("Pre výber z účtu pripoj **Interactive Brokers** v sidebar.")

    st.divider()
    st.markdown("##### Alternatíva — otvorené nohy v denníku")
    st.caption(
        "Nohy so stavom **Open** v **aktuálnej DB podľa režimu** (bez živého IB). "
        "Vyber riadok a **Načítať** predvyplní podľa denníka (vrátane väzby na obchod)."
    )
    _och_new = _open_position_choices()
    _rn1, _rn2 = st.columns([5, 1])
    with _rn1:
        pick_open_new = st.selectbox(
            "Otvorená pozícia z denníka",
            options=_och_new,
            format_func=lambda x: x[1],
            key="tc_new_pick_open_list",
        )
    with _rn2:
        st.write("")
        st.write("")
        if st.button("Načítať do formulára", key="tc_new_load_open_btn", type="secondary"):
            if pick_open_new[0]:
                _tr_load = db.get_trade_by_id(int(pick_open_new[0]))
                if _tr_load:
                    _apply_prefill_new_session(_prefill_from_open_trade(_tr_load))
                    st.rerun()
                else:
                    st.error("Obchod sa nenašiel.")
            else:
                st.warning("Najprv vyber nohu zo zoznamu.")

    with st.form("tc_new", clear_on_submit=True):
        _tc_sync_show_prices_from_order_kind(form="new")
        nt = st.text_input("Názov / stručný popis", placeholder="napr. Zatvoriť short call", key="tc_new_title")
        _new_sym = _ticker_choices_from_symbols()
        ntk_pair = st.selectbox(
            "Ticker (zo záložky Symboly)",
            options=_new_sym,
            format_func=lambda x: x[1],
            key="tc_new_sym",
            help="Zoznam z tabuľky Symboly.",
        )
        c1, c2, c3 = st.columns(3)
        with c1:
            na = st.selectbox("Smer", _ACTION_OPT, format_func=lambda x: x[1], key="tc_new_a")
        with c2:
            nk = st.selectbox(
                "Typ príkazu",
                _ORDER_OPT,
                format_func=lambda x: x[1],
                key="tc_new_k",
                help=_ORDER_KIND_HELP_TWS,
            )
        with c3:
            ns = st.selectbox("Stav", _STATUS_OPT, format_func=lambda x: x[1], key="tc_new_status")
        st.caption(_TC_MARKET_FAMILY_CAPTION)
        show_lsp_new = st.checkbox(
            "Zobraziť polia **Limit ($)** a **Stop ($)**",
            value=False,
            key="tc_new_show_lsp",
            help="Pri výbere **MTL / Limit / Stop** sa polia **samé rozbalia**. Inak zapni ručne, ak chceš mať ceny v pláne aj pri **Trh**.",
        )
        if show_lsp_new:
            p1, p2 = st.columns(2)
            with p1:
                nl = st.number_input(
                    "Limit ($)",
                    step=0.01,
                    format="%.2f",
                    key="tc_new_l",
                    help=_TC_HELP_LIMIT_PRICE,
                )
            with p2:
                nsx = st.number_input(
                    "Stop ($)",
                    step=0.01,
                    format="%.2f",
                    key="tc_new_stp",
                    help=_TC_HELP_STOP_PRICE,
                )
        else:
            nl = 0.0
            nsx = 0.0
        nq = st.number_input("Množstvo (kontrakty / akcie)", step=1.0, format="%.4f", key="tc_new_q")
        st.markdown("**Podmienky** (plán — len zápis, nie prepojenie na TWS)")
        nc1, nc2 = st.columns(2)
        with nc1:
            n_cmp = st.selectbox(
                "Cena podkladu (podľa tickera vyššie)",
                options=_COND_UNDER_OPT,
                format_func=lambda x: x[1],
                key="tc_new_cmp",
                help="Podklad = zvyčajne akcia toho istého tickera. Hranicu doplníš vpravo.",
            )
        with nc2:
            n_cpx = st.number_input(
                "Hranica ceny podkladu ($)",
                step=0.01,
                format="%.2f",
                key="tc_new_cpx",
                help="Vyplň, ak vľavo nie je „žiadna podmienka“. Inak ignorované.",
            )
        n_fill = st.selectbox(
            "Predchádzajúci obchod (čo musí nastať pred týmto príkazom)",
            options=_COND_FILL_OPT,
            format_func=lambda x: x[1],
            key="tc_new_fill",
        )
        n_cdet = st.text_area(
            "Poznámka k podmienkam (čas, konkrétna noha, OCA…)",
            height=70,
            placeholder="Voliteľné doplnenie k výberom vyššie",
            key="tc_new_cdet",
        )
        st.markdown("**Kontrakt na zatvorenie (IBKR)** — pre odoslanie príkazu do TWS")
        n_trig = st.selectbox(
            "Spúšťacia logika pre odoslanie",
            options=_TRIGGER_OPT,
            format_func=lambda x: x[1],
            key="tc_new_trig",
            help="Pri „Po uplatnení short nohy“ musíš pri odoslaní potvrdiť assignment.",
        )
        n_cls = st.selectbox(
            "Čo obchodovať v TWS",
            options=_CLOSE_SEC_OPT,
            format_func=lambda x: x[1],
            key="tc_new_cls",
        )
        oc1, oc2, oc3 = st.columns(3)
        with oc1:
            n_cex = st.text_input(
                "Expirácia (YYYYMMDD)",
                placeholder="20260320",
                help="Len pre typ OPT.",
                key="tc_new_cex",
            )
        with oc2:
            n_cst = st.number_input("Strike ($)", step=0.01, format="%.2f", key="tc_new_cst")
        with oc3:
            n_crt = st.selectbox(
                "Call / Put",
                options=_CLOSE_RIGHT_OPT,
                format_func=lambda x: x[1],
                key="tc_new_crt",
            )
        _lnk_new = _trade_link_choices()
        n_link = st.selectbox(
            "Väzba na obchod v denníku",
            options=_lnk_new,
            format_func=lambda x: x[1],
            key="tc_new_link",
            help="Len **otvorené opčné nohy** — stratégia, ticker, typ nohy, strike, expirácia, počet kontraktov.",
        )
        _w_new = _short_watch_choices()
        n_watch = st.selectbox(
            "Sledovaná short noha (kontrola vs IB — voliteľné)",
            options=_w_new,
            format_func=lambda x: x[1],
            key="tc_new_watch",
            help="Riadok z denníka: noha **Short** (Open). Oproti nej sa porovná snímok pozícií z IB (blokovanie long close).",
        )
        nb = st.text_area(
            "Detail / čas / vlastný popis príkazu",
            height=100,
            placeholder="Voliteľné",
            key="tc_new_body",
        )
        st.markdown("**Postupnosť** (voliteľné — rovnaký text = jedna logická séria)")
        pc1, pc2 = st.columns(2)
        with pc1:
            npg = st.text_input(
                "Skupina postupnosti",
                placeholder="napr. ROLL-QQQ-2026-04",
                help="Rovnaký reťazec u viacerých záznamov; zoraď podľa „Postupnosť“.",
                key="tc_new_pg",
            )
        with pc2:
            nsi = st.number_input(
                "Krok (poradie)",
                min_value=0,
                value=0,
                step=1,
                help="1, 2, 3… v rámci skupiny. 0 = bez poradia.",
                key="tc_new_step",
            )
        st.markdown("**Ručne z TWS** (prepíšeš z okna objednávok)")
        t1, t2 = st.columns(2)
        with t1:
            ntperm = st.text_input("Perm ID", placeholder="napr. 123456789", key="tc_new_perm")
        with t2:
            ntord = st.text_input("Order ID", placeholder="voliteľné", key="tc_new_ord")
        ntwsn = st.text_area("Poznámka z TWS", height=70, placeholder="napr. Submitted 30.4.", key="tc_new_twsn")
        sub = st.form_submit_button("Uložiť príkaz", type="primary")
        if sub:
            ntk = (ntk_pair[0] or "").strip().upper() or None
            _nk0 = (nk[0] or "").strip().lower()
            if not (nt or "").strip():
                st.error("Vyplň aspoň **názov**.")
            elif n_cmp[0] and n_cpx == 0.0:
                st.error("Pri podmienke na **cenu podkladu** vyplň nenulovú **hranicu ($)**.")
            elif _nk0 in ("mtl", "limit", "stop") and not show_lsp_new:
                st.error(
                    "Pre zvolený typ príkazu (**MTL**, **Limit**, **Stop**) zapni **Zobraziť polia Limit ($) a Stop ($)** "
                    "a vyplň príslušnú cenu."
                )
            elif _nk0 in ("mtl", "limit") and nl == 0.0:
                st.error("Vyplň **Limit ($)** — kladná hodnota.")
            elif _nk0 == "stop" and nsx == 0.0:
                st.error("Vyplň **Stop ($)** — kladná hodnota.")
            else:
                try:
                    db.insert_trading_command(
                        (nt or "").strip(),
                        ticker=ntk,
                        action=na[0] or None,
                        order_kind=nk[0] or None,
                        quantity=nq if nq != 0.0 else None,
                        limit_price=nl if nl != 0.0 else None,
                        stop_price=nsx if nsx != 0.0 else None,
                        body=nb.strip() or None,
                        status=ns[0],
                        plan_group=npg.strip() or None,
                        step_index=int(nsi) if nsi else None,
                        tws_perm_id=ntperm.strip() or None,
                        tws_order_id=ntord.strip() or None,
                        tws_manual_note=ntwsn.strip() or None,
                        cond_under_cmp=n_cmp[0] or None,
                        cond_under_price=float(n_cpx) if n_cmp[0] else None,
                        cond_after_fill=n_fill[0] or None,
                        cond_detail=n_cdet.strip() or None,
                        trigger_kind=n_trig[0],
                        close_sec_type=n_cls[0] or None,
                        close_expiry=n_cex.strip() if n_cls[0] == "OPT" else None,
                        close_strike=n_cst if n_cls[0] == "OPT" else None,
                        close_right=n_crt[0] if n_cls[0] == "OPT" else None,
                        linked_trade_id=int(n_link[0])
                        if str(n_link[0] or "").strip().isdigit()
                        else None,
                        assignment_watch_trade_id=int(n_watch[0])
                        if str(n_watch[0] or "").strip().isdigit()
                        else None,
                    )
                    st.success("Príkaz uložený.")
                    st.session_state.pop("_tc_new_kind_lsp_prev", None)
                    st.session_state["tc_new_show_lsp"] = False
                    st.rerun()
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")


_tc_col_new, _tc_col_edit = st.columns([5, 2])
with _tc_col_new:
    with st.expander("➕ Nový príkaz", expanded=False):
        _render_tc_new_command_expander_body()
with _tc_col_edit:
    with st.expander("✏️ Upraviť príkazy — koncepty", expanded=False):
        st.caption(
            "Úpravy sú v **zozname nižšie** — rozbaľ riadok. Tu len nastavíš filter."
        )
        if st.button("Len koncepty", key="tc_quick_only_draft", use_container_width=True):
            st.session_state["tc_filter"] = ("draft", "Koncept")
            st.session_state["tc_only_assign"] = False
            st.session_state[TC_RESTRICT_DRAFT_READY] = False
            st.rerun()
        if st.button("Len pripravené", key="tc_quick_only_ready", use_container_width=True):
            st.session_state["tc_filter"] = ("ready", "Pripravené")
            st.session_state["tc_only_assign"] = False
            st.session_state[TC_RESTRICT_DRAFT_READY] = False
            st.rerun()
        if st.button("Koncept + pripravené", key="tc_quick_draft_ready", use_container_width=True):
            st.session_state["tc_filter"] = ("all", "Všetky")
            st.session_state["tc_only_assign"] = False
            st.session_state[TC_RESTRICT_DRAFT_READY] = True
            st.rerun()
        if st.button("Všetky záznamy", key="tc_quick_all", use_container_width=True):
            st.session_state["tc_filter"] = ("all", "Všetky")
            st.session_state["tc_only_assign"] = False
            st.session_state[TC_RESTRICT_DRAFT_READY] = False
            st.rerun()


def _tc_clear_restrict_filters() -> None:
    st.session_state[TC_RESTRICT_DRAFT_READY] = False


flt = st.selectbox(
    "Filtrovať podľa stavu",
    options=[("all", "Všetky")] + _STATUS_OPT,
    format_func=lambda x: x[1],
    key="tc_filter",
    on_change=_tc_clear_restrict_filters,
)
sort_key = st.selectbox(
    "Zoradiť zoznam",
    options=_SORT_OPT,
    format_func=lambda x: x[1],
    index=0,
    key="tc_sort",
)
_only_assign = st.checkbox(
    "Len príkazy „Po uplatnení short nohy“ v stave Koncept / Pripravené",
    key="tc_only_assign",
    help="Zúži zoznam na plán po assignmente, ktorý ešte nie je odoslaný.",
    on_change=_tc_clear_restrict_filters,
)
rows = (
    db.list_trading_commands(limit=300, sort_by=sort_key[0])
    if flt[0] == "all"
    else db.list_trading_commands(status=flt[0], limit=300, sort_by=sort_key[0])
)
if st.session_state.get(TC_RESTRICT_DRAFT_READY):
    rows = [
        x
        for x in rows
        if str(x.get("status") or "").strip().lower() in ("draft", "ready")
    ]
if _only_assign:
    rows = [
        x
        for x in rows
        if str(x.get("trigger_kind") or "").strip().lower() == "short_leg_assignment"
        and str(x.get("status") or "").strip().lower() in ("draft", "ready")
    ]

if st.session_state.get(TC_RESTRICT_DRAFT_READY):
    st.caption(
        "📌 Zapnutý filter **Koncept + Pripravené** (z pravého expandera). Zrušíš ho zmenou „Filtrovať podľa stavu“ alebo **Všetky záznamy**."
    )

if not rows:
    st.info("Zatiaľ **žiadne** záznamy. Pridaj prvý v expanderi vyššie.")
else:
    for r in rows:
        rid = int(r["id"])
        with st.expander(_fmt_cmd_line(r), expanded=False):
            st.caption(
                f"Vytvorené: **{r.get('created_at', '')}** · Upravené: **{r.get('updated_at', '')}**"
            )
            _lc = (str(r.get("cond_under_cmp") or "")).strip()
            _lp = r.get("cond_under_price")
            _lf = (str(r.get("cond_after_fill") or "")).strip()
            if _lc and _lp is not None:
                _lbl = next((b for a, b in _COND_UNDER_OPT if a == _lc), _lc)
                st.caption(f"Cena podkladu: **{_lbl}** **{_lp:g}** $")
            if _lf:
                _lfb = next((b for a, b in _COND_FILL_OPT if a == _lf), _lf)
                st.caption(f"Predchádzajúci obchod: {_lfb}")
            if (str(r.get("cond_detail") or "")).strip():
                st.caption(f"Doplnenie: {str(r.get('cond_detail') or '').strip()[:200]}{'…' if len(str(r.get('cond_detail') or '')) > 200 else ''}")
            _tkm = str(r.get("trigger_kind") or "").strip().lower()
            if _tkm:
                st.caption(f"Spúšťacia logika: {_TRIGGER_SK.get(_tkm, _tkm)}")
            _cx = str(r.get("close_sec_type") or "").strip()
            if _cx:
                _ex = ""
                if _cx == "OPT":
                    _ex = f" · exp **{r.get('close_expiry') or ''}** strike **{r.get('close_strike')}** {r.get('close_right') or ''}"
                st.caption(f"Kontrakt na zatvorenie: **{_cx}**{_ex}")
            _lid = r.get("linked_trade_id")
            if _lid:
                st.caption(f"Väzba na obchod ID **{_lid}**")
            _awx = r.get("assignment_watch_trade_id")
            if _awx:
                st.caption(f"Sledovaná short noha (denník): **ID {_awx}**")
            _ac_at = r.get("assignment_check_at")
            _ac_sum = (str(r.get("assignment_check_summary") or "")).strip()
            if _ac_at or _ac_sum:
                st.caption(
                    f"Posledná kontrola short vs IB: **{_ac_at or '—'}** — {_ac_sum[:180]}{'…' if len(_ac_sum) > 180 else ''}"
                )
            _ib_cache_ed = st.session_state.get(_tc_ib_pos_cache_key()) or []
            _ib_err_ed = st.session_state.get(_tc_ib_pos_err_key())
            st.markdown("##### Z Interactive Brokers")
            if ibkr.is_connected():
                _ef1, _ef2 = st.columns([2, 4])
                with _ef1:
                    if st.button("Stiahnuť pozície z IB", key=f"tc_ed_ib_fetch_{rid}", type="primary"):
                        _fetch_ib_positions_into_session()
                        st.rerun()
                with _ef2:
                    if _ib_err_ed:
                        st.error(_ib_err_ed)
                    else:
                        st.caption(f"V pamäti: **{len(_ib_cache_ed)}** pozícií (rovnaký snapshot ako pri „Nový príkaz“).")
                _ib_opts_ed = _ib_position_select_options(_ib_cache_ed)
                pick_ib_ed = st.selectbox(
                    "Pozícia z IB",
                    options=_ib_opts_ed,
                    format_func=lambda x: x[1],
                    key=f"tc_edit_ib_pick_{rid}",
                )
                if st.button("Načítať z IB do formulára", key=f"tc_edit_ib_load_{rid}", type="secondary"):
                    val = pick_ib_ed[0] if pick_ib_ed else ""
                    if val and str(val).startswith("ib:"):
                        try:
                            ix_e = int(str(val).split(":", 1)[1])
                        except (IndexError, ValueError):
                            ix_e = -1
                        if 0 <= ix_e < len(_ib_cache_ed):
                            _apply_prefill_edit_session(rid, _prefill_from_ib_position(_ib_cache_ed[ix_e]))
                            st.rerun()
                        else:
                            st.error("Neplatný výber — znova stiahni pozície z IB.")
                    else:
                        st.warning("Vyber pozíciu z IB alebo najprv **Stiahnuť pozície z IB**.")
            else:
                st.caption("Pre živý výber z účtu pripoj IB v sidebar.")

            st.divider()
            st.markdown("##### Alternatíva — denník")
            _och_ed = _open_position_choices()
            _edc1, _edc2 = st.columns([5, 1])
            with _edc1:
                pick_ed = st.selectbox(
                    "Otvorená pozícia z denníka (predvyplnenie)",
                    options=_och_ed,
                    format_func=lambda x: x[1],
                    key=f"tc_edit_pick_open_{rid}",
                    help="Rovnaký zoznam ako pri novom príkaze — uložené nohy Open.",
                )
            with _edc2:
                st.write("")
                if st.button("Načítať", key=f"tc_edit_load_open_{rid}", type="secondary"):
                    if pick_ed[0]:
                        _ted = db.get_trade_by_id(int(pick_ed[0]))
                        if _ted:
                            _apply_prefill_edit_session(rid, _prefill_from_open_trade(_ted))
                            st.rerun()
                        else:
                            st.error("Obchod sa nenašiel.")
                    else:
                        st.warning("Vyber nohu zo zoznamu.")
            with st.form(f"tc_edit_{rid}"):
                et = st.text_input("Názov", value=str(r.get("title") or ""), key=f"t_{rid}_title")
                _ed_sym = _ticker_choices_from_symbols(ensure_ticker=r.get("ticker"))
                _ed_idx = _ticker_choice_index(_ed_sym, r.get("ticker"))
                etk_pair = st.selectbox(
                    "Ticker (zo záložky Symboly)",
                    options=_ed_sym,
                    format_func=lambda x: x[1],
                    index=_ed_idx,
                    key=f"t_{rid}_sym",
                    help="Zoznam z tabuľky Symboly.",
                )
                etk = (etk_pair[0] or "").strip().upper() or None
                c1, c2, c3 = st.columns(3)
                cur_a = r.get("action") or ""
                cur_k = r.get("order_kind") or ""
                cur_s = r.get("status") or "draft"
                a_idx = next((i for i, x in enumerate(_ACTION_OPT) if x[0] == cur_a), 0)
                k_idx = next((i for i, x in enumerate(_ORDER_OPT) if x[0] == cur_k), 0)
                s_idx = next((i for i, x in enumerate(_STATUS_OPT) if x[0] == cur_s), 0)
                _tc_sync_show_prices_from_order_kind(form="edit", rid=rid, cur_k_seed=str(cur_k or ""))
                with c1:
                    ea = st.selectbox("Smer", _ACTION_OPT, index=a_idx, format_func=lambda x: x[1], key=f"t_{rid}_a")
                with c2:
                    ek = st.selectbox(
                        "Typ príkazu",
                        _ORDER_OPT,
                        index=k_idx,
                        format_func=lambda x: x[1],
                        key=f"t_{rid}_k",
                        help=_ORDER_KIND_HELP_TWS,
                    )
                with c3:
                    es = st.selectbox("Stav", _STATUS_OPT, index=s_idx, format_func=lambda x: x[1], key=f"t_{rid}_status")
                st.caption(_TC_MARKET_FAMILY_CAPTION)
                lv = float(r["limit_price"]) if r.get("limit_price") is not None else 0.0
                sv = float(r["stop_price"]) if r.get("stop_price") is not None else 0.0
                _ek0_px = str(cur_k or "").strip().lower()
                _need_px_ed = (
                    _ek0_px in ("mtl", "limit", "stop")
                    or lv != 0.0
                    or sv != 0.0
                )
                show_lsp_ed = st.checkbox(
                    "Zobraziť polia **Limit ($)** a **Stop ($)**",
                    value=_need_px_ed,
                    key=f"t_{rid}_show_lsp",
                    help="Pri zmene typu na **MTL / Limit / Stop** sa polia **samé rozbalia**. Inak zapni ručne pri úprave cien alebo poznámky pri **Trh**.",
                )
                if show_lsp_ed:
                    p1, p2 = st.columns(2)
                    with p1:
                        enl = st.number_input(
                            "Limit ($)",
                            value=lv,
                            step=0.01,
                            format="%.2f",
                            key=f"t_{rid}_l",
                            help=_TC_HELP_LIMIT_PRICE,
                        )
                    with p2:
                        ens = st.number_input(
                            "Stop ($)",
                            value=sv,
                            step=0.01,
                            format="%.2f",
                            key=f"t_{rid}_stp",
                            help=_TC_HELP_STOP_PRICE,
                        )
                else:
                    enl = lv
                    ens = sv
                qv = float(r["quantity"]) if r.get("quantity") is not None else 0.0
                enq = st.number_input("Množstvo", value=qv, step=1.0, format="%.4f", key=f"t_{rid}_q")
                eb = st.text_area("Detail", value=str(r.get("body") or ""), key=f"t_{rid}_b")
                st.markdown("**Podmienky** (plán — len zápis)")
                ed_cmp = st.selectbox(
                    "Cena podkladu (podľa tickera)",
                    options=_COND_UNDER_OPT,
                    format_func=lambda x: x[1],
                    index=_opt_index(_COND_UNDER_OPT, r.get("cond_under_cmp")),
                    key=f"t_{rid}_cuc",
                )
                _ecpx = float(r["cond_under_price"]) if r.get("cond_under_price") is not None else 0.0
                ed_cpx = st.number_input(
                    "Hranica ceny podkladu ($)",
                    value=_ecpx,
                    step=0.01,
                    format="%.2f",
                    key=f"t_{rid}_cup",
                )
                ed_fill = st.selectbox(
                    "Predchádzajúci obchod",
                    options=_COND_FILL_OPT,
                    format_func=lambda x: x[1],
                    index=_opt_index(_COND_FILL_OPT, r.get("cond_after_fill")),
                    key=f"t_{rid}_caf",
                )
                ed_cdet = st.text_area(
                    "Poznámka k podmienkam",
                    value=str(r.get("cond_detail") or ""),
                    height=70,
                    key=f"t_{rid}_cdet",
                )
                st.markdown("**Kontrakt na zatvorenie (IBKR)**")
                etrg = st.selectbox(
                    "Spúšťacia logika pre odoslanie",
                    options=_TRIGGER_OPT,
                    format_func=lambda x: x[1],
                    index=_opt_index(_TRIGGER_OPT, r.get("trigger_kind")),
                    key=f"t_{rid}_trg",
                    help="Pri „Po uplatnení short nohy“ musíš pri odoslaní potvrdiť assignment.",
                )
                ecls = st.selectbox(
                    "Čo obchodovať v TWS",
                    options=_CLOSE_SEC_OPT,
                    format_func=lambda x: x[1],
                    index=_choice_index_val(_CLOSE_SEC_OPT, str(r.get("close_sec_type") or "")),
                    key=f"t_{rid}_cls",
                )
                _ev_cex = str(r.get("close_expiry") or "")
                _ev_cst = float(r["close_strike"]) if r.get("close_strike") is not None else 0.0
                ec1, ec2, ec3 = st.columns(3)
                with ec1:
                    e_cex = st.text_input(
                        "Expirácia (YYYYMMDD)",
                        value=_ev_cex,
                        key=f"t_{rid}_cex",
                        placeholder="20260320",
                    )
                with ec2:
                    e_cst = st.number_input(
                        "Strike ($)",
                        value=_ev_cst,
                        step=0.01,
                        format="%.2f",
                        key=f"t_{rid}_cstk",
                    )
                with ec3:
                    e_crt = st.selectbox(
                        "Call / Put",
                        options=_CLOSE_RIGHT_OPT,
                        format_func=lambda x: x[1],
                        index=_choice_index_val(_CLOSE_RIGHT_OPT, str(r.get("close_right") or "").strip()),
                        key=f"t_{rid}_crt",
                    )
                _li = str(r.get("linked_trade_id") or "").strip()
                _li_i = int(_li) if _li.isdigit() else None
                _ed_lnk = _trade_link_choices(include_trade_id=_li_i)
                elink = st.selectbox(
                    "Väzba na obchod v denníku",
                    options=_ed_lnk,
                    format_func=lambda x: x[1],
                    index=_choice_index_val(_ed_lnk, _li),
                    key=f"t_{rid}_lnk",
                    help="Len **otvorené opčné nohy**; ak je uložená väzba na už uzavretý riadok, zobrazí sa doplnkový riadok so stavom.",
                )
                _ed_watch = _short_watch_choices()
                _aw_i = str(r.get("assignment_watch_trade_id") or "").strip()
                ewatch = st.selectbox(
                    "Sledovaná short noha (kontrola vs IB — voliteľné)",
                    options=_ed_watch,
                    format_func=lambda x: x[1],
                    index=_choice_index_val(_ed_watch, _aw_i),
                    key=f"t_{rid}_watch",
                    help="Open **Short** v denníku. Tlačidlo kontroly je pod formulárom (po uložení).",
                )
                st.markdown("**Postupnosť**")
                ec1, ec2 = st.columns(2)
                with ec1:
                    epg = st.text_input(
                        "Skupina postupnosti",
                        value=str(r.get("plan_group") or ""),
                        key=f"t_{rid}_pg",
                    )
                with ec2:
                    _si0 = int(r["step_index"]) if r.get("step_index") is not None else 0
                    esi = st.number_input(
                        "Krok (poradie)",
                        min_value=0,
                        value=_si0,
                        step=1,
                        key=f"t_{rid}_step",
                    )
                st.markdown("**Ručne z TWS**")
                et1, et2 = st.columns(2)
                with et1:
                    etperm = st.text_input(
                        "Perm ID",
                        value=str(r.get("tws_perm_id") or ""),
                        key=f"t_{rid}_perm",
                    )
                with et2:
                    etord = st.text_input(
                        "Order ID",
                        value=str(r.get("tws_order_id") or ""),
                        key=f"t_{rid}_oid",
                    )
                etwsn = st.text_area(
                    "Poznámka z TWS",
                    value=str(r.get("tws_manual_note") or ""),
                    height=70,
                    key=f"t_{rid}_twsn",
                )
                u1, u2 = st.columns(2)
                with u1:
                    save = st.form_submit_button("Uložiť zmeny", type="primary")
                with u2:
                    delete = st.form_submit_button("Zmazať príkaz", type="secondary")
                if save:
                    if ed_cmp[0] and ed_cpx == 0.0:
                        st.error("Pri podmienke na **cenu podkladu** vyplň nenulovú **hranicu ($)**.")
                    else:
                        cur_snap = db.get_trading_command(rid) or r
                        ek0 = (ek[0] or "").strip().lower()
                        if show_lsp_ed:
                            lp_u = None if enl == 0.0 else enl
                            sp_u = None if ens == 0.0 else ens
                        else:
                            lp_u = cur_snap.get("limit_price")
                            sp_u = cur_snap.get("stop_price")
                        try:
                            _lpv = float(lp_u) if lp_u is not None else 0.0
                        except (TypeError, ValueError):
                            _lpv = 0.0
                        try:
                            _spv = float(sp_u) if sp_u is not None else 0.0
                        except (TypeError, ValueError):
                            _spv = 0.0
                        err_px: str | None = None
                        if ek0 in ("mtl", "limit") and _lpv <= 0:
                            err_px = (
                                "Pre **MTL** alebo **Limit** treba **kladnú** limitnú cenu — zapni **Zobraziť Limit/Stop** "
                                "a vyplň **Limit ($)**."
                            )
                        elif ek0 == "stop" and _spv <= 0:
                            err_px = (
                                "Pre **Stop** treba **kladnú** stop cenu — zapni **Zobraziť Limit/Stop** a vyplň **Stop ($)**."
                            )
                        if err_px:
                            st.error(err_px)
                        else:
                            try:
                                db.update_trading_command(
                                    rid,
                                    title=et,
                                    ticker=etk or "",
                                    action=ea[0] or None,
                                    order_kind=ek[0] or None,
                                    quantity=None if enq == 0.0 else enq,
                                    limit_price=None if lp_u is None else float(lp_u),
                                    stop_price=None if sp_u is None else float(sp_u),
                                    body=eb,
                                    status=es[0],
                                    plan_group=epg.strip() or None,
                                    step_index=int(esi) if esi else None,
                                    tws_perm_id=etperm.strip() or None,
                                    tws_order_id=etord.strip() or None,
                                    tws_manual_note=etwsn.strip() or None,
                                    cond_under_cmp=ed_cmp[0] or None,
                                    cond_under_price=float(ed_cpx) if ed_cmp[0] else None,
                                    cond_after_fill=ed_fill[0] or None,
                                    cond_detail=ed_cdet.strip() or None,
                                    trigger_kind=etrg[0],
                                    close_sec_type=ecls[0] or None,
                                    close_expiry=e_cex.strip() if ecls[0] == "OPT" else None,
                                    close_strike=e_cst if ecls[0] == "OPT" else None,
                                    close_right=e_crt[0] if ecls[0] == "OPT" else None,
                                    linked_trade_id=int(elink[0])
                                    if str(elink[0] or "").strip().isdigit()
                                    else None,
                                    assignment_watch_trade_id=int(ewatch[0])
                                    if str(ewatch[0] or "").strip().isdigit()
                                    else None,
                                    _all_fields=True,
                                )
                                st.success("Uložené.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"{type(e).__name__}: {e}")
                if delete:
                    try:
                        n = db.delete_trading_command(rid)
                        if n:
                            st.session_state["tc_notice"] = f"Záznam **{rid}** bol zmazaný."
                        st.rerun()
                    except Exception as e:
                        st.error(f"{type(e).__name__}: {e}")
            st.divider()
            st.markdown("##### Kontrola short nohy vs IB (blokovanie long close)")
            st.caption(
                "Kým **short** kontrakt sedí v účte, TWS často neprejde príkaz na uzavretie **long** nohy. "
                "Nastav **Sledovanú short nohu** vo formulári vyššie, **Ulož zmeny**, potom občas klikni kontrolu — "
                "porovná denník s aktuálnym snímkom pozícií z IB."
            )
            _r2 = db.get_trading_command(rid) or r
            _w2 = _r2.get("assignment_watch_trade_id")
            if ibkr.is_connected():
                if _w2:
                    _wtr2 = db.get_trade_by_id(int(_w2))
                    if _wtr2 and st.button("Skontrolovať short voči IB", key=f"tc_wchk_{rid}", type="primary"):
                        cr = ibkr.check_assignment_watch_vs_ib(_wtr2)
                        if cr.get("error"):
                            msg = f"IB: {cr['error']}"
                        elif cr.get("blocked"):
                            vq = float(cr.get("visible_qty") or 0)
                            msg = (
                                f"BLOK: short stále v snímke (~{vq:g} ks). {cr.get('detail_sk') or ''}"
                            )
                        else:
                            msg = f"Short v tomto snímku neviditeľný — {cr.get('detail_sk') or ''}"
                        db.record_trading_command_assignment_check(rid, msg)
                        st.success(msg)
                        st.rerun()
                elif str(_r2.get("trigger_kind") or "").strip().lower() == "short_leg_assignment":
                    st.warning("Doplň a ulož **Sledovanú short nohu** vo formulári vyššie, aby šla kontrola voči IB.")
            else:
                if _w2 or str(_r2.get("trigger_kind") or "").strip().lower() == "short_leg_assignment":
                    st.caption("Na kontrolu pripoj **Interactive Brokers** v sidebar a stiahni pozície, ak potrebuješ čerstvý snímok.")
            if ibkr.is_connected():
                st.divider()
                st.markdown("**Odoslanie do TWS** (skutočný príkaz, `transmit=True`)")
                _row = db.get_trading_command(rid) or r
                _st = str(_row.get("status") or "").strip().lower()
                _ttr = str(_row.get("trigger_kind") or "manual").strip().lower()
                _csk = str(_row.get("close_sec_type") or "").strip().upper()
                _can = _st in ("draft", "ready") and _csk in ("STK", "OPT")
                if not _can:
                    st.caption(
                        "Odoslanie je možné len v stave **Koncept** alebo **Pripravené** a s typom kontraktu **STK** alebo **OPT**. "
                        "Najprv **Uložiť zmeny** vo formulári vyššie."
                    )
                with st.form(f"tc_send_{rid}"):
                    st.caption(
                        "Použijú sa údaje **uložené v DB** (po uložení). Skontroluj smer, typ, množstvo a limity."
                    )
                    _confirm_word = "ASSIGN" if _ttr == "short_leg_assignment" else "SEND"
                    cb_risk = st.checkbox(
                        "Beriem na vedomie, že sa odošle skutočný príkaz na účet pripojený v TWS.",
                        key=f"tc_risk_{rid}",
                    )
                    cb_asg = st.checkbox(
                        "Potvrdzujem uplatnenie / priradenie short nohy (assignment).",
                        key=f"tc_asg_{rid}",
                        disabled=_ttr != "short_leg_assignment",
                    )
                    typed = st.text_input(
                        f"Potvrdenie: napíš slovo **{_confirm_word}** (presne)",
                        key=f"tc_assign_txt_{rid}",
                        placeholder=_confirm_word,
                    )
                    submitted_send = st.form_submit_button("Odoslať príkaz do TWS", type="primary")
                    if submitted_send:
                        st.toast("Spracovávam klik…", icon="⏳")
                        if not _can:
                            st.error(
                                "Nesplnené podmienky pre odoslanie (stav alebo kontrakt). Ulož zmeny a skontroluj STK/OPT."
                            )
                            st.toast("Odoslanie zablokované — skontroluj stav a typ kontraktu.", icon="⚠️")
                        elif not cb_risk:
                            st.error("Potvrď riziko (prvé zaškrtávacie políčko).")
                            st.toast("Chýba potvrdenie rizika.", icon="⚠️")
                        elif _ttr == "short_leg_assignment" and not cb_asg:
                            st.error('Pri spúšťacej logike „Po uplatnení short nohy“ potvrď assignment.')
                            st.toast("Chýba potvrdenie assignment.", icon="⚠️")
                        elif (typed or "").strip() != _confirm_word:
                            st.error(f"Pre odoslanie napíš presne {_confirm_word}.")
                            st.toast(f"Do poľa potvrdenia napíš {_confirm_word}.", icon="⚠️")
                        else:
                            snap = db.get_trading_command(rid)
                            if not snap:
                                st.error("Záznam sa nenašiel.")
                                st.toast("Záznam v DB sa nenašiel.", icon="⚠️")
                            else:
                                st.toast("Volám TWS (môže trvať desiatky sekúnd)…", icon="📡")
                                with st.spinner(
                                    "Komunikujem s IBKR: kvalifikácia kontraktu a odoslanie príkazu…"
                                ):
                                    res = ibkr.submit_trading_command_order(snap)
                                if res.get("error"):
                                    st.error(res["error"])
                                    st.toast("IBKR vrátil chybu — pozri text vyššie.", icon="❌")
                                else:
                                    note_prev = (snap.get("tws_manual_note") or "").strip()
                                    note_add = (
                                        f"{note_prev}\n" if note_prev else ""
                                    ) + "Odoslané z TradeJournal (IBKR transmit)."
                                    try:
                                        db.update_trading_command(
                                            rid,
                                            status="submitted",
                                            tws_perm_id=res.get("perm_id"),
                                            tws_order_id=res.get("order_id"),
                                            tws_manual_note=note_add.strip(),
                                        )
                                        _perm = res.get("perm_id") or "?"
                                        _oid = res.get("order_id")
                                        _msg = (
                                            f"Príkaz **{rid}** odoslaný do TWS. Perm ID **{_perm}**"
                                            + (f", Order ID **{_oid}**" if _oid else "")
                                            + "."
                                        )
                                        if res.get("warning"):
                                            _msg += f" {res['warning']}"
                                        st.session_state["tc_notice"] = _msg
                                        st.toast("Hotovo — odoslané do TWS.", icon="✅")
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"{type(e).__name__}: {e}")
                                        st.toast("Chyba pri zápise do DB.", icon="❌")
            else:
                st.caption("Pre odoslanie príkazu do TWS pripoj **Interactive Brokers** (sidebar).")

_n = st.session_state.pop("tc_notice", None)
if _n:
    st.success(_n)
