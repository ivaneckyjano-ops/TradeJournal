import json
from typing import Optional

import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from datetime import date, datetime, timedelta, timezone

from core.probability import calc_greeks, bs_price, calc_iv_from_price, calc_sd_lines
from core import database as db
from core import ibkr
from core import agent as ai_agent
from core.page_context import set_tradejournal_page
from core.portfolio_data import compute_spread_model_theta_aptr_pct
from core.spread_mentor import (
    analyze_calendar_mentor,
    analyze_diagonal_mentor,
    mentor_calendar_rows,
    mentor_comparison_rows,
)
from core.spread_margin import estimate_spread_margin_usd
from core.spread_leg_sync import sync_pair_after_edit
from core.expiration_catalog import (
    append_expiries_from_text,
    format_expiry_select_options,
    get_catalog_expiries,
    merge_catalog_with_generated,
    remove_expiries_from_catalog,
    remove_expiries_from_text,
    replace_catalog_with_generated,
    save_catalog_expiries,
)

db.init_db()
set_tradejournal_page("spread_builder")

st.title("Spread Builder")
st.caption(
    "Poskladaj opčný spread z ľubovoľných nôh alebo začni **šablónou** (kalendár, železný kondor, vertikál) — potom uprav striky, expirácie a ceny. "
    "P&L, Greeks, max profit/loss a breakeveny. **APTR (Θ)** = rovnaká logika ako na TWS dashboarde: Θ×365 / (net debet + marža), Theta zo zadaných hodnôt nôh (alebo ručného súčtu)."
)


def _sb_plot_aptr_trend(series: pd.Series, *, chart_key: str, height: int = 200) -> None:
    s = series.dropna()
    if len(s) < 2:
        return
    x_axis = s.index
    if hasattr(x_axis, "tz") and getattr(x_axis, "tz", None) is not None:
        try:
            x_axis = x_axis.tz_convert("UTC").tz_localize(None)
        except (TypeError, ValueError):
            x_axis = s.index
    fig = go.Figure(data=[go.Scatter(x=x_axis, y=s.values, mode="lines", connectgaps=True)])
    fig.update_layout(
        height=height,
        margin=dict(l=8, r=8, t=8, b=36),
        showlegend=False,
        xaxis=dict(showgrid=True, title=None),
        yaxis=dict(showgrid=True, title=None),
    )
    st.plotly_chart(fig, use_container_width=True, key=chart_key)


# ─── Session state init ────────────────────────────────────────────────────────
if "sb_legs" not in st.session_state:
    st.session_state["sb_legs"] = []   # list of leg dicts
if "sb_spot" not in st.session_state:
    st.session_state["sb_spot"] = 200.0
if "sb_iv" not in st.session_state:
    st.session_state["sb_iv"] = 0.30
if "sb_active_idea_id" not in st.session_state:
    st.session_state["sb_active_idea_id"] = None
if "sb_maint_margin" not in st.session_state:
    st.session_state["sb_maint_margin"] = 0.0


def _sync_sb_market_widgets(*, ticker: str, spot: float, iv: float) -> None:
    """Nastaví ticker/spot/IV vrátane *inp* kľúčov — volať len PRED vykreslením príslušných widgetov."""
    st.session_state["sb_ticker"] = ticker
    st.session_state["sb_spot"] = float(spot)
    st.session_state["sb_iv"] = float(iv)
    st.session_state["sb_ticker_inp"] = ticker
    st.session_state["sb_spot_inp"] = float(spot)
    st.session_state["sb_iv_inp"] = float(iv)


def _queue_sb_new_draft() -> None:
    """Po kliknutí len zaradí patch — samotná zmena prebehne na začiatku ďalšieho behu (pred widgetmi)."""
    st.session_state["_sb_pending_patch"] = {"op": "new_draft"}


def _apply_sb_pending_patch() -> None:
    """Aplikuje zmeny z tlačidiel skôr, než sa vytvoria widgety s kľúčmi sb_*_inp (Streamlit to inak zakáže)."""
    patch = st.session_state.pop("_sb_pending_patch", None)
    if not patch:
        return
    op = patch.get("op")
    if op == "new_draft":
        st.session_state["sb_active_idea_id"] = None
        st.session_state["sb_legs"] = []
        st.session_state["sb_maint_margin"] = 0.0
        _sync_sb_market_widgets(ticker="AMZN", spot=200.0, iv=0.30)
        st.session_state["sb_save_name_input"] = ""
        st.session_state["sb_idea_notes_area"] = ""
        if "sb_pick_idea_lbl" in st.session_state:
            del st.session_state["sb_pick_idea_lbl"]
        if "sb_del_confirm" in st.session_state:
            st.session_state["sb_del_confirm"] = False
    elif op == "load":
        _sync_sb_market_widgets(
            ticker=str(patch["ticker"]),
            spot=float(patch["spot"]),
            iv=float(patch["iv"]),
        )
        st.session_state["sb_legs"] = patch["legs"]
        st.session_state["sb_maint_margin"] = float(patch["maint_margin"])
        st.session_state["sb_active_idea_id"] = int(patch["idea_id"])
        st.session_state["sb_save_name_input"] = patch.get("name") or ""
        st.session_state["sb_idea_notes_area"] = patch.get("notes") or ""
    elif op == "spot":
        _tk = (st.session_state.get("sb_ticker") or "AMZN").upper()
        _iv = float(st.session_state.get("sb_iv", 0.30))
        _sync_sb_market_widgets(ticker=_tk, spot=float(patch["spot"]), iv=_iv)
    elif op == "strategy":
        tpl = patch.get("template")
        spot = float(patch["spot"])
        iv = float(patch["iv"])
        contracts = int(patch.get("contracts", 1) or 1)
        if tpl == "calendar":
            r = str(patch.get("right", "C"))
            if r not in ("C", "P"):
                r = "C"
            near, far = _calendar_near_far_expiries()
            _sk_mode = str(patch.get("calendar_strike", "atm") or "atm")
            atm_k = _strike_round_spot(spot)
            if _sk_mode == "otm":
                _lv = int(patch.get("calendar_otm_levels", 1) or 1)
                k = _calendar_strike_otm_from_atm_levels(atm_k, r, _lv)
            elif _sk_mode == "manual":
                k = _strike_round_spot(float(patch.get("calendar_manual_strike", spot)))
            else:
                k = atm_k
            st.session_state["sb_legs"] = [
                _make_sb_leg(1, "Short", r, k, near, contracts, spot, iv),
                _make_sb_leg(2, "Long", r, k, far, contracts, spot, iv),
            ]
        elif tpl == "iron_condor":
            exp = _first_monthly_expiry()
            atm_k = _strike_round_spot(spot)
            body = max(1, int(patch.get("ic_body_levels", 2) or 2))
            wing = max(1, int(patch.get("ic_wing_levels", 2) or 2))
            short_put_k = atm_k - body * 0.5
            long_put_k = short_put_k - wing * 0.5
            short_call_k = atm_k + body * 0.5
            long_call_k = short_call_k + wing * 0.5
            long_put_k = max(0.5, long_put_k)
            short_put_k = max(0.5, short_put_k)
            if long_put_k >= short_put_k:
                long_put_k = max(0.5, short_put_k - 0.5)
            st.session_state["sb_legs"] = [
                _make_sb_leg(1, "Long", "P", long_put_k, exp, contracts, spot, iv),
                _make_sb_leg(2, "Short", "P", short_put_k, exp, contracts, spot, iv),
                _make_sb_leg(3, "Short", "C", short_call_k, exp, contracts, spot, iv),
                _make_sb_leg(4, "Long", "C", long_call_k, exp, contracts, spot, iv),
            ]
        elif tpl == "vertical_call_debit":
            exp = _first_monthly_expiry()
            k0 = _strike_round_spot(spot)
            long_k, short_k = k0 - 2.5, k0 + 2.5
            if long_k < 0.5:
                long_k, short_k = 0.5, min(short_k, 5.0)
            st.session_state["sb_legs"] = [
                _make_sb_leg(1, "Long", "C", long_k, exp, contracts, spot, iv),
                _make_sb_leg(2, "Short", "C", short_k, exp, contracts, spot, iv),
            ]
        elif tpl == "vertical_put_debit":
            exp = _first_monthly_expiry()
            k0 = _strike_round_spot(spot)
            long_k, short_k = k0 + 2.5, k0 - 2.5
            if short_k < 0.5:
                short_k = 0.5
            st.session_state["sb_legs"] = [
                _make_sb_leg(1, "Long", "P", long_k, exp, contracts, spot, iv),
                _make_sb_leg(2, "Short", "P", short_k, exp, contracts, spot, iv),
            ]


# ─── Pomocné funkcie ───────────────────────────────────────────────────────────

def _dte(expiry_str: str) -> int:
    try:
        e = date(int(expiry_str[:4]), int(expiry_str[4:6]), int(expiry_str[6:8]))
        return max(0, (e - date.today()).days)
    except Exception:
        return 0


def _strike_round_spot(spot: float) -> float:
    """Najbližší strike po 0,5 $ k spotu."""
    return round(float(spot) * 2.0) / 2.0


def _calendar_strike_otm_from_atm_levels(atm_strike: float, right: str, levels: int) -> float:
    """
    OTM od ATM striku: každá úroveň = jeden krok 0,5 $ (call smerom hore, put dolu).
    """
    n = max(1, min(int(levels), 50))
    step = 0.5 * n
    r = str(right).upper()[:1]
    ak = float(atm_strike)
    if r == "C":
        return max(0.5, ak + step)
    return max(0.5, ak - step)


def _calendar_near_far_expiries() -> tuple[str, str]:
    """Predná a zadná expirácia z katalógu (aspoň ~21 DTE predná, ~28+ dní rozstup)."""
    exps = get_catalog_expiries(months=18)
    if not exps:
        td = date.today()
        return (td + timedelta(days=30)).strftime("%Y%m%d"), (td + timedelta(days=75)).strftime("%Y%m%d")
    near = None
    for e in exps:
        if _dte(e) >= 21:
            near = e
            break
    if near is None:
        near = exps[0]
    dn = _dte(near)
    far = near
    for e in exps:
        if _dte(e) - dn >= 28:
            far = e
            break
    if far == near and len(exps) >= 2:
        far = exps[-1]
    return near, far


def _first_monthly_expiry(min_dte: int = 28) -> str:
    exps = get_catalog_expiries(months=18)
    for e in exps:
        if _dte(e) >= min_dte:
            return e
    return exps[0] if exps else date.today().strftime("%Y%m%d")


def _journal_leg_tws_ba(l: dict) -> str:
    b, a = float(l.get("tws_bid") or 0), float(l.get("tws_ask") or 0)
    if b > 0 and a > 0:
        return f"{b:.2f}/{a:.2f}"
    if b > 0:
        return f"{b:.2f}/—"
    if a > 0:
        return f"—/{a:.2f}"
    return "—"


def _journal_leg_tws_theta(l: dict) -> str:
    th = l.get("leg_theta_per_day_usd", l.get("tws_theta_per_day_usd"))
    if th is None or abs(float(th)) <= 1e-9:
        return "—"
    return f"${float(th):+.2f}"


def _merge_leg_tws_quote_fields(leg: dict, *, bid: float, ask: float, last: float) -> None:
    """Bid / Ask / Last z obrazovky TWS (0 = vymaž). Neovplyvňujú BS ceny v P&L — len referencia."""
    if float(bid) > 0:
        leg["tws_bid"] = float(bid)
    else:
        leg.pop("tws_bid", None)
    if float(ask) > 0:
        leg["tws_ask"] = float(ask)
    else:
        leg.pop("tws_ask", None)
    if float(last) > 0:
        leg["tws_last"] = float(last)
    else:
        leg.pop("tws_last", None)


