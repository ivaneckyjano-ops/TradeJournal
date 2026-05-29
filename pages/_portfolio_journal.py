"""
Casopis — otvorené nohy z denníka: skupiny a ručný zápis Grékov / IV do DB.
"""
from __future__ import annotations

import hashlib
import math
import time
from collections import defaultdict
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from core import database as db
from core import ibkr
from core.greeks import bs_price, calc_iv, iv_display_to_bs_fraction, spot_for_abs_delta_bs
from core.ib_row_extract import ParsedIbRow, ocr_image_to_text, parse_ibkr_row_text
from core.journal_pnl_curve import (
    _iv_fraction_for_leg,
    _journal_leg_instrument_label as _journal_leg_instrument_label_ui,
    _legs_display_order as _legs_display_order_ui,
    _right_bs,
    _single_leg_pl_now_usd,
    journal_group_pl_ladder_tws_style_rows,
    journal_group_pl_stoploss_short_window,
    journal_group_pl_vs_spot,
    journal_spot_levels_band,
    journal_spot_levels_descending,
)
from core import delta_hedge_paper as dhp
from core.portfolio_data import (
    calc_dte,
    find_ibkr_option_for_trade,
    greek_for_trade,
    ib_opt_greeks_scaled_for_journal,
    journal_contract_shares_multiplier,
    unrealized_by_journal_ids_for_ib_legs,
)
from core.page_context import set_tradejournal_page
from core.spread_mentor import (
    analyze_calendar_mentor,
    analyze_diagonal_mentor,
    compute_journal_group_greek_snapshot,
    journal_greek_comparison_rows,
    journal_greek_mentor_hints,
    mentor_calendar_rows,
    mentor_comparison_rows,
)

_journal_mode = str(st.session_state.get("ib_mode") or "").strip().upper()
if _journal_mode == "PAPER":
    db.DB_PATH = db.PAPER_DB_PATH
else:
    db.DB_PATH = db.LIVE_DB_PATH

db.init_db()
set_tradejournal_page("portfolio")

_SHORT_DELTA_WARN_RATIO = 1.5
_SHORT_DELTA_ALERT_RATIO = 2.0

_JOURNAL_METRICS_HELP_MD = """
### Σ čistá Δ vstup · Σ čistá Δ aktuálna

Súčet cez **všetky nohy** v skupine: **Δ (0–1) × počet kontraktov × 100 × znamienko nohy** (+1 long, −1 short).  
Výsledok je **ekvivalent podkladu v počte akcií** (rovnaká logika ako pri súčtoch nôh v analýze portfólia).  
**Vstup** = z bunky Δ vstup, **aktuálna** = z bunky Δ aktuálna (z denníka alebo po zápise z TWS).

**Prakticky — vyrovnanie Δ podkladom (cieľ 0):**  
Číslo pod tabuľkou je „koľko akcií“ ti dávajú opcie (a prípadné **STK** riadky v DB) dokopy.

- **Σ = +10** → na vyrovnanie **predaj** (alebo podshort) cca **10 akcií** podkladu  
- **Σ = −10** → na vyrovnanie **dokúp** cca **10 akcií** podkladu  

Inak povedané: opačný obchod na podklade v rovnakej veľkosti ako súčet (±). Presné množstvo doladíš podľa cieľa (napr. chceš zostať mierne long +5) a podľa toho, či už v TWS držíš akcie mimo tejto skupiny.

### Σ čistá Θ vstup · Σ čistá Θ aktuálna

Súčet stĺpcov **Θ … ($/deň)** — theta **v USD za kalendárny deň za celú nohu** (už s kontraktmi a znamienkom).  
Prázdne bunky (NA) sa do súčtu **nezapočítavajú**.

### Delta doláre (Σ)

**Ide o expozíciu v $** pri pohybe podkladu: súčet **(ekvivalent akcií z Δ) × spot**.

- **Prednosť IB (súlad s TWS):** modelová delta z IB (`ib_opt_greeks_scaled_for_journal`) × **spot podkladu** — najprv z riadku **akcie (STK)** v tom istom fetchi z TWS, ak chýba, **spot** zo záložky **Symboly**.
- **Záloha:** Δ aktuálna z tabuľky časopisu × spot zo **Symboly** (ak nie je kompletný IB výpočet pre všetky nohy).

Na IB vetvu treba v session **`live_positions`** z fetchu **s Grékmi** (`with_greeks=True`) — tlačidlo **Obnoviť údaje z TWS** pri danej skupine (alebo *Doplniť aktuálne Gréky z TWS → journal* v expanderi vyššie).

### Trhová hodnota (Σ)

Súčet **trhovej hodnoty opčných nôh** so zhodou v IB cache. Prednostne **cena kontraktu × kontrakty × 100 × znamienko** (rovnako ako pri dopočítaní v `ibkr._apply_upnl_from_price`), inak pole **`marketValue`** z API.  
Bez načítaných pozícií z IB je hodnota **—**.

### Nákladová základňa (Σ)

Súčet **nákladovej bázy** so **znamienkom**, aby bol výsledok blízky **net cost basis** v TWS pri spreadoch: záporné `averageCost` z IB ostávajú; ak je short **kladný** (kredit ako kladné číslo), berie sa ako **odpočet**.  
Bez IB cache je hodnota **—**.

### Trhová hodnota − náklad (Σ)

**Súčet MV mínus súčet nákladovej bázy** z rovnakých IB riadkov ako vyššie — orientačný **nezrealizovaný P&L** celej skupiny (nie je to samostatné pole z API). Pri neúplnej zhode nôh s IB sú to súčty len z **zhodných** nôh.  
Bez IB cache je hodnota **—**.

### Horný súčet nad tabuľkou (Σ |vstup| — USD)

Riadok metrík **nad** tabuľkou časopisu: súčet **|entry_price| × contracts** s násobkom **100 len pre opčné** nohy (veľkosť kontraktu). Pre **STK** v denníku je to **|$/akcia| × počet akcií** — násobok 100 sa **nepoužíva** (inak by akciová noha umelo nafukovala súčet).

### Časté problémy

- **Metriky z IB ≠ TWS:** obnov dáta tlačidlom pri skupine; over, že v účte je **STK** podkladu (pre spot) alebo že v **Symboly** je aktuálny **spot**.
- **Žiadna zhoda IB:** noha v denníku musí sedieť s kontraktom v TWS (ticker, strike, expirácia, typ, Long/Short).

### Graf P&L vs. cena podkladu

Pod tabuľkou skupiny je expander s **orientačným** grafom (Plotly, tmavý motív): **plná čiara** = model P&L pri dnešnom zostávajúcom čase (BS), **čiarkovaná** = model pri dni **najbližšej expirácie** v skupine (pre každú nohu zostávajúci čas do jej expirácie oproti tomu dňu; pri viacerých expiráciách ide o zjednodušenie).  
**Tenšie farebné čiary (+2, +3, +5 dní):** ten istý BS model, ale ak by už ubehli tieto **kalendárne** dni (kratší čas do expirácie, rovnaká IV z journalu). Pri **rozdielnej Δ** medzi nohami uvidíš, ako sa krivky pod spotom v čase **rozbiehajú** — nohy sa pri pohybe podkladu nekompenzujú rovnako.  
IV berie z **IV aktuálna / IV vstup** v journali (inak 30 %). **Aktuálny spot** v grafe (ak je IB pripojené): najprv **živý podklad** (`fetch_underlying` — snapshot alebo história, krátka cache), potom **STK** z portfólia (môže meškať oproti TWS), inak **Symboly** — vertikálna **tyrkysová čiara** a **diamanty**; rozsah osi X sa rozšíri okolo spotu.

### Stop-loss graf (podklad okolo K shortu)

Samostatný expander: os **X = cena podkladu (USD)** v úzkom pásme okolo **striku short** nohy. **Šírka** pásma pod a nad K sa predvolene odvodí od **vstupnej prémie** (`entry_price`) short nohy v journali; ak prémia chýba, použije sa širší predvolený rozsah. Zobrazí **P&L dnes** a **+2 / +3 / +5 dní**. Tyrkysová čiara = aktuálny spot, sivá bodkovaná = **K** shortu. Pod grafom je **tabuľka v štýle TWS**: pre každý scenár **spotu** riadky **nožičiek** (kontrakt, noha, ks., P&L) a riadok **Σ NET** — orientačný BS, nie presná kópia TWS.
"""


st.title("Casopis — Gréky a skupiny")

st.caption(
    "**Návod:** Uprav **Δ vstup / Δ aktuálna**, **Θ vstup ($/deň) / Θ aktuálna ($/deň)** a ďalšie polia v tabuľke, potom **Uložiť journal** pri skupine. "
    "Pod tabuľkou sú **súčty za skupinu** (čistá Δ/Θ), **Delta doláre / trhová hodnota / nákladová základňa / rozdiel TH−náklad** (prednostne z IB ako v TWS; inak journal + Symboly). "
    "Hodnoty sú z denníka (ručný zápis alebo iný import)."
)

