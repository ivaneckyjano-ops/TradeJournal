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
    journal_spot_levels_descending,
)
from core.portfolio_data import (
    calc_dte,
    find_ibkr_option_for_trade,
    greek_for_trade,
    ib_opt_greeks_scaled_for_journal,
    unrealized_by_journal_ids_for_ib_legs,
)
from core.page_context import set_tradejournal_page

db.init_db()
set_tradejournal_page("portfolio")

_SHORT_DELTA_WARN_RATIO = 1.5
_SHORT_DELTA_ALERT_RATIO = 2.0

_JOURNAL_METRICS_HELP_MD = """
### Σ čistá Δ vstup · Σ čistá Δ aktuálna

Súčet cez **všetky nohy** v skupine: **Δ (0–1) × počet kontraktov × 100 × znamienko nohy** (+1 long, −1 short).  
Výsledok je **ekvivalent podkladu v počte akcií** (rovnaká logika ako pri súčtoch nôh v analýze portfólia).  
**Vstup** = z bunky Δ vstup, **aktuálna** = z bunky Δ aktuálna (z denníka alebo po zápise z TWS).

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


def _journal_group_tab_label(gname: str, *, max_len: int = 28) -> str:
    """Názov skupiny pre záložku ``st.tabs`` (krátke mená sa vo UI lmú lepšie)."""
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
    """Znamienko nohy (Long +1, Short −1) a násobiteľ kontrakt × 100."""
    mult = float(trade.get("contracts") or 1) * 100.0
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
    """Kontrakty a znamienko z riadku časopisnej tabuľky (stĺpce Kontr., Noha)."""
    t_like = {"contracts": row.get("Kontr."), "leg_type": row.get("Noha")}
    return _journal_leg_sign_mult(t_like)


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
    g, src = greek_for_trade(trade, positions, {}, {})
    if src not in ("live", "bs") or not g:
        return False
    if not any(
        g.get(k) not in (None, 0, 0.0) for k in ("delta", "theta", "vega", "iv")
    ):
        return False
    tid = int(trade["id"])
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
    n_ok = 0
    for tr in legs_edit:
        if str(tr.get("status") or "Open").strip().lower() != "open":
            continue
        if _journal_write_live_greeks_for_trade(tr, poss):
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
        "**Skupiny:** každá skupina z denníka má **vlastnú hornú záložku** (napr. Kal.01) — klikni na názov a uprav len tú časť portfólia."
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
    _pf_live_pos: list[dict] = list(ibkr.get_scoped_session_value("live_positions", []) or [])
    _journal_tab_labels = [_journal_group_tab_label(g) for g in _sort_keys]
    _grp_tabs = st.tabs(_journal_tab_labels)
    for _ti, gname in enumerate(_sort_keys):
        with _grp_tabs[_ti]:
            legs = by_group[gname]
            meta = groups_meta.get(gname) if gname != PF_GROUP_NONE else None
            _gkey = hashlib.sha256(gname.encode("utf-8")).hexdigest()[:16]
            legs_edit = sorted(legs, key=lambda x: (str(x.get("ticker") or ""), int(x.get("id") or 0)))
            if not legs_edit:
                continue

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
                        help="Rovnaké mená ako v záložke **Skupiny** ako pri úpravách v denníku. Ak sa výber neuloží, skús znova po obnovení stránky.",
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

**Layout:** ako tabuľka **P&L vs. spot** — scenár spot v stĺpci, riadky nôh, **Σ NET**. Dlhá noha: PL **nezávisí od Spot** v stĺpci (opcia sa oceňuje referenčne / IB), mení sa len krátka časť podľa **Spot** v riadku.

*Poznámka:* pre **short put** je reálny cashflow pri údere iný než pre **short call**; tá istá jednoradová formula je len **orientačný model** pre obe nohy.
"""
                                )
                                st.markdown(
                                    f"**Referenčný spot** (horný v rebríku) = **{_hi_asn:.2f}** ({_src_asn_hi or '—'})."
                                    + _sum_note
                                )

                            _ac1, _ac2 = st.columns(2)
                            with _ac1:
                                _asn_lo = st.number_input(
                                    "Spot až po (USD)",
                                    min_value=1.0,
                                    value=170.0,
                                    step=1.0,
                                    key=f"pf_asn_tbl_lo_{_gkey}",
                                )
                            with _ac2:
                                _asn_step = st.number_input(
                                    "Krok (USD)",
                                    min_value=0.05,
                                    value=0.5,
                                    step=0.05,
                                    format="%.2f",
                                    key=f"pf_asn_tbl_st_{_gkey}",
                                )
                            _lv_asn = journal_spot_levels_descending(
                                float(_hi_asn), float(_asn_lo), float(_asn_step)
                            )
                            _asn_tws_rows: list[dict] = []
                            for _sv in _lv_asn:
                                _spot_r = round(float(_sv), 2)
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
                                        "kontrakt": "Kontrakt",
                                        "noha": "Noha",
                                        "ks": "Ks.",
                                        "pl_usd": "PL (USD)",
                                    }
                                )
                                _adf_asn["Spot"] = _adf_asn["Spot"].map(_journal_fmt_spot_cell_str)
                                _adf_asn["PL (USD)"] = _adf_asn["PL (USD)"].map(_journal_fmt_pl_usd_cell_str)
                                _cc_asn: dict = {}
                                if "Spot" in _adf_asn.columns:
                                    _cc_asn["Spot"] = st.column_config.TextColumn(
                                        "Spot",
                                        help="Scenár spotu (text = zarovnanie vľavo).",
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
