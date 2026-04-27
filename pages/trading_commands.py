"""
Obchodné príkazy — lokálny denník plánovaných príkazov (nie synchronizácia s IBKR).
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


def _sk_map(pairs: list[tuple[str, str]]) -> dict[str, str]:
    return {k: v for k, v in pairs}


_STATUS_SK = _sk_map(_STATUS_OPT)


st.title("Obchodné príkazy")
st.caption(
    "Zapisuj si **plánované** príkazy (limit, množstvo, poznámka). Údaje sú len v **journal.db** — "
    "nenahrádzajú príkazy v IBKR ani TWS."
)


def _fmt_cmd_line(r: dict) -> str:
    t = str(r.get("title") or "")
    tk = str(r.get("ticker") or "").strip()
    stt = _STATUS_SK.get(str(r.get("status") or ""), r.get("status"))
    return f"**{r['id']}** · {t}" + (f" · {tk}" if tk else "") + f" · _{stt}_"


with st.expander("➕ Nový príkaz", expanded=False):
    with st.form("tc_new", clear_on_submit=True):
        nt = st.text_input("Názov / stručný popis", placeholder="napr. Roll QQQ call")
        ntk = st.text_input("Ticker", placeholder="QQQ").upper()
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
        nb = st.text_area("Detail / podmienky / čas", height=100, placeholder="Voliteľné")
        sub = st.form_submit_button("Uložiť príkaz", type="primary")
        if sub:
            if not (nt or "").strip():
                st.error("Vyplň aspoň **názov**.")
            else:
                try:
                    db.insert_trading_command(
                        (nt or "").strip(),
                        ticker=ntk.strip() or None,
                        action=na[0] or None,
                        order_kind=nk[0] or None,
                        quantity=nq if nq != 0.0 else None,
                        limit_price=nl if nl != 0.0 else None,
                        stop_price=nsx if nsx != 0.0 else None,
                        body=nb.strip() or None,
                        status=ns[0],
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
rows = (
    db.list_trading_commands(limit=300)
    if flt[0] == "all"
    else db.list_trading_commands(status=flt[0], limit=300)
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
            with st.form(f"tc_edit_{rid}"):
                et = st.text_input("Názov", value=str(r.get("title") or ""), key=f"t_{rid}_title")
                etk = st.text_input("Ticker", value=str(r.get("ticker") or ""), key=f"t_{rid}_tk").upper()
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
                    es = st.selectbox("Stav", _STATUS_OPT, index=s_idx, format_func=lambda x: x[1], key=f"t_{rid}_s")
                qv = float(r["quantity"]) if r.get("quantity") is not None else 0.0
                lv = float(r["limit_price"]) if r.get("limit_price") is not None else 0.0
                sv = float(r["stop_price"]) if r.get("stop_price") is not None else 0.0
                enq = st.number_input("Množstvo", value=qv, step=1.0, format="%.4f", key=f"t_{rid}_q")
                p1, p2 = st.columns(2)
                with p1:
                    enl = st.number_input("Limit ($)", value=lv, step=0.01, format="%.2f", key=f"t_{rid}_l")
                with p2:
                    ens = st.number_input("Stop ($)", value=sv, step=0.01, format="%.2f", key=f"t_{rid}_s")
                eb = st.text_area("Detail", value=str(r.get("body") or ""), key=f"t_{rid}_b")
                u1, u2 = st.columns(2)
                with u1:
                    save = st.form_submit_button("Uložiť zmeny", type="primary")
                with u2:
                    delete = st.form_submit_button("Zmazať príkaz", type="secondary")
                if save:
                    try:
                        db.update_trading_command(
                            rid,
                            title=et,
                            ticker=etk,
                            action=ea[0] or None,
                            order_kind=ek[0] or None,
                            quantity=None if enq == 0.0 else enq,
                            limit_price=None if enl == 0.0 else enl,
                            stop_price=None if ens == 0.0 else ens,
                            body=eb,
                            status=es[0],
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