st.info(
    "Časopis načítava **iba databázu** — nie priamy náhľad z TWS. "
    "Ten istý zoznam nôh ako na **Dashboarde** dostaneš po **importe pozícií z IBKR** (nižšie alebo na Dashboarde). "
    "Globálna auto-sync v sidebari tiež zapisuje pozície do DB, ak je zapnutá."
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


def _render_delta_hedge_panel() -> None:
    """
    Súčet Δ z otvorených nôh v aktívnej DB (LIVE aj PAPER), spot (Symboly + override),
    odporúčaný obchod na podklade a deadband. Sekcia „Úvaha“ — manuálne nohy bez DB.
    """
    def _cell_float(v) -> float | None:
        """Parsovanie čísla z bunky editora (modul ešte nemusí mať ``_journal_float_for_sum`` — voláme skoro na začiatku súboru)."""
        if v is None or v is pd.NA:
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        if isinstance(x, float) and math.isnan(x):
            return None
        return x

    with st.expander("Delta hedge — podklad prvého rádu", expanded=False):
        _dh_alt_col_help = (
            "Odporúčanie berie najbližší reálny kontrakt z posledného uloženého option chainu "
            "pre daný ticker. Preferuje expiráciu short nohy a minimálne 1 DTE. "
            "Ak chain chýba, zobrazí sa upozornenie."
        )
        _dh_alt_legend = (
            "**Odporúčanie (opcia)** = najbližší **reálny kontrakt** z lokálnej databázy option chainov "
            "(strana Call/Put podľa smeru hedgu). Preferencia: expirácia podľa **short nohy**, ale aspoň **1 DTE**. "
            "V rámci tejto expirácie sa vyberá kombinácia **počet kontraktov + skutočná delta**, ktorá čo najlepšie sedí "
            "na požadovaný hedge. Text ukazuje počet kontraktov, približnú **skutočnú deltu** z chainu a aj "
            "**strike + expiráciu**, aby si vedel, ktorý kontrakt hľadať v TWS."
        )
        st.caption(
            "**Z DB:** otvorené nohy a **Δ aktuálna** (inak Δ vstup). **Úvaha:** skopíruj Δ z OptionTrader/TWS — "
            "**nič sa neukladá** do denníka. **Neodosiela príkazy** — len orientačné čísla. "
            "**Alternatíva (opcia)** používa orientačnú Δ≈0,45/akcia (call / put); konkrétny kontrakt v TWS si vyber podľa expirácie a striku. "
            "Odporúčania sú v **dvoch stĺpcoch**, aby sa text v tabuľke neorezával."
        )
        open_tr = [t for t in db.get_open_trades() if str(t.get("status") or "Open").lower() == "open"]
        by_tk = dhp.net_delta_shares_by_ticker(open_tr)

        st.markdown("##### Z portfólia (otvorené nohy v DB)")
        _dh_ib_ok = ibkr.is_connected()
        if not _dh_ib_ok:
            st.warning("**IBKR nie je pripojený** — na Dashboarde alebo v sidebari stlač **Pripojiť** (TWS musí bežať).")
        else:
            _dbc1, _dbc2 = st.columns(2)
            with _dbc1:
                if st.button(
                    "Stiahnuť pozície z IBKR → DB",
                    type="primary",
                    use_container_width=True,
                    key="dh_journal_sync_positions",
                    help="Zosúladí otvorené nohy v denníku s portfóliom v TWS (rovnaké ako „Importovať pozície“ nižšie na stránke).",
                ):
                    with st.spinner("Načítavam portfólio z IBKR…"):
                        _res_p = ibkr.fetch_positions(use_historical_last=False)
                    if _res_p.get("error"):
                        st.session_state["dh_ib_sync_msg"] = ("error", str(_res_p["error"]))
                    else:
                        _sync = ibkr.sync_positions_to_db(_res_p["positions"], db, close_missing=True)
                        ibkr.set_scoped_session_value("live_positions", _res_p["positions"])
                        st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
                        st.session_state["dh_ib_sync_msg"] = (
                            "ok",
                            f"Pozície v DB: pridané **{_sync['added']}** · aktualizované **{_sync.get('updated', 0)}** · "
                            f"uzavreté **{_sync.get('closed', 0)}**.",
                        )
                    st.rerun()
            with _dbc2:
                if st.button(
                    "Doplniť Gréky z TWS → DB (všetky otvorené nohy)",
                    type="secondary",
                    use_container_width=True,
                    key="dh_journal_sync_greeks_all",
                    help="Načíta pozície s Grékmi a zapíše Δ, IV, Θ, Vega do DB pre každú otvorenú nohu so zhodou v TWS.",
                ):
                    with st.spinner("Sťahujem Gréky z TWS…"):
                        _res_g = ibkr.fetch_positions(with_greeks=True, use_historical_last=False)
                    if _res_g.get("error"):
                        st.session_state["dh_ib_sync_msg"] = ("error", str(_res_g["error"]))
                    else:
                        _poss = list(_res_g.get("positions") or [])
                        ibkr.set_scoped_session_value("live_positions", _poss)
                        st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
                        _n_ok = 0
                        _nom: list[str] = []
                        for _tr in db.get_open_trades():
                            if str(_tr.get("status") or "Open").strip().lower() != "open":
                                continue
                            if _journal_write_live_greeks_for_trade(_tr, _poss):
                                _n_ok += 1
                            else:
                                _tk = str(_tr.get("ticker") or "")
                                _nom.append(f"{_tk} {_tr.get('strike')} {_tr.get('expiry')} {_tr.get('option_type')}")
                        _ex = f" Bez zhody ({len(_nom)}): {', '.join(_nom[:6])}" if _nom else ""
                        if len(_nom) > 6:
                            _ex += "…"
                        st.session_state["dh_ib_sync_msg"] = ("ok", f"Gréky v DB: **{_n_ok}** nôh.{_ex}")
                    st.rerun()
        _dh_sync_pop = st.session_state.pop("dh_ib_sync_msg", None)
        if _dh_sync_pop:
            _dk, _dt = _dh_sync_pop
            if _dk == "error":
                st.error(_dt)
            else:
                st.success(_dt)

        if not by_tk:
            st.info(
                "Žiadne otvorené nohy v DB — ak máš pozície v TWS, stlač **Stiahnuť pozície z IBKR → DB**, "
                "potom **Doplniť Gréky z TWS → DB** (potrebné je IB pripojenie). Alebo použij **Úvaha bez pozície** nižšie."
            )
        else:
            c1, c2 = st.columns(2)
            with c1:
                target_d = st.number_input(
                    "Cieľová čistá Δ na ticker (akcie; opcie + podklad po hedži)",
                    value=5.0,
                    step=1.0,
                    format="%.2f",
                    key="dh_paper_target_shares",
                    help="Rovnaký cieľ pre každý podklad zvlášť; predvolene +5 akcií (mierne long delta po hedži).",
                )
            with c2:
                deadband = st.number_input(
                    "Deadband (|hedge| pod týmto = neobchodovať)",
                    value=5.0,
                    min_value=0.0,
                    step=1.0,
                    format="%.1f",
                    key="dh_paper_deadband_shares",
                )
            st.markdown(
                "**Spot podkladu (USD)** — prednosť **Symboly**; ak je spot 0, panel doplní "
                "orientačné predvolené hodnoty **AMZN 262.84**, **UNH 375.46** (iba úvodné načítanie)."
            )
            tickers = list(by_tk.keys())
            ncols = min(3, len(tickers))
            cols = st.columns(ncols)
            _paper_spot_fallback = {"AMZN": 262.84, "UNH": 375.46}
            for i, tk in enumerate(tickers):
                sym = db.get_symbol(tk)
                base_spot = float(sym["spot"] or 0.0) if sym else 0.0
                sk = f"dh_paper_spot_{tk}"
                if sk not in st.session_state:
                    init_sp = base_spot if base_spot > 0 else float(_paper_spot_fallback.get(tk, 0.0))
                    st.session_state[sk] = float(init_sp)
                with cols[i % ncols]:
                    st.number_input(f"{tk}", min_value=0.0, step=0.01, format="%.2f", key=sk)
            rows: list[dict] = []
            for tk in tickers:
                net = float(by_tk[tk])
                spot = float(st.session_state.get(f"dh_paper_spot_{tk}", 0.0) or 0.0)
                dd = dhp.dollar_delta(net, spot) if spot > 0 else None
                hedge = dhp.hedge_shares_for_target(net, float(target_d))
                _, inside = dhp.apply_deadband(hedge, float(deadband))
                _pref_exp = dhp.preferred_short_expiry_for_ticker(open_tr, tk)
                _rec_stk, _rec_opt = dhp.hedge_table_recommendation_cells(
                    hedge,
                    inside_deadband=inside,
                    ticker=tk,
                    preferred_expiry=_pref_exp,
                )
                rows.append(
                    {
                        "Ticker": tk,
                        "Čistá Δ opcie (akcie)": round(net, 2),
                        "Spot": round(spot, 2) if spot else None,
                        "$ Δ (opcie)": round(dd, 0) if dd is not None else None,
                        "Hedge podklad (akcie)": round(hedge, 2),
                        "V deadband": "Áno" if inside else "Nie",
                        "Odporúčanie (podklad)": _rec_stk,
                        "Odporúčanie (opcia)": _rec_opt,
                    }
                )
            _dh_df = pd.DataFrame(rows)
            st.dataframe(
                _dh_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Odporúčanie (podklad)": st.column_config.TextColumn(width="large"),
                    "Odporúčanie (opcia)": st.column_config.TextColumn(
                        width="large",
                        help=_dh_alt_col_help,
                    ),
                },
            )
            st.caption(_dh_alt_legend)
            if any(float(st.session_state.get(f"dh_paper_spot_{tk}", 0.0) or 0.0) <= 0 for tk in tickers):
                st.warning("Pre ticker bez spotu dopln **Symboly** alebo spot vyššie — inak chýba **$Δ**.")

        st.divider()
        st.markdown("##### Úvaha bez pozície v DB (manuálne nohy)")
        st.caption(
            "Vyber **ticker zo Symboly** a do tabuľky **Long/Short**, **Δ na akciu** (0–1) a **kontrakty** ako v TWS. "
            "Príklad: MSFT calendar — Short 0,317 / Long 0,475, po 1 kontrakte → čistá Δ **+15,8** akcií."
        )
        _sym_list = sorted({str(t).strip().upper() for t in db.get_symbol_tickers() if str(t).strip()})
        _wt1, _wt2 = st.columns([1, 2])
        with _wt1:
            if _sym_list:
                whatif_tk = str(
                    st.selectbox(
                        "Ticker podkladu (Symboly)",
                        options=_sym_list,
                        key="dh_whatif_symbol",
                    )
                    or ""
                ).strip().upper()
            else:
                st.warning("V **Symboly** nie sú žiadne tickery — dopln ich v aplikácii alebo zadaj ticker ručne.")
                whatif_tk = (
                    st.text_input("Ticker podkladu (text)", value="", key="dh_whatif_ticker_fallback")
                    .strip()
                    .upper()
                )
        with _wt2:
            if st.button("Predvolený príklad: MSFT calendar (430 C)", key="dh_whatif_reset_msft"):
                if "dh_whatif_ed" in st.session_state:
                    del st.session_state["dh_whatif_ed"]
                if "MSFT" in _sym_list:
                    st.session_state["dh_whatif_symbol"] = "MSFT"
                st.rerun()
        if not whatif_tk:
            st.info("Vyber ticker zo **Symboly** (tabuľka v aplikácii), aby sa dal dopočítať spot a $Δ.")
        _whatif_default = pd.DataFrame(
            [
                {"Noha": "Short", "Delta (na akciu)": 0.317, "Kontrakty": 1},
                {"Noha": "Long", "Delta (na akciu)": 0.475, "Kontrakty": 1},
            ]
        )
        _wdf = st.data_editor(
            _whatif_default,
            num_rows="dynamic",
            column_config={
                "Noha": st.column_config.SelectboxColumn("Noha", options=["Short", "Long"], required=True),
                "Delta (na akciu)": st.column_config.NumberColumn(format="%.4f", min_value=-1.0, max_value=1.0, step=0.001),
                "Kontrakty": st.column_config.NumberColumn(format="%d", min_value=1, step=1),
            },
            hide_index=True,
            use_container_width=True,
            key="dh_whatif_ed",
        )
        _wic1, _wic2, _wic3 = st.columns(3)
        with _wic1:
            w_target = st.number_input(
                "Cieľová čistá Δ (akcie)",
                value=5.0,
                step=1.0,
                format="%.2f",
                key="dh_whatif_target",
            )
        with _wic2:
            w_dead = st.number_input(
                "Deadband (akcie)",
                value=5.0,
                min_value=0.0,
                step=1.0,
                format="%.1f",
                key="dh_whatif_deadband",
            )
        sym_w = db.get_symbol(whatif_tk) if whatif_tk else None
        _w_sp0 = float(sym_w["spot"] or 0.0) if sym_w else 0.0
        with _wic3:
            _spot_lbl = f"Spot {whatif_tk} ($)" if whatif_tk else "Spot ($)"
            w_spot = st.number_input(
                _spot_lbl,
                min_value=0.0,
                value=float(_w_sp0 or 0.0),
                step=0.01,
                format="%.2f",
                key="dh_whatif_spot",
            )

        _synth: list[dict] = []
        for _, _wr in _wdf.iterrows():
            _lt = str(_wr.get("Noha") or "").strip()
            if _lt.lower() not in ("long", "short"):
                continue
            _d = _cell_float(_wr.get("Delta (na akciu)"))
            if _d is None:
                continue
            try:
                _kc = max(1, int(round(float(_wr.get("Kontrakty") or 1))))
            except (TypeError, ValueError):
                _kc = 1
            _synth.append(
                {
                    "status": "Open",
                    "ticker": whatif_tk,
                    "leg_type": "Long" if _lt.lower() == "long" else "Short",
                    "delta_current": float(_d),
                    "contracts": _kc,
                }
            )
        _wnet = dhp.net_delta_shares_for_ticker(_synth, whatif_tk) if _synth else 0.0
        _wdd = dhp.dollar_delta(_wnet, float(w_spot)) if w_spot and w_spot > 0 else None
        _wh = dhp.hedge_shares_for_target(_wnet, float(w_target))
        _w_in = dhp.apply_deadband(_wh, float(w_dead))[1]
        _w_stk, _w_opt = dhp.hedge_table_recommendation_cells(
            _wh,
            inside_deadband=_w_in,
            ticker=whatif_tk,
        )
        _wrows = [
            {
                "Ticker": whatif_tk,
                "Čistá Δ opcie (akcie)": round(_wnet, 2),
                "Spot": round(float(w_spot), 2) if w_spot else None,
                "$ Δ (opcie)": round(_wdd, 0) if _wdd is not None else None,
                "Hedge podklad (akcie)": round(_wh, 2),
                "V deadband": "Áno" if _w_in else "Nie",
                "Odporúčanie (podklad)": _w_stk,
                "Odporúčanie (opcia)": _w_opt,
            }
        ]
        if not _synth:
            st.warning("Doplň aspoň jednu nohu s číselnou **Delta (na akciu)**.")
        elif not whatif_tk:
            st.warning("Vyber **ticker** zo Symboly.")
        else:
            st.dataframe(
                pd.DataFrame(_wrows),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Odporúčanie (podklad)": st.column_config.TextColumn(width="large"),
                    "Odporúčanie (opcia)": st.column_config.TextColumn(
                        width="large",
                        help=_dh_alt_col_help,
                    ),
                },
            )
            st.caption(_dh_alt_legend)


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


def _dte_signed(expiry_str: str) -> int | None:
    """Kalendárne dni do expirácie (záporné = už po expirácii)."""
    if not expiry_str:
        return None
    try:
        exp = date.fromisoformat(
            f"{expiry_str[:4]}-{expiry_str[4:6]}-{expiry_str[6:8]}"
            if len(expiry_str) == 8
            else expiry_str
        )
        return (exp - date.today()).days
    except Exception:
        return None


def _notional_per_leg(t: dict) -> float:
    """
    Orientačná hotovosť z journal ``entry_price`` (USD na akciu z DB / importu IB).

    - **OPT**: prémia × kontrakty × 100 (veľkosť opčného kontraktu).
    - **STK** (akcie v denníku): cena × počet akcií — **bez** ×100 (inak by súčet bol 100× príliš vysoký).
    """
    try:
        c = float(t.get("contracts") or 1)
        e = float(t.get("entry_price") or 0)
        ot = str(t.get("option_type") or "").strip().upper()
        mult = 1.0 if ot in ("STK", "STOCK") else 100.0
        return abs(e) * c * mult
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


def _greek_entry_from_current_when_missing(
    orig_entry: float | None,
    entry_cell,
    current_cell,
) -> float | None:
    """
    Ak je vstupný grécky údaj prázdny a používateľ doplnil aktuálnu hodnotu, použij ju aj ako vstup.
    Zachováva pôvodný DB zápis, keď už existuje.
    """
    entry_val = _greek_cell_to_db(orig_entry, entry_cell)
    if entry_val is not None:
        return entry_val
    if orig_entry is None:
        return _nan_to_none(current_cell)
    return entry_val


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


def _option_right_from_typ(typ) -> str | None:
    u = str(typ or "").strip().upper()
    if "CALL" in u or u == "C":
        return "C"
    if "PUT" in u or u == "P":
        return "P"
    return None


def _target_abs_delta_for_ratio(entry_abs: float, ratio: float) -> float | None:
    """Abs. delta opcie pri danom pomere voči |Δ vstup| (cap pri 1)."""
    if entry_abs <= 0:
        return None
    return min(abs(entry_abs) * ratio, 0.9999)


def _fmt_underlying_spot(v: float | None) -> str:
    if v is None:
        return "—"
    return f"${v:,.2f}"


def _pf_quick_apply_parsed(p: ParsedIbRow) -> None:
    if p.strike is not None and p.strike > 0:
        st.session_state["pf_q_strike"] = float(p.strike)
    if p.iv_raw is not None:
        st.session_state["pf_q_iv"] = float(p.iv_raw)
    if p.delta_current is not None:
        st.session_state["pf_q_dc"] = float(p.delta_current)
    if p.expiry is not None:
        st.session_state["pf_q_exp"] = p.expiry
    if p.right == "C":
        st.session_state["pf_q_right"] = "Call"
    elif p.right == "P":
        st.session_state["pf_q_right"] = "Put"
    if p.ticker:
        st.session_state["pf_q_last_ticker"] = p.ticker


with st.expander("📷 Odhad spotu z IB — obrázok alebo skopírovaný text", expanded=False):
    st.caption(
        "Nahraj **screenshot** riadka z IB, alebo **prilep text**. Program skúsi nájsť strike, expiráciu, IV, aktuálnu Δ. "
        "**Δ pri vstupe** broker v riadku často neukáže — dopln ju z obchodu / journalu. "
        "Na systéme musí byť nainštalovaný balík **tesseract-ocr** (napr. `sudo apt install tesseract-ocr`)."
    )
    for _k, _d in [("pf_q_strike", 0.0), ("pf_q_iv", 0.0), ("pf_q_de", 0.0), ("pf_q_dc", 0.0)]:
        if _k not in st.session_state:
            st.session_state[_k] = _d
    if "pf_q_exp" not in st.session_state:
        st.session_state["pf_q_exp"] = date.today()
    if "pf_q_right" not in st.session_state:
        st.session_state["pf_q_right"] = "Call"

    # Po OCR musíme doplniť textové pole *pred* st.text_area — Streamlit nepovoľuje prepísať kľúč widgetu po jeho vytvorení.
    _bundle = st.session_state.pop("pf_q_apply_bundle", None)
    if _bundle:
        if _bundle.get("paste"):
            st.session_state["pf_q_paste"] = _bundle["paste"]
        if _bundle.get("last_ocr"):
            st.session_state["pf_q_last_ocr"] = _bundle["last_ocr"]
        if _bundle.get("parsed") is not None:
            _pf_quick_apply_parsed(_bundle["parsed"])
        if _bundle.get("notes"):
            st.session_state["pf_q_flash_info"] = _bundle["notes"]
    _flash = st.session_state.pop("pf_q_flash_info", None)
    if _flash:
        st.info(_flash)

    c_up, c_paste = st.columns((1, 1))
    with c_up:
        img_f = st.file_uploader("Screenshot PNG / JPG", type=["png", "jpg", "jpeg"], key="pf_q_upload")
    with c_paste:
        st.text_area(
            "Alebo sem prilep text / výstup z OCR",
            height=90,
            key="pf_q_paste",
            placeholder="GLW May08'26 170 CALL … 76.773% … 0.414 …",
        )

    b_ocr, b_txt = st.columns(2)
    with b_ocr:
        if st.button("Načítať z obrázka (OCR)", key="pf_q_btn_ocr", type="primary", use_container_width=True):
            if img_f is None:
                st.warning("Vyber súbor obrázka.")
            else:
                txt, err = ocr_image_to_text(img_f.getvalue())
                if err:
                    st.error(f"OCR: {err}")
                elif txt:
                    p = parse_ibkr_row_text(txt)
                    st.session_state["pf_q_apply_bundle"] = {
                        "paste": txt,
                        "last_ocr": txt,
                        "parsed": p,
                        "notes": p.notes or "",
                    }
                    st.rerun()
                else:
                    st.warning("Prázdny OCR výstup — skús ostrejší výrez riadka.")
    with b_txt:
        if st.button("Parsovať text z poľa", key="pf_q_btn_parse", use_container_width=True):
            p = parse_ibkr_row_text(st.session_state.get("pf_q_paste") or "")
            _pf_quick_apply_parsed(p)
            if p.notes:
                st.info(p.notes)

    if st.session_state.get("pf_q_last_ocr"):
        with st.expander("Surový text z OCR (ladenie)", expanded=False):
            st.code(st.session_state.get("pf_q_last_ocr", ""))
    if st.session_state.get("pf_q_last_ticker"):
        st.caption(f"Ticker z posledného parsovania: **{st.session_state['pf_q_last_ticker']}**")

    st.markdown("##### Údaje pre výpočet")
    r1, r2, r3 = st.columns(3)
    with r1:
        st.number_input("Strike", min_value=0.0, step=0.5, key="pf_q_strike")
    with r2:
        st.date_input("Expirácia", key="pf_q_exp")
    with r3:
        st.selectbox("Typ", ["Call", "Put"], key="pf_q_right")

    r4, r5, r6 = st.columns(3)
    with r4:
        st.number_input(
            "IV (percentá alebo zlomok)",
            min_value=0.0,
            step=1.0,
            format="%g",
            key="pf_q_iv",
            help="Napr. 76,77 alebo 0,7677 — rovnako ako v BS nad časopisom.",
        )
    with r5:
        st.number_input(
            "Δ vstup (abs., povinné)",
            min_value=0.0,
            step=0.01,
            format="%g",
            key="pf_q_de",
            help="Z obchodu / journalu — nie z jedného riadka IB.",
        )
    with r6:
        st.number_input(
            "Δ aktuálna (voliteľné)",
            min_value=0.0,
            step=0.001,
            format="%g",
            key="pf_q_dc",
            help="0 = nepočítať pomer; parser ju vie doplniť z textu.",
        )

    strike_q = float(st.session_state.get("pf_q_strike") or 0)
    exp_q = st.session_state.get("pf_q_exp")
    right_q = st.session_state.get("pf_q_right", "Call")
    rc_q = "C" if right_q == "Call" else "P"
    iv_bs_q = iv_display_to_bs_fraction(float(st.session_state.get("pf_q_iv") or 0))
    de_q = float(st.session_state.get("pf_q_de") or 0)
    dc_q = float(st.session_state.get("pf_q_dc") or 0)

    raw_dte = (exp_q - date.today()).days if exp_q else None

    spot_wq = spot_aq = None
    if iv_bs_q and strike_q > 0 and raw_dte is not None and raw_dte >= 0 and de_q > 1e-12:
        tw = _target_abs_delta_for_ratio(de_q, _SHORT_DELTA_WARN_RATIO)
        ta = _target_abs_delta_for_ratio(de_q, _SHORT_DELTA_ALERT_RATIO)
        if tw is not None:
            spot_wq = spot_for_abs_delta_bs(strike_q, int(raw_dte), iv_bs_q, rc_q, tw)
        if ta is not None:
            spot_aq = spot_for_abs_delta_bs(strike_q, int(raw_dte), iv_bs_q, rc_q, ta)

    mq1, mq2, mq3 = st.columns(3)
    mq1.metric(f"Podklad @ {_SHORT_DELTA_WARN_RATIO:g}× (BS)", _fmt_underlying_spot(spot_wq))
    mq2.metric(f"Podklad @ {_SHORT_DELTA_ALERT_RATIO:g}× (BS)", _fmt_underlying_spot(spot_aq))
    if dc_q > 1e-12 and de_q > 1e-12:
        mq3.metric("|Δ akt.| / |Δ vstup|", f"{abs(dc_q) / abs(de_q):.2f}×")
    else:
        mq3.metric("|Δ akt.| / |Δ vstup|", "—")

    if raw_dte == 0:
        st.caption(
            "Expirácia je **dnes** — v BS je na výpočet použité orientačne **aspoň 1 deň** do expirácie "
            "(pri skutočnom T≈0 by model delta nedefinoval rozumne)."
        )

    if not (iv_bs_q and strike_q > 0 and raw_dte is not None and raw_dte >= 0 and de_q > 1e-12):
        if raw_dte is not None and raw_dte < 0:
            st.warning("Expirácia už bola — výpočet podkladu nie je k dispozícii.")
        else:
            st.info(
                "Pre výstup doplníš **strike**, **expiráciu**, **IV** a najmä **Δ vstup**. "
                "Po nahratí obrázka alebo parsovaní textu skontroluj čísla — OCR vie pomýliť znaky."
            )


