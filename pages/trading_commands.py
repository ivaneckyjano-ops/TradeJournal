"""
Obchodné príkazy — plán v journal.db; voliteľné **odoslanie** jedného príkazu do TWS (transmit),
ak vyplníš kontrakt na zatvorenie a potvrdíš riziká.
"""

from __future__ import annotations

import streamlit as st

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page

db.init_db()
set_tradejournal_page("trading_commands")

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
    ("limit", "Limit"),
    ("market", "Trh"),
    ("stop", "Stop"),
    ("bracket", "Bracket / combo"),
]
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


def _sk_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in pairs}


_STATUS_SK = _sk_map(_STATUS_OPT)
_TRIGGER_SK = _sk_map(_TRIGGER_OPT)


def _trade_link_choices() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = [("", "— bez väzby na obchod v denníku —")]
    try:
        trades = db.get_all_trades()
    except Exception:
        trades = []
    for t in trades[:250]:
        tid = t.get("id")
        if tid is None:
            continue
        tk = str(t.get("ticker") or "").strip()
        stv = t.get("strike")
        out.append((str(int(tid)), f"{tid} · {tk} @ {stv}"))
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


st.title("Obchodné príkazy")
st.caption(
    "**Návod:** Plán je v **journal.db**. Môžeš doplniť **kontrakt na zatvorenie** (STK/OPT) a pri "
    "pripojenom IB odoslať **jeden** skutočný príkaz do TWS po dvojitom potvrdení (slovom ASSIGN). "
    "Ticker vyberáš zo záložky **Symboly**. "
    "V sekcii **Podmienky** je len vlastný plán (nie automatika). "
    "Postupnosť: **Skupina postupnosti** + **Krok**. "
    "Údaje z TWS: **Perm ID** / **Order ID** (doplnia sa po odoslaní alebo ručne)."
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


with st.expander("➕ Nový príkaz", expanded=False):
    with st.form("tc_new", clear_on_submit=True):
        nt = st.text_input("Názov / stručný popis", placeholder="napr. Roll QQQ call")
        _new_sym = _ticker_choices_from_symbols()
        ntk_pair = st.selectbox(
            "Ticker (zo záložky Symboly)",
            options=_new_sym,
            format_func=lambda x: x[1],
            index=0,
            help="Zoznam z tabuľky Symboly.",
        )
        ntk = (ntk_pair[0] or "").strip().upper() or None
        c1, c2, c3 = st.columns(3)
        with c1:
            na = st.selectbox("Smer", _ACTION_OPT, format_func=lambda x: x[1], index=0)
        with c2:
            nk = st.selectbox("Typ príkazu", _ORDER_OPT, format_func=lambda x: x[1], index=0)
        with c3:
            ns = st.selectbox("Stav", _STATUS_OPT, format_func=lambda x: x[1], index=0)
        nq = st.number_input("Množstvo (kontrakty / akcie)", value=0.0, step=1.0, format="%.4f")
        p1, p2 = st.columns(2)
        with p1:
            nl = st.number_input("Limit ($)", value=0.0, step=0.01, format="%.2f")
        with p2:
            nsx = st.number_input("Stop ($)", value=0.0, step=0.01, format="%.2f")
        st.markdown("**Podmienky** (plán — len zápis, nie prepojenie na TWS)")
        nc1, nc2 = st.columns(2)
        with nc1:
            n_cmp = st.selectbox(
                "Cena podkladu (podľa tickera vyššie)",
                options=_COND_UNDER_OPT,
                format_func=lambda x: x[1],
                index=0,
                help="Podklad = zvyčajne akcia toho istého tickera. Hranicu doplníš vpravo.",
            )
        with nc2:
            _ncp_def = 0.0
            n_cpx = st.number_input(
                "Hranica ceny podkladu ($)",
                value=_ncp_def,
                step=0.01,
                format="%.2f",
                help="Vyplň, ak vľavo nie je „žiadna podmienka“. Inak ignorované.",
            )
        n_fill = st.selectbox(
            "Predchádzajúci obchod (čo musí nastať pred týmto príkazom)",
            options=_COND_FILL_OPT,
            format_func=lambda x: x[1],
            index=0,
        )
        n_cdet = st.text_area(
            "Poznámka k podmienkam (čas, konkrétna noha, OCA…)",
            height=70,
            placeholder="Voliteľné doplnenie k výberom vyššie",
        )
        st.markdown("**Kontrakt na zatvorenie (IBKR)** — pre odoslanie príkazu do TWS")
        n_trig = st.selectbox(
            "Spúšťacia logika pre odoslanie",
            options=_TRIGGER_OPT,
            format_func=lambda x: x[1],
            index=_opt_index(_TRIGGER_OPT, "manual"),
            help="Pri „Po uplatnení short nohy“ musíš pri odoslaní potvrdiť assignment.",
        )
        n_cls = st.selectbox(
            "Čo obchodovať v TWS",
            options=_CLOSE_SEC_OPT,
            format_func=lambda x: x[1],
            index=0,
        )
        oc1, oc2, oc3 = st.columns(3)
        with oc1:
            n_cex = st.text_input(
                "Expirácia (YYYYMMDD)",
                value="",
                placeholder="20260320",
                help="Len pre typ OPT.",
            )
        with oc2:
            n_cst = st.number_input("Strike ($)", value=0.0, step=0.01, format="%.2f")
        with oc3:
            n_crt = st.selectbox(
                "Call / Put",
                options=_CLOSE_RIGHT_OPT,
                format_func=lambda x: x[1],
                index=0,
            )
        _lnk_new = _trade_link_choices()
        n_link = st.selectbox(
            "Väzba na obchod v denníku",
            options=_lnk_new,
            format_func=lambda x: x[1],
            index=_choice_index_val(_lnk_new, ""),
            help="Voliteľné.",
        )
        nb = st.text_area("Detail / čas / vlastný popis príkazu", height=100, placeholder="Voliteľné")
        st.markdown("**Postupnosť** (voliteľné — rovnaký text = jedna logická séria)")
        pc1, pc2 = st.columns(2)
        with pc1:
            npg = st.text_input(
                "Skupina postupnosti",
                placeholder="napr. ROLL-QQQ-2026-04",
                help="Rovnaký reťazec u viacerých záznamov; zoraď podľa „Postupnosť“.",
            )
        with pc2:
            nsi = st.number_input(
                "Krok (poradie)",
                min_value=0,
                value=0,
                step=1,
                help="1, 2, 3… v rámci skupiny. 0 = bez poradia.",
            )
        st.markdown("**Ručne z TWS** (prepíšeš z okna objednávok)")
        t1, t2 = st.columns(2)
        with t1:
            ntperm = st.text_input("Perm ID", placeholder="napr. 123456789")
        with t2:
            ntord = st.text_input("Order ID", placeholder="voliteľné")
        ntwsn = st.text_area("Poznámka z TWS", height=70, placeholder="napr. Submitted 30.4., skontrolované 10:15")
        sub = st.form_submit_button("Uložiť príkaz", type="primary")
        if sub:
            if not (nt or "").strip():
                st.error("Vyplň aspoň **názov**.")
            elif n_cmp[0] and n_cpx == 0.0:
                st.error("Pri podmienke na **cenu podkladu** vyplň nenulovú **hranicu ($)**.")
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
                    )
                    st.success("Príkaz uložený.")
                    st.rerun()
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")


