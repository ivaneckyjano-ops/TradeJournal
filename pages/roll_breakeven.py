"""
Kalkulátor: pri akej cene podkladu (BS, ručné IV) dosiahne 2+ nohý spread cieľové netto (napr. 0 = hranica kredit/debet).
Všetky vstupy sú manuálne.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from core.page_context import set_tradejournal_page
from core.roll_breakeven import ManualLeg, breakeven_spots, net_premium

set_tradejournal_page("roll_breakeven")

st.title("Rolovanie / spread — cena podkladu pre cieľové netto")
st.caption(
    "**Návod:** Zapni nohy 1–4 (predaj/nákup, call/put, strike, DTE, IV, kontrakty) a nastav **cieľové netto** (často 0 = breakeven v prémii). "
    "Model **Black–Scholes** s ručnými vstupmi nájde spot(y), kde súčet (+ predaj, − nákup) prémii zodpovedá cieľu — bez IB, zjednodušený odhad."
)

c1, c2, c3 = st.columns(3)
with c1:
    r_pct = st.number_input("Bezriziková miera *r* (% p.a.)", value=4.5, min_value=0.0, max_value=30.0, step=0.1, key="rb_r")
with c2:
    target = st.number_input("Cieľové netto ($/akcia na kontrakt v sume nôh)", value=0.0, step=0.01, key="rb_target")
with c3:
    iv_is_pct = st.toggle("IV zadávať v % (napr. 35 = 35 %)", value=True, key="rb_ivpct")


def dte_to_years(dte: int) -> float:
    return max(1e-6, int(dte) / 365.0)


def leg_form(prefix: str, label: str, *, default_on: bool) -> ManualLeg | None:
    on = st.checkbox(f"Použiť {label}", value=default_on, key=f"{prefix}_on")
    if not on:
        return None
    a, b = st.columns(2)
    with a:
        side = st.selectbox("Smer", options=["Predaj (prémiu prijmeš)", "Nákup (zaplatíš)"], key=f"{prefix}_side")
    with b:
        right = st.selectbox("Typ", options=["Call", "Put"], key=f"{prefix}_right")
    k = st.number_input("Strike *K*", value=100.0, min_value=0.01, step=0.5, key=f"{prefix}_k")
    dte = st.number_input("Dni do expirácie (DTE)", value=30, min_value=1, max_value=5000, step=1, key=f"{prefix}_dte")
    iv = st.number_input("Implik. volatilita (IV)", value=32.0 if iv_is_pct else 0.32, step=0.1, key=f"{prefix}_iv")
    n_con = st.number_input("Počet kontraktov", value=1, min_value=1, max_value=1000, step=1, key=f"{prefix}_n")

    side_code: str = "sell" if "Predaj" in side else "buy"
    d = float(iv) / 100.0 if iv_is_pct else float(iv)
    if d <= 0 or d > 3.0:
        st.warning("Skontroluj IV (rozumný rozsah 1–150 %).")
    ty = dte_to_years(int(dte))
    rc = "C" if right == "Call" else "P"
    return ManualLeg(
        side=side_code,  # type: ignore[arg-type]
        right=rc,
        strike=float(k),
        t_years=ty,
        iv=d,
        contracts=int(n_con),
    )


legs: list[ManualLeg] = []
with st.expander("Noha 1", expanded=True):
    L1 = leg_form("L1", "nohu 1", default_on=True)
    if L1 is not None:
        legs.append(L1)
with st.expander("Noha 2", expanded=True):
    L2 = leg_form("L2", "nohu 2", default_on=True)
    if L2 is not None:
        legs.append(L2)
with st.expander("Noha 3 (voliteľné)", expanded=False):
    L3 = leg_form("L3", "nohu 3", default_on=False)
    if L3 is not None:
        legs.append(L3)
with st.expander("Noha 4 (voliteľné)", expanded=False):
    L4 = leg_form("L4", "nohu 4", default_on=False)
    if L4 is not None:
        legs.append(L4)

st.subheader("Rozpätie a referencia")
r_a, r_b, r_c, r_d = st.columns(4)
with r_a:
    s_lo = st.number_input("Min. *S* (hľadanie koreňov)", value=20.0, min_value=0.01, step=1.0, key="rb_smin")
with r_b:
    s_hi = st.number_input("Max. *S*", value=400.0, min_value=0.01, step=1.0, key="rb_smax")
with r_c:
    s_ref = st.number_input("Referenčný spot (porovnanie / tabuľka)", value=100.0, min_value=0.01, step=0.5, key="rb_sref")
with r_d:
    n_table = st.number_input("Počet bodov v tabuľke a grafe", value=60, min_value=20, max_value=2000, step=10, key="rb_ntbl")

r = float(r_pct) / 100.0

go = st.button("Vypočítať", type="primary", key="rb_go")

if go:
    if len(legs) < 1:
        st.info("Zapni aspoň jednu nohu a vyplň polia.")
    elif s_hi <= s_lo:
        st.error("Max. *S* musí byť väčšie než min. *S*.")
    else:
        n_scan = int(min(2000, max(100, 10 * n_table)))
        roots = breakeven_spots(
            legs,
            r=r,
            target_net=target,
            s_min=s_lo,
            s_max=s_hi,
            n_scan=n_scan,
        )
        n0 = net_premium(s_ref, legs, r)
        st.metric("Modelové netto pri referenčnom *S*", f"{n0:+.4f} ($/akcia v sčítaní nôh)")

        if roots:
            st.success(
                "Cena(y) spotu, kde model dá **netto = cieľ**: "
                + ", ".join(f"**{x:.2f}**" for x in roots)
            )
        else:
            st.warning(
                "V zadanom rozpätí sa nenašla cena *S* s `net = cieľ` (funkcia nemusí križovať nulu, "
                "alebo zúž/rozšír rozpätie)."
            )

        step = (s_hi - s_lo) / max(1, int(n_table) - 1)
        rows = []
        s_cur = s_lo
        for _i in range(int(n_table)):
            rows.append(
                {
                    "spot": round(s_cur, 4),
                    "net": round(net_premium(s_cur, legs, r), 6),
                }
            )
            s_cur += step
        df = pd.DataFrame(rows)
        st.dataframe(df, use_container_width=True, height=min(400, 24 * len(df)))
        st.line_chart(df.set_index("spot")["net"])


st.divider()
st.markdown(
    """
**Čítanie znamienka:** pre každú nohu + predaj, − nákup; súčet je modelové **čisté prémiové saldo** v $/akcia
(vážené `contracts` na nohe), nie cashflow po násobku 100, ten vieš vynásobiť sám.

**Obmedzenie modelu:** fixné IV na oboch nôhach — reálne sa IV so spotom a časom mení. Slúži na náčrt, nie ako záruka v TWS.
"""
)