st.divider()


def _iv_raw_to_bs_fraction(raw: float | None) -> float | None:
    """Deleguje na ``core.greeks.iv_display_to_bs_fraction`` (jedna škála IV)."""
    return iv_display_to_bs_fraction(raw)


def _resolve_iv_bs_for_spot_row(row, ticker_upper: str) -> tuple[float | None, str]:
    """
    Vráti (iv_bs_fraction, krátky popis zdroja) pre výpočet spotu.
    Poradie: IV aktuálna → IV vstup → symbols.iv_pct.
    """
    for label, key in (
        ("aktuálna", "IV aktuálna"),
        ("vstup", "IV vstup"),
    ):
        iv_bs = iv_display_to_bs_fraction(_nan_to_none(row.get(key)))
        if iv_bs is not None:
            return iv_bs, f"journal ({label})"
    if ticker_upper:
        sym = db.get_symbol(ticker_upper)
        if sym is not None and sym.get("iv_pct") is not None:
            try:
                iv_bs = iv_display_to_bs_fraction(float(sym["iv_pct"]))
            except (TypeError, ValueError):
                iv_bs = None
            if iv_bs is not None:
                return iv_bs, "Symboly IV %"
    return None, ""


def _fmt_iv_bs_line(iv_bs: float | None, src: str) -> str:
    if iv_bs is None:
        return "—"
    pct = iv_bs * 100.0
    tail = f" · {src}" if src else ""
    return f"{pct:.1f}%{tail}"


def _spot_missing_reason_short(
    *,
    iv_bs: float | None,
    strike_v: float,
    dte_signed: int | None,
    rc: str | None,
    entry_delta_ok: bool,
) -> str:
    if not entry_delta_ok:
        return "Vyplň **Δ vstup**."
    if rc is None:
        return "Typ opcie (Call/Put)."
    if strike_v <= 0:
        return "Strike."
    if dte_signed is None:
        return "Expirácia / DTE."
    if dte_signed < 0:
        return "Kontrakt už expiroval — orientačný spot z BS nedáva zmysel."
    if iv_bs is None:
        return "IV — vyplň **IV aktuálna** alebo **IV vstup** (môžeš z IB ako **76,77** aj ako **0,7677**), prípadne **IV %** v **Symboly**."
    return ""


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


def _render_add_stk_leg_journal_panel(open_tr_legs: list[dict]) -> None:
    """
    Ručne pridaná akcia podkladu (STK) do denníka — rovnaký zámer ako expander na stránke Skupiny.
    """
    _pop = st.session_state.pop("pf_journal_add_stk_msg", None)
    if _pop:
        st.success(_pop)

    _grp_sel = _journal_group_select_options(open_tr_legs)
    with st.expander("Pridať akciu podkladu (STK) do denníka", expanded=False):
        st.caption(
            "**Delta hedge alebo držba akcií vedľa spreadu.** Riadok má v denníku **Typ STK**; po uložení ho uvidíš "
            "v záložke skupiny dolu a v **Skupiny → Priradiť**. Pri **Importe z IB** sa rovnaká STK pozícia zvyčajne "
            "zlúči s týmto záznamom (ticker · Long/Short · ks)."
        )
        with st.form("pf_journal_add_stk_form", clear_on_submit=True):
            st.selectbox(
                "Skupina (Group ID)",
                options=_grp_sel,
                key="pf_journal_stk_group",
                help='"Bez skupiny" = prázdny group_id; neskôr môžeš doplniť v tabuľke alebo cez Rýchle priradenie.',
            )
            st.text_input("Ticker podkladu", placeholder="napr. AAPL", key="pf_journal_stk_ticker")
            st.selectbox("Smer pozície", ["Long", "Short"], key="pf_journal_stk_leg")
            st.number_input("Počet akcií (ks)", min_value=1, value=100, step=1, key="pf_journal_stk_shares")
            st.number_input(
                "Priemerná cena vstupu (USD / ks)",
                min_value=0.0,
                value=0.0,
                step=0.01,
                format="%.4f",
                key="pf_journal_stk_entry",
            )
            st.text_input("Poznámka / stratégia", value="Stock / hedge", key="pf_journal_stk_strat")
            _sub = st.form_submit_button("Uložiť STK do denníka", type="primary")
        if _sub:
            _g_raw = _skupina_cell_norm(st.session_state.get("pf_journal_stk_group"))
            _gid: str | None = None if _g_raw == PF_GROUP_NONE else str(_g_raw).strip()
            _tn = str(st.session_state.get("pf_journal_stk_ticker") or "").strip().upper()
            _leg = str(st.session_state.get("pf_journal_stk_leg") or "Long")
            try:
                _sh = int(st.session_state.get("pf_journal_stk_shares") or 1)
            except (TypeError, ValueError):
                _sh = 1
            if _sh < 1:
                _sh = 1
            try:
                _ep = float(st.session_state.get("pf_journal_stk_entry") or 0.0)
            except (TypeError, ValueError):
                _ep = 0.0
            _strat = str(st.session_state.get("pf_journal_stk_strat") or "Stock / hedge").strip() or "Stock / hedge"
            if not _tn:
                st.warning("Zadaj ticker.")
            else:
                _tid = db.add_trade(
                    ticker=_tn,
                    strategy=_strat,
                    leg_type=_leg,
                    option_type="STK",
                    strike=0.0,
                    expiry="",
                    contracts=_sh,
                    entry_price=_ep,
                    entry_date=date.today().isoformat(),
                    group_id=_gid,
                    delta_at_entry=1.0,
                )
                _g_disp = PF_GROUP_NONE if not (_gid or "").strip() else _gid.strip()
                st.session_state["pf_journal_add_stk_msg"] = (
                    f"Pridaná akcia **{_tn}** (ID **{_tid}**), skupina: **{_g_disp}**."
                )
                st.rerun()


def _journal_group_tab_label(gname: str, *, max_len: int = 28) -> str:
    """Krátky názov skupiny v prepínači časopisu (dlhé mená sa skracujú kvôli prehľadnosti)."""
    if gname == PF_GROUP_NONE:
        return "Bez skupiny"
    s = (gname or "").strip() or "?"
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


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


def _journal_leg_sign_mult(trade: dict) -> tuple[float, float]:
    """Znamienko nohy (Long +1, Short −1) a násobiteľ (×100 opcia, ×1 STK)."""
    mult = float(trade.get("contracts") or 1) * journal_contract_shares_multiplier(trade)
    sign = -1.0 if str(trade.get("leg_type") or "").strip() == "Short" else 1.0
    return sign, mult


def _journal_clean_delta_share_equiv(delta_coeff: float | None, sign: float, mult: float):
    """Ekvivalent akcií podkladu: Δ(0–1) × kontrakty × 100 × znamienko — ako ``build_group_data``."""
    d = _nan_to_none(delta_coeff)
    if d is None:
        return pd.NA
    return float(d) * mult * sign


def _journal_float_for_sum(v) -> float | None:
    """Skalár z bunky editora / pandas — None ak nie je číslo (vrátane NA)."""
    if v is None or v is pd.NA:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return x


def _journal_symbol_spot_usd(ticker: str) -> float | None:
    """Spot podkladu z tabuľky Symboly (pre Δ $)."""
    tk = (ticker or "").strip().upper()
    if not tk:
        return None
    sym = db.get_symbol(tk)
    if not sym:
        return None
    sp = sym.get("spot")
    try:
        x = float(sp) if sp is not None else None
    except (TypeError, ValueError):
        return None
    if x is None or x <= 0 or (isinstance(x, float) and math.isnan(x)):
        return None
    return x


def _journal_group_delta_dollars_usd(edited: pd.DataFrame) -> tuple[float, int]:
    """
    Záložný súčet dollar-delta: Δ aktuálna z journalu × spot zo **Symboly** × kontrakty × 100 × znamienko.
    Použije sa len keď z IB nevieme dopočítať (bez Grékov / spotu v cache).
    """
    total = 0.0
    n = 0
    for _, row in edited.iterrows():
        dc = _journal_float_for_sum(row.get("Δ aktuálna"))
        if dc is None:
            continue
        spot = _journal_symbol_spot_usd(str(row.get("Ticker") or ""))
        if spot is None:
            continue
        sgn, mlt = _journal_sign_mult_from_table_row(row)
        total += float(dc) * float(spot) * float(mlt) * float(sgn)
        n += 1
    return total, n


def _ib_underlying_spot_from_cache(ib_positions: list[dict], ticker: str) -> float | None:
    """``market_price`` akcie (STK) pre ticker podkladu — z rovnakého IB fetchu ako opcie."""
    tk = (ticker or "").strip().upper()
    if not tk or not ib_positions:
        return None
    for p in ib_positions:
        if p.get("sec_type") != "STK":
            continue
        if str(p.get("ticker") or "").strip().upper() != tk:
            continue
        mp = p.get("market_price")
        try:
            x = float(mp) if mp is not None else None
        except (TypeError, ValueError):
            continue
        if x is not None and x > 0 and not math.isnan(x):
            return x
    return None


_JOURNAL_UND_SPOT_TTL_S = 45.0


def _journal_underlying_spot_ib_live(ticker: str) -> tuple[float | None, str | None]:
    """
    Aktuálna cena podkladu z IB (snapshot / história — rovnaká vetva ako v TWS pri obnove).
    Krátka cache v session, aby sa pri každom rerune nevolalo IB opakovane.
    """
    tk = (ticker or "").strip().upper()
    if not tk or not ibkr.is_connected():
        return None, None
    cache: dict = st.session_state.setdefault("journal_und_spot_cache", {})
    ent = cache.get(tk)
    now_m = time.monotonic()
    if ent and isinstance(ent, (list, tuple)) and len(ent) >= 3:
        ts0, px0, lbl0 = ent[0], ent[1], ent[2]
        if (now_m - ts0) < _JOURNAL_UND_SPOT_TTL_S and px0 is not None:
            try:
                pv = float(px0)
            except (TypeError, ValueError):
                pv = 0.0
            if pv > 0 and not math.isnan(pv):
                return pv, str(lbl0) if lbl0 else "IB live"

    try:
        r = ibkr.fetch_underlying(tk, timeout=6.0)
    except Exception:
        r = {"price": None, "error": "exc"}
    px = r.get("price")
    try:
        pv = float(px) if px is not None else None
    except (TypeError, ValueError):
        pv = None
    if pv is None or pv <= 0 or (isinstance(pv, float) and math.isnan(pv)):
        return None, None
    src = str(r.get("source") or "").lower()
    lbl = "IB live (hist)" if "hist" in src else "IB live"
    cache[tk] = (now_m, pv, lbl)
    return pv, lbl


def _journal_fmt_spot_cell_str(v: object) -> str:
    """Spot do bunky tabuľky — text namiesto NumberColumn kvôli zarovnaniu (st.dataframe)."""
    try:
        return f"{float(v):.2f}"
    except (TypeError, ValueError):
        return ""


def _journal_fmt_dspot_cell_str(v: object) -> str:
    """Rozdiel scenárového spotu oproti referenčnému (USD), so znamienkom."""
    if v is None:
        return ""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return ""
    if math.isnan(fv):
        return ""
    if abs(fv) < 1e-6:
        return "0.00"
    sign = "+" if fv > 0 else ""
    return f"{sign}{fv:.2f}"


def _journal_fmt_pl_usd_cell_str(v: object) -> str:
    """P&L v USD ako celé číslo (TWS štýl); prázdne ak chýba hodnota (napr. long v assignment)."""
    if v is None:
        return ""
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return ""
    if math.isnan(fv):
        return ""
    return str(int(round(fv)))


def _journal_resolve_spot_for_pl(ticker: str, ib_positions: list[dict]) -> tuple[float | None, str | None]:
    """Spot pre P&L grafy: IB live → STK v cache → Symboly."""
    tk = (ticker or "").strip().upper()
    if not tk:
        return None, None
    liv, lbl = _journal_underlying_spot_ib_live(tk)
    if liv is not None and liv > 0:
        return float(liv), lbl or "IB live"
    ibs = _ib_underlying_spot_from_cache(ib_positions, tk)
    if ibs is not None and ibs > 0:
        return float(ibs), "IB STK (portfólio)"
    s = _journal_symbol_spot_usd(tk)
    return s, "Symboly" if s is not None else None


