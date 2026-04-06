"""
Steady Yields — APR z realizovaných tokov, monitoring shortu (delta/DTE), skener likvidity.
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import database as db
from core import ibkr
from core.expiration_catalog import format_expiry_select_options, get_catalog_expiries
from core.page_context import set_tradejournal_page
from core.portfolio_data import calc_dte, normalize_expiry
from core.steady_yields import (
    DEFAULT_MAX_IV_RANK_ENTRY,
    DEFAULT_MAX_SPREAD_PCT_MID,
    DEFAULT_MAX_TICKERS_PER_SECTOR,
    DEFAULT_MIN_OPEN_INTEREST,
    apply_sector_caps,
    build_roll_up_and_out_suggestion,
    build_yield_summary,
    efficiency_credit_delta,
    efficiency_theta_delta,
    estimate_roll_net_credit,
    iv_rank_passes,
    liquidity_passes,
    profit_target_message,
    semafor_alert_detail,
    short_premium_profit_pct,
    traffic_light,
)
from core.steady_yields.constants import (
    DEFAULT_SLIPPAGE_USD_PER_CONTRACT,
    DELTA_GREEN_MAX,
    DELTA_RED_MIN,
    ROLL_DTE_TRIGGER,
)

db.init_db()
set_tradejournal_page("steady_yields")

st.title("Steady Yields")
st.caption(
    "PMCC / diagonály / kalendáre — APR a cost basis z **realizovaných** údajov (roll udalosti, Trade Log). "
    "Semafor a roll odhad z **IBKR** (bid/ask). Nepodáva príkazy. "
    "**DB:** `trades` (skupiny), `steady_yield_roll_events`, `steady_yield_group_profile` "
    "(vrátane prahu profit alertu a zapnutia semafor alertov), `steady_yield_alert_events` (história upozornení)."
)

tab_yield, tab_mon, tab_scan = st.tabs(["Yield a APR", "Monitoring a roll", "Skener"])


def _pick_rich_chain(chains: list[dict]) -> dict | None:
    if not chains:
        return None
    return max(chains, key=lambda c: len(c.get("expirations") or []))


def _pick_expiry_for_dte(expirations: list[str], min_dte: int) -> str | None:
    """Prvá expirácia s DTE ≥ min_dte. Bez fallbacku na kratšie — DTE 0 je neobchodovateľný pre kalendár."""
    best = None
    best_dte = 10**9
    for e in expirations or []:
        d = calc_dte(e)
        if d is None or d < min_dte:
            continue
        if d < best_dte:
            best_dte = d
            best = e
    return best


def _nearest_strike(strikes: list[float], spot: float) -> float | None:
    if not strikes or spot <= 0:
        return None
    return min(strikes, key=lambda k: abs(float(k) - spot))


def _exp_cmp(s: str) -> str:
    return normalize_expiry(str(s)).replace("-", "")


def _match_exp_in_chain(expirations: list[str], manual: str) -> str | None:
    raw = (manual or "").strip()
    if not raw:
        return None
    m = _exp_cmp(raw)
    if len(m) != 8 or not m.isdigit():
        return None
    for e in expirations or []:
        if _exp_cmp(e) == m:
            return e
    return None


def _pick_long_expiry_after_short(
    expirations: list[str],
    short_exp: str,
    min_dte_long: int,
) -> str | None:
    sn = _exp_cmp(short_exp)
    best = None
    best_dte = 10**9
    for e in expirations or []:
        if _exp_cmp(e) <= sn:
            continue
        d = calc_dte(e)
        if d is None:
            continue
        if d >= min_dte_long and d < best_dte:
            best_dte = d
            best = e
    if best:
        return best
    later = [e for e in (expirations or []) if _exp_cmp(e) > sn]
    if not later:
        return None
    return max(later, key=lambda x: calc_dte(x) or -1)


def _iv_to_display_pct(iv_val) -> float | None:
    if iv_val is None:
        return None
    try:
        v = float(iv_val)
        if v != v or v <= 0:
            return None
        return round(v * 100.0, 2) if v <= 3.0 else round(v, 2)
    except (TypeError, ValueError):
        return None


def _resolve_scan_iv_pct(
    iv_fetch: dict,
    met: dict,
    spot: float,
    dte: int,
    strike: float,
    right: str,
) -> float | None:
    """Impl. IV %: najprv fetch_iv / tick 101, inak BS z mid a spotu."""
    v = _iv_to_display_pct(iv_fetch.get("iv")) or _iv_to_display_pct(met.get("iv"))
    if v is not None:
        return v
    mid = met.get("mid") or met.get("realized_fill_price")
    if mid is None or spot is None or spot <= 0 or dte <= 0:
        return None
    try:
        from core.probability import calc_iv_from_price

        rr = (right or "C")[:1].upper()
        if rr not in ("C", "P"):
            rr = "C"
        iv_raw = calc_iv_from_price(
            float(mid), float(spot), float(strike), max(1, int(dte)), rr
        )
        return _iv_to_display_pct(iv_raw)
    except (TypeError, ValueError):
        return None


def _impl_iv_pct_leg_filter(ivp: float | None, lo: float, hi: float) -> tuple[bool, str]:
    """Voliteľné min./max. na impl. IV % (0 = vypnuté)."""
    if lo <= 0 and hi <= 0:
        return True, ""
    if ivp is None:
        return False, "impl. IV neznáma — filtrovať nemôžem (IB nevrátil cenu/Greeks)"
    if lo > 0 and ivp < lo:
        return False, f"{ivp:.1f}% < min {lo:.1f}%"
    if hi > 0 and ivp > hi:
        return False, f"{ivp:.1f}% > max {hi:.1f}%"
    return True, ""


def _rank_hint(ir13, ir52) -> str:
    parts = []
    try:
        if ir52 is not None and float(ir52) < 30:
            parts.append("52t rank nízky — IV často v spodnej časti rozsahu")
        elif ir52 is not None and float(ir52) > 75:
            parts.append("52t rank vysoký — IV už bolo vyššie v roku")
    except (TypeError, ValueError):
        pass
    try:
        if ir13 is not None and float(ir13) < float(ir52 or 100):
            parts.append("13t < 52t — nedávna IV skôr nižšia ako ročná")
    except (TypeError, ValueError):
        pass
    return " · ".join(parts) if parts else "—"


def _sy_scan_cell_str(v) -> str:
    try:
        if v is None or pd.isna(v):
            return "—"
    except (TypeError, ValueError):
        pass
    if isinstance(v, float) and v != v:
        return "—"
    return str(v)


def _render_sy_scan_plotly(df: pd.DataFrame) -> None:
    """Plotly Table — konzistentné s Dashboard / Spread Builder, spoľahlivé farby riadkov."""
    cols = list(df.columns)
    n_rows = len(df)
    n_cols = len(cols)
    bg_k = "#e3f2fd"
    bg_d = "#e8f5e9"
    bg_neutral = "#ffffff"
    bg_header = "#263238"

    row_fill: list[str] = []
    for _, row in df.iterrows():
        if "noha" not in df.columns:
            row_fill.append(bg_neutral)
            continue
        n = row.get("noha")
        if pd.isna(n):
            row_fill.append(bg_neutral)
            continue
        s = str(n).strip()
        if s == "Krátka":
            row_fill.append(bg_k)
        elif s == "Dlhá":
            row_fill.append(bg_d)
        else:
            row_fill.append(bg_neutral)

    cell_vals = [[_sy_scan_cell_str(x) for x in df[c].tolist()] for c in cols]
    fill_by_col = [list(row_fill) for _ in range(n_cols)]

    fig = go.Figure(
        data=[
            go.Table(
                header=dict(
                    values=[f"<b>{c}</b>" for c in cols],
                    fill_color=bg_header,
                    font=dict(color="#eceff1", size=13),
                    align="left",
                    height=36,
                ),
                cells=dict(
                    values=cell_vals,
                    fill_color=fill_by_col,
                    align="left",
                    font=dict(color="#212121", size=12),
                    height=30,
                ),
            )
        ]
    )
    fig.update_layout(
        margin=dict(l=0, r=0, t=8, b=0),
        height=min(720, 80 + n_rows * 34),
    )
    st.plotly_chart(fig, use_container_width=True, key="sy_scan_table")


def _sy_scan_blocked_row(
    ticker: str,
    stav: str,
    *,
    r13=None,
    r52=None,
    exp_short: str | None = None,
    exp_long: str | None = None,
    strike=None,
    cp: str | None = None,
) -> dict:
    """
    Jeden riadok so **všetkými** stĺpcami ako pri úspešnom skene — pri filtri/chybe pred IB opciami,
    aby tabuľka nemala len ticker/stav (to vyzerá ako „starý“ výstup).
    """
    r13d = r13 if r13 is not None else "—"
    r52d = r52 if r52 is not None else "—"
    hint = _rank_hint(r13, r52)

    if exp_short and exp_long:
        exp_txt = f"{exp_short} / {exp_long}"
        ds = calc_dte(normalize_expiry(str(exp_short)))
        dl = calc_dte(normalize_expiry(str(exp_long)))
        dte_txt = f"{ds if ds is not None else '—'} / {dl if dl is not None else '—'}"
    elif exp_short:
        exp_txt = str(exp_short)
        ds = calc_dte(normalize_expiry(str(exp_short)))
        dte_txt = ds if ds is not None else "—"
    elif exp_long:
        exp_txt = str(exp_long)
        dl = calc_dte(normalize_expiry(str(exp_long)))
        dte_txt = dl if dl is not None else "—"
    else:
        exp_txt = "—"
        dte_txt = "—"

    return {
        "ticker": ticker,
        "typ": "—",
        "noha": "—",
        "exp": exp_txt,
        "DTE": dte_txt,
        "strike": strike if strike is not None else "—",
        "C/P": cp if cp else "—",
        "IV %": None,
        "sprd %": None,
        "Θ/deň": None,
        "OI": None,
        "R13": r13d,
        "R52": r52d,
        "kontext": hint,
        "stav": stav,
    }


STEADY_YIELDS_SCAN_LAST_PICK_KEY = "steady_yields_scan_last_pick"


def _sy_scan_parse_saved_tickers() -> list[str]:
    raw = (db.get_setting(STEADY_YIELDS_SCAN_LAST_PICK_KEY, "") or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for x in data:
        u = str(x).strip().upper()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _sy_persist_scan_pick(pick: list[str]) -> None:
    try:
        norm = [str(x).strip().upper() for x in pick if str(x).strip()]
        db.set_setting(
            STEADY_YIELDS_SCAN_LAST_PICK_KEY,
            json.dumps(norm, ensure_ascii=False),
        )
    except Exception:
        pass


# ─── Tab: Yield a APR ─────────────────────────────────────────────────────────
with tab_yield:
    gids = sorted(db.list_steady_yield_group_ids_from_trades())

    c1, c2 = st.columns([2, 1])
    with c1:
        sel_g = st.selectbox(
            "Skupina (Group ID z Trade Log)",
            options=gids or ["—"],
            index=0,
            help="Všetky nohy PMCC daj do jednej skupiny rovnakým Group ID.",
        )
    with c2:
        st.caption("Ak skupina chýba, priraď ju v **Trade Log**.")

    if not gids or sel_g == "—":
        st.info("Najprv v Trade Log nastav **Group ID** pre LEAPS a short overlay.")
    else:
        profile = db.get_steady_yield_group_profile(sel_g)
        with st.expander("Profil skupiny (očakávaný APR, náklad LEAPS)", expanded=not profile):
            with st.form("sy_profile"):
                e_apr = st.number_input(
                    "Očakávaný APR pri vstupe (%)",
                    value=float(profile["expected_apr_pct"] or 0) if profile else 0.0,
                    step=1.0,
                    format="%.1f",
                )
                leap_c = st.number_input(
                    "Počiatočný náklad LEAPS (USD, voliteľné)",
                    min_value=0.0,
                    value=float(profile["leap_initial_cost"] or 0) if profile else 0.0,
                    step=100.0,
                    help="Ak 0, odvodí sa z Long nôh v denníku (súčet entry × kontrakty × 100).",
                )
                pn = st.text_input("Poznámka", value=(profile or {}).get("notes") or "")
                prof_pt = float((profile or {}).get("profit_target_pct") or 0)
                sf_def = (profile or {}).get("alert_semafor_enabled")
                if sf_def is None:
                    sf_def = True
                else:
                    sf_def = bool(int(sf_def))
                profit_alert_pct = st.number_input(
                    "Alert: % max. zisku z short prémií (0 = vypnuté)",
                    min_value=0.0,
                    max_value=100.0,
                    step=5.0,
                    value=prof_pt,
                    help="Porovná vstupnú prémiu z denníka s aktuálnym markom z IBKR (Monitoring).",
                )
                alert_sf = st.checkbox(
                    "Upozornenia zo semafora (Δ / DTE)",
                    value=sf_def,
                )
                if st.form_submit_button("Uložiť profil"):
                    db.upsert_steady_yield_group_profile(
                        sel_g,
                        expected_apr_pct=float(e_apr),
                        leap_initial_cost=leap_c if leap_c > 0 else None,
                        notes=pn,
                        profit_target_pct=float(profit_alert_pct),
                        alert_semafor_enabled=alert_sf,
                    )
                    st.success("Profil uložený.")
                    st.rerun()

        st.subheader("Udalosť roll / inkasa")
        with st.form("sy_roll_ev"):
            oc = st.date_input("Dátum", value=date.today())
            act = st.selectbox(
                "Typ",
                ["roll_credit", "roll_debit", "short_premium", "other"],
                format_func=lambda x: {
                    "roll_credit": "Roll — čistý kredit",
                    "roll_debit": "Roll — debet",
                    "short_premium": "Prémia shortu (otvorenie)",
                    "other": "Iné",
                }[x],
            )
            netp = st.number_input("Netto hotovosť USD (+ kredit účtu)", step=10.0, value=0.0)
            comm = st.number_input("Komisia USD", step=0.5, value=0.0)
            tkr = st.text_input("Ticker").upper().strip()
            dlt = st.number_input("Delta snapshot (voliteľné)", step=0.01, value=0.0)
            dte_i = st.number_input("DTE snapshot (voliteľné)", step=1, value=0, min_value=0)
            nts = st.text_input("Poznámka k udalosti", "")
            if st.form_submit_button("Pridať udalosť"):
                db.append_steady_yield_roll_event(
                    sel_g,
                    oc.isoformat(),
                    tkr or "?",
                    act,
                    net_premium=netp,
                    commission=comm,
                    delta_snapshot=dlt if dlt else None,
                    dte_snapshot=int(dte_i) if dte_i else None,
                    notes=nts,
                )
                st.success("Udalosť uložená.")
                st.rerun()

        events = db.get_steady_yield_roll_events(sel_g)
        if events:
            st.dataframe(pd.DataFrame(events), use_container_width=True, hide_index=True)
            del_id = st.number_input("Zmazať udalosť ID", min_value=0, value=0, step=1)
            if st.button("Zmazať udalosť") and del_id > 0:
                db.delete_steady_yield_roll_event(int(del_id))
                st.rerun()

        trades = db.get_all_trades()
        summ = build_yield_summary(
            group_id=sel_g,
            trades=trades,
            roll_events=events,
            profile=profile,
        )
        st.subheader("Súhrn")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Báza LEAPS (USD)", f"{summ['leap_basis_usd']:,.0f}" if summ["leap_basis_usd"] else "—")
        m2.metric("Kredity (vybraný zdroj)", f"{summ['total_credits_used_usd']:+,.0f}")
        m3.metric("Realiz. APR %", f"{summ['realized_apr_pct']:.1f}" if summ["realized_apr_pct"] is not None else "—")
        m4.metric("Δ očak. APR", f"{summ['apr_gap_pct']:+.1f}" if summ["apr_gap_pct"] is not None else "—")

        st.caption(
            f"Roll udalosti: {summ['roll_event_count']} · Dni v okne: {summ['span_days']} · "
            f"Kredity z roll tabuľky: {summ['credits_from_roll_events_usd']:+,.0f} · "
            f"Uzavreté shorty (Log): {summ['credits_from_closed_shorts_usd']:+,.0f}"
        )

        st.subheader("Efficiency (posledné udalosti)")
        rows_ef = []
        for e in events[:20]:
            rows_ef.append(
                {
                    "dátum": e.get("occurred_at"),
                    "net": e.get("net_premium"),
                    "comm": e.get("commission"),
                    "θ/Δ": efficiency_theta_delta(
                        None,
                        float(e["delta_snapshot"]) if e.get("delta_snapshot") is not None else None,
                    ),
                    "credit/Δ": efficiency_credit_delta(
                        float(e.get("net_premium") or 0) - float(e.get("commission") or 0),
                        float(e["delta_snapshot"]) if e.get("delta_snapshot") is not None else None,
                    ),
                }
            )
        if rows_ef:
            st.dataframe(pd.DataFrame(rows_ef), use_container_width=True, hide_index=True)


# ─── Tab: Monitoring ─────────────────────────────────────────────────────────
with tab_mon:
    if not ibkr.is_connected():
        st.warning("Pripoj **TWS / Gateway** pre live delty a sken kontraktov.")
    if ibkr.is_connected() and st.button("Obnoviť pozície s Greeks", key="sy_refresh_greeks"):
        with st.spinner("IBKR portfolio + BS Greeks…"):
            _rpos = ibkr.fetch_positions(with_greeks=True, use_historical_last=False)
        if _rpos.get("error"):
            st.error(_rpos["error"])
        else:
            st.session_state["live_positions"] = _rpos.get("positions") or []
            st.success("Pozície aktualizované.")
            st.rerun()
    live = st.session_state.get("live_positions") or []

    gid_m = st.selectbox(
        "Skupina na párovanie s pozíciami",
        options=gids or ["—"],
        key="sy_mon_gid",
    )

    with st.expander("Prahové hodnoty semafora (voliteľné)", expanded=False):
        if "sy_th_dg" not in st.session_state:
            st.session_state["sy_th_dg"] = float(DELTA_GREEN_MAX)
        if "sy_th_dr" not in st.session_state:
            st.session_state["sy_th_dr"] = float(DELTA_RED_MIN)
        if "sy_th_rd" not in st.session_state:
            st.session_state["sy_th_rd"] = int(ROLL_DTE_TRIGGER)
        st.number_input(
            "|Δ| pod — zelená zóna",
            min_value=0.01,
            max_value=0.99,
            step=0.01,
            key="sy_th_dg",
            help="Oranžová pri |Δ| ≥ tejto hodnoty (ak nie je červená).",
        )
        st.number_input(
            "|Δ| nad — červená zóna",
            min_value=0.01,
            max_value=0.99,
            step=0.01,
            key="sy_th_dr",
        )
        st.number_input(
            "Roll časový trigger (DTE menej ako)",
            min_value=1,
            max_value=120,
            step=1,
            key="sy_th_rd",
        )

    _th_dg = float(st.session_state.get("sy_th_dg", DELTA_GREEN_MAX))
    _th_dr = float(st.session_state.get("sy_th_dr", DELTA_RED_MIN))
    _th_rd = int(st.session_state.get("sy_th_rd", ROLL_DTE_TRIGGER))

    pt_alert: float | None = None
    semafor_alerts_on = True
    if gid_m != "—":
        prof_mon = db.get_steady_yield_group_profile(gid_m)
        if prof_mon and prof_mon.get("profit_target_pct") is not None:
            _pt = float(prof_mon["profit_target_pct"])
            pt_alert = _pt if _pt > 0 else None
        _sf = (prof_mon or {}).get("alert_semafor_enabled")
        semafor_alerts_on = True if _sf is None else bool(int(_sf))

    if gid_m != "—":
        gt = [t for t in db.get_open_trades() if (t.get("group_id") or "").strip() == gid_m]
        shorts = [t for t in gt if t.get("leg_type") == "Short"]
        if not shorts:
            st.info("V tejto skupine nie sú **otvorené Short** nohy v denníku.")
        else:
            for t in shorts:
                exp = normalize_expiry(str(t.get("expiry") or ""))
                strike = float(t.get("strike") or 0)
                tk = (t.get("ticker") or "").upper()
                opt = t.get("option_type", "Call")
                r = "C" if str(opt).lower().startswith("c") else "P"
                dte = calc_dte(exp) or 0

                pos = None
                for p in live:
                    if p.get("sec_type") != "OPT":
                        continue
                    if (p.get("ticker") or "").upper() != tk:
                        continue
                    if abs(float(p.get("strike") or -1) - strike) > 0.01:
                        continue
                    if normalize_expiry(str(p.get("expiry") or "")) != exp:
                        continue
                    pos = p
                    break

                ad = None
                th = None
                if pos:
                    dv = pos.get("delta")
                    if dv is not None:
                        ad = abs(float(dv))
                    tv = pos.get("theta")
                    if tv is not None:
                        th = float(tv)

                tl = traffic_light(
                    abs_delta=ad,
                    dte=dte,
                    delta_green_max=_th_dg,
                    delta_red_min=_th_dr,
                    roll_dte_trigger=_th_rd,
                )
                color = {"green": "🟢", "orange": "🟠", "red": "🔴"}.get(tl.level, "⚪")
                with st.container():
                    st.markdown(f"#### {color} {tk} short {opt} K{strike:g} exp {exp} · DTE {dte}")
                    for line in tl.reasons:
                        st.caption(line)
                    if pos:
                        st.caption(f"Live Δ {pos.get('delta')} · Θ {pos.get('theta')}")
                    else:
                        st.caption("Live pozícia v cache nenájdená — skús **TWS Dashboard** načítať alebo auto-sync.")

                    tid = t.get("id")
                    _tid = int(tid) if tid is not None else None
                    if pt_alert and pos and pos.get("market_price") is not None:
                        ent = t.get("entry_price")
                        if ent is not None and float(ent) > 0:
                            mk = float(pos["market_price"])
                            spp = short_premium_profit_pct(float(ent), mk)
                            if spp is not None and spp >= pt_alert:
                                pmsg = profit_target_message(
                                    ticker=tk,
                                    strike=strike,
                                    expiry=exp,
                                    profit_pct=spp,
                                    threshold_pct=pt_alert,
                                )
                                st.warning(pmsg)
                                db.append_steady_yield_alert_event(
                                    gid_m,
                                    "profit_target",
                                    pmsg,
                                    trade_id=_tid,
                                    detail_json=json.dumps(
                                        {
                                            "profit_pct": spp,
                                            "threshold_pct": pt_alert,
                                            "entry": float(ent),
                                            "mark": mk,
                                        },
                                        ensure_ascii=False,
                                    ),
                                )
                    if semafor_alerts_on:
                        if tl.level == "red":
                            smsg = f"Semafor: červená — {tk} K{strike:g} {exp}"
                            st.error(f"{smsg} — " + "; ".join(tl.reasons[:2]))
                            db.append_steady_yield_alert_event(
                                gid_m,
                                "semafor_red",
                                smsg,
                                trade_id=_tid,
                                detail_json=semafor_alert_detail("red", tl.reasons),
                            )
                        elif tl.level == "orange":
                            omsg = f"Semafor: oranžová — {tk} K{strike:g} {exp}"
                            st.warning(f"{omsg} — " + "; ".join(tl.reasons[:2]))
                            db.append_steady_yield_alert_event(
                                gid_m,
                                "semafor_orange",
                                omsg,
                                trade_id=_tid,
                                detail_json=semafor_alert_detail("orange", tl.reasons),
                            )

            with st.expander("História upozornení (uložené v DB)", expanded=False):
                evs = db.get_steady_yield_alert_events(gid_m, limit=40)
                if evs:
                    disp = [
                        {
                            "id": e["id"],
                            "kedy": e["created_at"],
                            "typ": e["alert_type"],
                            "potvrdené": bool(e.get("acknowledged")),
                            "správa": (e.get("message") or "")[:200],
                        }
                        for e in evs
                    ]
                    st.dataframe(pd.DataFrame(disp), hide_index=True, use_container_width=True)
                    a1, a2 = st.columns([1, 2])
                    with a1:
                        ack_id = st.number_input("Potvrdiť alert ID", min_value=0, value=0, step=1, key="sy_ack_id")
                    with a2:
                        st.markdown("")
                        if st.button("Označiť ako prečítané", key="sy_ack_btn") and ack_id > 0:
                            db.acknowledge_steady_yield_alert(int(ack_id))
                            st.rerun()
                else:
                    st.caption(
                        "Zatiaľ žiadne. Pri prekročení prahu sa záznam pridá pri načítaní stránky "
                        "(rovnaký typ + noha sa do 24 h neopakuje, kým ho nepotvrdíš)."
                    )

            st.divider()
            st.subheader("Odhad net kreditu (roll)")
            st.caption("Konzervatívne: nový short za **bid**, starý zatvoriť za **ask**.")
            c_a, c_b = st.columns(2)
            with c_a:
                st.markdown("**Aktuálny short (zatvorenie nákupom)**")
                t_old = st.text_input("Ticker", value=shorts[0].get("ticker", ""), key="sy_ro_t")
                e_old = st.text_input("Exp YYYYMMDD", value=str(shorts[0].get("expiry", "")), key="sy_ro_e")
                k_old = st.number_input("Strike", value=float(shorts[0].get("strike", 0)), key="sy_ro_k")
                r_old = st.selectbox("Put/Call", ["C", "P"], index=0 if "c" in str(shorts[0].get("option_type", "")).lower() else 1, key="sy_ro_r")
            with c_b:
                st.markdown("**Nový short (predaj)**")
                e_new = st.text_input("Exp nová", value="", key="sy_rn_e")
                k_new = st.number_input("Strike nový", value=float(shorts[0].get("strike", 0)), key="sy_rn_k")
                r_new = st.selectbox("P/C nová", ["C", "P"], index=0, key="sy_rn_r")
            if ibkr.is_connected() and st.button(
                "Navrhnúť exp + strike (roll out — ďalšia expirácia + posun striku)",
                key="sy_suggest_roll",
            ):
                _ch_res = ibkr.fetch_secdef_option_params(str(t_old).upper())
                _rich = _pick_rich_chain(_ch_res.get("chains") or [])
                if _ch_res.get("error"):
                    st.error(_ch_res["error"])
                elif not _rich:
                    st.warning("Žiadny opčný reťazec.")
                else:
                    _sug = build_roll_up_and_out_suggestion(
                        expirations=_rich.get("expirations") or [],
                        strikes=_rich.get("strikes") or [],
                        current_expiry=str(e_old).replace("-", ""),
                        current_strike=float(k_old),
                        right=str(r_old),
                    )
                    if _sug.get("next_expiry"):
                        st.session_state["sy_rn_e"] = _sug["next_expiry"]
                    if _sug.get("next_strike") is not None:
                        st.session_state["sy_rn_k"] = float(_sug["next_strike"])
                    for _n in _sug.get("notes") or []:
                        st.info(_n)
                    st.rerun()
            slip = st.number_input(
                "Slippage $/kontrakt prémií",
                value=DEFAULT_SLIPPAGE_USD_PER_CONTRACT,
                step=0.01,
                format="%.3f",
                key="sy_slip",
            )
            ctr = int(shorts[0].get("contracts") or 1)
            if st.button("Načítať bid/ask a vypočítať", key="sy_roll_btn") and ibkr.is_connected():
                with st.spinner("IBKR…"):
                    o1 = ibkr.fetch_option_scan_metrics(t_old.upper(), e_old.replace("-", ""), k_old, r_old)
                    o2 = ibkr.fetch_option_scan_metrics(t_old.upper(), e_new.replace("-", ""), k_new, r_new)
                if o1.get("error"):
                    st.error(f"Starý kontrakt: {o1['error']}")
                if o2.get("error"):
                    st.error(f"Nový kontrakt: {o2['error']}")
                _sug_list = []
                if _rich_s := _pick_rich_chain(ibkr.fetch_secdef_option_params(t_old.upper()).get("chains") or []):
                    _bs = build_roll_up_and_out_suggestion(
                        expirations=_rich_s.get("expirations") or [],
                        strikes=_rich_s.get("strikes") or [],
                        current_expiry=str(e_old).replace("-", ""),
                        current_strike=float(k_old),
                        right=str(r_old),
                    )
                    _sug_list = _bs.get("suggested_contracts") or []
                adv = estimate_roll_net_credit(
                    close_short_bid=o1.get("bid"),
                    close_short_ask=o1.get("ask"),
                    open_short_bid=o2.get("bid"),
                    open_short_ask=o2.get("ask"),
                    contracts=ctr,
                    slippage_per_contract=float(slip),
                    suggested_contracts=_sug_list,
                )
                for m in adv.messages:
                    st.write(m)
                for w in adv.warnings:
                    st.warning(w)
                if adv.suggested_contracts:
                    st.caption("Navrhované nohy (orientačné): " + str(adv.suggested_contracts))
                if adv.est_net_credit_per_contract is not None:
                    st.metric("Odhad $/kontrakt prémií (konz.)", f"{adv.est_net_credit_per_contract:+.3f}")


# ─── Tab: Skener ──────────────────────────────────────────────────────────────
with tab_scan:
    sym_rows = db.get_symbols()
    sym_tickers = [str(s["ticker"]).strip().upper() for s in sym_rows if s.get("ticker")]
    trade_tickers = db.get_distinct_trade_tickers()
    _remembered: list[str] = st.session_state.setdefault("sy_scan_remembered", [])
    _sy_saved_pick = _sy_scan_parse_saved_tickers()
    for _u in _sy_saved_pick:
        if _u not in _remembered:
            _remembered.append(_u)

    st.caption(
        "Políčko **Ďalšie tickery** je nad zoznamom: nové symboly sa ihned zapamätajú a **pridajú do výberu**. "
        "Predvolený výber po reštarte appky = **posledný zoznam z tlačidla Spustiť sken** (uložené v DB), nie prvých 5 z abecedy."
    )
    extra = st.text_input(
        "Ďalšie tickery (čiarkou)",
        key="sy_scan_extra",
        placeholder="napr. IWM,QQQ",
        help="Zadaj ticker, ktorý ešte nie je v Symboly / obchodoch; po rerune pribudne do multiselectu.",
    )
    for part in extra.split(","):
        p = part.strip().upper()
        if p and p not in _remembered:
            _remembered.append(p)

    _base_opts = sorted(set(sym_tickers) | set(trade_tickers))
    tick_opts = sorted(set(_base_opts) | set(_remembered))

    if not tick_opts:
        st.info("Žiadne tickery na výber: doplň **Symboly**, **Trade Log** alebo pole vyššie.")
        pick = []
    else:
        if "sy_scan_ms" not in st.session_state:
            _opt_set = set(tick_opts)
            _restored = [t for t in _sy_saved_pick if t in _opt_set]
            if _restored:
                st.session_state["sy_scan_ms"] = _restored
            else:
                _dn = min(5, len(tick_opts))
                st.session_state["sy_scan_ms"] = tick_opts[:_dn]
        pick = st.multiselect(
            "Tickery na sken (všetky zdroje + zapamätané)",
            options=tick_opts,
            key="sy_scan_ms",
            help="Po reštarte Streamlitu sa obnoví posledný uložený výber (DB). Predvolených 5 z abecedy len ak ešte nič nebolo uložené.",
        )

    for part in extra.split(","):
        p = part.strip().upper()
        if p and p not in pick:
            pick.append(p)

    for t in pick:
        u = (t or "").strip().upper()
        if u and u not in _remembered:
            _remembered.append(u)

    sec_map = {s["ticker"].upper(): (s.get("sector") or "") for s in sym_rows}
    iv_map = {s["ticker"].upper(): s.get("iv_rank") for s in sym_rows}
    iv_map_13 = {s["ticker"].upper(): s.get("iv_rank_13w") for s in sym_rows}
    iv_map_52 = {s["ticker"].upper(): s.get("iv_rank_52w") for s in sym_rows}

    s1, s2, s3, s4 = st.columns(4)
    with s1:
        min_oi = st.number_input("Min. OI", value=float(DEFAULT_MIN_OPEN_INTEREST), step=50.0)
    with s2:
        max_sp = st.number_input("Max. spread % mid", value=float(DEFAULT_MAX_SPREAD_PCT_MID), step=0.1)
    with s3:
        max_iv = st.number_input("Max. IV rank % (stĺpec Symboly)", value=float(DEFAULT_MAX_IV_RANK_ENTRY), step=1.0)
    with s4:
        max_sec = st.number_input("Max. tickerov / sektor", value=float(DEFAULT_MAX_TICKERS_PER_SECTOR), step=1.0)

    st.markdown("**Dve expirácie (kalendár / diagonála)** — rovnaký ATM strike, Call alebo Put.")
    exp_mode = st.radio(
        "Režim expirácií",
        ["auto", "manual"],
        index=0,
        horizontal=True,
        format_func=lambda x: "Automaticky (min. DTE krátka + dlhá)" if x == "auto" else "Manuálne (katalóg DB / YYYYMMDD)",
        key="sy_exp_mode",
    )
    _sy_cat_lbls, _sy_cat_map = format_expiry_select_options(get_catalog_expiries(months=18))
    _SY_EXP_CUSTOM = "— vlastný dátum (YYYYMMDD nižšie) —"
    _sy_exp_opts = ([_SY_EXP_CUSTOM] + _sy_cat_lbls) if _sy_cat_lbls else [_SY_EXP_CUSTOM]

    manual_exp_s = ""
    manual_exp_l = ""
    if exp_mode == "manual":
        st.caption(
            "Manuálne dátumy: **centrálny katalóg** (rovnaký ako Spread Builder → Centrálny katalóg expirácií, uložené v DB) "
            "alebo vlastný YYYYMMDD. Dátum musí byť v IBKR reťazci tickeru."
        )
    ec1, ec2 = st.columns(2)
    with ec1:
        min_dte_short = st.number_input(
            "Auto: min. DTE **krátkej** exp",
            min_value=1,
            max_value=400,
            value=21,
            step=1,
            key="sy_dte_s",
            disabled=exp_mode != "auto",
        )
        if exp_mode == "manual":
            _sel_s = st.selectbox(
                "Krátka exp — výber z katalógu",
                options=_sy_exp_opts,
                key="sy_pick_s",
                help="Dátum musí byť v IBKR reťazci daného tickeru; inak skener použije fallback (min. DTE).",
            )
            if _sel_s == _SY_EXP_CUSTOM:
                manual_exp_s = st.text_input(
                    "Krátka: YYYYMMDD",
                    key="sy_man_s",
                    placeholder="napr. 20250418",
                )
            else:
                manual_exp_s = _sy_cat_map[_sel_s]
    with ec2:
        min_dte_long = st.number_input(
            "Auto: min. DTE **dlhej** exp (musí byť neskôr ako krátka)",
            min_value=1,
            max_value=500,
            value=50,
            step=1,
            key="sy_dte_l",
            disabled=exp_mode != "auto",
        )
        if exp_mode == "manual":
            _sel_l = st.selectbox(
                "Dlhá exp — výber z katalógu",
                options=_sy_exp_opts,
                key="sy_pick_l",
                help="Musí byť kalendárne neskôr ako krátka a v IBKR reťazci.",
            )
            if _sel_l == _SY_EXP_CUSTOM:
                manual_exp_l = st.text_input(
                    "Dlhá: YYYYMMDD",
                    key="sy_man_l",
                    placeholder="napr. 20250620",
                )
            else:
                manual_exp_l = _sy_cat_map[_sel_l]

    right_scan = st.selectbox(
        "Call alebo Put (obidve nohy)",
        ["C", "P"],
        index=0,
        format_func=lambda x: "Call (C)" if x == "C" else "Put (P)",
        help="IBKR: `fetch_option_scan_metrics` + `fetch_iv` pre obe expirácie, rovnaký strike.",
    )
    require_iv_term = st.checkbox(
        "Vyžadovať vyššiu impl. vol na **krátkej** než na dlhej (IV krátka > IV dlhá)",
        value=True,
        help="Kalendár / diagonála: predávaš drahšiu krátku vol, kúpiš lacnejšiu dlhú. Vypni, ak chceš len rozsahy IV bez tohto pravidla.",
    )

    st.markdown(
        "**Rozsah impl. volatility (%)** — pre každú nohu zadaj **od** a **do** (napr. krátka 40–60, dlhá 15–30). "
        "To **nie je** IV rank v Symboly (ten rieši horný riadok „Max. IV rank %“). "
        "**0** pri „od“ alebo „do“ = tá hranica je vypnutá."
    )
    st.caption(
        "Príklad: krátka **od 40** **do 60** · dlhá **od 15** **do 30** — potom ešte zapni vyššie „IV krátka > IV dlhá“ (predvolené)."
    )
    ir1, ir2 = st.columns(2)
    with ir1:
        st.markdown("**Krátka noha**")
        _s1, _s2 = st.columns(2)
        with _s1:
            min_impl_iv_s = st.number_input(
                "Od (%)",
                min_value=0.0,
                max_value=500.0,
                value=0.0,
                step=0.5,
                key="sy_mi_s",
                help="Impl. IV krátkej nohy ≥ tohto (%). 0 = bez spodnej hranice.",
            )
        with _s2:
            max_impl_iv_s = st.number_input(
                "Do (%)",
                min_value=0.0,
                max_value=500.0,
                value=0.0,
                step=0.5,
                key="sy_ma_s",
                help="Impl. IV krátkej nohy ≤ tohto (%). 0 = bez hornej hranice.",
            )
    with ir2:
        st.markdown("**Dlhá noha**")
        _l1, _l2 = st.columns(2)
        with _l1:
            min_impl_iv_l = st.number_input(
                "Od (%)",
                min_value=0.0,
                max_value=500.0,
                value=0.0,
                step=0.5,
                key="sy_mi_l",
                help="Impl. IV dlhej nohy ≥ tohto (%). 0 = bez spodnej hranice.",
            )
        with _l2:
            max_impl_iv_l = st.number_input(
                "Do (%)",
                min_value=0.0,
                max_value=500.0,
                value=0.0,
                step=0.5,
                key="sy_ma_l",
                help="Impl. IV dlhej nohy ≤ tohto (%). 0 = bez hornej hranice.",
            )

    if (
        min_impl_iv_s > 0
        and max_impl_iv_s > 0
        and float(min_impl_iv_s) > float(max_impl_iv_s)
    ):
        st.warning("Krátka noha: „Od“ je väčšie ako „Do“ — uprav rozsah.")
    if (
        min_impl_iv_l > 0
        and max_impl_iv_l > 0
        and float(min_impl_iv_l) > float(max_impl_iv_l)
    ):
        st.warning("Dlhá noha: „Od“ je väčšie ako „Do“ — uprav rozsah.")

    if exp_mode == "auto" and int(min_dte_long) <= int(min_dte_short):
        st.warning(
            "Min. DTE **dlhej** exp by mal byť **väčší** ako min. DTE krátkej — inak sú heuristiky príliš blízko a výber môže byť menej zmysluplný."
        )

    with st.expander("Logika: dve expirácie + IV rank 13t / 52t", expanded=False):
        st.markdown(
            """