_SB_GK_DELTA = "leg_delta_usd"
_SB_GK_THETA = "leg_theta_per_day_usd"
_SB_GK_VEGA = "leg_vega_usd"
_SB_GK_GAMMA = "leg_gamma"
_SB_LEG_GREEK_KEYS = (_SB_GK_DELTA, _SB_GK_THETA, _SB_GK_VEGA, _SB_GK_GAMMA)


def _set_leg_stored_greeks(
    leg: dict,
    *,
    delta_usd: float,
    theta_per_day_usd: float,
    vega_usd: float,
    gamma: float,
) -> None:
    leg[_SB_GK_DELTA] = float(delta_usd)
    leg[_SB_GK_THETA] = float(theta_per_day_usd)
    leg[_SB_GK_VEGA] = float(vega_usd)
    leg[_SB_GK_GAMMA] = float(gamma)
    for _k in (
        "use_tws_greeks",
        "tws_delta_usd",
        "tws_theta_per_day_usd",
        "tws_vega_usd",
        "tws_gamma",
    ):
        leg.pop(_k, None)


def _leg_greeks_bs(leg: dict, spot: float) -> dict:
    iv = leg.get("iv") or st.session_state["sb_iv"]
    dte = _dte(leg["expiry"])
    sign = -1 if leg["leg_type"] == "Short" else 1
    n = int(leg.get("contracts", 1))
    if dte <= 0 or spot <= 0 or iv <= 0:
        return {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    g = calc_greeks(spot, leg["strike"], dte, iv, leg["right"])
    return {
        "delta": (g.get("delta") or 0) * sign * n * 100,
        "theta": (g.get("theta") or 0) * sign * n * 100,
        "vega": (g.get("vega") or 0) * sign * n * 100,
        "gamma": (g.get("gamma") or 0) * sign * n * 100,
    }


def _migrate_sb_leg_greeks(leg: dict, spot: float) -> None:
    if all(k in leg for k in _SB_LEG_GREEK_KEYS):
        for _k in (
            "use_tws_greeks",
            "tws_delta_usd",
            "tws_theta_per_day_usd",
            "tws_vega_usd",
            "tws_gamma",
        ):
            leg.pop(_k, None)
        return
    if leg.get("use_tws_greeks") and "tws_delta_usd" in leg:
        _set_leg_stored_greeks(
            leg,
            delta_usd=float(leg.get("tws_delta_usd") or 0.0),
            theta_per_day_usd=float(leg.get("tws_theta_per_day_usd") or 0.0),
            vega_usd=float(leg.get("tws_vega_usd") or 0.0),
            gamma=float(leg.get("tws_gamma") or 0.0),
        )
    else:
        _g = _leg_greeks_bs(leg, spot)
        _set_leg_stored_greeks(
            leg,
            delta_usd=_g["delta"],
            theta_per_day_usd=_g["theta"],
            vega_usd=_g["vega"],
            gamma=_g["gamma"],
        )


def _make_sb_leg(
    leg_id: int,
    leg_type: str,
    right: str,
    strike: float,
    expiry: str,
    contracts: int,
    spot: float,
    iv: float,
) -> dict:
    dte = max(1, _dte(expiry))
    ep = bs_price(spot, strike, dte, iv, right)
    ep = round(max(0.01, ep or 0.5), 2)
    leg = {
        "id": leg_id,
        "leg_type": leg_type,
        "right": right,
        "strike": float(strike),
        "expiry": expiry,
        "contracts": int(contracts),
        "entry_price": ep,
        "iv": float(iv),
    }
    _g0 = _leg_greeks_bs(leg, spot)
    _set_leg_stored_greeks(
        leg,
        delta_usd=_g0["delta"],
        theta_per_day_usd=_g0["theta"],
        vega_usd=_g0["vega"],
        gamma=_g0["gamma"],
    )
    return leg


def _leg_greeks(leg: dict, spot: float) -> dict:
    if all(k in leg for k in _SB_LEG_GREEK_KEYS):
        return {
            "delta": float(leg[_SB_GK_DELTA]),
            "theta": float(leg[_SB_GK_THETA]),
            "vega": float(leg[_SB_GK_VEGA]),
            "gamma": float(leg[_SB_GK_GAMMA]),
        }
    return _leg_greeks_bs(leg, spot)


def _pnl_at_exp(leg: dict, spot_val: float) -> float:
    n       = int(leg.get("contracts", 1))
    entry   = float(leg.get("entry_price", 0))
    strike  = float(leg["strike"])
    right   = leg["right"]
    lt      = leg["leg_type"]
    intrinsic = max(0.0, spot_val - strike) if right == "C" else max(0.0, strike - spot_val)
    if lt == "Short":
        return (entry - intrinsic) * n * 100
    else:
        return (intrinsic - entry) * n * 100


def _pnl_at_dte(leg: dict, spot_val: float, dte_v: int) -> float:
    n      = int(leg.get("contracts", 1))
    entry  = float(leg.get("entry_price", 0))
    strike = float(leg["strike"])
    right  = leg["right"]
    lt     = leg["leg_type"]
    iv     = leg.get("iv") or st.session_state["sb_iv"]
    if dte_v <= 0:
        theo = max(0.0, spot_val - strike) if right == "C" else max(0.0, strike - spot_val)
    else:
        theo = bs_price(spot_val, strike, dte_v, iv, right) or 0.0
    if lt == "Short":
        return (entry - theo) * n * 100
    else:
        return (theo - entry) * n * 100


_apply_sb_pending_patch()

# ─── Panel: Spot + globálne IV ─────────────────────────────────────────────────
with st.container():
    hc1, hc2, hc3 = st.columns([2, 2, 2])
    _ticker_input = hc1.text_input("Ticker", value=st.session_state.get("sb_ticker", "AMZN"),
                                    key="sb_ticker_inp").upper()
    st.session_state["sb_ticker"] = _ticker_input

    _spot_val = hc2.number_input(
        "Spot ($)", min_value=1.0, step=0.5,
        value=float(st.session_state["sb_spot"]), key="sb_spot_inp",
    )
    st.session_state["sb_spot"] = _spot_val

    _iv_val = hc3.number_input(
        "Globálna IV (0.30 = 30%)", min_value=0.01, max_value=5.0, step=0.01,
        value=float(st.session_state["sb_iv"]), key="sb_iv_inp",
        help="Použije sa pre nohy bez vlastnej IV",
    )
    st.session_state["sb_iv"] = _iv_val

    if ibkr.is_connected():
        if st.button("📡 Načítať Spot z IBKR", key="sb_load_spot"):
            with st.spinner(f"Načítavam spot pre {_ticker_input}..."):
                _res = ibkr.fetch_underlying(_ticker_input, timeout=6.0)
            if not _res.get("error") and _res.get("price"):
                st.session_state["_sb_pending_patch"] = {"op": "spot", "spot": float(_res["price"])}
                st.rerun()
            else:
                st.warning(_res.get("error", "Spot nenájdený"))

# ─── Centrálny katalóg expirácií ───────────────────────────────────────────────
with st.expander("📅 Centrálny katalóg expirácií (výber v celom Spread Builderi)", expanded=False):
    st.caption(
        "Jeden zoznam **YYYYMMDD** pre **Pridať nohu**, **úpravu nôh** a automatické posuny kalendára. "
        "Kým nič neuložíš, používa sa generátor (piatky + 3. piatok). Dátumy z TWS, ktoré generátor nemá, sem **doplň**; "
        "chybné alebo neobchodovateľné dni **vymaž** (výber alebo pole nižšie)."
    )
    _cat_now = get_catalog_expiries()
    st.metric("Počet dátumov v katalógu", len(_cat_now))
    _rm_lbls, _rm_map = format_expiry_select_options(_cat_now)
    if _rm_lbls:
        _pick_rm = st.multiselect(
            "Vymazať z katalógu (napr. deň, ktorý TWS neponúka)",
            options=_rm_lbls,
            default=[],
            key="sb_exp_cat_pick_rm",
        )
        if st.button("🗑️ Vymazať vybrané dátumy", key="sb_exp_cat_rm_selected"):
            if _pick_rm:
                remove_expiries_from_catalog([_rm_map[x] for x in _pick_rm])
                st.success(f"Odstránených dátumov: {len(_pick_rm)}")
                st.rerun()
            else:
                st.warning("Vyber aspoň jeden dátum.")
    _ta_rm = st.text_area(
        "Alebo dátumy na vymazanie — jeden na riadok (YYYYMMDD, 2026-05-29, 29.05.2026)",
        height=72,
        key="sb_exp_cat_rm_lines",
        placeholder="20260529\n29.05.2026",
    )
    if st.button("🗑️ Odstrániť dátumy z poľa", key="sb_exp_cat_rm_lines_btn"):
        if (_ta_rm or "").strip():
            remove_expiries_from_text(_ta_rm)
            st.success("Zadané dátumy odstránené (ak boli v katalógu).")
            st.rerun()
        else:
            st.warning("Pole je prázdne.")
    st.divider()
    _ta_add = st.text_area(
        "Pridať expirácie (YYYYMMDD alebo YYYY-MM-DD, jeden riadok = jeden dátum)",
        height=88,
        key="sb_exp_cat_add_lines",
        placeholder="20260522\n20260619",
    )
    _c1, _c2, _c3, _c4 = st.columns(4)
    with _c1:
        if st.button("➕ Pridať riadky do katalógu", key="sb_exp_cat_btn_append"):
            if (_ta_add or "").strip():
                append_expiries_from_text(_ta_add, months=18)
                st.success("Doplnené.")
                st.rerun()
            else:
                st.warning("Nič na pridanie.")
    with _c2:
        if st.button("⧉ Zlúčiť s generovaným", key="sb_exp_cat_btn_merge"):
            merge_catalog_with_generated(months=18)
            st.success("Zlúčené s generovaným zoznamom.")
            st.rerun()
    with _c3:
        if st.button("🔁 Nahradiť generovaným", key="sb_exp_cat_btn_replace"):
            replace_catalog_with_generated(months=18)
            st.success("Katalóg = len generované piatky / mesačné.")
            st.rerun()
    with _c4:
        pass
    _bulk = st.text_area(
        "Upraviť celý zoznam (prepíše katalóg)",
        value="\n".join(_cat_now),
        height=160,
        key="sb_exp_cat_bulk_edit",
        help="Jeden YYYYMMDD na riadok. Neplatné riadky sa vyhodia.",
    )
    if st.button("💾 Uložiť katalóg z tohto poľa", key="sb_exp_cat_btn_save_bulk"):
        _lines: list[str] = []
        for _ln in (_bulk or "").splitlines():
            _s = _ln.strip().replace("-", "").replace(".", "")
            if len(_s) == 8 and _s.isdigit():
                _lines.append(_s)
        save_catalog_expiries(_lines)
        st.success("Katalóg uložený.")
        st.rerun()

# ─── Zoznam uložených nápadov ─────────────────────────────────────────────────
_ideas_list = db.list_spread_builder_ideas()
_hl, _hr = st.columns([4, 1])
with _hl:
    st.subheader("📋 Zoznam nápadov")
with _hr:
    if st.button(
        "➕ Pridať nový nápad",
        type="primary",
        key="sb_btn_new_napad",
        use_container_width=True,
        help="Vyčistí editor: žiadne nohy, nový názov — uložením vznikne nový záznam v tabuľke.",
    ):
        _queue_sb_new_draft()
        st.rerun()

if st.session_state.get("sb_active_idea_id"):
    st.info(
        f"Upravuješ uložený nápad #{st.session_state['sb_active_idea_id']}. "
        "Prepísať ho môžeš tlačidlom „Uložiť do aktuálneho nápadu“; "
        "ak chceš pôvod nechať a skúšať úpravy, použi „Uložiť ako nový variant“. "
        "Čistý draft: „Pridať nový nápad“."
    )
else:
    st.success(
        "Nový nápad — ešte nie je v databáze. Poskladaj nohy, doplň názov a v expandéri ulož "
        "(prvýkrát vznikne nový riadok; variant vždy nový riadok)."
    )

if _ideas_list:
    _list_df = pd.DataFrame(
        [
            {
                "ID": r["id"],
                "Názov": r["name"],
                "Ticker": r.get("ticker") or "—",
                "Spot ($)": r["spot"],
                "IV %": round(float(r["global_iv"]) * 100, 1),
                "Marža ($)": r["maint_margin"],
                "Nohy": int(r.get("leg_count", 0)),
                "Bodov trendu": int(r.get("snapshot_count", 0)),
                "Variant z": (
                    f"#{int(r['variant_of_id'])}"
                    if r.get("variant_of_id") is not None
                    else "—"
                ),
                "Upravené": r.get("updated_at") or "",
            }
            for r in _ideas_list
        ]
    )
    st.dataframe(
        _list_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Spot ($)": st.column_config.NumberColumn(format="%.2f"),
            "Marža ($)": st.column_config.NumberColumn(format="%.0f"),
        },
    )
    st.caption(
        "Načítanie = vybraný riadok do editora. Uložiť môžeš ako prepis aktívneho záznamu alebo ako nový variant (pôvodný riadok ostane)."
    )