def _journal_ib_opt_last_per_share(p: dict) -> float | None:
    """Aktuálna cena opcie $/akcia z IB (``market_price`` po enrichi — preferuje Last)."""
    if p.get("sec_type") not in ("OPT", "FOP"):
        return None
    px = p.get("market_price")
    if px is None:
        return None
    try:
        v = float(px)
    except (TypeError, ValueError):
        return None
    if v <= 0 or math.isnan(v):
        return None
    return v


def _journal_long_option_entry_per_share(trade: dict, p: dict | None) -> float | None:
    """Kúpna prémia $/akcia: journal ``entry_price``, inak ``avg_cost``/100 z IB (ako sync z TWS)."""
    try:
        ep = float(trade.get("entry_price") or 0)
    except (TypeError, ValueError):
        ep = 0.0
    a = abs(ep)
    if a > 1e-12:
        return a
    if not p:
        return None
    try:
        ac = float(p.get("avg_cost") or 0)
    except (TypeError, ValueError):
        return None
    if ac <= 0 or math.isnan(ac):
        return None
    return ac / 100.0


def _journal_long_leg_ib_last_vs_entry_pl_usd(trade: dict, ib_positions: list[dict]) -> float | None:
    """Long: (last cena opcie − kúpna $/akcia) × kontrakty × 100 — z IB + journal."""
    if str(trade.get("leg_type") or "").strip().capitalize() != "Long":
        return None
    p = find_ibkr_option_for_trade(trade, ib_positions)
    if not p:
        return None
    last = _journal_ib_opt_last_per_share(p)
    if last is None:
        return None
    entry = _journal_long_option_entry_per_share(trade, p)
    if entry is None:
        return None
    try:
        q = abs(float(trade.get("contracts") or 1))
    except (TypeError, ValueError):
        return None
    if q <= 0:
        return None
    return (last - entry) * q * 100.0


def _journal_long_leg_bs_mark_value_usd(trade: dict, spot_anchor: float, r: float = 0.045) -> float | None:
    """
    Teoretická hodnota Long nohy (USD): BS cena opcie pri ``spot_anchor`` × kontrakty × 100.
    IV z journalu rovnako ako pri ostatných BS grafoch v časopise.
    """
    if str(trade.get("leg_type") or "").strip().capitalize() != "Long":
        return None
    try:
        sf = float(spot_anchor)
    except (TypeError, ValueError):
        return None
    if sf <= 0 or math.isnan(sf):
        return None
    dte = calc_dte(trade.get("expiry"))
    if dte is None:
        return None
    try:
        K = float(trade.get("strike") or 0)
        c_abs = abs(float(trade.get("contracts") or 1))
    except (TypeError, ValueError):
        return None
    if K <= 0 or c_abs <= 0:
        return None
    iv = _iv_fraction_for_leg(trade)
    Tn = max(1.0 / 365.0, float(dte) / 365.0)
    right = _right_bs(trade.get("option_type"))
    if Tn <= 0.0 or iv <= 0.0:
        px = bs_price(sf, K, 0.0, max(iv, 1e-6), right, r)
    else:
        px = bs_price(sf, K, Tn, iv, right, r)
    return float(px) * c_abs * 100.0


def _journal_long_leg_bs_pl_vs_entry_usd(
    trade: dict, spot_anchor: float, r: float = 0.045
) -> float | None:
    """Long bez IB: (BS teoret. cena $/akcia pri ``spot_anchor`` − kúpna z žurnálu) × ks × 100."""
    tot = _journal_long_leg_bs_mark_value_usd(trade, float(spot_anchor), r=r)
    if tot is None:
        return None
    entry = _journal_long_option_entry_per_share(trade, None)
    if entry is None:
        return None
    try:
        q = abs(float(trade.get("contracts") or 1))
    except (TypeError, ValueError):
        return None
    if q <= 0:
        return None
    return tot - entry * q * 100.0


def _journal_long_leg_assignment_column_usd(
    trade: dict,
    ib_positions: list[dict],
    spot_anchor: float | None,
    r: float = 0.045,
) -> tuple[float | None, str]:
    """
    PL pre Long: **(last − kúpna prémia $/akcia) × kontrakty × 100** (IB + žurnál),
    inak rovnaký vzorec s BS cenou pri ``spot_anchor`` ak chýba IB.
    """
    pl_ib = _journal_long_leg_ib_last_vs_entry_pl_usd(trade, ib_positions)
    if pl_ib is not None:
        return float(pl_ib), "ib"
    if spot_anchor is None:
        return None, ""
    pl_bs = _journal_long_leg_bs_pl_vs_entry_usd(trade, float(spot_anchor), r=r)
    if pl_bs is not None:
        return float(pl_bs), "bs"
    return None, ""


def _assignment_short_pl_usd(trade: dict, spot: float | None) -> float | None:
    """
    Orientačný P/L pri **uplatnení (assignment)** short opcie v USD (bez poplatkov).

    Na akciu: ``|entry_price| + strike − spot``. Na pozíciu: ``(...) × kontrakty × 100``.
    Zjednodušený scenár dodania podkladu pri strike (short call aj short put).
    """
    if str(trade.get("leg_type") or "").strip() != "Short":
        return None
    if spot is None:
        return None
    try:
        sf = float(spot)
    except (TypeError, ValueError):
        return None
    if sf <= 0 or math.isnan(sf):
        return None
    try:
        K = float(trade.get("strike") or 0)
        q = float(trade.get("contracts") or 1)
        ep = float(trade.get("entry_price") or 0)
    except (TypeError, ValueError):
        return None
    if K <= 0 or q <= 0:
        return None
    prem = abs(ep)
    per_share = prem + K - sf
    return per_share * q * 100.0


def _journal_group_delta_dollars_ib(
    edited: pd.DataFrame,
    orig_by_id: dict[int, dict],
    ib_positions: list[dict],
) -> tuple[float, int]:
    """
    Dollar-delta z IB (súlad s TWS): ekvivalent akcií z modelovej Δ × spot podkladu z IB STK.
    Ekvivalent akcií = ``ib_opt_greeks_scaled_for_journal`` (δ × kontr. × 100 × znamienko).
    """
    total = 0.0
    n = 0
    for _, row in edited.iterrows():
        tid = int(row["ID"])
        orig = orig_by_id.get(tid)
        if not orig:
            continue
        p = find_ibkr_option_for_trade(orig, ib_positions)
        if not p or p.get("sec_type") not in ("OPT", "FOP"):
            continue
        deff = ib_opt_greeks_scaled_for_journal(p).get("delta")
        if deff is None:
            continue
        tk = str(orig.get("ticker") or row.get("Ticker") or "")
        spot = _ib_underlying_spot_from_cache(ib_positions, tk)
        if spot is None or spot <= 0:
            spot = _journal_symbol_spot_usd(tk)
        if spot is None or spot <= 0:
            continue
        try:
            total += float(deff) * float(spot)
            n += 1
        except (TypeError, ValueError):
            pass
    return total, n


def _journal_ib_opt_market_value_line(p: dict) -> float | None:
    """
    Trhová hodnota opcie ako pri ``_apply_upnl_from_price``: cena × kontrakty × 100 × znamienko.
    Inak API ``market_value`` (napr. keď ešte nie je ``market_price``).
    """
    if p.get("sec_type") not in ("OPT", "FOP"):
        return None
    px = p.get("market_price")
    if px is not None:
        try:
            pxv = float(px)
            if pxv > 0 and not math.isnan(pxv):
                c = float(p.get("contracts") or 0)
                if c > 0:
                    sign = -1.0 if str(p.get("leg_type") or "") == "Short" else 1.0
                    return round(pxv * c * 100.0 * sign, 2)
        except (TypeError, ValueError):
            pass
    mv = p.get("market_value")
    if mv is None:
        return None
    try:
        return float(mv)
    except (TypeError, ValueError):
        return None


def _journal_ib_opt_cost_basis_line(p: dict) -> float | None:
    """
    Nákladová báza v zmysle TWS **net** spreadu: IB ``averageCost`` so znamienkom.
    Záporné hodnoty z IB (kredit shortu) nechávame; ak je short **kladný** kredit, odpočítame ho.
    """
    if p.get("sec_type") not in ("OPT", "FOP"):
        return None
    ac = p.get("avg_cost")
    if ac is None:
        return None
    try:
        acv = float(ac)
    except (TypeError, ValueError):
        return None
    if math.isnan(acv):
        return None
    if acv < 0:
        return acv
    if str(p.get("leg_type") or "").strip() == "Short":
        return -acv
    return acv


def _journal_group_ib_market_value_and_cost_basis(
    edited: pd.DataFrame,
    orig_by_id: dict[int, dict],
    ib_positions: list[dict],
) -> tuple[float, float, int]:
    """
    Súčet trhovej hodnoty a nákladovej základne z IB pre nohy so zhodou s cache.
    MV = z ceny kontraktu (ako TWS po dopočítaní), báza = súčet so znamienkom (net spread).
    """
    mv_sum = cb_sum = 0.0
    n_match = 0
    for _, row in edited.iterrows():
        tid = int(row["ID"])
        orig = orig_by_id.get(tid)
        if not orig:
            continue
        p = find_ibkr_option_for_trade(orig, ib_positions)
        if not p:
            continue
        n_match += 1
        mv_line = _journal_ib_opt_market_value_line(p)
        if mv_line is not None:
            mv_sum += mv_line
        cb_line = _journal_ib_opt_cost_basis_line(p)
        if cb_line is not None:
            cb_sum += cb_line
    return mv_sum, cb_sum, n_match


def _journal_sign_mult_from_table_row(row) -> tuple[float, float]:
    """Kontrakty a znamienko z riadku časopisnej tabuľky (stĺpce Kontr., Noha, Typ)."""
    t_like = {
        "contracts": row.get("Kontr."),
        "leg_type": row.get("Noha"),
        "option_type": row.get("Typ"),
    }
    return _journal_leg_sign_mult(t_like)


def _journal_edited_to_mentor_legs(edited: pd.DataFrame) -> list[dict]:
    """Riadky z ``data_editor`` → dict pre ``spread_mentor`` (DTE + čisté Gréky)."""
    out: list[dict] = []
    for _, row in edited.iterrows():
        exp = row.get("Expirácia")
        if exp is None or (isinstance(exp, float) and pd.isna(exp)):
            exp_s = ""
        else:
            exp_s = str(exp).strip()
        dc = _journal_float_for_sum(row.get("Δ aktuálna"))
        de = _journal_float_for_sum(row.get("Δ vstup"))
        thc = _journal_float_for_sum(row.get("Θ aktuálna ($/deň)"))
        the = _journal_float_for_sum(row.get("Θ vstup ($/deň)"))
        vc = _journal_float_for_sum(row.get("Vega aktuálna"))
        ve = _journal_float_for_sum(row.get("Vega vstup"))
        try:
            k = int(row.get("Kontr.") or 1)
        except (TypeError, ValueError):
            k = 1
        out.append(
            {
                "leg_type": str(row.get("Noha") or ""),
                "expiry": exp_s,
                "strike": float(row.get("Strike") or 0),
                "option_type": str(row.get("Typ") or ""),
                "contracts": k,
                "delta_current": dc if dc is not None else de,
                "delta_at_entry": de,
                "theta_current": thc if thc is not None else the,
                "theta_at_entry": the,
                "vega_current": vc if vc is not None else ve,
                "vega_at_entry": ve,
            }
        )
    return out


def _journal_leg_theta_vega_usd(trade: dict, greeks: dict) -> tuple[float | None, float | None]:
    """Θ a Vega v USD za celú nohu — rovnako ako pri výpočtoch v ``portfolio_data``."""
    sign, mult = _journal_leg_sign_mult(trade)
    th = greeks.get("theta")
    ve = greeks.get("vega")
    try:
        th_usd = float(th) * mult * sign if th is not None else None
    except (TypeError, ValueError):
        th_usd = None
    try:
        ve_usd = float(ve) * mult * sign if ve is not None else None
    except (TypeError, ValueError):
        ve_usd = None
    return th_usd, ve_usd


def _journal_write_live_greeks_for_trade(trade: dict, positions: list[dict]) -> bool:
    """Zápis aktuálnych Grékov z IB/TWS cache do DB pre jednu nohu. Vráti True ak sa podarilo."""
    try:
        tid = int(trade["id"])
    except (KeyError, TypeError, ValueError):
        return False
    db_trade = db.get_trade_by_id(tid)
    if not db_trade or str(db_trade.get("status") or "Open").strip().lower() != "open":
        return False
    trade = db_trade
    g, src = greek_for_trade(trade, positions, {}, {})
    if src not in ("live", "bs") or not g:
        return False
    if not any(
        g.get(k) not in (None, 0, 0.0) for k in ("delta", "theta", "vega", "iv")
    ):
        return False
    th_usd, ve_usd = _journal_leg_theta_vega_usd(trade, g)
    try:
        dc = float(g["delta"]) if g.get("delta") is not None else None
    except (TypeError, ValueError):
        dc = None
    try:
        iv_c = float(g["iv"]) if g.get("iv") is not None else None
    except (TypeError, ValueError):
        iv_c = None
    iv_entry = trade.get("iv_at_entry")
    delta_entry = trade.get("delta_at_entry")
    theta_entry = trade.get("theta_at_entry")
    vega_entry = trade.get("vega_at_entry")
    if iv_entry is None and iv_c is not None:
        iv_entry = iv_c
    if delta_entry is None and dc is not None:
        delta_entry = dc
    if theta_entry is None and th_usd is not None:
        theta_entry = th_usd
    if vega_entry is None and ve_usd is not None:
        vega_entry = ve_usd

    _old_dc = trade.get("delta_current")
    _old_tc = trade.get("theta_current")
    _old_vc = trade.get("vega_current")
    _old_ivc = trade.get("iv_current")

    def _changed(a, b, tol: float = 1e-9) -> bool:
        if a is None and b is None:
            return False
        if a is None or b is None:
            return True
        try:
            return abs(float(a) - float(b)) > tol
        except (TypeError, ValueError):
            return str(a) != str(b)

    db.set_trade_portfolio_greeks(
        tid,
        iv_entry,
        delta_entry,
        theta_entry,
        dc,
        vega_at_entry=vega_entry,
        vega_current=ve_usd,
        iv_current=iv_c,
        theta_current=th_usd,
    )
    if any(v is not None for v in (dc, th_usd, ve_usd, iv_c)) and any(
        _changed(old, new)
        for old, new in (
            (_old_dc, dc),
            (_old_tc, th_usd),
            (_old_vc, ve_usd),
            (_old_ivc, iv_c),
        )
    ):
        db.insert_trade_greek_snapshot(
            tid,
            delta=dc,
            theta_usd=th_usd,
            vega=ve_usd,
            iv=iv_c,
            source="tws_sync",
        )
    return True


def _journal_refresh_group_from_tws(legs_edit: list[dict]) -> tuple[bool, str]:
    """
    Fetch pozícií s Grékmi z TWS, uloženie live cache a zápis aktuálnych Grékov do DB pre zadané nohy.
    """
    if not ibkr.is_connected():
        return False, "IBKR nie je pripojený — najprv pripoj TWS (sidebar alebo Dashboard)."
    res = ibkr.fetch_positions(with_greeks=True, use_historical_last=False)
    if res.get("error"):
        return False, str(res["error"])
    poss = list(res.get("positions") or [])
    ibkr.set_scoped_session_value("live_positions", poss)
    st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
    open_by_id = {int(t["id"]): t for t in db.get_open_trades()}
    n_ok = 0
    for tr in legs_edit:
        if str(tr.get("status") or "Open").strip().lower() != "open":
            continue
        try:
            tid = int(tr["id"])
        except (KeyError, TypeError, ValueError):
            continue
        fresh = open_by_id.get(tid)
        if not fresh:
            continue
        if _journal_write_live_greeks_for_trade(fresh, poss):
            n_ok += 1
    n_legs = len(legs_edit)
    return True, f"TWS: cache obnovená; do DB zapísané Gréky pre **{n_ok}** / **{n_legs}** nôh tejto skupiny."