flt = st.selectbox(
    "Filtrovať podľa stavu",
    options=[("all", "Všetky")] + _STATUS_OPT,
    format_func=lambda x: x[1],
    key="tc_filter",
)
sort_key = st.selectbox(
    "Zoradiť zoznam",
    options=_SORT_OPT,
    format_func=lambda x: x[1],
    index=0,
    key="tc_sort",
)
rows = (
    db.list_trading_commands(limit=300, sort_by=sort_key[0])
    if flt[0] == "all"
    else db.list_trading_commands(status=flt[0], limit=300, sort_by=sort_key[0])
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
                with c1:
                    ea = st.selectbox("Smer", _ACTION_OPT, index=a_idx, format_func=lambda x: x[1], key=f"t_{rid}_a")
                with c2:
                    ek = st.selectbox("Typ príkazu", _ORDER_OPT, index=k_idx, format_func=lambda x: x[1], key=f"t_{rid}_k")
                with c3:
                    es = st.selectbox("Stav", _STATUS_OPT, index=s_idx, format_func=lambda x: x[1], key=f"t_{rid}_status")
                qv = float(r["quantity"]) if r.get("quantity") is not None else 0.0
                lv = float(r["limit_price"]) if r.get("limit_price") is not None else 0.0
                sv = float(r["stop_price"]) if r.get("stop_price") is not None else 0.0
                enq = st.number_input("Množstvo", value=qv, step=1.0, format="%.4f", key=f"t_{rid}_q")
                p1, p2 = st.columns(2)
                with p1:
                    enl = st.number_input("Limit ($)", value=lv, step=0.01, format="%.2f", key=f"t_{rid}_l")
                with p2:
                    ens = st.number_input("Stop ($)", value=sv, step=0.01, format="%.2f", key=f"t_{rid}_stp")
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
                _ed_lnk = _trade_link_choices()
                _li = str(r.get("linked_trade_id") or "").strip()
                elink = st.selectbox(
                    "Väzba na obchod v denníku",
                    options=_ed_lnk,
                    format_func=lambda x: x[1],
                    index=_choice_index_val(_ed_lnk, _li),
                    key=f"t_{rid}_lnk",
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
                        try:
                            db.update_trading_command(
                                rid,
                                title=et,
                                ticker=etk or "",
                                action=ea[0] or None,
                                order_kind=ek[0] or None,
                                quantity=None if enq == 0.0 else enq,
                                limit_price=None if enl == 0.0 else enl,
                                stop_price=None if ens == 0.0 else ens,
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
                        'Potvrdenie: napíš slovo **ASSIGN** (presne)',
                        key=f"tc_assign_txt_{rid}",
                        placeholder="ASSIGN",
                    )
                    submitted_send = st.form_submit_button("Odoslať príkaz do TWS", type="primary")
                    if submitted_send:
                        if not _can:
                            st.error("Nesplnené podmienky pre odoslanie (stav alebo kontrakt). Ulož zmeny a skontroluj STK/OPT.")
                        elif not cb_risk:
                            st.error("Potvrď riziko (prvé zaškrtávacie políčko).")
                        elif _ttr == "short_leg_assignment" and not cb_asg:
                            st.error('Pri spúšťacej logike „Po uplatnení short nohy“ potvrď assignment.')
                        elif (typed or "").strip() != "ASSIGN":
                            st.error("Pre odoslanie napíš presne ASSIGN.")
                        else:
                            snap = db.get_trading_command(rid)
                            if not snap:
                                st.error("Záznam sa nenašiel.")
                            else:
                                res = ibkr.submit_trading_command_order(snap)
                                if res.get("error"):
                                    st.error(res["error"])
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
                                        st.success(
                                            f"Odoslané. Perm ID **{res.get('perm_id')}**"
                                            + (
                                                f", Order ID **{res.get('order_id')}**"
                                                if res.get("order_id")
                                                else ""
                                            )
                                        )
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"{type(e).__name__}: {e}")
            else:
                st.caption("Pre odoslanie príkazu do TWS pripoj **Interactive Brokers** (sidebar).")

_n = st.session_state.pop("tc_notice", None)
if _n:
    st.success(_n)
