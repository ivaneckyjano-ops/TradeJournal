"""
Casopis — otvorené nohy z denníka: skupiny a ručný zápis Grékov / IV do DB.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from pathlib import Path
from datetime import date, datetime

import pandas as pd
import streamlit as st

from core import database as db
from core import ibkr
from core.greeks import iv_display_to_bs_fraction, spot_for_abs_delta_bs
from core.ib_row_extract import ParsedIbRow, ocr_image_to_text, parse_ibkr_row_text
from core.portfolio_data import (
    find_ibkr_option_for_trade,
    greek_for_trade,
    ib_opt_greeks_scaled_for_journal,
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

### Časté problémy

- **Metriky z IB ≠ TWS:** obnov dáta tlačidlom pri skupine; over, že v účte je **STK** podkladu (pre spot) alebo že v **Symboly** je aktuálny **spot**.
- **Žiadna zhoda IB:** noha v denníku musí sedieť s kontraktom v TWS (ticker, strike, expirácia, typ, Long/Short).
"""


st.title("Casopis — Gréky a skupiny")

st.caption(
    "**Návod:** Uprav **Δ vstup / Δ aktuálna**, **Θ vstup ($/deň) / Θ aktuálna ($/deň)** a ďalšie polia v tabuľke, potom **Uložiť journal** pri skupine. "
    "Pod tabuľkou sú **súčty za skupinu** (čistá Δ/Θ), **Delta doláre / trhová hodnota / nákladová základňa** (prednostne z IB ako v TWS; inak journal + Symboly). "
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
    db.set_trade_portfolio_greeks(
        tid,
        trade.get("iv_at_entry"),
        trade.get("delta_at_entry"),
        trade.get("theta_at_entry"),
        dc,
        vega_at_entry=trade.get("vega_at_entry"),
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
    st.session_state["live_positions"] = poss
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
                    st.session_state["live_positions"] = res["positions"]
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
                help="Pre každú otvorenú nohu nájde zhodu v portfóliu a zapíše Δ, IV, Θ, Vega do DB (bez úpravy vstupných polí).",
            ):
                with st.spinner("Sťahujem pozície s Grékmi (môže trvať)…"):
                    res = ibkr.fetch_positions(with_greeks=True, use_historical_last=False)
                if res.get("error"):
                    st.session_state["pf_journal_sync_msg"] = ("error", str(res["error"]))
                else:
                    poss = res.get("positions", [])
                    st.session_state["live_positions"] = poss
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

(tab_legs,) = st.tabs(["Skupiny a Gréky"])

with tab_legs:
    st.caption(
        "**Návod:** Táto tabuľka je na ručný zápis. Doplň **Δ vstup / Δ aktuálna** a **Θ vstup ($/deň) / Θ aktuálna ($/deň)**, prípadne aj ďalšie hodnoty, a stlač **Uložiť journal**. "
        "Pod editorom sú **súčty za skupinu**; podrobnosti metrík sú v expanderi **Vysvetlivky k metrikám** (predvolene zbalený). "
        "Zápis sa uloží do DB a zostane aj na ďalší deň."
    )
    if not open_trades:
        st.info(
            "Pre aktívny filter nemáš žiadne otvorené nohy (*Open*), alebo v denníku ešte nie sú záznamy. "
            "Nohy pridáš importom z IBKR na Dashboarde alebo priamo v denníku."
        )
    else:
        _fb = st.session_state.pop("pf_journal_tws_refresh_msg", None)
        if _fb:
            _k, _txt = _fb
            if _k == "ok":
                st.success(_txt)
            else:
                st.error(_txt)
        with st.expander("Vysvetlivky k metrikám (súčty pod tabuľkou skupiny)", expanded=False):
            st.markdown(_JOURNAL_METRICS_HELP_MD)
        _pf_live_pos: list[dict] = list(st.session_state.get("live_positions") or [])
        for gname in _sort_keys:
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

                st.markdown("##### Súčet čistej Δ a Θ (všetky nohy v skupine)")
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
                st.markdown("##### Delta doláre · Trhová hodnota · Nákladová základňa (skupina)")
                _m_dd = f"${_dd_sum:+,.0f}" if _n_dd else "—"
                _m_mv = f"${_mv_sum:+,.2f}" if _n_ib else "—"
                _m_cb = f"${_cb_sum:+,.2f}" if _n_ib else "—"
                _ib1, _ib2, _ib3 = st.columns(3)
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
                    st.markdown("##### Sledovanie delty (shortové nohy)")
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