1. **Krátka exp:** auto = prvá exp z IBKR reťazca s DTE ≥ min. krátkej; manuálne = dátum z **centrálneho katalógu** (DB, ako Spread Builder) alebo vlastný YYYYMMDD — musí sedieť s reťazcom IBKR.
2. **Dlhá exp:** auto = prvá exp **neskôr** ako krátka s DTE ≥ min. dlhej; manuálne = rovnako katalóg alebo YYYYMMDD.
3. **Strike:** jeden ATM strike (najbližší k spotu) pre obe nohy.
4. **IV % v tabuľke:** impl. vol. konkrétnej nohy — `fetch_iv` + tick 101, prípadne BS z mid; **nie je to** IV rank zo Symboly.
5. **Rozsah impl. IV % (od–do)** pre krátku a dlhú nohu — napr. krátka 40–60 %, dlhá 15–30 %; voliteľne **IV krátka > IV dlhá**.
6. **IV Rank 13t / 52t:** zadáš v **Symboly** (TWS) — skener ich len zobrazí a krátky text „kontext“; **automaticky ich nestiahneme** z API.

Filter „Max. IV rank %“ používa stĺpec **IV Rank (%)** v Symboly (primárny rank / Yahoo história) — **iná veličina** ako impl. IV % v tabuľke.
            """
        )

    selected, rejected = apply_sector_caps(pick, sec_map, int(max_sec))
    if rejected:
        st.caption("Sektorový limit: " + "; ".join(f"{r['ticker']}: {r['reason']}" for r in rejected))

    if st.button("Spustiť sken", type="primary", disabled=not ibkr.is_connected() or not selected):
        _sy_persist_scan_pick(pick)
        out_rows: list[dict] = []
        scan_log: list[str] = []
        success_iv_notes: list[tuple[str, str]] = []
        theta_breakdown: list[tuple[str, float | None, float | None]] = []
        _cp_lbl = "Call" if right_scan == "C" else "Put"
        prog = st.progress(0.0)
        for i, tkr in enumerate(selected):
            prog.progress((i + 1) / max(1, len(selected)), text=f"{tkr}…")
            _r13 = iv_map_13.get(tkr)
            _r52 = iv_map_52.get(tkr)
            ivr = iv_map.get(tkr)
            ok_iv, msg_iv = iv_rank_passes(ivr, max_iv)
            if not ok_iv:
                scan_log.append(f"**{tkr}** — ⏭ IV rank {ivr}% > {max_iv}% (filter)")
                continue
            ch = ibkr.fetch_secdef_option_params(tkr)
            if ch.get("error"):
                scan_log.append(f"**{tkr}** — ❌ Reťazec: {ch['error']}")
                continue
            rich = _pick_rich_chain(ch.get("chains") or [])
            if not rich:
                scan_log.append(f"**{tkr}** — ❌ IBKR nevrátil opčný reťazec (prázdny)")
                continue
            exps = rich.get("expirations") or []
            if not exps:
                scan_log.append(f"**{tkr}** — ❌ Žiadne expirácie v reťazci")
                continue

            if exp_mode == "manual":
                exp_s = _match_exp_in_chain(exps, manual_exp_s)
                if not exp_s:
                    exp_s = _pick_expiry_for_dte(exps, int(min_dte_short))
                exp_l = _match_exp_in_chain(exps, manual_exp_l)
                if not exp_l:
                    exp_l = _pick_long_expiry_after_short(exps, exp_s or "", int(min_dte_long)) if exp_s else None
            else:
                exp_s = _pick_expiry_for_dte(exps, int(min_dte_short))
                exp_l = _pick_long_expiry_after_short(exps, exp_s or "", int(min_dte_long)) if exp_s else None

            if not exp_s:
                _dtes = [d for d in (calc_dte(normalize_expiry(str(e))) for e in exps) if d is not None]
                _dte_info = f"DTE v reťazci: {min(_dtes)}–{max(_dtes)} ({len(exps)} exp)" if _dtes else f"{len(exps)} exp"
                scan_log.append(
                    f"**{tkr}** — ❌ Žiadna expirácia s DTE ≥ {min_dte_short} (krátka). {_dte_info}"
                )
                continue
            if not exp_l or _exp_cmp(exp_l) <= _exp_cmp(exp_s):
                scan_log.append(
                    f"**{tkr}** — ❌ Žiadna expirácia s DTE ≥ {min_dte_long} po krátkej ({exp_s}). "
                    f"Posledná v reťazci: {exps[-1] if exps else '?'}"
                )
                continue

            und = ibkr.fetch_underlying(tkr, timeout=5.0)
            spot = float(und.get("price") or 0)
            if spot <= 0:
                scan_log.append(f"**{tkr}** — ❌ Spot nedostupný z IBKR")
                continue
            all_strikes = rich.get("strikes") or []
            k_short = _nearest_strike(all_strikes, spot)
            if k_short is None:
                scan_log.append(f"**{tkr}** — ❌ Strike mriežka prázdna")
                continue
            k_long = k_short if k_short in all_strikes else _nearest_strike(all_strikes, spot)
            if k_long is None:
                k_long = k_short
            is_diagonal = abs(float(k_short) - float(k_long)) > 0.001

            met_s = ibkr.fetch_option_scan_metrics(tkr, exp_s, k_short, right_scan)
            met_l = ibkr.fetch_option_scan_metrics(tkr, exp_l, k_long, right_scan)
            err_s = met_s.get("error")
            err_l = met_l.get("error")

            if err_s and err_l:
                ok_s, msg_s = False, err_s
                ok_l, msg_l = False, err_l
            elif err_s:
                ok_s, msg_s = False, err_s
                ok_l, msg_l = liquidity_passes(
                    open_interest=met_l.get("open_interest"),
                    bid=met_l.get("bid"), ask=met_l.get("ask"),
                    min_oi=int(min_oi), max_spread_pct=float(max_sp),
                )
            elif err_l:
                ok_s, msg_s = liquidity_passes(
                    open_interest=met_s.get("open_interest"),
                    bid=met_s.get("bid"), ask=met_s.get("ask"),
                    min_oi=int(min_oi), max_spread_pct=float(max_sp),
                )
                ok_l, msg_l = False, err_l
            else:
                ok_s, msg_s = liquidity_passes(
                    open_interest=met_s.get("open_interest"),
                    bid=met_s.get("bid"), ask=met_s.get("ask"),
                    min_oi=int(min_oi), max_spread_pct=float(max_sp),
                )
                ok_l, msg_l = liquidity_passes(
                    open_interest=met_l.get("open_interest"),
                    bid=met_l.get("bid"), ask=met_l.get("ask"),
                    min_oi=int(min_oi), max_spread_pct=float(max_sp),
                )

            dte_s = calc_dte(normalize_expiry(str(exp_s)))
            dte_l = calc_dte(normalize_expiry(str(exp_l)))
            _ds = max(1, int(dte_s or 1))
            _dl = max(1, int(dte_l or 1))

            ivr_s = ibkr.fetch_iv(tkr, exp_s, k_short, right_scan)
            ivr_l = ibkr.fetch_iv(tkr, exp_l, k_long, right_scan)
            ivp_s = _resolve_scan_iv_pct(ivr_s, met_s, spot, _ds, k_short, right_scan)
            ivp_l = _resolve_scan_iv_pct(ivr_l, met_l, spot, _dl, k_long, right_scan)
            term_ok = ivp_s is not None and ivp_l is not None and ivp_s > ivp_l

            ok_ims, msg_ims = _impl_iv_pct_leg_filter(ivp_s, float(min_impl_iv_s), float(max_impl_iv_s))
            ok_iml, msg_iml = _impl_iv_pct_leg_filter(ivp_l, float(min_impl_iv_l), float(max_impl_iv_l))
            if not ok_ims:
                scan_log.append(f"**{tkr}** — ⏭ Impl. IV krátka: {msg_ims}")
                continue
            if not ok_iml:
                scan_log.append(f"**{tkr}** — ⏭ Impl. IV dlhá: {msg_iml}")
                continue

            if require_iv_term and ivp_s is not None and ivp_l is not None and not term_ok:
                scan_log.append(
                    f"**{tkr}** — ⏭ IV term filter: krátka {ivp_s}% ≤ dlhá {ivp_l}%"
                )
                continue

            ir13 = iv_map_13.get(tkr)
            ir52 = iv_map_52.get(tkr)
            hint = _rank_hint(ir13, ir52)
            if ivr is None:
                _iv_rank_detail = "IV rank (Symboly) neznámy — filter max. % preskočený"
            else:
                _iv_rank_detail = f"IV rank (Symboly) {float(ivr):.1f}% ≤ max {float(max_iv):.1f}%"
            success_iv_notes.append((tkr, _iv_rank_detail))
            try:
                _ths = met_s.get("theta")
                _thl = met_l.get("theta")
                _thsf = float(_ths) if _ths is not None and _ths == _ths else None
                _thlf = float(_thl) if _thl is not None and _thl == _thl else None
            except (TypeError, ValueError):
                _thsf, _thlf = None, None
            theta_breakdown.append((tkr, _thsf, _thlf))

            stav_parts = []
            stav_parts.append("✓K" if ok_s else "×K")
            stav_parts.append("✓D" if ok_l else "×D")
            if ivp_s is not None and ivp_l is not None:
                stav_parts.append("✓IV" if term_ok else "×IV")
            stav_text = " ".join(stav_parts)

            _typ = "Diag." if is_diagonal else "Kal."

            out_rows.append({
                "ticker": tkr, "typ": _typ, "noha": "Krátka",
                "exp": exp_s, "DTE": dte_s if dte_s is not None else "—",
                "strike": k_short, "C/P": _cp_lbl,
                "IV %": ivp_s,
                "sprd %": met_s.get("spread_pct_mid"),
                "Θ/deň": met_s.get("theta"),
                "OI": met_s.get("open_interest"),
                "R13": ir13 if ir13 is not None else "—",
                "R52": ir52 if ir52 is not None else "—",
                "kontext": hint,
                "stav": stav_text,
            })
            out_rows.append({
                "ticker": "", "typ": "", "noha": "Dlhá",
                "exp": exp_l, "DTE": dte_l if dte_l is not None else "—",
                "strike": k_long, "C/P": _cp_lbl,
                "IV %": ivp_l,
                "sprd %": met_l.get("spread_pct_mid"),
                "Θ/deň": met_l.get("theta"),
                "OI": met_l.get("open_interest"),
                "R13": ir13 if ir13 is not None else "—",
                "R52": ir52 if ir52 is not None else "—",
                "kontext": hint,
                "stav": stav_text,
            })

            if not ok_s:
                scan_log.append(f"**{tkr}** krátka — ⚠ Likvidita: {msg_s}")
            if not ok_l:
                scan_log.append(f"**{tkr}** dlhá — ⚠ Likvidita: {msg_l}")

        prog.progress(1.0, text="Hotovo")

        _df_scan = pd.DataFrame(out_rows)
        if not _df_scan.empty:
            st.caption(
                "Modrá = krátka noha · zelená = dlhá · ticker len pri krátkej. "
                "Stĺpec IV % = impl. volatilita danej nohy (nie IV rank zo Symboly). "
                "Θ/deň = theta na akciu za deň (IBKR, modelGreeks); sprd % = šírka spreadu voči mid."
            )
            _render_sy_scan_plotly(_df_scan)
        else:
            st.info("Žiadny ticker neprešiel cez filtre — pozri zhrnutie nižšie.")

        if theta_breakdown:
            st.markdown("### Theta — rozpad na nohy (na akciu / deň)")
            st.caption(
                "Hodnoty z IB pre každý kontrakt zvlášť. **Krátka − dlhá** je len orientačný rozdiel čísel v riadkoch; "
                "skutočný tok z pozície závisí od smeru (short/long) nohy."
            )
            for _tt, _tk, _tl in theta_breakdown:
                _sk = f"{_tk:.5f}" if _tk is not None else "—"
                _sl = f"{_tl:.5f}" if _tl is not None else "—"
                if _tk is not None and _tl is not None:
                    _diff = _tk - _tl
                    st.markdown(
                        f"- **{_tt}:** krátka Θ/deň = {_sk} · dlhá Θ/deň = {_sl} · **krátka − dlhá** = {_diff:.5f}"
                    )
                else:
                    st.markdown(f"- **{_tt}:** krátka Θ/deň = {_sk} · dlhá Θ/deň = {_sl}")

        if success_iv_notes:
            st.markdown("### Úspešné (IV rank zo Symboly)")
            for _t, _d in success_iv_notes:
                st.markdown(f"- **{_t}:** {_d}")

        if scan_log:
            st.markdown("### Ostatné")
            for _msg in scan_log:
                st.markdown(f"- {_msg}")
