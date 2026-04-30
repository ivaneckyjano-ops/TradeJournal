"""
Casopis — otvorené nohy z denníka: skupiny a ručný zápis Grékov / IV do DB.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from datetime import date

import pandas as pd
import streamlit as st

from core import database as db
from core.page_context import set_tradejournal_page

db.init_db()
set_tradejournal_page("portfolio")

_SHORT_DELTA_WARN_RATIO = 1.5
_SHORT_DELTA_ALERT_RATIO = 2.0

st.title("Casopis — Gréky a skupiny")

st.caption(
    "**Návod:** Uprav **Δ vstup / Δ aktuálna**, **Θ vstup ($/deň) / Θ aktuálna ($/deň)** a ďalšie polia v tabuľke, potom **Uložiť journal** pri skupine. "
    "Hodnoty sú z denníka (ručný zápis alebo iný import)."
)

_JOURNAL_GUIDE_PATH = Path(__file__).resolve().parents[1] / "docs" / "journal-greky.md"
try:
    _journal_guide_md = _JOURNAL_GUIDE_PATH.read_text(encoding="utf-8")
except OSError:
    _journal_guide_md = ""

with st.expander("Návod na použitie", expanded=False):
    if _journal_guide_md.strip():
        st.markdown(_journal_guide_md)
    else:
        st.warning(
            f"V projekte chýba súbor s návodom: `{_JOURNAL_GUIDE_PATH}`. "
            "Skopíruj z repozitára **docs/journal-greky.md** alebo ho obnov z gitu."
        )

st.divider()


def _dte(expiry_str: str) -> int | None:
    if not expiry_str:
        return None
    try:
        exp = date.fromisoformat(
            f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:8]}"
            if len(expiry_str) == 8
            else expiry_str
        )
        return max(0, (exp - date.today()).days)
    except Exception:
        return None


def _notional_per_leg(t: dict) -> float:
    try:
        c = float(t.get("contracts") or 1)
        e = float(t.get("entry_price") or 0)
        return abs(e) * c * 100.0
    except (TypeError, ValueError):
        return 0.0


def _nan_to_none(v) -> float | None:
    if isinstance(v, (list, tuple)) and len(v) == 1:
        v = v[0]
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (ValueError, TypeError):
        pass
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


def _greek_cell_to_db(orig_val: float | None, cell_val) -> float | None:
    """
    Bunka z ``data_editor`` s Float64/NA: **prázdna (NA)** = ponechaj ``orig_val`` z DB.
    Inak sa pri uložení jedného Gréka prepísali ostatné stĺpce hodnotou NULL (delta sa „nezapísala“ / zmazala IV).
    """
    if isinstance(cell_val, (list, tuple)) and len(cell_val) == 1:
        cell_val = cell_val[0]
    if cell_val is None:
        return orig_val
    try:
        if pd.isna(cell_val):
            return orig_val
    except (TypeError, ValueError):
        pass
    try:
        x = float(cell_val)
    except (TypeError, ValueError):
        return orig_val
    if isinstance(x, float) and math.isnan(x):
        return orig_val
    return x


def _entry_float_eq(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def _short_delta_abs_ratio(leg_type: str, entry_d: float | None, curr_d: float | None) -> float | None:
    if str(leg_type or "").strip() != "Short":
        return None
    if entry_d is None or curr_d is None:
        return None
    ae = abs(float(entry_d))
    if ae < 1e-12:
        return None
    return abs(float(curr_d)) / ae


# Rovnaký význam ako GROUP_NONE_LABEL inde v UI (Selectbox v data_editor).
PF_GROUP_NONE = "— (bez skupiny) —"


def _journal_group_select_options(legs: list[dict]) -> list[str]:
    """Skupiny z DB + group_id z nôh, ktoré ešte nie sú v tabuľke Skupiny (ako pri výbere skupiny v časopise)."""
    registered = db.get_group_names()
    reg_set = set(registered)
    extra = sorted(
        {
            (t.get("group_id") or "").strip()
            for t in legs
            if (t.get("group_id") or "").strip() and (t.get("group_id") or "").strip() not in reg_set
        }
    )
    return [PF_GROUP_NONE] + registered + extra


def _skupina_cell_norm(v) -> str:
    """Hodnota z data_editor (niekedy jednoprvkový list); NaN → prázdna skupina."""
    if isinstance(v, (list, tuple)) and len(v) == 1:
        v = v[0]
    if v is None:
        return PF_GROUP_NONE
    try:
        if pd.isna(v):
            return PF_GROUP_NONE
    except (TypeError, ValueError):
        pass
    s = str(v).strip()
    return s if s else PF_GROUP_NONE


with st.expander("Filter", expanded=False):
    _sym_raw = db.get_symbol_tickers()
    _sym_sorted = sorted({str(t).strip().upper() for t in _sym_raw if str(t).strip()})
    _sym_opts = ["— všetky —"] + _sym_sorted
    _sel = st.selectbox(
        "Ticker (zo záložky Symboly)",
        options=_sym_opts,
        index=0,
        key="pf_journal_symbol_filter",
        help="Zoznam berie z tabuľky Symboly.",
    )
    ticker_filter = "" if _sel == "— všetky —" else _sel
    if not _sym_sorted:
        st.info("V **Symboly** zatiaľ nemáš žiadny ticker.")

open_trades_raw = [
    t
    for t in db.get_open_trades()
    if str(t.get("status") or "Open").strip().lower() == "open"
]
if ticker_filter:
    open_trades_raw = [t for t in open_trades_raw if str(t.get("ticker") or "").upper() == ticker_filter]

groups_meta = {g["name"]: g for g in db.get_groups()}

open_trades = list(open_trades_raw)

by_group: dict[str, list[dict]] = defaultdict(list)
for t in open_trades:
    gid = (t.get("group_id") or "").strip()
    label = gid if gid else PF_GROUP_NONE
    by_group[label].append(t)

_sort_keys = sorted(by_group.keys(), key=lambda x: (x == PF_GROUP_NONE, x.lower()))

_grp_opts = _journal_group_select_options(open_trades)

n_legs = len(open_trades)
n_groups = len(by_group)
st.divider()
m1, m2, m3 = st.columns(3)
m1.metric("Otvorené nohy", str(n_legs))
m2.metric("Skupín (v zobrazení)", str(n_groups))
if open_trades:
    notionals = [_notional_per_leg(t) for t in open_trades]
    m3.metric(
        "Σ |vstupná prémia| × 100",
        f"${sum(notionals):,.0f}",
        help="Súčet |prémia| × kontrakty × 100 z denníka.",
    )
else:
    m3.metric("Σ |vstupná prémia| × 100", "—")

st.subheader("Otvorené pozície")

if not open_trades:
    st.warning(
        "V denníku nemáš žiadne otvorené nohy (*Open*), preto nie je čo dopĺňať. "
        "Ak čakáš na import, choď na Dashboard a načítaj pozície z IBKR."
    )

(tab_legs,) = st.tabs(["Skupiny a Gréky"])

with tab_legs:
    st.caption(
        "**Návod:** Táto tabuľka je na ručný zápis. Doplň **Δ vstup / Δ aktuálna** a **Θ vstup ($/deň) / Θ aktuálna ($/deň)**, prípadne aj ďalšie hodnoty, a stlač **Uložiť journal**. "
        "Zápis sa uloží do DB a zostane aj na ďalší deň."
    )
    if not open_trades:
        st.info(
            "Pre aktívny filter nemáš žiadne otvorené nohy (*Open*), alebo v denníku ešte nie sú záznamy. "
            "Nohy pridáš importom z IBKR na Dashboarde alebo priamo v denníku."
        )
    else:
        for gname in _sort_keys:
            legs = by_group[gname]
            meta = groups_meta.get(gname) if gname != PF_GROUP_NONE else None
            _gkey = hashlib.sha256(gname.encode("utf-8")).hexdigest()[:16]
            legs_edit = sorted(legs, key=lambda x: (str(x.get("ticker") or ""), int(x.get("id") or 0)))
            if not legs_edit:
                continue

            with st.container():
                st.markdown(f"#### {gname}")
                if meta:
                    _tk = meta.get("ticker") or ""
                    _st = meta.get("strategy") or ""
                    if _tk or _st:
                        st.caption(f"Skupina v DB: **{_tk}** · {_st}")
                rows = []
                orig_by_id: dict[int, dict] = {}
                for t in legs_edit:
                    tid = int(t["id"])
                    orig_by_id[tid] = t
                    exp = t.get("expiry") or ""
                    dte_v = _dte(str(exp))
                    iv_e = t.get("iv_at_entry")
                    iv_c = t.get("iv_current")
                    dlt_e = t.get("delta_at_entry")
                    th_e = t.get("theta_at_entry")
                    th_c = t.get("theta_current")
                    dlt_c = t.get("delta_current")
                    v_e = t.get("vega_at_entry")
                    v_c = t.get("vega_current")
                    gid_disp = (t.get("group_id") or "").strip() or PF_GROUP_NONE
                    if gid_disp not in _grp_opts:
                        gid_disp = PF_GROUP_NONE
                    r = {
                        "ID": tid,
                        "Skupina": gid_disp,
                        "Stratégia": t.get("strategy") or "",
                        "Ticker": t.get("ticker") or "",
                        "Noha": t.get("leg_type") or "",
                        "Typ": t.get("option_type") or "",
                        "Strike": float(t.get("strike") or 0),
                        "Expirácia": exp,
                        "DTE": int(dte_v) if dte_v is not None else None,
                        "Kontr.": int(t.get("contracts") or 1),
                        "Entry $": float(t.get("entry_price") or 0),
                        "Entry dátum": t.get("entry_date") or "",
                        "Θ vstup ($/deň)": pd.NA if th_e is None else float(th_e),
                        "Θ aktuálna ($/deň)": pd.NA if th_c is None else float(th_c),
                        "Δ vstup": pd.NA if dlt_e is None else float(dlt_e),
                        "Δ aktuálna": pd.NA if dlt_c is None else float(dlt_c),
                        "Vega vstup": pd.NA if v_e is None else float(v_e),
                        "Vega aktuálna": pd.NA if v_c is None else float(v_c),
                        "IV vstup": pd.NA if iv_e is None else float(iv_e),
                        "IV aktuálna": pd.NA if iv_c is None else float(iv_c),
                    }
                    rows.append(r)
                df = pd.DataFrame(rows)
                if "Skupina" in df.columns:
                    _sk_cells = [_skupina_cell_norm(x) for x in df["Skupina"].tolist()]
                    df["Skupina"] = _sk_cells
                    _grp_opts_editor = list(dict.fromkeys([*_grp_opts, *_sk_cells]))
                else:
                    _grp_opts_editor = list(_grp_opts)
                _float_cols = [
                    "Θ vstup ($/deň)",
                    "Θ aktuálna ($/deň)",
                    "Δ vstup",
                    "Δ aktuálna",
                    "Vega vstup",
                    "Vega aktuálna",
                    "IV vstup",
                    "IV aktuálna",
                ]
                for _c in _float_cols:
                    if _c in df.columns:
                        df[_c] = df[_c].astype("Float64")
                st.caption(
                    "**IV** ako zlomok (0,35 = 35 %). **Θ** = USD/deň za celú nohu. **Vega** = za pozíciu (× kontrakty × 100, znamienko podľa nohy). "
                    "Po **Uložiť journal** sa z aktuálnych hodnôt uloží aj **bod do histórie** snímok Grékov v DB."
                )
                _disabled = [
                    "ID",
                    "Stratégia",
                    "Ticker",
                    "Noha",
                    "Typ",
                    "Strike",
                    "Expirácia",
                    "DTE",
                    "Kontr.",
                    "Entry $",
                    "Entry dátum",
                ]
                _col_cfg = {
                    "Skupina": st.column_config.SelectboxColumn(
                        "Skupina",
                        options=_grp_opts_editor,
                        required=False,
                        help="Rovnaké mená ako v záložke **Skupiny** ako pri úpravách v denníku. Ak sa výber neuloží, skús znova po obnovení stránky.",
                    ),
                    "Strike": st.column_config.NumberColumn(format="$%.2f"),
                    "Entry $": st.column_config.NumberColumn(format="$%.2f"),
                    "DTE": st.column_config.NumberColumn(format="%d dní"),
                    "Θ vstup ($/deň)": st.column_config.NumberColumn(format="$%.3f", step=0.001),
                    "Θ aktuálna ($/deň)": st.column_config.NumberColumn(
                        format="$%.3f", step=0.001, help="Aktuálna theta pozície ($/deň)."
                    ),
                    "Δ vstup": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                    "Δ aktuálna": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                    "Vega vstup": st.column_config.NumberColumn(format="%.2f", step=0.01),
                    "Vega aktuálna": st.column_config.NumberColumn(format="%.2f", step=0.01),
                    "IV vstup": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                    "IV aktuálna": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                }
                edited = st.data_editor(
                    df,
                    use_container_width=True,
                    hide_index=True,
                    disabled=_disabled,
                    column_config=_col_cfg,
                    key=f"pf_ed_{_gkey}",
                )
                _watch_rows: list[dict] = []
                for _, row in edited.iterrows():
                    lt = str(row.get("Noha") or "")
                    de = _nan_to_none(row["Δ vstup"])
                    dc = _nan_to_none(row["Δ aktuálna"])
                    ratio = _short_delta_abs_ratio(lt, de, dc)
                    if lt != "Short":
                        p_str, st_lbl = "—", "—"
                    elif ratio is None:
                        p_str, st_lbl = "—", "doplň Δ vstup + Δ aktuálnu"
                    elif ratio >= _SHORT_DELTA_ALERT_RATIO:
                        p_str, st_lbl = f"{ratio:.2f}×", "⛔ |Δ| ≥ 2× oproti vstupu"
                    elif ratio >= _SHORT_DELTA_WARN_RATIO:
                        p_str, st_lbl = f"{ratio:.2f}×", "⚠ blíži sa k 2×"
                    else:
                        p_str, st_lbl = f"{ratio:.2f}×", "OK"
                    _watch_rows.append(
                        {
                            "ID": int(row["ID"]),
                            "Ticker": row.get("Ticker") or "",
                            "Noha": lt,
                            "|Δ aktuál| / |Δ vstup|": p_str,
                            "Stav": st_lbl,
                        }
                    )
                if any(str(r.get("Noha")) == "Short" for r in _watch_rows):
                    st.markdown("##### Sledovanie delty (shortové nohy)")
                    st.caption(
                        f"Pomer |Δ aktuálna| ÷ |Δ vstup|. Varovanie od **{_SHORT_DELTA_WARN_RATIO}×**, "
                        f"silné od **{_SHORT_DELTA_ALERT_RATIO}×**."
                    )
                    st.dataframe(pd.DataFrame(_watch_rows), use_container_width=True, hide_index=True)

                if st.button("Uložiť journal (Gréky, IV, Vega, skupina)", key=f"pf_sv_{_gkey}", type="primary"):
                    nchg = 0
                    nsnap = 0
                    for _, row in edited.iterrows():
                        tid = int(row["ID"])
                        orig = orig_by_id.get(tid, {})
                        sk = _skupina_cell_norm(row.get("Skupina"))
                        new_gid = None if sk in (PF_GROUP_NONE, "— bez skupiny") else sk
                        old_gid = (orig.get("group_id") or "").strip() or None
                        if (new_gid or "") != (old_gid or ""):
                            db.update_trade(tid, group_id="" if not new_gid else new_gid)
                            nchg += 1

                        new_iv = _greek_cell_to_db(orig.get("iv_at_entry"), row["IV vstup"])
                        new_d = _greek_cell_to_db(orig.get("delta_at_entry"), row["Δ vstup"])
                        new_th = _greek_cell_to_db(orig.get("theta_at_entry"), row["Θ vstup ($/deň)"])
                        new_dc = _greek_cell_to_db(orig.get("delta_current"), row["Δ aktuálna"])
                        new_tc = _greek_cell_to_db(orig.get("theta_current"), row["Θ aktuálna ($/deň)"])
                        new_ve = _greek_cell_to_db(orig.get("vega_at_entry"), row["Vega vstup"])
                        new_vc = _greek_cell_to_db(orig.get("vega_current"), row["Vega aktuálna"])
                        new_ivc = _greek_cell_to_db(orig.get("iv_current"), row["IV aktuálna"])

                        greek_changed = (
                            not _entry_float_eq(orig.get("iv_at_entry"), new_iv)
                            or not _entry_float_eq(orig.get("delta_at_entry"), new_d)
                            or not _entry_float_eq(orig.get("theta_at_entry"), new_th)
                            or not _entry_float_eq(orig.get("delta_current"), new_dc)
                            or not _entry_float_eq(orig.get("vega_at_entry"), new_ve)
                            or not _entry_float_eq(orig.get("vega_current"), new_vc)
                            or not _entry_float_eq(orig.get("iv_current"), new_ivc)
                            or not _entry_float_eq(orig.get("theta_current"), new_tc)
                        )
                        if greek_changed:
                            db.set_trade_portfolio_greeks(
                                tid,
                                new_iv,
                                new_d,
                                new_th,
                                new_dc,
                                vega_at_entry=new_ve,
                                vega_current=new_vc,
                                iv_current=new_ivc,
                                theta_current=new_tc,
                            )
                            nchg += 1
                        if greek_changed and any(
                            x is not None for x in (new_dc, new_tc, new_vc, new_ivc)
                        ):
                            db.insert_trade_greek_snapshot(
                                tid,
                                delta=new_dc,
                                theta_usd=new_tc,
                                vega=new_vc,
                                iv=new_ivc,
                            )
                            nsnap += 1
                    if nchg or nsnap:
                        st.success(f"Uložené — zmenených záznamov: **{nchg}**, nových bodov histórie: **{nsnap}**.")
                        st.rerun()
                    else:
                        st.info("Žiadna zmena.")
            st.divider()