with st.expander("Synchronizácia s TWS / databázou (rovnako ako Dashboard)", expanded=False):
    _ib_ok = ibkr.is_connected()
    if not _ib_ok:
        st.warning("IBKR nie je pripojený — v sidebari sa najprv pripoj k TWS.")
    else:
        st.caption(
            "**1)** Zosúladí zoznam nôh v DB s portfóliom (kontrakty, vstupná cena). "
            "**2)** Doplní **aktuálnu** Δ, IV, Θ, Vega z cache TWS (BS z trhových cenách, ako pri fetch s Grékmi)."
        )
        c_s1, c_s2 = st.columns(2)
        with c_s1:
            if st.button(
                "Importovať pozície z IBKR → DB",
                type="primary",
                use_container_width=True,
                key="pf_journal_sync_positions",
                help="Rovnaké ako „Importuj pozície z IBKR“ na Dashboarde (vrátane uzavretia chýbajúcich v IB).",
            ):
                with st.spinner("Načítavam portfólio z IBKR…"):
                    res = ibkr.fetch_positions(use_historical_last=False)
                if res.get("error"):
                    st.session_state["pf_journal_sync_msg"] = ("error", str(res["error"]))
                else:
                    sync = ibkr.sync_positions_to_db(res["positions"], db, close_missing=True)
                    ibkr.set_scoped_session_value("live_positions", res["positions"])
                    st.session_state["last_sync"] = datetime.now().strftime("%H:%M:%S")
                    st.session_state["pf_journal_sync_msg"] = (
                        "ok",
                        f"Pridané **{sync['added']}** · aktualizované **{sync.get('updated', 0)}** · "
                        f"uzavreté **{sync.get('closed', 0)}** (v IB už nie sú).",
                    )
                st.rerun()
        with c_s2:
            if st.button(
                "Doplniť aktuálne Gréky z TWS → journal (DB)",
                type="secondary",
                use_container_width=True,
                key="pf_journal_sync_greeks",
                help="Pre každú otvorenú nohu nájde zhodu v portfóliu a zapíše Δ, IV, Θ, Vega do DB; ak vstup chýba, doplní sa z aktuálnej hodnoty.",
            ):
                with st.spinner("Sťahujem pozície s Grékmi (môže trvať)…"):
                    res = ibkr.fetch_positions(with_greeks=True, use_historical_last=False)
                if res.get("error"):
                    st.session_state["pf_journal_sync_msg"] = ("error", str(res["error"]))
                else:
                    poss = res.get("positions", [])
                    ibkr.set_scoped_session_value("live_positions", poss)
                    n_ok = 0
                    nomatch: list[str] = []
                    for tr in db.get_open_trades():
                        if str(tr.get("status") or "Open").strip().lower() != "open":
                            continue
                        if _journal_write_live_greeks_for_trade(tr, poss):
                            n_ok += 1
                        else:
                            tk = str(tr.get("ticker") or "")
                            nomatch.append(
                                f"{tk} {tr.get('strike')} {tr.get('expiry')} {tr.get('option_type')}"
                            )
                    extra = f" Bez zhody ({len(nomatch)}): {', '.join(nomatch[:6])}" if nomatch else ""
                    if len(nomatch) > 6:
                        extra += "…"
                    st.session_state["pf_journal_sync_msg"] = (
                        "ok",
                        f"Aktualizovaných nôh v DB: **{n_ok}**.{extra}",
                    )
                st.rerun()
        _msg = st.session_state.pop("pf_journal_sync_msg", None)
        if _msg:
            kind, text = _msg
            if kind == "error":
                st.error(text)
            else:
                st.success(text)
        _ls = st.session_state.get("last_sync")
        if _ls:
            st.caption(f"Posledná globálna sync (sidebar): **{_ls}**")


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
        "Σ |vstup| — USD (opcie ×100, STK nie)",
        f"${sum(notionals):,.0f}",
        help="Opčné nohy: |entry $/akcia| × kontrakty × 100. STK: |$/akcia| × počet ks (bez ×100).",
    )
else:
    m3.metric("Σ |vstup| — USD (opcie ×100, STK nie)", "—")

st.subheader("Otvorené pozície")

st.divider()
_render_delta_hedge_panel()
st.divider()
_render_add_stk_leg_journal_panel(open_trades)
st.divider()

if not open_trades:
    st.warning(
        "V denníku nemáš žiadne otvorené nohy (*Open*), preto nie je čo dopĺňať. "
        "Ak čakáš na import, choď na Dashboard a načítaj pozície z IBKR."
    )

if not open_trades:
    st.info(
        "Pre aktívny filter nemáš žiadne otvorené nohy (*Open*), alebo v denníku ešte nie sú záznamy. "
        "Nohy pridáš importom z IBKR na Dashboarde alebo priamo v denníku."
    )