else:
    st.caption("Zatiaľ žiadne riadky v tabuľke — po prvom **Uložiť** sa nápad objaví tu.")

# ─── Uložené nápady (DB) ───────────────────────────────────────────────────────
with st.expander("📂 Uložené nápady — vyber, načítaj, ulož, trend APTR, vymaž", expanded=False):
    _opt_labels = {"—": 0}
    for _row in _ideas_list:
        _vf = _row.get("variant_of_id")
        _vs = f" ← #{int(_vf)}" if _vf is not None else ""
        _opt_labels[f"{_row['name']} (#{_row['id']}){_vs}"] = int(_row["id"])
    _lbl_keys = list(_opt_labels.keys())
    _default_lbl = "—"
    if st.session_state.get("sb_active_idea_id"):
        for _lk, _vid in _opt_labels.items():
            if _vid == st.session_state["sb_active_idea_id"]:
                _default_lbl = _lk
                break
    try:
        _idx_pick = _lbl_keys.index(_default_lbl) if _default_lbl in _lbl_keys else 0
    except ValueError:
        _idx_pick = 0
    _sel_lbl = st.selectbox(
        "Vyber nápad",
        options=_lbl_keys,
        index=_idx_pick,
        key="sb_pick_idea_lbl",
    )
    _picked_id = int(_opt_labels[_sel_lbl])

    _b1, _b2, _b3, _b4 = st.columns(4)
    with _b1:
        if st.button("📥 Načítať", key="sb_load_idea", disabled=_picked_id == 0):
            _idea = db.get_spread_builder_idea(_picked_id)
            if _idea:
                try:
                    _loaded_legs = json.loads(_idea["legs_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    st.session_state["sb_legs"] = []
                    st.error("Nohy v databáze sú poškodené — skús iný nápad alebo ulož znova.")
                else:
                    st.session_state["_sb_pending_patch"] = {
                        "op": "load",
                        "legs": _loaded_legs,
                        "ticker": (_idea.get("ticker") or "AMZN").upper(),
                        "spot": float(_idea["spot"]),
                        "iv": float(_idea["global_iv"]),
                        "maint_margin": float(_idea.get("maint_margin") or 0),
                        "idea_id": _picked_id,
                        "name": _idea.get("name") or "",
                        "notes": _idea.get("notes") or "",
                    }
                    st.rerun()
            else:
                st.warning("Nápad sa v databáze nenašiel.")
    with _b2:
        if st.button("🆕 Pridať nový nápad (rovnako ako hore)", key="sb_new_draft"):
            _queue_sb_new_draft()
            st.rerun()
    with _b3:
        _sb_del_confirm = st.checkbox("Potvrdiť vymazanie", key="sb_del_confirm")
    with _b4:
        if st.button(
            "🗑 Vymazať nápad",
            key="sb_del_idea",
            disabled=_picked_id == 0 or not _sb_del_confirm,
        ):
            db.delete_spread_builder_idea(_picked_id)
            if st.session_state.get("sb_active_idea_id") == _picked_id:
                st.session_state["sb_active_idea_id"] = None
            st.success("Nápad vymazaný.")
            st.rerun()

    _save_name = st.text_input(
        "Názov pri uložení",
        key="sb_save_name_input",
        placeholder="napr. AMZN PMCC skúška",
    )
    _idea_notes = st.text_area(
        "Poznámka k nápadu (uloží sa do DB)",
        key="sb_idea_notes_area",
        height=68,
    )

    def _sb_payload_for_save() -> tuple[str, str, float, float, float, list, str] | None:
        if not st.session_state["sb_legs"]:
            return None
        _sn = (_save_name or "").strip() or "Bez názvu"
        _tk = st.session_state.get("sb_ticker", "AMZN")
        _sp = float(st.session_state["sb_spot"])
        _ivs = float(st.session_state["sb_iv"])
        _mm = float(st.session_state.get("sb_maint_margin", 0) or 0)
        _legs_copy = json.loads(json.dumps(st.session_state["sb_legs"]))
        return _sn, _tk, _sp, _ivs, _mm, _legs_copy, _idea_notes

    _sb_save = st.columns(2)
    with _sb_save[0]:
        if st.button(
            "💾 Uložiť do aktuálneho nápadu",
            type="primary",
            key="sb_save_idea_db",
            help="Ak máš načítaný nápad z DB, prepíše ten istý riadok. Ak nie, vytvorí prvý nový záznam.",
        ):
            _pl = _sb_payload_for_save()
            if _pl is None:
                st.warning("Najprv pridaj aspoň jednu nohu spreadu.")
            else:
                _sn, _tk, _sp, _ivs, _mm, _legs_copy, _notes = _pl
                _aid = st.session_state.get("sb_active_idea_id")
                if _aid:
                    db.update_spread_builder_idea(
                        int(_aid), _sn, _tk, _sp, _ivs, _mm, _legs_copy, _notes
                    )
                    st.success(f"Aktualizované (#{_aid}).")
                else:
                    _new_id = db.insert_spread_builder_idea(
                        _sn, _tk, _sp, _ivs, _mm, _legs_copy, _notes
                    )
                    st.session_state["sb_active_idea_id"] = _new_id
                    st.success(f"Uložené ako nový nápad #{_new_id}.")
                st.rerun()
    with _sb_save[1]:
        if st.button(
            "📑 Uložiť ako nový variant",
            key="sb_save_idea_variant",
            help="Vždy nový riadok v tabuľke. Pôvodný nápad ostane nezmenený. Ak máš aktívny nápad, nový riadok sa k nemu prepojí (stĺpec Variant z).",
        ):
            _pl = _sb_payload_for_save()
            if _pl is None:
                st.warning("Najprv pridaj aspoň jednu nohu spreadu.")
            else:
                _sn, _tk, _sp, _ivs, _mm, _legs_copy, _notes = _pl
                _parent = st.session_state.get("sb_active_idea_id")
                _new_id = db.insert_spread_builder_idea(
                    _sn,
                    _tk,
                    _sp,
                    _ivs,
                    _mm,
                    _legs_copy,
                    _notes,
                    variant_of_id=int(_parent) if _parent else None,
                )
                st.session_state["sb_active_idea_id"] = _new_id
                if _parent:
                    st.success(f"Nový variant #{_new_id} (odvodený od #{_parent}). Zmeň názov vyššie, ak chceš varianty rozlíšiť.")
                else:
                    st.success(f"Uložené ako nový nápad #{_new_id} (bez nadriadeného — najprv načítaj pôvod, ak chceš väzbu variantu).")
                st.rerun()

    if st.session_state.get("sb_active_idea_id"):
        st.caption(f"Aktívny nápad v DB: **#{st.session_state['sb_active_idea_id']}** — body trendu viažu na tento záznam.")
    else:
        st.caption("Bez aktívneho ID v DB sa trend neukladá — ulož nápad aspoň raz.")

st.divider()

# ─── Šablóny stratégií ─────────────────────────────────────────────────────────
_SB_STRATEGY_OPTIONS: dict[str, Optional[str]] = {
    "— Manuálne (bez šablóny) —": None,
    "Kalendárny spread": "calendar",
    "Železný kondor (kredit)": "iron_condor",
    "Vertikálny call spread (debet)": "vertical_call_debit",
    "Vertikálny put spread (debet)": "vertical_put_debit",
}

with st.expander("📋 Šablóna stratégie — predvyplnené nohy", expanded=False):
    st.caption(
        "1) Zadaj **Ticker**, **Spot** a **IV** vyššie (pre KO napr. spot z IBKR). "
        "2) Vyber stratégiu. 3) **Nastaviť nohy** — **prepíše celý zoznam nôh**; expirácie berie z **centrálneho katalógu** (predvolene generované piatky / mesačné; vieš ho upraviť v expandéri vyššie). "
        "Úvodné ceny nôh sú **BS odhad** — v tabuľke ich uprav podľa bid/ask z TWS."
    )
    _strat_lbl = st.selectbox(
        "Stratégia",
        list(_SB_STRATEGY_OPTIONS.keys()),
        key="sb_strat_lbl",
    )
    _strat_id = _SB_STRATEGY_OPTIONS[_strat_lbl]
    if _strat_id == "calendar":
        st.selectbox(
            "Kalendár — typ opcie",
            ["C", "P"],
            key="sb_strat_cal_right",
            format_func=lambda x: "Call" if x == "C" else "Put",
        )
        st.selectbox(
            "Kalendár — strike",
            ["atm", "otm", "manual"],
            key="sb_strat_cal_strike",
            format_func=lambda x: (
                "ATM — referenčný strike (najbližší k spotu po 0,5 $)"
                if x == "atm"
                else (
                    "OTM — o koľko úrovní od ATM (nie od spotu)"
                    if x == "otm"
                    else "Ručný strike ($) — presná hodnota (zaokrúhli sa na 0,5 $)"
                )
            ),
            help="ATM = zaokrúhlený strike k spotu. OTM = call smerom nahor / put smerom dolu o N×0,5 $ od ATM. Ručný = vlastný strike (napr. podľa TWS).",
        )
        if st.session_state.get("sb_strat_cal_strike", "atm") == "manual":
            st.number_input(
                "Kalendár — ručný strike ($)",
                min_value=0.5,
                step=0.5,
                value=float(st.session_state.get("sb_spot", 100)),
                key="sb_strat_cal_manual_k",
            )
        if st.session_state.get("sb_strat_cal_strike", "atm") == "otm":
            st.number_input(
                "OTM — počet úrovní od ATM (1 úroveň = 0,5 $)",
                min_value=1,
                max_value=50,
                value=1,
                step=1,
                key="sb_strat_cal_otm_levels",
                help="Napr. pri ATM 75 a 2 úrovne: call strike 76, put strike 74.",
            )
    if _strat_id == "iron_condor":
        st.caption(
            "ATM = strike najbližší k spotu (0,5 $). **Telo** = vzdialenosť short put/call od ATM v úrovniach. "
            "**Krídlo** = šírka medzi short a long na každej strane (tiež v úrovniach × 0,5 $)."
        )
        st.number_input(
            "Kondor — telo (úrovne od ATM k short strike)",
            min_value=1,
            max_value=40,
            value=2,
            step=1,
            key="sb_strat_ic_body",
        )
        st.number_input(
            "Kondor — krídlo (úrovne short → long)",
            min_value=1,
            max_value=40,
            value=2,
            step=1,
            key="sb_strat_ic_wing",
        )
    _strat_k = st.number_input(
        "Kontrakty (šablóna)",
        min_value=1,
        step=1,
        value=1,
        key="sb_strat_contracts",
    )
    _strat_apply = st.button(
        "Nastaviť nohy z šablóny",
        type="primary",
        key="sb_strat_apply",
        disabled=_strat_id is None,
    )
    if _strat_apply and _strat_id is not None:
        _payload: dict = {
            "op": "strategy",
            "template": _strat_id,
            "spot": float(st.session_state.get("sb_spot", 100)),
            "iv": float(st.session_state.get("sb_iv", 0.30)),
            "contracts": int(_strat_k),
        }
        if _strat_id == "calendar":
            _payload["right"] = str(st.session_state.get("sb_strat_cal_right", "C"))
            _payload["calendar_strike"] = str(
                st.session_state.get("sb_strat_cal_strike", "atm")
            )
            _payload["calendar_otm_levels"] = int(
                st.session_state.get("sb_strat_cal_otm_levels", 1) or 1
            )
            if st.session_state.get("sb_strat_cal_strike", "atm") == "manual":
                _payload["calendar_manual_strike"] = float(
                    st.session_state.get("sb_strat_cal_manual_k", st.session_state.get("sb_spot", 100))
                )
        if _strat_id == "iron_condor":
            _payload["ic_body_levels"] = int(st.session_state.get("sb_strat_ic_body", 2) or 2)
            _payload["ic_wing_levels"] = int(st.session_state.get("sb_strat_ic_wing", 2) or 2)
        st.session_state["_sb_pending_patch"] = _payload
        st.rerun()

# ─── Panel: Pridanie nohy ──────────────────────────────────────────────────────
with st.expander("➕ Pridať nohu", expanded=len(st.session_state["sb_legs"]) == 0):
    lc1, lc2, lc3, lc4 = st.columns(4)
    _add_lt     = lc1.selectbox("Long / Short", ["Long", "Short"], key="sb_add_lt")
    _add_right  = lc2.selectbox("Call / Put",   ["C", "P"],        key="sb_add_right",
                                 format_func=lambda x: "Call" if x=="C" else "Put")
    _add_strike = lc3.number_input("Strike ($)", min_value=0.5, step=0.5,
                                    value=float(st.session_state["sb_spot"]), key="sb_add_strike")
    _add_contr  = lc4.number_input("Kontrakty", min_value=1, step=1, value=1, key="sb_add_contr")

    lc5, lc6, lc7 = st.columns(3)
    _exps = get_catalog_expiries(months=18)
    _add_lbls, _add_exp_map = format_expiry_select_options(_exps)
    if not _add_lbls:
        st.error(
            "Katalóg expirácií je prázdny — v expandéri „Centrálny katalóg“ použi **Zlúčiť s generovaným**."
        )
        _add_exp = ""
    else:
        _sel_exp_lbl = lc5.selectbox("Expirácia", _add_lbls, key="sb_add_exp_sel")
        _add_exp = _add_exp_map[_sel_exp_lbl]

    _add_entry = lc6.number_input(
        "Vstupná cena ($)", min_value=0.01, step=0.05,
        value=round(max(0.01,
            bs_price(_spot_val, _add_strike, max(1, _dte(_add_exp)), _iv_val,
                     _add_right) or 0.5), 2),
        key="sb_add_entry",
        help="BS odhad je predvyplnený – uprav podľa trhu",
    )
    _add_leg_iv = lc7.number_input(
        "IV pre túto nohu", min_value=0.01, max_value=5.0, step=0.01,
        value=_iv_val, key="sb_add_leg_iv",
        help="Nechaj rovnakú ako globálna IV, alebo uprav pre konkrétnu nohu",
    )
    if _add_exp:
        _add_g_prev = _leg_greeks_bs(
            {
                "leg_type": _add_lt,
                "right": _add_right,
                "strike": float(_add_strike),
                "expiry": _add_exp,
                "contracts": int(_add_contr),
                "entry_price": float(_add_entry),
                "iv": float(_add_leg_iv),
            },
            _spot_val,
        )
    else:
        _add_g_prev = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    _ag1, _ag2, _ag3, _ag4 = st.columns(4)
    _add_th = _ag1.number_input(
        "Θ $/deň",
        min_value=-999999.0,
        max_value=999999.0,
        step=0.25,
        value=float(_add_g_prev["theta"]),
        key="sb_add_leg_theta",
    )
    _add_d = _ag2.number_input(
        "Δ $",
        min_value=-999999.0,
        max_value=999999.0,
        step=10.0,
        value=float(_add_g_prev["delta"]),
        key="sb_add_leg_delta",
    )
    _add_v = _ag3.number_input(
        "Vega $",
        min_value=-999999.0,
        max_value=999999.0,
        step=1.0,
        value=float(_add_g_prev["vega"]),
        key="sb_add_leg_vega",
    )
    _add_gm = _ag4.number_input(
        "Gamma",
        min_value=-999.0,
        max_value=999.0,
        step=0.0001,
        format="%.4f",
        value=float(_add_g_prev["gamma"]),
        key="sb_add_leg_gamma",
    )
    st.caption("**Citácie z TWS** (voliteľné; 0 = prázdne — len referencia, nie P&L graf)")
    _at1, _at2, _at3 = st.columns(3)
    _add_tws_bid = _at1.number_input("TWS Bid ($)", min_value=0.0, step=0.01, value=0.0, key="sb_add_tws_bid")
    _add_tws_ask = _at2.number_input("TWS Ask ($)", min_value=0.0, step=0.01, value=0.0, key="sb_add_tws_ask")
    _add_tws_last = _at3.number_input("TWS Last ($)", min_value=0.0, step=0.01, value=0.0, key="sb_add_tws_last")

    if st.button("✅ Pridať nohu", type="primary", key="sb_btn_add"):
        if not _add_exp:
            st.warning("Najprv doplni katalóg expirácií (expandér „Centrálny katalóg“).")
        else:
            _nl = {
                "id":         len(st.session_state["sb_legs"]) + 1,
                "leg_type":   _add_lt,
                "right":      _add_right,
                "strike":     _add_strike,
                "expiry":     _add_exp,
                "contracts":  int(_add_contr),
                "entry_price": _add_entry,
                "iv":          _add_leg_iv,
            }
            _merge_leg_tws_quote_fields(_nl, bid=_add_tws_bid, ask=_add_tws_ask, last=_add_tws_last)
            _set_leg_stored_greeks(
                _nl,
                delta_usd=float(_add_d),
                theta_per_day_usd=float(_add_th),
                vega_usd=float(_add_v),
                gamma=float(_add_gm),
            )
            st.session_state["sb_legs"].append(_nl)
            st.rerun()

# ─── Tabuľka nôh ──────────────────────────────────────────────────────────────
legs = st.session_state["sb_legs"]
for _mleg in legs:
    _migrate_sb_leg_greeks(_mleg, float(st.session_state["sb_spot"]))

if not legs:
    st.info("Žiadne nohy. Pridaj aspoň jednu nohu spreadu vyššie.")
    st.stop()

_sb_sync_note = st.session_state.pop("_sb_sync_notice", None)
if _sb_sync_note:
    st.success(_sb_sync_note)

st.markdown(f"### Nohy spreadu  ({len(legs)})")

# Riadky tabuľky + Greeks
_spot = st.session_state["sb_spot"]
rows  = []
tot   = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
for i, leg in enumerate(legs):
    g = _leg_greeks(leg, _spot)
    for k in tot:
        tot[k] += g[k]
    _bs_est = bs_price(_spot, leg["strike"], max(1, _dte(leg["expiry"])),
                       leg.get("iv") or _iv_val, leg["right"]) or 0.0
    _tb, _ta = float(leg.get("tws_bid") or 0), float(leg.get("tws_ask") or 0)
    if _tb > 0 and _ta > 0:
        _tws_ba = f"{_tb:.2f} / {_ta:.2f}"
    elif _tb > 0:
        _tws_ba = f"{_tb:.2f} / —"
    elif _ta > 0:
        _tws_ba = f"— / {_ta:.2f}"
    else:
        _tws_ba = "—"
    _tl = float(leg.get("tws_last") or 0)
    _tws_l = f"{_tl:.2f}" if _tl > 0 else "—"
    rows.append({
        "#":             i + 1,
        "L/S":           leg["leg_type"],
        "C/P":           "Call" if leg["right"] == "C" else "Put",
        "Strike":        leg["strike"],
        "Expiry":        leg["expiry"],
        "DTE":           _dte(leg["expiry"]),
        "Kontr.":        leg["contracts"],
        "Vstup $":       leg["entry_price"],
        "BS odhad $":    round(_bs_est, 2),
        "IV":            f"{leg.get('iv', _iv_val)*100:.1f}%",
        "TWS bid/ask":   _tws_ba,
        "TWS Last":      _tws_l,
        "Theta $/deň":   round(g["theta"], 2),
        "Delta $":       round(g["delta"], 0),
        "Vega $":        round(g["vega"], 2),
        "Gamma":         round(g["gamma"], 4),
    })

df_legs = pd.DataFrame(rows)
st.dataframe(
    df_legs, use_container_width=True, hide_index=True,
    column_config={
        "Strike":       st.column_config.NumberColumn(format="$%.2f"),
        "Vstup $":      st.column_config.NumberColumn(format="$%.2f"),
        "BS odhad $":   st.column_config.NumberColumn(format="$%.2f"),
        "Theta $/deň":  st.column_config.NumberColumn(format="$%+.2f"),
        "Delta $":      st.column_config.NumberColumn(format="$%+.0f"),
        "Vega $":       st.column_config.NumberColumn(format="$%+.2f"),
        "TWS bid/ask":  st.column_config.TextColumn(),
        "TWS Last":     st.column_config.TextColumn(),
    },
)
st.caption(
    "Θ / Δ / Vega / Gamma = hodnoty zadané pri nohe (predvolene BS podľa IV a striku). **P&L diagram** zostáva z BS (IV + vstupná cena)."
)

st.markdown("##### Upraviť nohu")
_sel_ix = st.selectbox(
    "Vyber nohu na úpravu",
    options=list(range(len(legs))),
    format_func=lambda i: (
        f"#{i + 1} {legs[i]['leg_type']} "
        f"{'Call' if legs[i]['right'] == 'C' else 'Put'} K{legs[i]['strike']} "
        f"{legs[i]['expiry']} ×{legs[i]['contracts']}"
    ),
    key="sb_legedit_pick",
)
_Le = legs[_sel_ix]
_edit_cat = get_catalog_expiries(months=18)
_edit_lbls, _edit_exp_map = format_expiry_select_options(_edit_cat)
_cur_ex = str(_Le["expiry"]).strip().replace("-", "")
if _cur_ex and all(_edit_exp_map[lb] != _cur_ex for lb in _edit_lbls):
    _orphan_lbl = f"⚠ Nie v katalógu — {_cur_ex} (doplň v „Centrálnom katalógu“ alebo zmeň výber)"
    _edit_lbls = [_orphan_lbl] + _edit_lbls
    _edit_exp_map = {**{_orphan_lbl: _cur_ex}, **_edit_exp_map}
_edit_default_lbl = next(
    (lb for lb in _edit_lbls if _edit_exp_map[lb] == _cur_ex),
    _edit_lbls[0] if _edit_lbls else "",
)
with st.form(f"sb_legedit_{_sel_ix}"):
    _f1, _f2 = st.columns(2)
    _e_lt = _f1.selectbox(
        "Long / Short",
        ["Long", "Short"],
        index=0 if _Le["leg_type"] == "Long" else 1,
    )
    _e_rt = _f2.selectbox(
        "Call / Put",
        ["C", "P"],
        index=0 if _Le["right"] == "C" else 1,
        format_func=lambda x: "Call" if x == "C" else "Put",
    )
    _e_st = st.number_input(
        "Strike ($)", min_value=0.5, step=0.5, value=float(_Le["strike"])
    )
    if not _edit_lbls:
        st.error("Katalóg expirácií je prázdny.")
        _e_ex_lbl = ""
    else:
        _e_ex_lbl = st.selectbox(
            "Expirácia",
            _edit_lbls,
            index=_edit_lbls.index(_edit_default_lbl),
            key=f"sb_legedit_exp_{_sel_ix}",
        )
    _f3, _f4 = st.columns(2)
    _e_ct = _f3.number_input(
        "Kontrakty", min_value=1, step=1, value=int(_Le["contracts"])
    )
    _e_en = _f4.number_input(
        "Vstupná cena ($)", min_value=0.01, step=0.05, value=float(_Le["entry_price"])
    )
    _e_iv = st.number_input(
        "IV (0.30 = 30 %)",
        min_value=0.01,
        max_value=5.0,
        step=0.01,
        value=float(_Le.get("iv") or _iv_val),
    )
    _tg1, _tg2, _tg3, _tg4 = st.columns(4)
    with _tg1:
        _e_leg_theta = st.number_input(
            "Θ $/deň",
            min_value=-999999.0,
            max_value=999999.0,
            step=0.25,
            value=float(_Le.get(_SB_GK_THETA, 0.0)),
            key=f"sb_legedit_th_{_sel_ix}",
        )
    with _tg2:
        _e_leg_delta = st.number_input(
            "Δ $",
            min_value=-999999.0,
            max_value=999999.0,
            step=10.0,
            value=float(_Le.get(_SB_GK_DELTA, 0.0)),
            key=f"sb_legedit_dl_{_sel_ix}",
        )
    with _tg3:
        _e_leg_vega = st.number_input(
            "Vega $",
            min_value=-999999.0,
            max_value=999999.0,
            step=1.0,
            value=float(_Le.get(_SB_GK_VEGA, 0.0)),
            key=f"sb_legedit_vg_{_sel_ix}",
        )
    with _tg4:
        _e_leg_gamma = st.number_input(
            "Gamma",
            min_value=-999.0,
            max_value=999.0,
            step=0.0001,
            format="%.4f",
            value=float(_Le.get(_SB_GK_GAMMA, 0.0)),
            key=f"sb_legedit_gm_{_sel_ix}",
        )
    st.checkbox(
        "Po uložení **zosúladiť druhú nohu** — kalendár: rovnaký strike + expirácie podľa mentora; "
        "diagonál / vertikál: zachovaný rozostup strikov (v $)",
        value=True,
        key="sb_legedit_sync_pair",
    )
    st.checkbox(
        "Uložiť aj keď mentor hlási parametre mimo odporúčaného okna (kalendár / diagonál)",
        value=False,
        key="sb_legedit_skip_mentor",
        help="Ak tabuľka mentora vyzerá v poriadku, ale uloženie stále zlyhá, skús vypnúť zosúladenie druhej nohy; "
        "ak problém pretrváva, zapni túto voľbu — riziko nesúladu s konzervatívnymi pravidlami berieš na seba.",
    )
    st.markdown("**Citácie z TWS** (voliteľné; **nepoužívajú** sa v P&L grafe — len referencia). „0“ = prázdne.")
    _tw1, _tw2, _tw3 = st.columns(3)
    with _tw1:
        _e_tws_bid = st.number_input(
            "TWS Bid ($)",
            min_value=0.0,
            step=0.01,
            value=float(_Le.get("tws_bid") or 0),
        )
    with _tw2:
        _e_tws_ask = st.number_input(
            "TWS Ask ($)",
            min_value=0.0,
            step=0.01,
            value=float(_Le.get("tws_ask") or 0),
        )
    with _tw3:
        _e_tws_last = st.number_input(
            "TWS Last ($)",
            min_value=0.0,
            step=0.01,
            value=float(_Le.get("tws_last") or 0),
        )
    if st.form_submit_button("💾 Uložiť zmeny na tejto nohe"):
        _ex_ok = bool(_edit_lbls) and bool(_e_ex_lbl)
        _ex_norm = _edit_exp_map.get(_e_ex_lbl, "").strip().replace("-", "") if _ex_ok else ""
        if not _ex_ok:
            st.error("Vyber expiráciu z katalógu.")
        elif len(_ex_norm) != 8 or not _ex_norm.isdigit():
            st.error("Neplatná expirácia.")
            _ex_ok = False
        else:
            try:
                date(int(_ex_norm[:4]), int(_ex_norm[4:6]), int(_ex_norm[6:8]))
            except ValueError:
                st.error("Neplatný dátum expirácie.")
                _ex_ok = False
        if _ex_ok:
            _allowed_exp = set(get_catalog_expiries(months=18))
            _prev_ex = str(_Le["expiry"]).strip().replace("-", "")
            if _ex_norm not in _allowed_exp and _ex_norm != _prev_ex:
                st.error(
                    "Táto expirácia **nie je v katalógu**. Pridaj ju v expandéri „Centrálny katalóg expirácií“ "
                    "alebo zvoľ iný dátum z rovnakého zdroja ako pri „Pridať nohu“."
                )
                _ex_ok = False
        if _ex_ok:
            _snap_k = ("strike", "expiry", "right", "leg_type")
            _old_e = {k: legs[_sel_ix][k] for k in _snap_k}
            _old_o = (
                {k: legs[1 - _sel_ix][k] for k in _snap_k}
                if len(legs) == 2
                else None
            )
            _try_legs = json.loads(json.dumps(st.session_state["sb_legs"]))
            _try_legs[_sel_ix].update(
                {
                    "leg_type": _e_lt,
                    "right": _e_rt,
                    "strike": float(_e_st),
                    "expiry": _ex_norm,
                    "contracts": int(_e_ct),
                    "entry_price": float(_e_en),
                    "iv": float(_e_iv),
                }
            )
            _set_leg_stored_greeks(
                _try_legs[_sel_ix],
                delta_usd=float(_e_leg_delta),
                theta_per_day_usd=float(_e_leg_theta),
                vega_usd=float(_e_leg_vega),
                gamma=float(_e_leg_gamma),
            )
            _merge_leg_tws_quote_fields(
                _try_legs[_sel_ix],
                bid=_e_tws_bid,
                ask=_e_tws_ask,
                last=_e_tws_last,
            )
            for _j, _lg in enumerate(_try_legs):
                _lg["id"] = _j + 1
            _sync_msgs: list[str] = []
            if (
                len(_try_legs) == 2
                and _old_o is not None
                and st.session_state.get("sb_legedit_sync_pair", True)
            ):
                _sync_msgs = sync_pair_after_edit(_try_legs, _sel_ix, _old_e, _old_o)
            if _sync_msgs and len(_try_legs) == 2:
                _oj = 1 - _sel_ix
                _g_sync = _leg_greeks_bs(_try_legs[_oj], _spot)
                _set_leg_stored_greeks(
                    _try_legs[_oj],
                    delta_usd=_g_sync["delta"],
                    theta_per_day_usd=_g_sync["theta"],
                    vega_usd=_g_sync["vega"],
                    gamma=_g_sync["gamma"],
                )
            _cal_chk = analyze_calendar_mentor(_try_legs)
            _diag_chk = analyze_diagonal_mentor(_try_legs)

            _cal_fail = _cal_chk is not None and (
                _cal_chk.inverted
                or not (_cal_chk.short_ok and _cal_chk.long_ok and _cal_chk.spread_ok)
            )
            _diag_fail = (
                _cal_chk is None
                and _diag_chk is not None
                and (
                    _diag_chk.inverted
                    or not (_diag_chk.short_ok and _diag_chk.long_ok and _diag_chk.spread_ok)
                )
            )
            if st.session_state.get("sb_legedit_skip_mentor", False):
                _cal_fail = False
                _diag_fail = False

            if _cal_fail:
                _why: list[str] = []
                if _cal_chk is not None and _cal_chk.inverted:
                    _why.append("prehodené expirácie (Long musí byť neskôr než Short)")
                if _cal_chk is not None and not _cal_chk.short_ok:
                    _why.append("Short DTE mimo okna kalendárového mentora")
                if _cal_chk is not None and not _cal_chk.long_ok:
                    _why.append("Long DTE mimo okna kalendárového mentora")
                if (
                    _cal_chk is not None
                    and not _cal_chk.spread_ok
                    and not _cal_chk.inverted
                ):
                    _why.append("rozstup expirácií (mesiace) mimo okna kalendárového mentora")
                st.error(
                    "**Kalendárny mentor:** " + "; ".join(_why) + ". "
                    "**Nič sa neuložilo.** Uprav dátumy/strike, vypni zosúladenie druhej nohy, "
                    "alebo zaškrtni *Uložiť aj keď mentor hlási…*."
                )
            elif _diag_fail:
                _dw: list[str] = []
                if _diag_chk is not None and _diag_chk.inverted:
                    _dw.append("prehodené DTE (max Long musí byť ≥ min Short naprieč nohami)")
                if _diag_chk is not None and not _diag_chk.short_ok:
                    _dw.append("Short DTE mimo okna diagonálneho mentora (30–45 dní)")
                if _diag_chk is not None and not _diag_chk.long_ok:
                    _dw.append("Long DTE mimo okna diagonálneho mentora (60–120 dní)")
                if (
                    _diag_chk is not None
                    and not _diag_chk.spread_ok
                    and not _diag_chk.inverted
                ):
                    _dw.append("rozptyl expirácií (1–3 mes.) mimo okna diagonálneho mentora")
                st.error(
                    "**Diagonál / KO mentor:** " + "; ".join(_dw) + ". "
                    "**Nič sa neuložilo.** Uprav dátumy, vypni zosúladenie druhej nohy, "
                    "alebo zaškrtni *Uložiť aj keď mentor hlási…*."
                )
            else:
                st.session_state["sb_legs"] = _try_legs
                if _sync_msgs:
                    st.session_state["_sb_sync_notice"] = "\n\n".join(_sync_msgs)
                st.rerun()

if ibkr.is_connected():
    st.caption(
        "Z IBKR vieš natiahnuť **mid cenu** (bid/ask alebo last) a **IV** pre každý kontrakt — potrebuješ market data na opcie a otvorené TWS/Gateway."
    )
    if st.button(
        "📡 Načítať z IBKR: vstupné ceny + IV pre všetky nohy",
        key="sb_load_legs_ibkr",
        help="Prepíše Vstup $ a IV na každej nohe podľa snapshotu. Pri viacerých nohách to môže trvať desiatky sekúnd.",
    ):
        _tk_ib = (st.session_state.get("sb_ticker") or "").strip().upper()
        if not _tk_ib:
            st.warning("Najprv zadaj ticker hore.")
        else:
            _warn: list[str] = []
            _ok = 0
            _bar = st.progress(0.0, text="Pripravujem…")
            _spot_ib = float(st.session_state.get("sb_spot", _spot))
            for _idx, _leg in enumerate(st.session_state["sb_legs"]):
                _bar.progress(
                    (_idx) / max(1, len(st.session_state["sb_legs"])),
                    text=f"IBKR {_tk_ib} noha {_idx + 1}/{len(st.session_state['sb_legs'])}…",
                )
                _od = ibkr.fetch_option_data(
                    _tk_ib,
                    str(_leg["expiry"]),
                    float(_leg["strike"]),
                    str(_leg["right"]),
                )
                _mid = _od.get("mid")
                if _mid is None and _od.get("bid") and _od.get("ask"):
                    _mid = (_od["bid"] + _od["ask"]) / 2.0
                if _mid is not None and _mid > 0:
                    _leg["entry_price"] = round(float(_mid), 2)
                    _ok += 1
                _iv_ib = _od.get("iv")
                if _iv_ib is not None and float(_iv_ib) > 0:
                    _leg["iv"] = float(_iv_ib)
                if _od.get("error") and (_mid is None or _mid <= 0):
                    _warn.append(
                        f"#{_idx + 1} {_tk_ib} {_leg['expiry']} K{_leg['strike']}{_leg['right']}: {_od.get('error', '?')}"
                    )
                _g_ib = _leg_greeks_bs(_leg, _spot_ib)
                _set_leg_stored_greeks(
                    _leg,
                    delta_usd=_g_ib["delta"],
                    theta_per_day_usd=_g_ib["theta"],
                    vega_usd=_g_ib["vega"],
                    gamma=_g_ib["gamma"],
                )
            _bar.progress(1.0, text="Hotovo.")
            if _ok:
                st.success(
                    f"IBKR: aktualizované ceny pre {_ok} nohu/ôh (IV tam, kde ju API vrátilo). "
                    "Greeks na nohách sú zosúladené s BS podľa aktuálnej IV a striku."
                )
            if _warn:
                st.warning("Problémy:\n" + "\n".join(_warn))
            st.rerun()
else:
    st.caption("Pre automatické ceny a IV z brokera pripoj **TWS / IB Gateway** (rovnako ako pri načítaní spotu).")

# Tlačidlá na mazanie nôh
_del_cols = st.columns(min(len(legs), 6))
for i, leg in enumerate(legs):
    _lbl = f"{'Call' if leg['right']=='C' else 'Put'} ${leg['strike']:.0f} {leg['leg_type'][0]}"
    if _del_cols[i % 6].button(f"🗑 #{i+1} {_lbl}", key=f"sb_del_{i}"):
        st.session_state["sb_legs"].pop(i)
        # Prečísluj ID
        for j, l in enumerate(st.session_state["sb_legs"]):
            l["id"] = j + 1
        st.rerun()

if st.button("🗑 Vymazať všetky nohy", key="sb_clear_all"):
    st.session_state["sb_legs"] = []
    st.rerun()

_sb_mentor = analyze_diagonal_mentor(legs)
_sb_calendar = analyze_calendar_mentor(legs)
with st.expander(
    "Mentor — porovnanie s konzervatívnym nastavením (kalendár / diagonál / KO)",
    expanded=bool(_sb_mentor or _sb_calendar),
):
    st.caption(
        "**Kalendár** (rovnaký strike & call/put): short **25–45 DTE**, long **50–150 DTE**, rozstup **0,5–2,5 mes.** "
        "**Diagonál / KO:** short **30–45 DTE**, long **60–120 DTE**, rozptyl **1–3 mes.** "
        "Orientačné okná — nie investičná rada."
    )
    if _sb_calendar is None and _sb_mentor is None:
        st.info(
            "**Diagonál:** aspoň jedna **Short** a jedna **Long** s expiráciou (YYYYMMDD). "
            "**Kalendárny spread:** navyše rovnaký **strike** a typ opcie (Call/Put) na oboch stranách. "
            "Pri jednej nohe alebo iba long/iba short mentor nehodnotí."
        )
    if _sb_calendar is not None:
        st.markdown("##### Kalendárny spread (rovnaký strike)")
        st.dataframe(
            pd.DataFrame(mentor_calendar_rows(_sb_calendar)),
            use_container_width=True,
            hide_index=True,
            column_config={"Stav": st.column_config.TextColumn()},
        )
        st.markdown("##### Charakteristika (kalendár)")
        for _line in _sb_calendar.summary_lines:
            st.markdown(f"- {_line}")
    if _sb_mentor is not None:
        if _sb_calendar is not None:
            st.markdown("---")
        st.markdown("##### Diagonál / KO (celý setup — všetky nohy)")
        st.dataframe(
            pd.DataFrame(mentor_comparison_rows(_sb_mentor)),
            use_container_width=True,
            hide_index=True,
            column_config={"Stav": st.column_config.TextColumn()},
        )
        st.markdown("##### Charakteristika (diagonál)")
        for _line in _sb_mentor.summary_lines:
            st.markdown(f"- {_line}")

st.divider()

_mg_on = st.checkbox(
    "Ručná úprava súčtových Greeks (Delta, Theta, Vega, Gamma)",
    key="sb_manual_net_greeks_on",
    help="Súčty z tabuľky nôh = súčet zadaných hodnôt na každej nohe. Tieto polia ich prepíšu v metrikách, APTR, trende, denníku a AI — ak chceš úplne vlastné neto bez ohľadu na nohy.",
)
if _mg_on:
    for _gk in ("delta", "theta", "vega", "gamma"):
        if f"sb_man_{_gk}" not in st.session_state:
            st.session_state[f"sb_man_{_gk}"] = float(tot[_gk])
    if st.button("↺ Prevziať súčty z nôh do polí", key="sb_man_greeks_fill_bs"):
        for _gk in ("delta", "theta", "vega", "gamma"):
            st.session_state[f"sb_man_{_gk}"] = float(tot[_gk])
        st.rerun()
    _mx1, _mx2, _mx3, _mx4 = st.columns(4)
    with _mx1:
        st.number_input("Net Delta $", step=10.0, format="%.0f", key="sb_man_delta")
    with _mx2:
        st.number_input("Net Theta $/deň", step=0.25, format="%.2f", key="sb_man_theta")
    with _mx3:
        st.number_input("Net Vega $", step=1.0, format="%.2f", key="sb_man_vega")
    with _mx4:
        st.number_input("Net Gamma", step=0.0001, format="%.4f", key="sb_man_gamma")
    tot_eff = {
        "delta": float(st.session_state["sb_man_delta"]),
        "theta": float(st.session_state["sb_man_theta"]),
        "vega": float(st.session_state["sb_man_vega"]),
        "gamma": float(st.session_state["sb_man_gamma"]),
    }
    st.caption("**Súčty Greeksov** = ručné hodnoty (Theta v grafe trendu APTR a v exporte = táto).")
else:
    for _gk in ("delta", "theta", "vega", "gamma"):
        st.session_state.pop(f"sb_man_{_gk}", None)
    tot_eff = dict(tot)

# ─── Net Greeks + súhrn ───────────────────────────────────────────────────────
st.markdown("### Net Greeks celého spreadu")
_gc1, _gc2, _gc3, _gc4 = st.columns(4)
_gc_help_d = "O koľko sa zmení hodnota spreadu pri pohybe spotu o $1"
_gc_help_t = "Denný časový rozpad celého spreadu"
_gc_help_v = "Zmena hodnoty pri 1% pohybe IV"
_gc_help_g = "Rýchlosť zmeny delty"
if _mg_on:
    _gc_help_d += " (ručne)"
    _gc_help_t += " (ručne)"
    _gc_help_v += " (ručne)"
    _gc_help_g += " (ručne)"
_gc1.metric("Net Delta $",      f"${tot_eff['delta']:+.0f}", help=_gc_help_d)
_gc2.metric("Net Theta $/deň",  f"${tot_eff['theta']:+.2f}", help=_gc_help_t)
_gc3.metric("Net Vega $",       f"${tot_eff['vega']:+.2f}", help=_gc_help_v)
_gc4.metric("Net Gamma",        f"{tot_eff['gamma']:+.4f}", help=_gc_help_g)

# Net kredit / debet
_net_flow = sum(
    (-leg["entry_price"] if leg["leg_type"] == "Long" else leg["entry_price"])
    * leg["contracts"] * 100
    for leg in legs
)
_flow_lbl = "Čistý kredit" if _net_flow >= 0 else "Čistý debet"
st.metric(_flow_lbl, f"${abs(_net_flow):,.0f}",
          help="Suma prijatého prémia mínus zaplatené prémium za celý spread")

_marg_est = estimate_spread_margin_usd(legs)
st.markdown("#### Marža pri otvorení")
_mcol1, _mcol2, _mcol3 = st.columns([1.2, 1.2, 1.6])
_me_usd = _marg_est.get("maintenance_usd")
if _me_usd is None:
    _me_usd = _marg_est.get("initial_usd")
with _mcol1:
    st.metric(
        "Lokálny odhad ($)",
        f"${_me_usd:,.0f}" if _me_usd is not None else "—",
        help="Zjednodušený Reg T / max. strata — nie presná IB Portfolio Margin.",
    )
with _mcol2:
    if _me_usd is not None:
        if st.button(
            "Dosadiť odhad do maržy (APTR)",
            key="sb_margin_apply_est",
            help="Nastaví pole „Modelová udržiavacia marža“ podľa lokálneho odhadu.",
        ):
            st.session_state["sb_maint_margin"] = float(_me_usd)
            st.rerun()
    else:
        st.caption("Lokálny odhad nie je k dispozícii pre túto štruktúru.")
with _mcol3:
    if ibkr.is_connected():
        if st.button(
            "📐 Marža z IB what-if (combo)",
            key="sb_margin_ib_whatif",
            help="Pošle BAG combo do IB a doplní Init/Maint maržu z odpovede; udržiavacia sa dosadí do poľa APTR.",
        ):
            _tk_m = (st.session_state.get("sb_ticker") or "").strip().upper()
            if not _tk_m:
                st.warning("Najprv zadaj ticker.")
            else:
                with st.spinner("IB what-if marža (combo)…"):
                    _wm = ibkr.fetch_spread_whatif_margin(_tk_m, legs)
                if _wm.get("error") and _wm.get("maintenance_margin") is None and _wm.get("initial_margin") is None:
                    st.warning(_wm["error"])
                else:
                    _use_m = _wm.get("maintenance_margin")
                    if _use_m is None:
                        _use_m = _wm.get("initial_margin")
                    if _use_m is not None and _use_m >= 0:
                        st.session_state["sb_maint_margin"] = float(_use_m)
                        st.success(
                            f"IB what-if: init {_wm.get('initial_margin')}, maint {_wm.get('maintenance_margin')} "
                            f"— objednávka { _wm.get('order_action') }, počet combo { _wm.get('combo_quantity') }."
                        )
                        st.rerun()
                    else:
                        st.warning(_wm.get("error") or "IB nevrátil číselnú maržu.")
    else:
        st.caption("Pre **what-if** z IB pripoj TWS / Gateway.")
st.caption(_marg_est.get("note", ""))

st.markdown("#### APTR z Theta (model — rovnako ako TWS Dashboard)")
st.number_input(
    "Modelová udržiavacia marža ($) — pridá sa k net debetu do bázy APTR",
    min_value=0.0,
    step=50.0,
    key="sb_maint_margin",
    help="Náklad = vstupný net debet z prémií + táto marža. Môžeš ju dosadiť odhadom alebo IB what-if vyššie, alebo z TWS (Margin Impact).",
)
_maint_sb = float(st.session_state.get("sb_maint_margin", 0) or 0)
_net_debit_mod = -float(_net_flow)
_aptr_mod = compute_spread_model_theta_aptr_pct(_net_debit_mod, float(tot_eff["theta"]), _maint_sb)
_th_src_lbl = "ručne" if _mg_on else "súčtu nôh"
_th_cap_lbl = "ručný vstup" if _mg_on else "súčet z nôh"
if _aptr_mod is not None:
    st.metric(
        "APTR (Θ)",
        f"{_aptr_mod['yield_pct']:+.1f} %",
        help=f"(Net Theta $/deň z {_th_src_lbl} × 365 / (net debet prémií + marža)) × 100",
    )
    st.caption(
        f"Net debet z prémií: {_aptr_mod['net_debit_usd']:,.0f} USD + marža: {_aptr_mod['maintenance_margin_usd']:,.0f} USD "
        f"= báza {_aptr_mod['capital_basis_usd']:,.0f} USD · Theta ({_th_cap_lbl}): {tot_eff['theta']:+.2f} USD/deň"
    )
else:
    st.caption(
        "APTR teraz nie je: súčet net debetu a marže musí byť väčší ako 0. Pri čistom kredite zväčši maržu, aby bola báza kladná."
    )

_sb_aid = st.session_state.get("sb_active_idea_id")
if _sb_aid:
    st.markdown("##### Trend APTR (uložený nápad)")
    _t1, _t2 = st.columns(2)
    with _t1:
        if st.button(
            "📌 Pridať dnešný bod do trendu",
            key="sb_add_aptr_point",
            disabled=_aptr_mod is None,
            help="Uloží aktuálny APTR, Θ a bázu pod aktívny nápad v DB.",
        ):
            db.append_spread_builder_snapshot(
                int(_sb_aid),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                float(_aptr_mod["yield_pct"]),
                float(tot_eff["theta"]),
                float(_aptr_mod["capital_basis_usd"]),
                float(_spot),
                float(_iv_val),
            )
            st.success("Bod pridaný.")
            st.rerun()
    with _t2:
        st.caption("Po pár dňoch znovu načítaj nápad, skontroluj BS/spot a pridaj ďalší bod.")
    _sb_hist = db.get_spread_builder_snapshots(int(_sb_aid), limit=120)
    if len(_sb_hist) >= 2:
        _sdf = pd.DataFrame(_sb_hist)
        _sdf["Čas"] = pd.to_datetime(_sdf["captured_at"], utc=True)
        _sdf = _sdf.sort_values("Čas")
        _sline = _sdf.set_index("Čas")["aptr_pct"].rename("APTR Θ %")
        st.caption("Vývoj **APTR (Θ)** pre tento nápad (body = tlačidlo vyššie).")
        _sb_plot_aptr_trend(_sline, chart_key=f"sb_aptr_trend_{_sb_aid}", height=220)
    elif len(_sb_hist) == 1:
        st.caption("Máš jeden bod — po ďalšom **Pridať dnešný bod** sa zobrazí graf.")
else:
    st.caption("Pre **trend APTR** najprv **ulož nápad** do databázy (expandér *Uložené nápady*).")

st.divider()

# ─── P&L diagram ──────────────────────────────────────────────────────────────
st.markdown("### P&L diagram")

# Najkratšia expirácia (referenčná pre DTE slider a SD pásma)
_min_dte = min((_dte(l["expiry"]) for l in legs), default=30)
_min_dte = max(_min_dte, 1)

_show_dte = st.slider(
    "Zobraziť P&L k tomuto DTE",
    min_value=0, max_value=_min_dte,
    value=min(_min_dte, max(1, _min_dte // 2)),
    step=1, key="sb_dte_slider",
)

_price_range = np.linspace(_spot * 0.65, _spot * 1.35, 500)

def _combined_pnl(price_arr, dte_v):
    result = np.zeros(len(price_arr))
    for leg in legs:
        # Pre nohy s dlhším DTE ako slider: vypočítaj zostatok
        _leg_dte_now = _dte(leg["expiry"])
        _elapsed = _min_dte - dte_v
        _leg_dte_at = max(0, _leg_dte_now - _elapsed)
        for j, s in enumerate(price_arr):
            result[j] += _pnl_at_dte(leg, float(s), _leg_dte_at)
    return np.round(result, 0)

_pnl_exp     = _combined_pnl(_price_range, 0)
_pnl_now     = _combined_pnl(_price_range, _min_dte)
_pnl_slider  = _combined_pnl(_price_range, _show_dte)

fig = go.Figure()

# Farebná plocha pre slider P&L
fig.add_trace(go.Scatter(
    x=_price_range, y=np.where(_pnl_slider >= 0, _pnl_slider, 0),
    fill="tozeroy", fillcolor="rgba(46,204,113,0.07)",
    line=dict(width=0), showlegend=False, hoverinfo="skip",
))
fig.add_trace(go.Scatter(
    x=_price_range, y=np.where(_pnl_slider < 0, _pnl_slider, 0),
    fill="tozeroy", fillcolor="rgba(231,76,60,0.06)",
    line=dict(width=0), showlegend=False, hoverinfo="skip",
))

# Časové rezy
_time_slices = [
    (_min_dte,               "#60a5fa", f"Teraz ({_min_dte}d)", 2.0),
    (max(1, _min_dte * 2//3), "#a78bfa", f"{_min_dte*2//3}d",  1.5),
    (max(1, _min_dte // 3),   "#fb923c", f"{_min_dte//3}d",    1.5),
    (0,                      "#f43f5e", "Expirácia (0d)",       2.5),
]
for d_v, col, lbl, lw in _time_slices:
    _y = _combined_pnl(_price_range, d_v)
    fig.add_trace(go.Scatter(
        x=_price_range, y=_y, mode="lines",
        line=dict(color=col, width=lw), name=lbl,
        hovertemplate=f"{lbl} — Spot: $%{{x:.1f}}  P&L: $%{{y:+.0f}}<extra></extra>",
    ))

# Slider rez (žltá prerušovaná) – ak nie je duplikát
if not any(abs(d - _show_dte) <= 1 for d, *_ in _time_slices):
    fig.add_trace(go.Scatter(
        x=_price_range, y=_pnl_slider, mode="lines",
        line=dict(color="#facc15", width=3, dash="dash"),
        name=f"Slider {_show_dte}d",
        hovertemplate=f"DTE {_show_dte}d — $%{{x:.1f}} → $%{{y:+.0f}}<extra></extra>",
    ))

# Vertikálne línie
fig.add_hline(y=0, line_color="rgba(255,255,255,0.2)", line_width=1)
fig.add_vline(x=_spot, line_color="#fbbf24", line_width=2, line_dash="dash",
              annotation_text=f"Spot ${_spot:.0f}", annotation_font_color="#fbbf24",
              annotation_position="top right")

# Strikeы každej nohy
_colors_strikes = ["#34d399", "#fb7185", "#a78bfa", "#fdba74", "#67e8f9", "#f9a8d4"]
for i, leg in enumerate(legs):
    _col_s = _colors_strikes[i % len(_colors_strikes)]
    _lbl_s = f"{'C' if leg['right']=='C' else 'P'} ${leg['strike']:.0f} {'S' if leg['leg_type']=='Short' else 'L'}"
    fig.add_vline(x=leg["strike"], line_color=_col_s, line_width=1, line_dash="dot",
                  annotation_text=_lbl_s, annotation_font_color=_col_s,
                  annotation_font_size=10)

# SD pásma
try:
    _sd = calc_sd_lines(_spot, _iv_val, max(1, _show_dte))
    for _lvl, _lbl_sd, _col_sd in [
        (_sd.upper_1sd, "1SD+", "rgba(96,165,250,0.5)"),
        (_sd.lower_1sd, "1SD−", "rgba(96,165,250,0.5)"),
        (_sd.upper_2sd, "2SD+", "rgba(167,139,250,0.4)"),
        (_sd.lower_2sd, "2SD−", "rgba(167,139,250,0.4)"),
    ]:
        fig.add_vline(x=_lvl, line_color=_col_sd, line_width=1, line_dash="dot",
                      annotation_text=_lbl_sd, annotation_font_size=9,
                      annotation_font_color=_col_sd)
except Exception:
    pass

fig.update_layout(
    title=f"Spread P&L — {st.session_state.get('sb_ticker','?')}  ·  Spot ${_spot:.0f}  ·  IV {_iv_val*100:.0f}%",
    xaxis_title="Cena podkladu ($)",
    yaxis_title="P&L ($)",
    height=460, showlegend=True,
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    margin=dict(l=60, r=60, t=80, b=50),
    plot_bgcolor="rgba(20,20,30,0.97)",
    paper_bgcolor="rgba(20,20,30,0.0)",
    font_color="#e0e0e0",
    hovermode="x unified",
    yaxis=dict(tickformat="$,.0f"),
)
_big = st.session_state.get("sb_pnl_big", False)
fig.update_layout(height=750 if _big else 460)
_z1, _ = st.columns([1, 5])
if _z1.button("🔲 " + ("Zmenši" if _big else "Zväčši"), key="sb_zoom"):
    st.session_state["sb_pnl_big"] = not _big
    st.rerun()
st.plotly_chart(fig, use_container_width=True, key="sb_main_chart")
st.caption(
    "Čiary: modrá=teraz → fialová → oranžová → červená=expirácia. "
    "Žltá prerušovaná = slider DTE. Modré pásma = 1SD/2SD pre slider DTE."
)

st.divider()

# ─── Max profit / Max loss / Breakeven ────────────────────────────────────────
st.markdown("### Max profit · Max loss · Breakeven")

_pnl_at_exp_arr = _combined_pnl(_price_range, 0)
_max_profit = float(np.max(_pnl_at_exp_arr))
_max_loss   = float(np.min(_pnl_at_exp_arr))

# Breakeven body (prechody cez 0 pri expirácii)
_be_points = []
for _i in range(len(_pnl_at_exp_arr) - 1):
    if _pnl_at_exp_arr[_i] * _pnl_at_exp_arr[_i + 1] < 0:
        _be = _price_range[_i] + (_price_range[_i+1] - _price_range[_i]) * \
              abs(_pnl_at_exp_arr[_i]) / (abs(_pnl_at_exp_arr[_i]) + abs(_pnl_at_exp_arr[_i+1]))
        _be_points.append(round(_be, 2))

_ma1, _ma2, _ma3 = st.columns(3)
_ma1.metric(
    "Max Profit (pri expirácii)",
    f"${_max_profit:+,.0f}" if _max_profit < 50_000 else "Neohraničený",
    help="Maximálny P&L v rozsahu ±35% od spotu"
)
_ma2.metric(
    "Max Loss (pri expirácii)",
    f"${_max_loss:+,.0f}" if _max_loss > -50_000 else "Neohraničená",
    help="Maximálna strata v rozsahu ±35% od spotu"
)
if _be_points:
    _ma3.metric("Breakeven body", "  /  ".join(f"${b:.2f}" for b in _be_points))
else:
    _ma3.metric("Breakeven", "—")

# Risk/Reward
if _max_loss < 0 and _max_profit > 0:
    _rr = _max_profit / abs(_max_loss)
    st.caption(
        f"Risk/Reward pomer: {_rr:.2f}× — na každú 1 USD rizika pripadá približne {_rr:.2f} USD potenciálneho zisku."
    )

st.divider()

# ─── Scenárová analýza ────────────────────────────────────────────────────────
st.markdown("### Scenárová analýza pri expirácii")
_pct_steps  = [-15, -10, -7.5, -5, -2.5, 0, +2.5, +5, +7.5, +10, +15]
_spot_lvls  = [round(_spot * (1 + p / 100), 2) for p in _pct_steps]
_scen_pnls  = [round(sum(_pnl_at_exp(l, s) for l in legs), 0) for s in _spot_lvls]

fig_scen = go.Figure()
fig_scen.add_trace(go.Bar(
    x=[f"{p:+.1f}%" for p in _pct_steps],
    y=_scen_pnls,
    marker_color=["#2ecc71" if v >= 0 else "#e74c3c" for v in _scen_pnls],
    hovertemplate="Zmena: %{x}<br>P&L: $%{y:+,.0f}<extra></extra>",
    showlegend=False,
))
fig_scen.add_hline(y=0, line_color="gray", line_width=1)
fig_scen.update_layout(
    height=300,
    xaxis_title="Zmena ceny podkladu pri expirácii",
    yaxis_title="P&L ($)", yaxis=dict(tickformat="$,.0f"),
    margin=dict(l=10, r=10, t=20, b=40),
    plot_bgcolor="rgba(20,20,30,0.97)",
    paper_bgcolor="rgba(20,20,30,0.0)",
    font_color="#e0e0e0",
)
st.plotly_chart(fig_scen, use_container_width=True, key="sb_scen_chart")

st.divider()

# ─── Uložiť do denníka ────────────────────────────────────────────────────────
st.markdown("### Uložiť analýzu do denníka")

_snap_note = st.text_input(
    "Poznámka (voliteľné)",
    placeholder="napr. Zvažujem Bear Put Spread na hedge...",
    key="sb_snap_note",
)
_snap_group = st.selectbox(
    "Priradiť ku skupine (voliteľné)",
    ["—"] + [g["name"] for g in db.get_groups()],
    key="sb_snap_group",
)

if st.button("📝 Uložiť snapshot do denníka", type="primary", key="sb_save_btn"):
    _ticker = st.session_state.get("sb_ticker", "?")
    _legs_md = "\n".join(
        f"| {'Long' if l['leg_type']=='Long' else 'Short'} | "
        f"{'Call' if l['right']=='C' else 'Put'} | "
        f"${l['strike']:.0f} | {l['expiry']} (DTE {_dte(l['expiry'])}d) | "
        f"${l['entry_price']:.2f} | {l['contracts']}× | "
        f"{_journal_leg_tws_ba(l)} | {_journal_leg_tws_theta(l)} |"
        for l in legs
    )
    _be_str = "  /  ".join(f"${b:.2f}" for b in _be_points) if _be_points else "—"
    _note_md = f"""## Spread Builder — {_ticker}
**Dátum:** {date.today().strftime('%d.%m.%Y')}  ·  Spot: ${_spot:.2f}  ·  IV: {_iv_val*100:.1f}%

### Nohy
| L/S | C/P | Strike | Expiry | Vstup | Kontr. | TWS bid/ask | TWS Θ/deň |
|-----|-----|--------|--------|-------|--------|-------------|-----------|
{_legs_md}

### Greeks celého spreadu{" (ručné súčty — pozri Spread Builder)" if _mg_on else ""}
| Net Delta $ | Net Theta $/deň | Net Vega $ | Net Gamma |
|------------|-----------------|-----------|----------|
| ${tot_eff['delta']:+.0f} | ${tot_eff['theta']:+.2f} | ${tot_eff['vega']:+.2f} | {tot_eff['gamma']:+.4f} |

### Výsledky
| Metrika | Hodnota |
|---------|---------|
| Čistý kredit/debet | ${_net_flow:+,.0f} |
| Max profit | {"${:+,.0f}".format(_max_profit) if _max_profit < 50000 else "Neohraničený"} |
| Max loss | {"${:+,.0f}".format(_max_loss) if _max_loss > -50000 else "Neohraničená"} |
| Breakeven | {_be_str} |
{f"| APTR (Θ) | {_aptr_mod['yield_pct']:+.1f} % · náklad ${_aptr_mod['capital_basis_usd']:,.0f} (net debet + marža) |" if _aptr_mod is not None else "| APTR (Θ) | — (báza ≤ 0 alebo uprav maržu) |"}

{("**Poznámka:** " + _snap_note) if _snap_note else ""}
"""
    _gid = _snap_group if _snap_group != "—" else None
    _nid = db.add_note(
        title=f"Spread Builder — {_ticker} [{date.today().strftime('%d.%m.%Y')}]",
        content=_note_md,
        group_id=_gid,
    )
    st.success(f"✅ Poznámka #{_nid} uložená do denníka{' (skupina ' + _gid + ')' if _gid else ''}.")

st.divider()

# ─── AI Analýza spreadu ────────────────────────────────────────────────────────
st.markdown("### 🤖 AI Analýza spreadu")

if not legs:
    st.caption("Pridaj aspoň jednu nohu aby bola dostupná AI analýza.")
else:
    _ticker    = st.session_state.get("sb_ticker", "?")
    _model_opt = list(ai_agent.AVAILABLE_MODELS.keys())
    _model_lbl = [ai_agent.AVAILABLE_MODELS[m]["label"] for m in _model_opt]
    _saved_m   = st.session_state.get("selected_claude_model", "claude-sonnet-4-6")
    _saved_idx = _model_opt.index(_saved_m) if _saved_m in _model_opt else 1

    ai_c1, ai_c2, ai_c3 = st.columns([3, 2, 1])
    with ai_c1:
        _ai_question = st.text_input(
            "Otázka (voliteľné)",
            placeholder="napr. Je teraz vhodný čas? Aká podmienka vstupu?",
            key="sb_ai_question",
            label_visibility="collapsed",
        )
    with ai_c2:
        _sel_idx = st.selectbox(
            "Model",
            options=range(len(_model_opt)),
            format_func=lambda i: _model_lbl[i],
            index=_saved_idx,
            key="sb_model_sel",
            label_visibility="collapsed",
        )
        _selected_model = _model_opt[_sel_idx]
        st.session_state["selected_claude_model"] = _selected_model
    with ai_c3:
        _run_ai = st.button("Analyzovať", type="primary", key="sb_ai_btn", use_container_width=True)

    if _run_ai:
        with st.spinner("Claude analyzuje spread..."):
            try:
                # Zostav popis nôh pre prompt
                _legs_lines = []
                for l in legs:
                    _g = _leg_greeks(l, _spot)
                    _ls  = "Long" if l["leg_type"] == "Long" else "Short"
                    _cp  = "Call" if l["right"] == "C" else "Put"
                    _dte_v = _dte(l["expiry"])
                    _iv_leg = float(l.get("iv") or _iv_val)
                    _iv_lbl = (
                        "individuálna"
                        if abs(_iv_leg - float(_iv_val)) > 1e-5
                        else "globálna základná"
                    )
                    _legs_lines.append(
                        f"  {_ls} {l.get('contracts',1)}× {_cp} ${l['strike']:.0f} exp {l['expiry']} (DTE {_dte_v})"
                        f" | Entry ${l['entry_price']:.2f}"
                        f" | IV {_iv_leg*100:.1f}% ({_iv_lbl})"
                        f" | Delta ${_g['delta']:+.0f} | Theta ${_g['theta']:+.2f}/deň"
                    )

                # Otvorené objednávky z TWS pre tento ticker
                _tws_ord_lines = []
                _tws_orders = ibkr.DASHBOARD_FETCH_JOB.get("orders") or []
                for o in _tws_orders:
                    if o.get("ticker", "").upper() == _ticker.upper():
                        sec = o.get("sec_type", "")
                        if sec in ("OPT", "FOP"):
                            detail = f"{o.get('option_type')} ${o.get('strike',0):.0f} exp {o.get('expiry')}"
                        elif sec == "BAG":
                            detail = f"Combo: {o.get('legs_descr') or '?'}"
                        else:
                            detail = sec
                        conds = "; ".join(
                            f"Cena {'>' if c.get('isMore') else '<'} {c.get('price')} USD"
                            for c in (o.get("conditions") or [])
                            if c.get("type") == "PriceCondition"
                        )
                        cond_s = f" ⟦{conds}⟧" if conds else ""
                        _tws_ord_lines.append(
                            f"  - {o.get('action')} {o.get('total_qty')}× {detail}"
                            f" | {o.get('order_type')} | {o.get('status')}{cond_s}"
                        )
                tws_text = ("\n## Súvisiace objednávky v TWS:\n" + "\n".join(_tws_ord_lines)) if _tws_ord_lines else ""
                q_text   = f"\n## Otázka obchodníka:\n{_ai_question}" if _ai_question else ""

                _be_str  = "  /  ".join(f"${b:.2f}" for b in _be_points) if _be_points else "—"
                mp_str   = f"${_max_profit:+,.0f}" if _max_profit < 50000 else "Neohraničený"
                ml_str   = f"${_max_loss:+,.0f}" if _max_loss > -50000 else "Neohraničená"

                prompt = f"""Si skúsený obchodník s opciami. Analyzuj nasledujúci spread.

PRAVIDLÁ:
- Píš v slovenčine, ceny ako "190 USD", bez LaTeX
- Buď konkrétny a číselný, max 350 slov

## Spread: {_ticker}
- Dátum: {date.today().strftime('%d.%m.%Y')}
- Spot podkladu: ${_spot:.2f} USD
- Globálna IV (použije sa len tam, kde noha nemá vlastnú IV v dátach): {_iv_val*100:.1f}%
- Pri každej nohe je uvedená **IV zadaná pre tú nohu**; ak chýba, v BS časti modelu (P&L krivka) platí globálna IV vyššie. Delta/Theta na riadku = hodnoty zadané pri nohe.

## Nohy:
{chr(10).join(_legs_lines)}

## Výsledky spreadu:
- Čistý kredit/debet: ${_net_flow:+,.0f}
- Max profit: {mp_str}
- Max loss: {ml_str}
- Breakeven: {_be_str}
- Net Delta: ${tot_eff['delta']:+.0f} | Net Theta: ${tot_eff['theta']:+.2f}/deň | Net Vega: ${tot_eff['vega']:+.2f}
- Poznámka k súčtom: {"**ručné neto** (prepíše súčet z nôh)" if _mg_on else "súčet Delta/Theta/Vega/Gamma z hodnôt nôh vyššie"}
{tws_text}
{q_text}
---
Odpovedaj v tomto formáte:

## Hodnotenie spreadu
(silné stránky, slabiny, vhodnosť pre súčasné trhové podmienky)

## Riziká a podmienky vstupu
- Podmienka vstupu: (napr. len ak IV > 30%, alebo spot > X USD)
- Stop-loss úroveň: (konkrétna cena alebo % pohyb)
- Čo sledovať: (kľúčové úrovne a udalosti)

## Návrh úpravy (ak relevantný)
(alternatívny strike/expiry alebo iný typ spreadu pre lepší pomer rizika)

## Záver
(vstúpiť teraz / počkať / zamietnuť)
"""
                client    = ai_agent._load_client()
                m_info    = ai_agent.AVAILABLE_MODELS.get(_selected_model, {})
                max_tok   = m_info.get("max_tokens", 1200)
                msg       = client.messages.create(
                    model=_selected_model,
                    max_tokens=max_tok,
                    messages=[{"role": "user", "content": prompt}],
                )
                _ai_result = msg.content[0].text

                # Uložiť do denníka
                _gid2 = _snap_group if _snap_group != "—" else None
                db.add_note(
                    title=f"🤖 AI Spread: {_ticker} [{date.today().strftime('%d.%m.%Y')}]",
                    content=_ai_result,
                    group_id=_gid2,
                )
                st.success("Analýza uložená do Konzultácií!")
                with st.container(border=True):
                    st.markdown(_ai_result)

            except Exception as e:
                st.error(f"Chyba: {e}")
