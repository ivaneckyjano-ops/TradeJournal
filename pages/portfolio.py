"""
Portfolio — prehľad otvorených pozícií z denníka (DB), podľa skupín.
Žiadne sťahovanie z IBKR/API; live dáta ostávajú na TWS Dashboarde.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import date

import pandas as pd
import streamlit as st

from core import database as db
from core.page_context import set_tradejournal_page

db.init_db()
set_tradejournal_page("portfolio")

# Short noha: |Δ aktuálna| vs |Δ vstup| — či sa približuješ k dvojnásobku rizika (delta exposure)
_SHORT_DELTA_WARN_RATIO = 1.5
_SHORT_DELTA_ALERT_RATIO = 2.0

st.title("Portfolio")
st.caption(
    "Zdroj sú **výhradne záznamy v denníku** (otvorené obchody). "
    "Ticker vo filtri vyberáš zo **zoznamu Symboly**."
)


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
    """Orientačný kapitál nohy: |entry| × kontrakty × 100."""
    try:
        c = float(t.get("contracts") or 1)
        e = float(t.get("entry_price") or 0)
        return abs(e) * c * 100.0
    except (TypeError, ValueError):
        return 0.0


def _nan_to_none(v) -> float | None:
    # Streamlit 1.4x občas vráti jednoprvkový list pri úprave NumberColumn
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


def _entry_float_eq(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) < 1e-9


def _short_delta_abs_ratio(leg_type: str, entry_d: float | None, curr_d: float | None) -> float | None:
    """Pre Short: |Δ aktuálna| / |Δ vstup|. Iné nohy — None."""
    if str(leg_type or "").strip() != "Short":
        return None
    if entry_d is None or curr_d is None:
        return None
    ae = abs(float(entry_d))
    if ae < 1e-12:
        return None
    return abs(float(curr_d)) / ae


with st.expander("Filter", expanded=False):
    _sym_raw = db.get_symbol_tickers()
    _sym_sorted = sorted({str(t).strip().upper() for t in _sym_raw if str(t).strip()})
    _sym_opts = ["— všetky —"] + _sym_sorted
    _sel = st.selectbox(
        "Ticker (zo záložky Symboly)",
        options=_sym_opts,
        index=0,
        key="pf_journal_symbol_filter",
        help="Zoznam berie z tabuľky Symboly. Chýbajúci ticker doplníš v záložke **Symboly**.",
    )
    ticker_filter = "" if _sel == "— všetky —" else _sel
    if not _sym_sorted:
        st.info("V **Symboly** zatiaľ nemáš žiadny ticker — filtrovať podľa symbolu zatiaľ nejde.")

open_trades = db.get_open_trades()
if ticker_filter:
    open_trades = [t for t in open_trades if str(t.get("ticker") or "").upper() == ticker_filter]

groups_meta = {g["name"]: g for g in db.get_groups()}

by_group: dict[str, list[dict]] = defaultdict(list)
for t in open_trades:
    gid = (t.get("group_id") or "").strip()
    label = gid if gid else "— bez skupiny"
    by_group[label].append(t)

# stabilné poradie: najprv pomenované skupiny abecedne, nakoniec „bez skupiny“
_sort_keys = sorted(by_group.keys(), key=lambda x: (x == "— bez skupiny", x.lower()))

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
        help="Súčet absolútnej vstupnej prémie × kontrakty × 100 — len z denníka, nie mark-to-market.",
    )
else:
    m3.metric("Σ |vstupná prémia| × 100", "—")

st.subheader("Otvorené pozície podľa skupín")

if not open_trades:
    st.info("V denníku nemáš žiadne **otvorené** obchody. Pridaj alebo importuj ich v Trade Logu.")
    st.stop()

for gname in _sort_keys:
    legs = by_group[gname]
    meta = groups_meta.get(gname) if gname != "— bez skupiny" else None
    _gkey = hashlib.sha256(gname.encode("utf-8")).hexdigest()[:16]
    with st.container():
        st.markdown(f"#### {gname}")
        if meta:
            _tk = meta.get("ticker") or ""
            _st = meta.get("strategy") or ""
            if _tk or _st:
                st.caption(f"Skupina v DB: **{_tk}** · {_st}")
        rows = []
        orig_by_id: dict[int, dict] = {}
        for t in sorted(legs, key=lambda x: (str(x.get("ticker") or ""), int(x.get("id") or 0))):
            tid = int(t["id"])
            orig_by_id[tid] = t
            exp = t.get("expiry") or ""
            dte_v = _dte(str(exp))
            iv_e = t.get("iv_at_entry")
            dlt_e = t.get("delta_at_entry")
            th_e = t.get("theta_at_entry")
            dlt_c = t.get("delta_current")
            rows.append(
                {
                    "ID": tid,
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
                    "Δ vstup": pd.NA if dlt_e is None else float(dlt_e),
                    "Δ aktuálna": pd.NA if dlt_c is None else float(dlt_c),
                    "IV vstup": pd.NA if iv_e is None else float(iv_e),
                }
            )
        df = pd.DataFrame(rows)
        for _c in ("Θ vstup ($/deň)", "Δ vstup", "Δ aktuálna", "IV vstup"):
            df[_c] = df[_c].astype("Float64")
        st.caption(
            "Stĺpce **Θ vstup**, **Δ vstup**, **Δ aktuálna** a **IV vstup** môžeš doplniť alebo zmeniť tu. **IV** ako zlomok (napr. **0,35** = 35 %). "
            "**Θ** = theta pozície v **USD za deň** (ako z TWS). **Δ aktuálna** = aktuálna delta pozície z TWS (pre **Short** nohu pod tabuľkou uvidíš pomer k Δ vstup — varovanie pri ~2×). "
            "Prázdne bunky = nevyplnené."
        )
        edited = st.data_editor(
            df,
            use_container_width=True,
            hide_index=True,
            disabled=[
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
            ],
            column_config={
                "Strike": st.column_config.NumberColumn(format="$%.2f"),
                "Entry $": st.column_config.NumberColumn(format="$%.2f"),
                "DTE": st.column_config.NumberColumn(format="%d dní"),
                "Θ vstup ($/deň)": st.column_config.NumberColumn(
                    format="$%.3f",
                    step=0.001,
                    help="Theta v USD za deň pre celú nohu (podľa brokera / vlastný zápis).",
                ),
                "Δ vstup": st.column_config.NumberColumn(
                    format="%.4f",
                    step=0.0001,
                    help="Delta pozície pri otvorení (TWS), znak podľa long/short.",
                ),
                "Δ aktuálna": st.column_config.NumberColumn(
                    format="%.4f",
                    step=0.0001,
                    help="Aktuálna delta pozície z TWS — pre short nohu porovnávaj s Δ vstup (pomer nižšie).",
                ),
                "IV vstup": st.column_config.NumberColumn(
                    format="%.4f",
                    step=0.0001,
                    help="Impl. volatilita pri vstupe ako desatinný zlomok (0,30 = 30 %).",
                ),
            },
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
                f"Porovnanie **aktuálnej delty** (z TWS) s **deltou pri vstupe**. "
                f"Pomer = |Δ aktuálna| ÷ |Δ vstup|. Varovanie od **{_SHORT_DELTA_WARN_RATIO}×**, "
                f"silné upozornenie od **{_SHORT_DELTA_ALERT_RATIO}×** (typicky zvýšené riziko pri short opciách)."
            )
            st.dataframe(
                pd.DataFrame(_watch_rows),
                use_container_width=True,
                hide_index=True,
            )

        if st.button("Uložiť Θ, Δ, IV a Δ aktuálnu", key=f"pf_sv_{_gkey}", type="primary"):
            nchg = 0
            for _, row in edited.iterrows():
                tid = int(row["ID"])
                orig = orig_by_id.get(tid, {})
                new_iv = _nan_to_none(row["IV vstup"])
                new_d = _nan_to_none(row["Δ vstup"])
                new_th = _nan_to_none(row["Θ vstup ($/deň)"])
                new_dc = _nan_to_none(row["Δ aktuálna"])
                if (
                    not _entry_float_eq(orig.get("iv_at_entry"), new_iv)
                    or not _entry_float_eq(orig.get("delta_at_entry"), new_d)
                    or not _entry_float_eq(orig.get("theta_at_entry"), new_th)
                    or not _entry_float_eq(orig.get("delta_current"), new_dc)
                ):
                    db.set_trade_portfolio_greeks(tid, new_iv, new_d, new_th, new_dc)
                    nchg += 1
            if nchg:
                st.success(f"Uložené — upravených {nchg} nôh.")
                st.rerun()
            else:
                st.info("Žiadna zmena v Θ, Δ, IV ani Δ aktuálnej.")
    st.divider()
