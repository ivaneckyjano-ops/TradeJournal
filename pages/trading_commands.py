"""
Obchodné príkazy — lokálny denník plánovaných príkazov (nie synchronizácia s IBKR).
Postupnosť a údaje z TWS zapisuješ ručne (bez sťahovania z API).
"""

from __future__ import annotations

import streamlit as st

from core import database as db
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


def _sk_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in pairs}


_STATUS_SK = _sk_map(_STATUS_OPT)


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
    "**Návod:** Plán zapisuješ tu v **journal.db** — aplikácia **nesťahuje** objednávky z TWS. "
    "Ticker vyberáš zo záložky **Symboly**. "
    "V sekcii **Podmienky** môžeš ručne zapísať plán: **cena podkladu** voči hranici a či má predchádzať **obchod s opciou / podkladom** (iba zápis pre teba, nie automatika). "
    "Postupnosť krokov: **Skupina postupnosti** + **Krok**. "
    "Údaje z TWS po odoslaní: **Perm ID** / **Order ID**."
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


_n = st.session_state.pop("tc_notice", None)
if _n:
    st.success(_n)