else:
    st.caption(
        "**Návod:** Táto tabuľka je na ručný zápis. Doplň **Δ vstup / Δ aktuálna** a **Θ vstup ($/deň) / Θ aktuálna ($/deň)**, prípadne aj ďalšie hodnoty, a stlač **Uložiť journal**. "
        "Ak je **vstup** ešte prázdny, ale vyplníš **aktuálnu** hodnotu, uloží sa aj ako vstup. "
        "Pod editorom sú **súčty za skupinu**; podrobnosti metrík sú v expanderi **Vysvetlivky k metrikám** (predvolene zbalený). "
        "Zápis sa uloží do DB a zostane aj na ďalší deň. "
        "**Skupiny:** na presun viacerých nôh naraz použi expander **Rýchle priradenie skupiny** nižšie. "
        "Inak zmeň stĺpec **Skupina** v tabuľke a v tej istej skupine stlač **Uložiť journal** — vyber skupinu v prepínači **Skupina** vyššie."
    )
    _fb = st.session_state.pop("pf_journal_tws_refresh_msg", None)
    if _fb:
        _k, _txt = _fb
        if _k == "ok":
            st.success(_txt)
        else:
            st.error(_txt)
    with st.expander("Vysvetlivky k metrikám (súčty pod tabuľkou skupiny)", expanded=False):
        st.markdown(_JOURNAL_METRICS_HELP_MD)

    with st.expander("Rýchle priradenie skupiny — viac nôh naraz", expanded=False):
        st.markdown(
            "**Problém:** stĺpec *Skupina* je v širokej tabuľke ďaleko od kontraktu a pri presune nohy medzi skupinami "
            "musíš potom hľadať správny prepínač **Skupina** vyššie.\n\n"
            "**Tu:** vyber **cieľovú skupinu**, označ **nohy** (multiselect), **Priradiť** — uloží sa len `group_id` v DB "
            "(Gréky/IV sa nemenia). Ako **vyradiť zo skupiny**: vyber cieľ **„bez skupiny“** a **Priradiť**, alebo použij expander **Vyradiť nohy zo skupiny** pod nadpisom aktuálnej skupiny. "
            "Po presune môžeš **obnoviť stránku** alebo prepnúť **Skupina** vyššie, ak nohy „preskočili“ medzi skupinami."
        )
        _bo1, _bo2 = st.columns([1, 1])
        with _bo1:
            _bulk_target = st.selectbox(
                "Cieľová skupina",
                options=_grp_opts,
                index=0,
                key="pf_journal_bulk_group_target",
                help='Rovnaké mená ako v záložke **Skupiny**. „Bez skupiny“ = vyprázdni group_id.',
            )
        with _bo2:
            _bulk_filt = st.text_input(
                "Filter ticker (voliteľné)",
                value="",
                key="pf_journal_bulk_ticker_filt",
                placeholder="napr. MSFT",
            ).strip().upper()
        _legs_bulk = list(open_trades)
        if _bulk_filt:
            _legs_bulk = [t for t in _legs_bulk if _bulk_filt in str(t.get("ticker") or "").upper()]
        _bulk_labels: list[str] = []
        _bulk_tid: dict[str, int] = {}
        for t in sorted(_legs_bulk, key=lambda x: (str(x.get("ticker") or ""), int(x.get("id") or 0))):
            tid = int(t["id"])
            _gcur = (t.get("group_id") or "").strip() or "—"
            _lbl = (
                f"[{tid}] {t.get('ticker') or ''} · {t.get('leg_type') or ''} {t.get('option_type') or ''} "
                f"K{t.get('strike') or ''} {t.get('expiry') or ''} ×{int(t.get('contracts') or 1)} "
                f"— teraz: {_gcur}"
            )
            _bulk_labels.append(_lbl)
            _bulk_tid[_lbl] = tid
        st.multiselect(
            "Nohy na priradenie",
            options=_bulk_labels,
            default=[],
            key="pf_journal_bulk_pick",
            help="Vyber všetky nohy, ktoré majú ísť do cieľovej skupiny vyššie.",
        )
        if st.button(
            "Priradiť vybrané nohy do cieľovej skupiny",
            key="pf_journal_bulk_apply",
            type="primary",
        ):
            _picked = list(st.session_state.get("pf_journal_bulk_pick") or [])
            _sk_norm = _skupina_cell_norm(_bulk_target)
            _new_gid: str | None = None if _sk_norm == PF_GROUP_NONE else _sk_norm
            n_up = 0
            for lab in _picked:
                tid = _bulk_tid.get(str(lab))
                if tid is None:
                    continue
                db.update_trade(tid, group_id="" if not _new_gid else str(_new_gid))
                n_up += 1
            if n_up:
                st.success(f"Priradených nôh: **{n_up}** (cieľová skupina: **{_sk_norm}**).")
                st.session_state["pf_journal_bulk_pick"] = []
                st.rerun()
            else:
                st.warning("Vyber aspoň jednu nohu v zozname vyššie.")

    _pf_live_pos: list[dict] = list(ibkr.get_scoped_session_value("live_positions", []) or [])
    _GR_SEL_KEY = "pf_journal_selected_group"
    if _GR_SEL_KEY not in st.session_state or st.session_state[_GR_SEL_KEY] not in _sort_keys:
        st.session_state[_GR_SEL_KEY] = _sort_keys[0]
    st.selectbox(
        "Skupina",
        options=_sort_keys,
        format_func=_journal_group_tab_label,
        key=_GR_SEL_KEY,
        help="Vyber skupinu na úpravu časopisu. Výber ostane aj po **Obnoviť údaje z TWS** alebo uložení.",
        label_visibility="visible",
    )
    gname = str(st.session_state[_GR_SEL_KEY])

    legs = by_group[gname]
    meta = groups_meta.get(gname) if gname != PF_GROUP_NONE else None
    _gkey = hashlib.sha256(gname.encode("utf-8")).hexdigest()[:16]
    legs_edit = sorted(legs, key=lambda x: (str(x.get("ticker") or ""), int(x.get("id") or 0)))
    if not legs_edit:
        st.caption(
            "Táto skupina nemá pri aktívnom filtri žiadne otvorené nohy (stav Open alebo filter **Symboly**)."
        )
    else:
        with st.container():
            _gh1, _gh2 = st.columns([4, 1])
            with _gh1:
                st.markdown(f"#### {gname}")
            with _gh2:
                if st.button(
                    "Obnoviť údaje z TWS",
                    key=f"pf_twsg_{_gkey}",
                    help="Načíta portfólio s Grékmi, obnoví cache pre metriky a zapíše Δ/Θ/IV/Vega do DB pre nohy tejto skupiny.",
                    use_container_width=True,
                ):
                    with st.spinner("Sťahujem z TWS…"):
                        _ok_t, _msg_t = _journal_refresh_group_from_tws(legs_edit)
                    st.session_state["pf_journal_tws_refresh_msg"] = (
                        ("ok", _msg_t) if _ok_t else ("err", _msg_t)
                    )
                    st.rerun()
            if meta:
                _tk = meta.get("ticker") or ""
                _st = meta.get("strategy") or ""
                if _tk or _st:
                    st.caption(f"Skupina v DB: **{_tk}** · {_st}")
            if gname != PF_GROUP_NONE:
                with st.expander("Vyradiť nohy zo skupiny", expanded=False):
                    st.caption(
                        "Vyber nohy v tejto skupine a stlač tlačidlo — **Group ID** sa vymaže (noha skončí v „bez skupiny“). "
                        "Údaje Grékov / IV v DB sa nemenia."
                    )
                    _rm_j_map: dict[str, int] = {}
                    _rm_j_labels: list[str] = []
                    for t in legs_edit:
                        tid = int(t["id"])
                        _gcur = (t.get("group_id") or "").strip() or "—"
                        _lbl_j = (
                            f"[{tid}] {t.get('ticker') or ''} · {t.get('leg_type') or ''} {t.get('option_type') or ''} "
                            f"K{t.get('strike') or ''} {t.get('expiry') or ''} ×{int(t.get('contracts') or 1)} "
                            f"— teraz: {_gcur}"
                        )
                        _rm_j_labels.append(_lbl_j)
                        _rm_j_map[_lbl_j] = tid
                    st.multiselect(
                        "Nohy na vyradenie",
                        options=_rm_j_labels,
                        key=f"pf_journal_rm_pick_{_gkey}",
                    )
                    if st.button(
                        "Vyradiť vybrané zo skupiny",
                        key=f"pf_journal_rm_btn_{_gkey}",
                        type="secondary",
                    ):
                        _pj_rm = list(st.session_state.get(f"pf_journal_rm_pick_{_gkey}") or [])
                        _n_rm_j = 0
                        for _lab_j in _pj_rm:
                            _tid_j = _rm_j_map.get(str(_lab_j))
                            if _tid_j is None:
                                continue
                            db.update_trade(int(_tid_j), group_id="")
                            _n_rm_j += 1
                        if _n_rm_j:
                            st.success(f"Vyradených nôh: **{_n_rm_j}** (bez skupiny).")
                            st.session_state[f"pf_journal_rm_pick_{_gkey}"] = []
                            st.rerun()
                        else:
                            st.warning("Vyber aspoň jednu nohu v zozname vyššie.")
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
                    "Δ vstup": pd.NA if dlt_e is None else float(dlt_e),
                    "Δ aktuálna": pd.NA if dlt_c is None else float(dlt_c),
                    "Θ vstup ($/deň)": pd.NA if th_e is None else float(th_e),
                    "Θ aktuálna ($/deň)": pd.NA if th_c is None else float(th_c),
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
                "Δ vstup",
                "Δ aktuálna",
                "Θ vstup ($/deň)",
                "Θ aktuálna ($/deň)",
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
                "**Súčet za skupinu** = sčítanie všetkých nôh v tabuľke vyššie (čistá Δ ako ekvivalent akcií, Θ ako súčet $/deň). "
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
                    help="Presun nohy do inej skupiny: zmeň hodnotu a pri aktuálnom výbere skupiny stlač **Uložiť journal** nižšie. "
                        "Na viac nôh naraz použi expander **Rýchle priradenie skupiny** nad prepínačom **Skupina**.",
                ),
                "Strike": st.column_config.NumberColumn(format="$%.2f"),
                "Entry $": st.column_config.NumberColumn(format="$%.2f"),
                "DTE": st.column_config.NumberColumn(format="%d dní"),
                "Δ vstup": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                "Δ aktuálna": st.column_config.NumberColumn(format="%.4f", step=0.0001),
                "Θ vstup ($/deň)": st.column_config.NumberColumn(format="$%.3f", step=0.001),
                "Θ aktuálna ($/deň)": st.column_config.NumberColumn(
                    format="$%.3f", step=0.001, help="Aktuálna theta pozície ($/deň)."
                ),
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
            _sum_cd_e = _sum_cd_c = 0.0
            _n_cd_e = _n_cd_c = 0
            _sum_th_e = _sum_th_c = 0.0
            _n_th_e = _n_th_c = 0
            for _, row in edited.iterrows():
                _sgn, _mlt = _journal_sign_mult_from_table_row(row)
                _cde = _journal_clean_delta_share_equiv(row.get("Δ vstup"), _sgn, _mlt)
                _cdc = _journal_clean_delta_share_equiv(row.get("Δ aktuálna"), _sgn, _mlt)
                _fe = _journal_float_for_sum(_cde)
                _fc = _journal_float_for_sum(_cdc)
                if _fe is not None:
                    _sum_cd_e += _fe
                    _n_cd_e += 1
                if _fc is not None:
                    _sum_cd_c += _fc
                    _n_cd_c += 1
                _te = _journal_float_for_sum(row.get("Θ vstup ($/deň)"))
                _tc = _journal_float_for_sum(row.get("Θ aktuálna ($/deň)"))
                if _te is not None:
                    _sum_th_e += _te
                    _n_th_e += 1
                if _tc is not None:
                    _sum_th_c += _tc
                    _n_th_c += 1
    
            _ment_legs = _journal_edited_to_mentor_legs(edited)
            _cal_m = analyze_calendar_mentor(_ment_legs)
            _diag_m = analyze_diagonal_mentor(_ment_legs)
            _greek_snap = compute_journal_group_greek_snapshot(_ment_legs)
            _greek_kind = "calendar" if _cal_m is not None else "diagonal"
            with st.expander(
                "Mentor — kalendár / diagonál (DTE + čisté Gréky)",
                expanded=False,
            ):
                st.caption(
                    "Rovnaké **DTE okná** ako vo Spread Builderi. **Čistá Δ** má orientačný prah (± podľa max. "
                    "kontraktov v skupine); Θ a Vega sú **návodné**, nie tvrdý cieľ. "
                    "Berie **aktuálnu tabuľku** (vrátane zmien pred uložením journalu)."
                )
                if _cal_m is None and _diag_m is None:
                    st.info(
                        "**Kalendár** potrebuje aspoň jeden Long a jeden Short s **rovnakým strike a typom opcie** "
                        "a rôznou expiráciou. **Diagonál:** aspoň jednu Short a jednu Long nohu s expiráciou "
                        "(typicky rôzne striky). Pri jednej nohe alebo chýbajúcich dátumoch mentor DTE nehodnotí."
                    )
                if _cal_m is not None:
                    st.markdown("##### Kalendárny spread (DTE)")
                    st.dataframe(
                        pd.DataFrame(mentor_calendar_rows(_cal_m)),
                        use_container_width=True,
                        hide_index=True,
                        column_config={"Stav": st.column_config.TextColumn()},
                    )
                    st.markdown("**Kalendár — poznámky**")
                    for _line in _cal_m.summary_lines:
                        st.markdown(f"- {_line}")
                if _diag_m is not None:
                    st.markdown("##### Diagonál / krížené expirácie (DTE)")
                    st.dataframe(
                        pd.DataFrame(mentor_comparison_rows(_diag_m)),
                        use_container_width=True,
                        hide_index=True,
                        column_config={"Stav": st.column_config.TextColumn()},
                    )
                    st.markdown("**Diagonál — poznámky**")
                    for _line in _diag_m.summary_lines:
                        st.markdown(f"- {_line}")
                if _cal_m is not None and _diag_m is not None:
                    st.caption(
                        "Skupina vyhovuje **kalendárnemu páru** aj **diagonálnemu** výpočtu DTE — pri viacerých "
                        "nohách môže byť štruktúra zložitejšia; Gréky ber ako orientáciu."
                    )
                if _cal_m is not None or _diag_m is not None:
                    st.markdown(
                        f"##### Čisté Gréky vs. orientačné okno ({'kalendár' if _greek_kind == 'calendar' else 'diagonál'})"
                    )
                    st.dataframe(
                        pd.DataFrame(journal_greek_comparison_rows(_greek_kind, _greek_snap)),
                        use_container_width=True,
                        hide_index=True,
                        column_config={"Stav": st.column_config.TextColumn()},
                    )
                    _gh = journal_greek_mentor_hints(_greek_kind, _greek_snap)
                    if _gh:
                        st.markdown("**Gréky — upozornenia**")
                        for _hint in _gh:
                            st.markdown(f"- {_hint}")
    
            with st.expander(
                "Súčet čistej Δ a Θ (všetky nohy v skupine)",
                expanded=False,
            ):
                _m_cd_e = f"{_sum_cd_e:+,.1f}" if _n_cd_e else "—"
                _m_cd_c = f"{_sum_cd_c:+,.1f}" if _n_cd_c else "—"
                _m_th_e = f"${_sum_th_e:+,.2f}/deň" if _n_th_e else "—"
                _m_th_c = f"${_sum_th_c:+,.2f}/deň" if _n_th_c else "—"
                _mc1, _mc2, _mc3, _mc4 = st.columns(4)
                _mc1.metric(
                    "Σ čistá Δ vstup",
                    _m_cd_e,
                    help="Súčet Δ vstup v ekvivalente akcií — expander Vysvetlivky k metrikám.",
                )
                _mc2.metric(
                    "Σ čistá Δ aktuálna",
                    _m_cd_c,
                    help="Súčet Δ aktuálna v ekvivalente akcií — expander Vysvetlivky k metrikám.",
                )
                _mc3.metric(
                    "Σ čistá Θ vstup",
                    _m_th_e,
                    help="Súčet Θ vstup ($/deň) — expander Vysvetlivky k metrikám.",
                )
                _mc4.metric(
                    "Σ čistá Θ aktuálna",
                    _m_th_c,
                    help="Súčet Θ aktuálna ($/deň) — expander Vysvetlivky k metrikám.",
                )

            with st.expander(
                "Sledovanie short nohy (rýchly prehľad)",
                expanded=False,
            ):
                _tk_sl_q = str(legs_edit[0].get("ticker") or "").strip().upper() if legs_edit else ""
                _s0sl_q, _src_sl_q = _journal_resolve_spot_for_pl(_tk_sl_q, _pf_live_pos)
                _pl_stop_q = journal_group_pl_stoploss_short_window(
                    legs_edit, spot_center=_s0sl_q, marker_source=_src_sl_q
                )
                if _pl_stop_q is None:
                    st.caption(
                        "Treba aspoň jednu **short opčnú nohu** so strike a expiráciou na jednom tickeri."
                    )
                else:
                    _kq = float(_pl_stop_q["k_short"])
                    _sq = _pl_stop_q.get("marker_spot")
                    _xrel_q = _pl_stop_q.get("marker_x_rel")
                    _ep_q = _pl_stop_q.get("short_entry_per_share")
                    _ex_q = str(_pl_stop_q.get("short_expiry") or "")
                    _ot_q = str(_pl_stop_q.get("short_option_type") or "").strip().upper()
                    _q1, _q2, _q3, _q4 = st.columns(4)
                    _q1.metric("Short strike", f"{_kq:g}")
                    _q2.metric("Short exp.", _ex_q or "—")
                    _q3.metric("Spot teraz", f"{float(_sq):.2f}" if _sq is not None else "—")
                    _q4.metric("spot − K", f"{float(_xrel_q):+,.2f}" if _xrel_q is not None else "—")
                    if _xrel_q is not None:
                        _xq = float(_xrel_q)
                        if abs(_xq) < 0.01:
                            st.warning("Short noha je teraz prakticky **na strike**.")
                        elif _xq > 0:
                            _msg = f"Short noha je teraz približne **{_xq:.2f} USD nad strike**."
                            if _ot_q.startswith("C"):
                                st.error(_msg)
                            else:
                                st.success(_msg)
                        else:
                            _msg = f"Short noha je teraz približne **{abs(_xq):.2f} USD pod strike**."
                            if _ot_q.startswith("P"):
                                st.error(_msg)
                            else:
                                st.success(_msg)
                    st.caption(
                        f"Vstupná prémia short nohy: **{abs(float(_ep_q)):.2f} $/akcia**."
                        if _ep_q is not None
                        else "Vstupná prémia short nohy zatiaľ nie je k dispozícii."
                    )
                    st.caption(
                        "Detailný graf a tabuľka P&L podľa short nohy sú nižšie v sekcii "
                        "**Stop-loss: P&L vs. podklad**."
                    )
    
            _dd_ib, _n_dd_ib = _journal_group_delta_dollars_ib(
                edited, orig_by_id, _pf_live_pos
            )
            _dd_sym, _n_dd_sym = _journal_group_delta_dollars_usd(edited)
            _len_ed = len(edited)
            if _len_ed > 0 and _n_dd_ib == _len_ed:
                _dd_sum, _n_dd, _dd_src = _dd_ib, _n_dd_ib, "ib"
            elif _len_ed > 0 and _n_dd_sym == _len_ed:
                _dd_sum, _n_dd, _dd_src = _dd_sym, _n_dd_sym, "journal"
            elif _n_dd_ib > 0:
                _dd_sum, _n_dd, _dd_src = _dd_ib, _n_dd_ib, "ib_partial"
            elif _n_dd_sym > 0:
                _dd_sum, _n_dd, _dd_src = _dd_sym, _n_dd_sym, "journal_partial"
            else:
                _dd_sum, _n_dd, _dd_src = 0.0, 0, "none"
            _mv_sum, _cb_sum, _n_ib = _journal_group_ib_market_value_and_cost_basis(
                edited, orig_by_id, _pf_live_pos
            )
            with st.expander(
                "Delta doláre · Trhová hodnota · Nákladová základňa · TH − náklad (skupina)",
                expanded=False,
            ):
                _m_dd = f"${_dd_sum:+,.0f}" if _n_dd else "—"
                _m_mv = f"${_mv_sum:+,.2f}" if _n_ib else "—"
                _m_cb = f"${_cb_sum:+,.2f}" if _n_ib else "—"
                _m_th_cb = f"${(_mv_sum - _cb_sum):+,.2f}" if _n_ib else "—"
                _ib1, _ib2, _ib3, _ib4 = st.columns(4)
                _ib1.metric(
                    "Delta doláre (Σ)",
                    _m_dd,
                    help="Expozícia v USD — detail v expanderi Vysvetlivky k metrikám.",
                )
                _ib2.metric(
                    "Trhová hodnota (Σ)",
                    _m_mv,
                    help="Súčet MV z IB (cena kontraktu alebo API) — expander.",
                )
                _ib3.metric(
                    "Nákladová základňa (Σ)",
                    _m_cb,
                    help="Súčet nákladovej bázy so znamienkom (net spread) — expander.",
                )
                _ib4.metric(
                    "TH − náklad (Σ)",
                    _m_th_cb,
                    help="Trhová hodnota mínus nákladová základňa (súčty z IB) — nezrealizovaný P&L skupiny; expander.",
                )
                if not _pf_live_pos:
                    st.caption(
                        "**IB:** v session nie sú pozície — import z IB alebo **Obnoviť údaje z TWS** pri skupine."
                    )
                elif _pf_live_pos and _n_ib < len(edited):
                    st.caption(f"**IB zhoda:** {_n_ib} / {len(edited)} nôh v skupine.")
                elif _dd_src in ("none", "ib_partial", "journal_partial") and _len_ed > 0:
                    st.caption(
                        "**Delta doláre:** niektoré nohy chýbajú v výpočte — pozri expander *Vysvetlivky k metrikám* alebo obnov TWS."
                    )
    
            with st.expander("Graf P&L vs. cena podkladu (BS model, orientačný)", expanded=False):
                _tk_pl = str(legs_edit[0].get("ticker") or "").strip().upper() if legs_edit else ""
                _s0pl, _pl_marker_src = _journal_resolve_spot_for_pl(_tk_pl, _pf_live_pos)
                _plcurve = journal_group_pl_vs_spot(
                    legs_edit, spot_center=_s0pl, marker_source=_pl_marker_src
                )
                if _plcurve is None:
                    st.caption(
                        "Graf sa nevykreslí, ak skupina má **viac tickerov podkladu**, chýbajú **expirácie**, "
                        "alebo dáta na výpočet nie sú dostačujúce."
                    )
                else:
                    _hlab = _plcurve["horizon_date"].strftime("%d.%m.%Y")
                    fig_pnl = go.Figure()
                    fig_pnl.add_trace(
                        go.Scatter(
                            x=_plcurve["spots"],
                            y=_plcurve["pl_now"],
                            mode="lines",
                            name="P&L (model, dnes)",
                            line=dict(width=2, color="#e8e8e8"),
                        )
                    )
                    fig_pnl.add_trace(
                        go.Scatter(
                            x=_plcurve["spots"],
                            y=_plcurve["pl_horizon"],
                            mode="lines",
                            name=f"P&L pri exp. {_hlab}",
                            line=dict(width=2, color="#a8a8a8", dash="dash"),
                        )
                    )
                    _fwd_palette = ("#64b5f6", "#9575cd", "#ff8a65", "#4dd0e1", "#aed581")
                    _fwd_dashes = ("dash", "dot", "longdash", "dashdot", "dot")
                    _fwd_days_list = list(_plcurve.get("forward_days") or [])
                    _pl_fwd_by = _plcurve.get("pl_fwd_by_day") or {}
                    for _fi, _kd in enumerate(_fwd_days_list):
                        _ys_f = _pl_fwd_by.get(int(_kd))
                        if not _ys_f:
                            continue
                        _c = _fwd_palette[_fi % len(_fwd_palette)]
                        _d = _fwd_dashes[_fi % len(_fwd_dashes)]
                        fig_pnl.add_trace(
                            go.Scatter(
                                x=_plcurve["spots"],
                                y=_ys_f,
                                mode="lines",
                                name=f"P&L o +{_kd} dní (model)",
                                line=dict(width=1.35, color=_c, dash=_d),
                                opacity=0.92,
                            )
                        )
                    _ms = _plcurve.get("marker_spot")
                    _msrc = _plcurve.get("marker_source") or "spot"
                    _ym1 = _plcurve.get("pl_now_at_marker")
                    _ym2 = _plcurve.get("pl_horizon_at_marker")
                    if _ms is not None and _ym1 is not None and _ym2 is not None:
                        fig_pnl.add_vline(
                            x=_ms,
                            line_width=3,
                            line_color="#26c6da",
                            opacity=0.95,
                            annotation_text=f"Aktuálny spot: {_ms:.2f}",
                            annotation_position="top",
                            annotation_font_size=12,
                            annotation_font_color="#26c6da",
                        )
                        fig_pnl.add_trace(
                            go.Scatter(
                                x=[_ms],
                                y=[_ym1],
                                mode="markers",
                                name="Spot → P&L (dnes)",
                                marker=dict(size=16, color="#26c6da", symbol="diamond", line=dict(width=2, color="#ffffff")),
                                hovertemplate=f"<b>Spot ({_msrc})</b> {_ms:.2f}<br><b>P&L dnes</b>: %{{y:.0f}} USD<extra></extra>",
                            )
                        )
                        fig_pnl.add_trace(
                            go.Scatter(
                                x=[_ms],
                                y=[_ym2],
                                mode="markers",
                                name="Spot → P&L (pri exp.)",
                                marker=dict(size=14, color="#ffb74d", symbol="diamond", line=dict(width=2, color="#ffffff")),
                                hovertemplate=f"<b>Spot ({_msrc})</b> {_ms:.2f}<br><b>P&L pri exp.</b>: %{{y:.0f}} USD<extra></extra>",
                            )
                        )
                        _pl_fwd_at = _plcurve.get("pl_fwd_at_marker") or {}
                        for _fi, _kd in enumerate(_fwd_days_list):
                            _yf = _pl_fwd_at.get(int(_kd))
                            if _yf is None:
                                continue
                            _c = _fwd_palette[_fi % len(_fwd_palette)]
                            fig_pnl.add_trace(
                                go.Scatter(
                                    x=[_ms],
                                    y=[_yf],
                                    mode="markers",
                                    marker=dict(size=8, color=_c, symbol="circle", line=dict(width=1, color="#ffffff")),
                                    showlegend=False,
                                    hovertemplate=(
                                        f"<b>Spot ({_msrc})</b> {_ms:.2f}<br>"
                                        f"<b>P&L o +{_kd} dní</b>: %{{y:.0f}} USD<extra></extra>"
                                    ),
                                )
                            )
                    fig_pnl.update_layout(
                        template="plotly_dark",
                        xaxis_title="Cena podkladu",
                        yaxis_title="P&L (USD)",
                        hovermode="x unified",
                        margin=dict(l=48, r=24, t=40, b=48),
                        legend=dict(
                            yanchor="top",
                            y=0.99,
                            xanchor="left",
                            x=0.01,
                            bgcolor="rgba(0,0,0,0)",
                        ),
                        height=420,
                    )
                    st.plotly_chart(fig_pnl, use_container_width=True, key=f"pf_pnl_{_gkey}")
                    st.caption(_plcurve["note"])
                    if _plcurve.get("forward_days"):
                        st.caption(
                            "**Čiary +2 / +3 / +5 dní:** P&L vs. spot po ubehnutí týchto kalendárnych dňoch (kratší čas do expirácie, rovnaká IV z journalu). "
                            "Pri **rozdielnej Δ** medzi nohami kompenzácia pri pohybe podkladu v čase **nie je rovnaká** — krivky sa od „dnes“ **rozbiehajú**."
                        )
                    if _plcurve.get("marker_spot") is None:
                        st.caption(
                            "**Tip:** Na čiaru spotu treba **pripojené IB** (automaticky sa skúsi aktuálny podklad), prípadne **STK** v portfóliu alebo **spot** v **Symboly**."
                        )
    
            with st.expander(
                "Stop-loss: P&L vs. podklad (rozsah z prémie short nohy)", expanded=False
            ):
                _tk_sl = str(legs_edit[0].get("ticker") or "").strip().upper() if legs_edit else ""
                _s0sl, _src_sl = _journal_resolve_spot_for_pl(_tk_sl, _pf_live_pos)
                _pl_stop = journal_group_pl_stoploss_short_window(
                    legs_edit, spot_center=_s0sl, marker_source=_src_sl
                )
                if _pl_stop is None:
                    st.caption(
                        "Tento graf vyžaduje **aspoň jednu short nohu** so strike a expiráciou a **jeden ticker** podkladu."
                    )
                else:
                    _xxs = _pl_stop["x_spot_minus_short"]
                    _kss = float(_pl_stop["k_short"])
                    _s_under = _pl_stop["spots"]
                    _cd_sl = [[float(s), float(xrk)] for s, xrk in zip(_s_under, _xxs)]
                    fig_sl = go.Figure()
                    fig_sl.add_trace(
                        go.Scatter(
                            x=_s_under,
                            y=_pl_stop["pl_now"],
                            mode="lines",
                            name="P&L (dnes)",
                            line=dict(width=2.2, color="#eceff1"),
                            customdata=_cd_sl,
                            hovertemplate="Spot=%{customdata[0]:.2f}<br>spot−K=%{customdata[1]:.2f}<br>P&L=%{y:.0f} USD<extra></extra>",
                        )
                    )
                    _pf_sl = ("#64b5f6", "#9575cd", "#ff8a65", "#4dd0e1", "#aed581")
                    _pd_sl = ("dash", "dot", "longdash", "dashdot", "dot")
                    for _si, _kd in enumerate(_pl_stop.get("forward_days") or []):
                        _yfs = (_pl_stop.get("pl_fwd_by_day") or {}).get(int(_kd))
                        if not _yfs:
                            continue
                        fig_sl.add_trace(
                            go.Scatter(
                                x=_s_under,
                                y=_yfs,
                                mode="lines",
                                name=f"P&L o +{_kd} dní",
                                line=dict(
                                    width=1.35,
                                    color=_pf_sl[_si % len(_pf_sl)],
                                    dash=_pd_sl[_si % len(_pd_sl)],
                                ),
                                opacity=0.9,
                                customdata=_cd_sl,
                                hovertemplate="Spot=%{customdata[0]:.2f}<br>spot−K=%{customdata[1]:.2f}<br>P&L=%{y:.0f} USD<extra></extra>",
                            )
                        )
                    _mspot = _pl_stop.get("marker_spot")
                    _y0m = _pl_stop.get("pl_now_at_marker")
                    if _mspot is not None and _y0m is not None:
                        fig_sl.add_vline(
                            x=_mspot,
                            line_width=3,
                            line_color="#26c6da",
                            opacity=0.95,
                            annotation_text=f"Aktuálny spot: {_mspot:.2f}",
                            annotation_position="top",
                            annotation_font_size=11,
                            annotation_font_color="#26c6da",
                        )
                        fig_sl.add_trace(
                            go.Scatter(
                                x=[_mspot],
                                y=[_y0m],
                                mode="markers",
                                name="Teraz",
                                marker=dict(
                                    size=15,
                                    color="#26c6da",
                                    symbol="diamond",
                                    line=dict(width=2, color="#ffffff"),
                                ),
                                hovertemplate=(
                                    f"<b>Spot</b> {_mspot:.2f}<br><b>spot−K</b> {_mspot - _kss:+.2f}<br>"
                                    "<b>P&L dnes</b> %{y:.0f} USD<extra></extra>"
                                ),
                            )
                        )
                        _fat_sl = _pl_stop.get("pl_fwd_at_marker") or {}
                        for _si, _kd in enumerate(_pl_stop.get("forward_days") or []):
                            _yf = _fat_sl.get(int(_kd))
                            if _yf is None:
                                continue
                            _c = _pf_sl[_si % len(_pf_sl)]
                            fig_sl.add_trace(
                                go.Scatter(
                                    x=[_mspot],
                                    y=[_yf],
                                    mode="markers",
                                    marker=dict(size=7, color=_c, symbol="circle", line=dict(width=1, color="#ffffff")),
                                    showlegend=False,
                                    hovertemplate=f"+{_kd} dní pri tom istom S: %{{y:.0f}} USD<extra></extra>",
                                )
                            )
                    fig_sl.add_vline(
                        x=_kss,
                        line_width=1,
                        line_color="#888888",
                        opacity=0.55,
                        line_dash="dot",
                    )
                    fig_sl.update_layout(
                        template="plotly_dark",
                        xaxis_title="Cena podkladu (USD) — rozsah z vstupnej prémie short nohy okolo K",
                        yaxis_title="P&L (USD)",
                        hovermode="x unified",
                        margin=dict(l=48, r=24, t=44, b=56),
                        legend=dict(
                            yanchor="top",
                            y=0.99,
                            xanchor="left",
                            x=0.01,
                            bgcolor="rgba(0,0,0,0)",
                        ),
                        height=400,
                    )
                    st.plotly_chart(fig_sl, use_container_width=True, key=f"pf_pnl_sl_{_gkey}")
                    st.caption(_pl_stop["note"])
                    st.caption(
                        f"**K (short):** {_kss:g}. **Sivá bodkovaná** = strike shortu. "
                        "Rozsah osi X podľa **vstupnej prémie** short nohy v journali; bez prémie širší základný interval."
                    )
                    _hi_tbl = _pl_stop.get("marker_spot") or _s0sl
                    if _hi_tbl is not None and float(_hi_tbl) > 0:
                        st.markdown("##### Tabuľka P&L vs. spot (štýl TWS, model BS)")
                        _tc1, _tc2 = st.columns(2)
                        with _tc1:
                            _tbl_lo = st.number_input(
                                "Spot až po (USD)",
                                min_value=1.0,
                                value=170.0,
                                step=1.0,
                                key=f"pf_sl_tbl_lo_{_gkey}",
                            )
                        with _tc2:
                            _tbl_step = st.number_input(
                                "Krok (USD)",
                                min_value=0.05,
                                value=0.5,
                                step=0.05,
                                format="%.2f",
                                key=f"pf_sl_tbl_st_{_gkey}",
                            )
                        _lv = journal_spot_levels_descending(float(_hi_tbl), float(_tbl_lo), float(_tbl_step))
    
                        # ── Tabuľka P&L: IB-anchored delta aprox. ──────────────────────────────
                        # Pre každú nohu:
                        #   P&L(S) = unrealized_pnl_IB  +  delta_IB × (S − S_now) × contracts × 100
                        # kde S_now = aktuálny spot (z IB / Symboly).
                        # Výhoda: presné číslo pri aktuálnom spote (zhoduje sa s TWS),
                        # správny smer (delta z IB) pre ostatné scenáre.
                        # Fallback na BS model ak IB greky/unrealized nie sú dostupné.
    
                        _s_now = float(_s0sl) if _s0sl is not None and float(_s0sl or 0) > 0 else None
                        _tws_rows: list[dict] = []
                        _used_ib = False
                        for _sv in _lv:
                            _spot_r = round(float(_sv), 2)
                            _leg_rows = []
                            _net_pl = 0
                            for _leg in (_legs_display_order_ui(legs_edit) if True else legs_edit):
                                _lt = str(_leg.get("leg_type") or "").strip().capitalize()
                                _ks = abs(int(round(float(_leg.get("contracts") or 1))))
                                _ks_signed = _ks if _lt == "Long" else -_ks
                                _lbl = _journal_leg_instrument_label_ui(_leg)
    
                                _ib_opt = find_ibkr_option_for_trade(_leg, _pf_live_pos)
                                _pl_leg: int | None = None
                                if _ib_opt is not None and _s_now is not None:
                                    try:
                                        _unrl = float(_ib_opt.get("unrealized_pnl") or 0.0)
                                        _delta_ps = float(_ib_opt.get("delta") or 0.0)
                                        _sgn_pos = 1.0 if _lt == "Long" else -1.0
                                        _dS = float(_sv) - _s_now
                                        _pl_leg = int(round(_unrl + _sgn_pos * _delta_ps * _dS * _ks * 100.0))
                                        _used_ib = True
                                    except Exception:
                                        _pl_leg = None
    
                                if _pl_leg is None:
                                    # fallback: BS model (pôvodná logika)
                                    _pl_bs = _single_leg_pl_now_usd(_leg, float(_sv), 0.045)
                                    _pl_leg = int(round(float(_pl_bs))) if _pl_bs is not None else 0
    
                                _net_pl += _pl_leg
                                _leg_rows.append({
                                    "spot": _spot_r,
                                    "kontrakt": _lbl,
                                    "noha": _lt or "—",
                                    "ks": f"{_ks_signed:+d}",
                                    "pl_usd": _pl_leg,
                                    "_typ": "noha",
                                })
                            _tws_rows.extend(_leg_rows)
                            _tws_rows.append({
                                "spot": _spot_r,
                                "kontrakt": "Σ NET",
                                "noha": "",
                                "ks": "",
                                "pl_usd": _net_pl,
                                "_typ": "net",
                            })
    
                        if _tws_rows:
                            _tdf = pd.DataFrame(_tws_rows).drop(columns=["_typ"], errors="ignore")
                            _tdf = _tdf.rename(
                                columns={
                                    "spot": "Spot",
                                    "kontrakt": "Kontrakt",
                                    "noha": "Noha",
                                    "ks": "Ks.",
                                    "pl_usd": "P&L (USD)",
                                }
                            )
                            _tdf["Spot"] = _tdf["Spot"].map(_journal_fmt_spot_cell_str)
                            _tdf["P&L (USD)"] = _tdf["P&L (USD)"].map(_journal_fmt_pl_usd_cell_str)
                            _cc_tws: dict = {}
                            if "Spot" in _tdf.columns:
                                _cc_tws["Spot"] = st.column_config.TextColumn(
                                    "Spot",
                                    help="Scenár ceny podkladu (text = zarovnanie vľavo).",
                                    width="small",
                                )
                            if "Kontrakt" in _tdf.columns:
                                _cc_tws["Kontrakt"] = st.column_config.TextColumn(
                                    "Kontrakt",
                                    help="Opčný kontrakt (z denníka).",
                                    width="large",
                                )
                            if "Noha" in _tdf.columns:
                                _cc_tws["Noha"] = st.column_config.TextColumn(
                                    "Noha",
                                    help="Long alebo Short.",
                                    width="small",
                                )
                            if "Ks." in _tdf.columns:
                                _cc_tws["Ks."] = st.column_config.TextColumn(
                                    "Ks.",
                                    help="Počet kontraktov so znamienkom (+ long, − short).",
                                    width="small",
                                )
                            if "P&L (USD)" in _tdf.columns:
                                _pl_help = (
                                    "P&L v USD: pri aktuálnom spote z IB (TWS); ostatné spoty = delta aproximácia. "
                                    "Riadok Σ NET = súčet nôh."
                                )
                                _cc_tws["P&L (USD)"] = st.column_config.TextColumn(
                                    "P&L (USD)",
                                    help=_pl_help + " Hodnoty ako text kvôli čitateľnému zarovnaniu.",
                                    width="small",
                                )
                            st.dataframe(
                                _tdf,
                                use_container_width=True,
                                hide_index=True,
                                column_config=_cc_tws,
                            )
                            _src_note = "IB delta aprox. (baseline = live unrealized P&L z TWS)" if _used_ib else "BS model (IB data nie sú dostupné – klikni Obnoviť údaje z TWS)"
                            st.caption(
                                f"**Layout ako v TWS:** Spot | Kontrakt | Noha | Ks. | P&L. "
                                f"P&L pri aktuálnom spote = presné IB číslo; ostatné spoty = lineárna delta aprox. "
                                f"**Zdroj:** {_src_note}."
                            )
                    else:
                        st.caption("**Tabuľka:** dopln **spot** (IB / Symboly), aby bolo od čoho počítať krok nadol.")
    
            _watch_rows: list[dict] = []
            for _, row in edited.iterrows():
                lt = str(row.get("Noha") or "")
                de = _nan_to_none(row["Δ vstup"])
                dc = _nan_to_none(row["Δ aktuálna"])
                ratio = _short_delta_abs_ratio(lt, de, dc)
                spot_w = spot_a = None
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
    
                # Orientačný podklad pri |Δ| = ratio × |Δ vstup| (BS; IV z journalu / Symboly)
                iv_bs, iv_src = None, ""
                strike_v = float(row.get("Strike") or 0)
                exp_s = str(row.get("Expirácia") or "")
                dte_sig = _dte_signed(exp_s)
                rc = _option_right_from_typ(row.get("Typ"))
                tk_up = str(row.get("Ticker") or "").strip().upper()
                spot_hint = ""
                if lt == "Short":
                    iv_bs, iv_src = _resolve_iv_bs_for_spot_row(row, tk_up)
                    spot_hint = _spot_missing_reason_short(
                        iv_bs=iv_bs,
                        strike_v=strike_v,
                        dte_signed=dte_sig,
                        rc=rc,
                        entry_delta_ok=de is not None,
                    )
    
                if lt == "Short" and de is not None:
                    if (
                        iv_bs is not None
                        and strike_v > 0
                        and dte_sig is not None
                        and dte_sig >= 0
                        and rc
                    ):
                        ae = abs(float(de))
                        tw = _target_abs_delta_for_ratio(ae, _SHORT_DELTA_WARN_RATIO)
                        ta = _target_abs_delta_for_ratio(ae, _SHORT_DELTA_ALERT_RATIO)
                        if tw is not None:
                            spot_w = spot_for_abs_delta_bs(
                                strike_v, int(dte_sig), float(iv_bs), rc, tw
                            )
                        if ta is not None:
                            spot_a = spot_for_abs_delta_bs(
                                strike_v, int(dte_sig), float(iv_bs), rc, ta
                            )
                        if spot_w is not None or spot_a is not None:
                            spot_hint = ""
                        if spot_w is None and spot_a is None and spot_hint == "":
                            spot_hint = (
                                "Numericky sa nepodarilo dopočítať spot (extrémne IV alebo údaje)."
                            )
    
                _watch_rows.append(
                    {
                        "ID": int(row["ID"]),
                        "Ticker": row.get("Ticker") or "",
                        "Noha": lt,
                        "|Δ aktuál| / |Δ vstup|": p_str,
                        "Stav": st_lbl,
                        "IV v BS": _fmt_iv_bs_line(iv_bs, iv_src),
                        f"Podklad @ {_SHORT_DELTA_WARN_RATIO:g}× (BS)": _fmt_underlying_spot(spot_w),
                        f"Podklad @ {_SHORT_DELTA_ALERT_RATIO:g}× (BS)": _fmt_underlying_spot(spot_a),
                        "Pre spot": spot_hint if lt == "Short" else "",
                    }
                )
            if any(str(r.get("Noha")) == "Short" for r in _watch_rows):
                with st.expander(
                    "Sledovanie delty (shortové nohy)",
                    expanded=False,
                ):
                    st.info(
                        "**Podklad @ …×** sa dopočíta z buniek v tabuľke vyššie pri každom obnovení stránky "
                        "(po úprave bunky Streamlit znova spustí výpočet; **Uložiť journal** len uloží hodnoty do DB). "
                        "Potrebné: **Δ vstup**, **IV aktuálna** alebo **IV vstup** — môžeš zadať ako **0,767** alebo ako percentá z brokera (**76,7**). "
                        "Ak je IV v journalu prázdne, použije sa **IV %** zo záložky **Symboly** (rovnaký ticker)."
                    )
                    st.caption(
                        f"Pomer |Δ aktuálna| ÷ |Δ vstup|. Varovanie od **{_SHORT_DELTA_WARN_RATIO}×**, "
                        f"silné od **{_SHORT_DELTA_ALERT_RATIO}×**. "
                        f"Stĺpce **Podklad @ …×** = orientačná cena podkladu (Black–Scholes), pri ktorej by **|delta opcie|** "
                        f"bola **≈ {_SHORT_DELTA_WARN_RATIO:g}× / {_SHORT_DELTA_ALERT_RATIO:g}×** oproti **|Δ vstup|**. "
                        "**IV v BS** = akú IV model použil (percentá zo vstupu sa normalizujú na zlomok). "
                        "Bez dividend, **r = 4,5 %**. Pri rollovaní len ako vodítko."
                    )
                    st.dataframe(
                        pd.DataFrame(_watch_rows),
                        use_container_width=True,
                        hide_index=True,
                    )
    
                with st.expander(
                    "PL pri assignment",
                    expanded=False,
                ):
                    _tk_asn = str(legs_edit[0].get("ticker") or "").strip().upper() if legs_edit else ""
                    _hi_asn, _src_asn_hi = _journal_resolve_spot_for_pl(_tk_asn, _pf_live_pos)
                    if _hi_asn is not None and float(_hi_asn) > 0:
                        _long_pl_by_tid: dict[int, float] = {}
                        _long_src_by_tid: dict[int, str] = {}
                        for _lg in legs_edit:
                            if str(_lg.get("leg_type") or "").strip().capitalize() != "Long":
                                continue
                            _lid = _lg.get("id")
                            if _lid is None:
                                continue
                            _val, _src = _journal_long_leg_assignment_column_usd(
                                _lg, _pf_live_pos, float(_hi_asn)
                            )
                            if _val is not None:
                                _long_pl_by_tid[int(_lid)] = float(_val)
                                _long_src_by_tid[int(_lid)] = _src
                        _long_sum = sum(_long_pl_by_tid.values())
                        _has_any_long = any(
                            str(lg.get("leg_type") or "").strip().capitalize() == "Long"
                            for lg in legs_edit
                        )
                        _src_set = set(_long_src_by_tid.values())
                        if _long_pl_by_tid:
                            _sum_note = f" **Σ Long (PL):** {int(round(_long_sum))} USD."
                            if _src_set == {"ib"}:
                                _sum_note += " Všetky Long z IB."
                            elif _src_set == {"bs"}:
                                _sum_note += " Všetky Long z BS modelu (zhoda IB chýba)."
                            elif _src_set <= {"ib", "bs"} and len(_src_set) > 1:
                                _sum_note += " Long: kombinácia IB a BS."
                        elif _has_any_long:
                            _sum_note = (
                                " Long: dopln **Obnoviť údaje z TWS**, **vstupnú cenu** v žurnáli "
                                "alebo údaje nohy (expirácia, strike, IV), aby sa doplnil PL."
                            )
                        else:
                            _sum_note = ""
    
                        with st.expander(
                            "Čo znamenajú čísla — rozšíriť",
                            expanded=False,
                        ):
                            st.markdown(
                                """
    **Čo znamenajú čísla** (bez poplatkov, orientačne):
    
    - **Krátka — PL:** pre **Spot** v danom riadku: na 1 akciu podkladu  
      **|vstupná prémia| + strike − spot**, na celú nohu **× kontrakty × 100**.  
      Je to **jeden riadok** so zjednodušeným účinkom short opcie pri údere: v čísle je naraz aj **prémia** (čo ste za opciu dostali / zaplatili podľa znamienka v journali berieme cez abs) aj časť **(strike − spot)** na akciu — teda nie je to len „prémia × ks × 100“, ale **prémia + (K − S)** na akciu × 100 × ks.
    
    - **Dlhá — PL:** **iba long opcia**, nie nákup akcie pri údere:  
      **(aktuálna cena opcie $/akcia − kúpna prémia $/akcia) × kontrakty × 100**.  
      Cena z IB (**Last** / mark / mid) alebo z BS, ak chýba zhoda s TWS. Kúpna prémia z **vstupnej ceny** v žurnáli, prípadne z **IB avg_cost/100**.
    
    - **Prečo nie „−S×ks×100 + K×ks×100 + opcia…“ na dlhej nohe?**  
      Súčet **−spot + strike** na akciu (× 100 × ks) je pri tomto zjednodušení **už obsiahnutý v riadku krátkej nohy** (v člene **+ K − S**). Keby sme ten istý **(K − S)×100×ks** pripočítali ešte raz na dlhej nohe, **Σ NET** by **podklad dvojnásobne** započítal.  
      Rozklad, ktorý popisuješ (**podklad (K−S) + zostatok long opcie (mark − nákup)**), sedí s **Σ NET**, len je u nás rozdelený: **(K−S) a prémia shortu** sú v **krátkej** bunke, **(cena long opcie − nákup)** v **dlhej**.
    
    - **Σ NET:** súčet PL v riadku (**krátka** + **dlhá**).
    
    **Layout:** ako tabuľka **P&L vs. spot** — scenár **Spot**, stĺpec **Δ spot** = o koľko USD je scenár nad/pod **referenčným spotom** (bázis z IB / Symboly), riadky nôh, **Σ NET**. Dlhá noha: PL **nezávisí od Spot** v stĺpci (opcia sa oceňuje referenčne / IB), mení sa len krátka časť podľa **Spot** v riadku.
    
    *Poznámka:* pre **short put** je reálny cashflow pri údere iný než pre **short call**; tá istá jednoradová formula je len **orientačný model** pre obe nohy.
    """
                            )
                            st.markdown(
                                f"**Referenčný spot** (bázis pre stĺpec Δ) = **{_hi_asn:.2f}** ({_src_asn_hi or '—'})."
                                + _sum_note
                            )
    
                        _ac1, _ac2, _ac3 = st.columns(3)
                        with _ac1:
                            _asn_above = st.number_input(
                                "Nad spotom (USD)",
                                min_value=0.0,
                                value=25.0,
                                step=1.0,
                                help="Scenáre s podkladom vyšším ako referenčný spot o túto sumu (horná hranica rebríka).",
                                key=f"pf_asn_tbl_ab_{_gkey}",
                            )
                        with _ac2:
                            _asn_below = st.number_input(
                                "Pod spotom (USD)",
                                min_value=0.0,
                                value=50.0,
                                step=1.0,
                                help="Scenáre s podkladom nižším ako referenčný spot o túto sumu (dolná hranica rebríka).",
                                key=f"pf_asn_tbl_be_{_gkey}",
                            )
                        with _ac3:
                            _asn_step = st.number_input(
                                "Krok (USD)",
                                min_value=0.05,
                                value=0.5,
                                step=0.05,
                                format="%.2f",
                                key=f"pf_asn_tbl_st_{_gkey}",
                            )
                        st.caption(
                            f"Rebrík spotov: od **{_hi_asn + float(_asn_above):.2f}** nadol po **{max(1.0, _hi_asn - float(_asn_below)):.2f}** "
                            f"(referencia **{_hi_asn:.2f}** uprostred rozsahu; krok **{_asn_step:g}** USD)."
                        )
                        _lv_asn = journal_spot_levels_band(
                            float(_hi_asn),
                            float(_asn_above),
                            float(_asn_below),
                            float(_asn_step),
                        )
                        _asn_tws_rows: list[dict] = []
                        _ref_asn = float(_hi_asn)
                        for _sv in _lv_asn:
                            _spot_r = round(float(_sv), 2)
                            _dspot_r = round(_spot_r - _ref_asn, 2)
                            _net_asn = 0
                            for _leg in _legs_display_order_ui(legs_edit):
                                _lt = str(_leg.get("leg_type") or "").strip().capitalize()
                                _ks = abs(int(round(float(_leg.get("contracts") or 1))))
                                _ks_signed = _ks if _lt == "Long" else -_ks
                                _lbl = _journal_leg_instrument_label_ui(_leg)
                                if _lt == "Long":
                                    _lid2 = _leg.get("id")
                                    _mv_l = (
                                        _long_pl_by_tid.get(int(_lid2))
                                        if _lid2 is not None
                                        else None
                                    )
                                    if _mv_l is not None:
                                        _pi_l = int(round(float(_mv_l)))
                                        _net_asn += _pi_l
                                        _pv_l = float(_pi_l)
                                    else:
                                        _pv_l = float("nan")
                                    _asn_tws_rows.append(
                                        {
                                            "spot": _spot_r,
                                            "dspot_usd": _dspot_r,
                                            "kontrakt": _lbl,
                                            "noha": _lt or "—",
                                            "ks": f"{_ks_signed:+d}",
                                            "pl_usd": _pv_l,
                                            "_typ": "noha",
                                        }
                                    )
                                    continue
                                if _lt != "Short":
                                    _asn_tws_rows.append(
                                        {
                                            "spot": _spot_r,
                                            "dspot_usd": _dspot_r,
                                            "kontrakt": _lbl,
                                            "noha": _lt or "—",
                                            "ks": f"{_ks_signed:+d}",
                                            "pl_usd": float("nan"),
                                            "_typ": "noha",
                                        }
                                    )
                                    continue
                                _pl_a = _assignment_short_pl_usd(_leg, float(_sv))
                                if _pl_a is not None:
                                    _pi = int(round(float(_pl_a)))
                                    _net_asn += _pi
                                    _pv = float(_pi)
                                else:
                                    _pv = float("nan")
                                _asn_tws_rows.append(
                                    {
                                        "spot": _spot_r,
                                        "dspot_usd": _dspot_r,
                                        "kontrakt": _lbl,
                                        "noha": _lt or "—",
                                        "ks": f"{_ks_signed:+d}",
                                        "pl_usd": _pv,
                                        "_typ": "noha",
                                    }
                                )
                            _asn_tws_rows.append(
                                {
                                    "spot": _spot_r,
                                    "dspot_usd": _dspot_r,
                                    "kontrakt": "Σ NET",
                                    "noha": "",
                                    "ks": "",
                                    "pl_usd": float(_net_asn),
                                    "_typ": "net",
                                }
                            )
                        if _asn_tws_rows:
                            _adf_asn = pd.DataFrame(_asn_tws_rows).drop(columns=["_typ"], errors="ignore")
                            _adf_asn = _adf_asn.rename(
                                columns={
                                    "spot": "Spot",
                                    "dspot_usd": "Δ spot (USD)",
                                    "kontrakt": "Kontrakt",
                                    "noha": "Noha",
                                    "ks": "Ks.",
                                    "pl_usd": "PL (USD)",
                                }
                            )
                            _adf_asn["Spot"] = _adf_asn["Spot"].map(_journal_fmt_spot_cell_str)
                            if "Δ spot (USD)" in _adf_asn.columns:
                                _adf_asn["Δ spot (USD)"] = _adf_asn["Δ spot (USD)"].map(
                                    _journal_fmt_dspot_cell_str
                                )
                            _adf_asn["PL (USD)"] = _adf_asn["PL (USD)"].map(_journal_fmt_pl_usd_cell_str)
                            _cc_asn: dict = {}
                            if "Spot" in _adf_asn.columns:
                                _cc_asn["Spot"] = st.column_config.TextColumn(
                                    "Spot",
                                    help="Scenár ceny podkladu (USD).",
                                    width="small",
                                )
                            if "Δ spot (USD)" in _adf_asn.columns:
                                _cc_asn["Δ spot (USD)"] = st.column_config.TextColumn(
                                    "Δ spot (USD)",
                                    help="Scenárny spot mínus referenčný spot (IB / Symboly); + = vyššie podklad.",
                                    width="small",
                                )
                            if "Kontrakt" in _adf_asn.columns:
                                _cc_asn["Kontrakt"] = st.column_config.TextColumn(
                                    "Kontrakt",
                                    help="Kontrakt z denníka.",
                                    width="large",
                                )
                            if "Noha" in _adf_asn.columns:
                                _cc_asn["Noha"] = st.column_config.TextColumn(
                                    "Noha",
                                    width="small",
                                )
                            if "Ks." in _adf_asn.columns:
                                _cc_asn["Ks."] = st.column_config.TextColumn(
                                    "Ks.",
                                    help="Kontrakty so znamienkom.",
                                    width="small",
                                )
                            if "PL (USD)" in _adf_asn.columns:
                                _cc_asn["PL (USD)"] = st.column_config.TextColumn(
                                    "PL (USD)",
                                    help="Krátka: |prémia|+K−spot na akciu ×ks×100 (zjednoduš. assignment). Dlhá: (last−kúpna)×ks×100. Σ NET = súčet.",
                                    width="small",
                                )
                            st.dataframe(
                                _adf_asn,
                                use_container_width=True,
                                hide_index=True,
                                column_config=_cc_asn,
                            )
                            st.caption(
                                "Stĺpce PL a Σ NET — význam výpočtov je v rozšíriteľnom texte **„Čo znamenajú čísla“** vyššie."
                            )
                    else:
                        st.caption(
                            "**PL pri assignment:** dopln **spot** podkladu (IB / Symboly), aby sa dal zobraziť rebrík scenárov."
                        )
    
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
    
                    new_iv = _greek_entry_from_current_when_missing(
                        orig.get("iv_at_entry"),
                        row["IV vstup"],
                        row["IV aktuálna"],
                    )
                    new_d = _greek_entry_from_current_when_missing(
                        orig.get("delta_at_entry"),
                        row["Δ vstup"],
                        row["Δ aktuálna"],
                    )
                    new_th = _greek_entry_from_current_when_missing(
                        orig.get("theta_at_entry"),
                        row["Θ vstup ($/deň)"],
                        row["Θ aktuálna ($/deň)"],
                    )
                    new_dc = _greek_cell_to_db(orig.get("delta_current"), row["Δ aktuálna"])
                    new_tc = _greek_cell_to_db(orig.get("theta_current"), row["Θ aktuálna ($/deň)"])
                    new_ve = _greek_entry_from_current_when_missing(
                        orig.get("vega_at_entry"),
                        row["Vega vstup"],
                        row["Vega aktuálna"],
                    )
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
