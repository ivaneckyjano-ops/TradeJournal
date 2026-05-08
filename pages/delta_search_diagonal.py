"""
Hľadanie diagonálnych spreadov z DB Grékov: čistá delta ~ cieľ, max. čistá theta (jednotky Barchart).
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from core import agent as ai_agent
from core import database as db
from core import diagonal_spread_search as dss
from core import option_chain_db as odb
from core import saved_diagonals_compare as sdc
from core import saved_diagonals_db as sdiag
from core.csv_spread_variant import (
    diagonal_legs_from_saved_display_row,
    ticker_spot_iv_for_diagonal_send,
)
from core.page_context import render_ai_chat_markdown, set_tradejournal_page

db.init_db()
set_tradejournal_page("delta_search_diagonal")

if "dsd_last_results" not in st.session_state:
    st.session_state["dsd_last_results"] = None
if "dsd_last_meta" not in st.session_state:
    st.session_state["dsd_last_meta"] = {}
if "dsd_per_ticker_opts" not in st.session_state:
    st.session_state["dsd_per_ticker_opts"] = {}
if "dsd_compare_agent_chat" not in st.session_state:
    try:
        _rcc = db.get_setting(db.DIAGONAL_COMPARE_AGENT_CHAT_KEY, "")
        st.session_state["dsd_compare_agent_chat"] = json.loads(_rcc) if _rcc else []
    except Exception:
        st.session_state["dsd_compare_agent_chat"] = []
if "dsd_post_search_is_empty" not in st.session_state:
    st.session_state["dsd_post_search_is_empty"] = False
if "dsd_strike_prox_leg" not in st.session_state:
    st.session_state["dsd_strike_prox_leg"] = "long"

# Tabuľkové číslice — rovnaká šírka číslic v stĺpci (lepšie zarovnanie v tabuľke výsledkov)
_DSD_TABLE_STYLE = """
<style>
div[data-testid="stDataFrame"] { font-variant-numeric: tabular-nums; }
</style>
"""


def _dsd_inject_tabular_css_once() -> None:
    """CSS len raz za session — opakované ``unsafe_allow_html`` pred ``data_editor`` zvyšuje riziko React DOM chýb."""
    if st.session_state.get("_dsd_tabular_css_injected"):
        return
    st.session_state["_dsd_tabular_css_injected"] = True
    st.markdown(_DSD_TABLE_STYLE, unsafe_allow_html=True)


def _dsd_save_compare_agent_chat(hist: list) -> None:
    try:
        db.set_setting(db.DIAGONAL_COMPARE_AGENT_CHAT_KEY, json.dumps(hist))
    except Exception:
        pass


def _dsd_data_editor_key(prefix: str, df: pd.DataFrame) -> str:
    """
    Kľúč pre ``st.data_editor``.

    Pôvodne hash stĺpcov menil kľúč pri každej maličkej zmene a pri ``st.rerun()``
    zvyšoval riziko React chýb typu *removeChild*. Stabilizujeme podľa tvaru tabuľky.
    """
    fp = "|".join(str(c) for c in df.columns)
    return f"{prefix}_c{len(fp)}_r{len(df)}_h{abs(hash(fp)) % 10_000_000_000_000}"


def _dsd_dte_ui_band_str(lo: int | None, hi: int | None) -> str:
    """Jednoriadok pre pásom DTE v upozorneniach (Pokročilé)."""
    if lo is None and hi is None:
        return "vypnuté (ľubovoľné dni)"
    if lo is not None and hi is not None:
        return f"**{int(lo)}–{int(hi)}** dní"
    if lo is not None:
        return f"aspoň **{int(lo)}** dní"
    return f"do **{int(hi)}** dní"  # type: ignore[misc]


def _dsd_explain_suggested_dte_mismatch(
    o: dss.DiagonalSearchOptions, pick: dict[str, object]
) -> str:
    """Nevyžaduje import dss fcií; porovnanie návrhu s pásmami z hľadania (žiadne kv.pen.)."""
    try:
        d_n = int(pick["dte_near"])
        d_f = int(pick["dte_far"])
    except (KeyError, TypeError, ValueError):
        return ""
    bits: list[str] = []
    if o.dte_near_min is not None and d_n < int(o.dte_near_min):
        bits.append(
            f"DTE k skoršej dátumovej nohe v návrhu (**{d_n}** dní) je **pod** min. pásma skoršia (**{int(o.dte_near_min)}**)"
        )
    if o.dte_near_max is not None and d_n > int(o.dte_near_max):
        bits.append(
            f"skoršia strana v návrhu ({d_n} dní) je **nad** max. pásma skoršia ({int(o.dte_near_max)})"
        )
    if o.dte_far_min is not None and d_f < int(o.dte_far_min):
        bits.append(
            f"**najčastejšie tento bod:** neskoršia noha v návrhu je **{d_f} dní**, ale v Pokročilých máš **Neskoršia min. ≥ {int(o.dte_far_min)}** – "
            "táto expirácia v importe môže byť **nižšie** než pásom"
        )
    if o.dte_far_max is not None and d_f > int(o.dte_far_max):
        bits.append(
            f"neskoršia strana ({d_f} dní) je **nad** max. pásma neskoršia ({int(o.dte_far_max)})"
        )
    if not bits:
        return ""
    return " **Prečo s tým „nesedí“ tvoj rozsah:** " + " ".join(bits) + "."


def _dsd_delta_tolerance_followup_from_opt(
    search_opts: dss.DiagonalSearchOptions,
) -> tuple[dict[str, str | float], str] | None:
    """Ďalší krok z ``RELAX_STEPS`` alebo vypnutie filtra; payload pre ``dsd_pending_delta_tolerance``."""
    cur = search_opts.delta_tolerance
    if cur is None:
        return None
    nxt_raw = dss._next_relax_value("delta_tolerance", float(cur))
    if nxt_raw is None:
        return None
    if nxt_raw is dss._RELAX_DISABLE:
        return (
            {"action": "off"},
            "vypnúť **toleranciu delty** (žiadny pevný prah — posledný krok z tabuľky zjemnení)",
        )
    try:
        v = float(nxt_raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return ({"action": "set", "tol": v}, f"zvýšiť toleranciu na **{v:g}** (ďalší krok z tabuľky zjemnení)")


def _dsd_drain_pending_delta_tolerance() -> None:
    """Pred widgetmi ``dsd_use_dt`` / ``dsd_delta_tol`` (po tlačidle z prázdneho panela)."""
    p = st.session_state.pop("dsd_pending_delta_tolerance", None)
    if not p or not isinstance(p, dict):
        return
    if p.get("action") == "off":
        st.session_state["dsd_use_dt"] = False
        return
    if p.get("action") != "set":
        return
    try:
        t = float(p["tol"])
    except (KeyError, TypeError, ValueError):
        return
    if t < 0:
        return
    st.session_state["dsd_use_dt"] = True
    st.session_state["dsd_delta_tol"] = t


def _dsd_drain_pending_strike_band_suggestion() -> None:
    """Pred widgetmi ``dsd_strike_band`` / ``dsd_strike_min`` / ``dsd_strike_max``."""
    p = st.session_state.pop("dsd_pending_strike_band_suggestion", None)
    if not p or not isinstance(p, dict):
        return
    try:
        lo = float(p["strike_min"])
        hi = float(p["strike_max"])
    except (KeyError, TypeError, ValueError):
        return
    if lo > hi:
        lo, hi = hi, lo
    st.session_state["dsd_strike_band"] = True
    st.session_state["dsd_strike_min"] = max(0.0, lo)
    st.session_state["dsd_strike_max"] = max(max(0.0, lo), hi)


def _dsd_drain_pending_otm_tuning_suggestion() -> None:
    """Pred widgetmi ``dsd_strike_band`` a ``dsd_maxk``."""
    p = st.session_state.pop("dsd_pending_otm_tuning_suggestion", None)
    if not p or not isinstance(p, dict):
        return
    try:
        lo = float(p["strike_min"])
        hi = float(p["strike_max"])
        max_k = int(p["max_strikes_per_expiry"])
    except (KeyError, TypeError, ValueError):
        return
    if lo > hi:
        lo, hi = hi, lo
    st.session_state["dsd_strike_band"] = True
    st.session_state["dsd_strike_min"] = max(0.0, lo)
    st.session_state["dsd_strike_max"] = max(max(0.0, lo), hi)
    st.session_state["dsd_maxk"] = max(15, min(120, max_k))


def _dsd_trigger_pending_search_rerun() -> None:
    """Rovnaký beh ako **Hľadať** — po úprave filtrov z prázdneho panela (bez skrolovania hore)."""
    st.session_state["dsd_pending_run_search"] = True
    st.rerun()


def _dsd_render_empty_search_panel() -> None:
    """Výsledok 0 — mimo tlačidla „Hľadať“ (inak vnorené widgety + ``rerun`` mätú React/Streamlit)."""
    if not st.session_state.get("dsd_post_search_is_empty", False):
        return
    if st.session_state.get("dsd_last_results") is not None:
        return
    ctx = st.session_state.get("dsd_empty_context") or {}
    if not ctx:
        return
    filter_log = st.session_state.get("dsd_last_filter_log")
    if not filter_log:
        return
    _t0 = str(ctx.get("ticker") or "")
    as_of0 = str(ctx.get("as_of") or "")
    _s0 = ctx.get("strategy")
    if not _s0 or not as_of0:
        return
    try:
        search_opts = _dsd_options_from_stored_blob(ctx.get("initial_opt") or {})
        effective_opt = _dsd_options_from_stored_blob(ctx.get("effective_opt") or {})
    except Exception:
        return
    _fs = (filter_log.failure_steps or [])
    _dte_pick = None
    try:
        _dte_pick = dss.suggest_dte_pair_closest_to_ui(
            ticker=_t0,
            as_of_date=as_of0,
            strategy=_s0,  # type: ignore[arg-type]
            opt=search_opts,
        )
    except Exception:
        _dte_pick = None
    if _fs:
        _first = _fs[0]
        st.error(
            f"Brána zlyhala na **{_first.label}** (`{_first.field}`). "
            "Pozri protokol nižšie, tam je presne vidno, kde sa to zastavilo."
        )
        if _first.field == "dte_near_min/dte_near_max/dte_far_min/dte_far_max":
            st.markdown(
                "— **Dôležité:** Upravíš len páslo **skoršej** expirácie (napr. 40–61), ale v Pokročilých ostane aj páslo **neskoršej** — "
                "hľadanie musí nájsť **kalendárnu** dvojicu, kde *zároveň* DTE k skoršiemu dátumu ∈ skoršia a DTE k neskoršiemu dátumu ∈ neskoršia. "
                "Ak **Neskoršia min** ostane napr. **90** dní a v importe druhá vhodná expirácia končí o pár dní skôr (napr. **85** dní), hlási sa zlyhanie DTE, "
                "hoci skoršia strana by mohla s niektorou expiráciou sedieť. "
                f"**Pásma z tohto hľadania — skoršia:** {_dsd_dte_ui_band_str(search_opts.dte_near_min, search_opts.dte_near_max)}; "
                f"**neskoršia:** {_dsd_dte_ui_band_str(search_opts.dte_far_min, search_opts.dte_far_max)}."
            )
        if _first.field == "dte_near_min/dte_near_max/dte_far_min/dte_far_max" and _dte_pick:
            _dsv = _dte_pick.get("distance_score")
            if _dsv is not None and float(_dsv) > 1e-6:
                _p = _dsd_explain_suggested_dte_mismatch(search_opts, _dte_pick)
                _pen = (
                    f" *Kvádr. penalizácia* (súčet odmocnín mimo intervalu): **{float(_dsv):.0f}**; pri **jedinej** medzere to býva druhá mocnina odstupu v dňoch (napr. 5 dní mimo pásma ⇒ 25). "
                    f"**0** = obe nohy v pásme.{_p}"
                )
            elif _dsv is not None:
                _pen = " *Penalizácia* **0** = návrh v oboch pásmach; ak aj tak padla DTE brána, je to ojedinelé — pozri protokol."
            else:
                _pen = ""
            st.info(
                "**Návrh (dvojica v kalendári najbližšia k tvojim pásnam):** "
                f"skoršia **{_dte_pick['expiry_near']}** (DTE **{_dte_pick['dte_near']}** dní), "
                f"neskoršia **{_dte_pick['expiry_far']}** (DTE **{_dte_pick['dte_far']}** dní).{_pen} "
                "Tlačidlom **Upraviť** zarovnáš **DTE pásma** v Pokročilých na túto dvojicu (dni); **Spustiť znova** spustí hľadanie s aktuálnymi filtrami."
            )
            if st.button(
                "Upraviť",
                key="dsd_apply_suggested_dte",
                type="secondary",
                help="Zapíše DTE skoršej/neskoršej nohy do Pokročilých a obnoví stránku.",
            ):
                st.session_state["dsd_pending_dte_suggestion"] = {
                    "dte_near": int(_dte_pick["dte_near"]),
                    "dte_far": int(_dte_pick["dte_far"]),
                }
                st.rerun()
            if st.button(
                "Spustiť znova",
                key="dsd_rerun_after_suggested_dte",
                type="secondary",
                help="Rovnaké ako **Hľadať** vyššie — znova spustí hľadanie.",
            ):
                _dsd_trigger_pending_search_rerun()
            st.caption(
                "Po **Upraviť** sa hodnoty zapíšu do session pred vykreslením polí v Pokročilých. **Spustiť znova** môžeš použiť aj bez úpravy (opakuje posledné kritériá)."
            )
        if _first.field == "delta_tolerance":
            st.markdown(
                "Brána **Delta tolerancia** znamená, že pri tvojom prahu **|čistá delta − cieľ|** nevyhovuje "
                "**žiadna** kalendárna dvojica v dátach (delta je v **jednotkách z reťazca**, nie ako stĺpec **×100** v tabuľke výsledkov)."
            )
            _dt_follow = _dsd_delta_tolerance_followup_from_opt(search_opts)
            if _dt_follow:
                _dt_payload, _dt_desc = _dt_follow
                st.info(
                    f"**Návrh úpravy:** {_dt_desc}. **Upraviť** nastaví **Pokročilé** a obnoví stránku; **Spustiť znova** hneď spustí hľadanie."
                )
                if st.button(
                    "Upraviť",
                    key="dsd_apply_suggested_delta_tol",
                    type="secondary",
                    help="Ďalší krok tolerancie delty (alebo vypnutie) podľa tabuľky zjemnení.",
                ):
                    st.session_state["dsd_pending_delta_tolerance"] = dict(_dt_payload)
                    st.rerun()
                if st.button(
                    "Spustiť znova",
                    key="dsd_rerun_after_suggested_delta_tol",
                    type="secondary",
                    help="Rovnaké ako **Hľadať** vyššie.",
                ):
                    _dsd_trigger_pending_search_rerun()
                st.caption(
                    "Zápis do session prebehne pred vykreslením polí (rovnako ako pri návrhu DTE)."
                )
        _strike_follow = None
        try:
            _strike_follow = dss.otm_keep_otm_strike_band_suggestion(search_opts, strategy=_s0)  # type: ignore[arg-type]
        except Exception:
            _strike_follow = None
        if _strike_follow:
            _spec = dss.STRATEGIES.get(_s0) if _s0 else None
            _direction = "vyššie" if (_spec and _spec.option_type == "Call") else "nižšie"
            _desc = (
                f"**Návrh:** ak chceš zostať OTM, rozšír rozsah strikov smerom **{_direction}**. "
                f"Toto ponechá OTM filtre zapnuté a len pridá viac strikov z tej správnej strany."
            )
            st.info(_desc)
            if st.button(
                "Upraviť",
                key="dsd_apply_suggested_strike_band",
                type="secondary",
                help="Rozšíri rozsah strike-ov smerom k OTM (Call vyššie, Put nižšie) a obnoví stránku.",
            ):
                st.session_state["dsd_pending_strike_band_suggestion"] = dict(_strike_follow)
                st.rerun()
            if st.button(
                "Spustiť znova",
                key="dsd_rerun_after_suggested_strike_band",
                type="secondary",
                help="Rovnaké ako **Hľadať** vyššie.",
            ):
                _dsd_trigger_pending_search_rerun()
            st.caption(
                "Predvyplnia sa striky pre hľadanie; OTM short/long zostanú zapnuté."
            )
            try:
                _otm_tune = dss.otm_keep_otm_tuning_suggestion(search_opts, strategy=_s0)  # type: ignore[arg-type]
            except Exception:
                _otm_tune = None
            if _otm_tune:
                _maxk = int(_otm_tune["max_strikes_per_expiry"])
                st.info(
                    f"**Voliteľne:** ak chceš ešte viac OTM strikov bez vypínania OTM, zvýš aj "
                    f"**Max. strike-ov / expiráciu** na **{_maxk}**."
                )
                if st.button(
                    "Upraviť",
                    key="dsd_apply_suggested_otm_tuning",
                    type="secondary",
                    help="Rozšíri strike band a zvýši max. strike-ov / expiráciu podľa odporúčania.",
                ):
                    st.session_state["dsd_pending_otm_tuning_suggestion"] = dict(_otm_tune)
                    st.rerun()
                if st.button(
                    "Spustiť znova",
                    key="dsd_rerun_after_suggested_otm_tuning",
                    type="secondary",
                    help="Rovnaké ako **Hľadať** vyššie.",
                ):
                    _dsd_trigger_pending_search_rerun()
                st.caption(
                    "Nastaví sa aj väčší limit strike-ov na expiráciu, aby bolo v DB viac OTM kandidátov. OTM filtre ostanú zapnuté."
                )
    else:
        st.error("DTE prešlo, ale ďalšie filtre nenašli kombináciu. Pozri protokol nižšie pre presnú bránu.")
        if st.button(
            "Spustiť znova",
            key="dsd_rerun_empty_no_failure_step",
            type="secondary",
            help="Opakuje hľadanie s aktuálnymi filtrami (rovnako ako **Hľadať**).",
        ):
            _dsd_trigger_pending_search_rerun()
    st.markdown(filter_log.failure_report_markdown(initial_opt=search_opts, last_tried_opt=effective_opt, strategy=_s0))
    if any(
        getattr(search_opts, f) is not None
        for f in ("dte_near_min", "dte_near_max", "dte_far_min", "dte_far_max")
    ):
        _dte_gate = bool(_fs and _fs[0].field == "dte_near_min/dte_near_max/dte_far_min/dte_far_max")
        try:
            _dm = dss.dte_calendar_diagnostic_markdown(
                _t0, as_of_date=as_of0, strategy=_s0, opt=search_opts  # type: ignore[arg-type]
            )
            if _dm:
                with st.expander(
                    "DTE diagnostika: expirácie a kalendárne dvojice (kde nevyhovujú pásma)",
                    expanded=_dte_gate,
                ):
                    st.markdown(_dm)
        except Exception:
            st.caption("DTE diagnostiku sa nepodarilo zostaviť.")
    try:
        st.markdown(dss.diagonal_relax_suggestions_markdown(search_opts))
    except Exception:
        st.caption("Skús v Pokročilých zjemniť DTE, theta alebo vegu, prípadne tlačidlo **Širšie filtre**.")
    try:
        _hint = dss.diagonal_search_why_empty_hint(
            _t0, as_of_date=as_of0, strategy=_s0, opt=search_opts
        )
        if _hint:
            st.info(_hint)
    except Exception:
        pass
    st.divider()
    if st.button(
        "Spustiť znova",
        key="dsd_rerun_empty_panel_footer",
        type="secondary",
        help="Znova spustí hľadanie s aktuálnymi filtrami (toto isté ako **Hľadať** hore na stránke).",
    ):
        _dsd_trigger_pending_search_rerun()


# Predvolby: najprv **striktné** (klasický skríning); pri 0 výsledkoch ponúkni **širšie** (ETF / kratší reťazec).
_DSD_REC_NOTE = (
    "**Predvolby** — orientačný skríning, nie investičné poradenstvo. "
    "Hľadanie vždy použije **aktuálne** hodnoty v Pokročilých; predvolené sú **striktné** filtre (vhodné napr. pre AMZN). "
    "Theta s **×100** = porovnanie s `čistá theta × 100`. Čistá vega je **na 1 akciu** z reťazca (nie USD z Buildera). "
    "**Striktné:** DTE **skoršej** exp. **40–55**, **neskoršej** **90–140** (kalendár, nie vždy = stĺpec Short); theta **3–8** pri ×100; vega **0,10–0,20**; OTM **short aj long 10 %**; "
    "debit/šírka **0,25**; rel. spread **0,08 / 0,05**; min. OI **100** (ak je v CSV). "
    "**Širšie filtre** (tlačidlo): DTE **10–60** / **35–400**, theta **0,5–15**, vega **0,05–0,35** — často pomôže pri **GLD** a kratších reťazcoch."
)
_DSD_REC_STRICT = {
    "dsd_strike_prox_leg": "long",
    "dsd_use_dt": True,
    "dsd_delta_tol": 2.0,
    "dsd_use_tmin": True,
    "dsd_use_tmax": True,
    "dsd_theta_scale": True,
    "dsd_net_theta_min": 3.0,
    "dsd_net_theta_max": 8.0,
    "dsd_use_nvmin": True,
    "dsd_use_nvmax": True,
    "dsd_nvmin": 0.10,
    "dsd_nvmax": 0.20,
    "dsd_use_ngmin": True,
    "dsd_use_ngmax": True,
    "dsd_ngmin": -0.03,
    "dsd_ngmax": 0.0,
    "dsd_use_dnmin": True,
    "dsd_dnmin": 40,
    "dsd_use_dnmax": True,
    "dsd_dnmax": 55,
    "dsd_use_dfmin": True,
    "dsd_dfmin": 90,
    "dsd_use_dfmax": True,
    "dsd_dfmax": 140,
    "dsd_use_otm": True,
    "dsd_otm_min": 0.10,
    "dsd_use_otm_long": True,
    "dsd_otm_long_min": 0.10,
    "dsd_use_dratio": True,
    "dsd_dratio": 0.25,
    "dsd_use_rss": True,
    "dsd_rss": 0.08,
    "dsd_use_rsl": True,
    "dsd_rsl": 0.05,
    "dsd_use_minoi": True,
    "dsd_minoi": 100,
    "dsd_relax_exclude_otm": False,
}
_DSD_REC_RELAXED = {
    "dsd_strike_prox_leg": "long",
    "dsd_use_dt": True,
    "dsd_delta_tol": 2.0,
    "dsd_use_tmin": True,
    "dsd_use_tmax": True,
    "dsd_theta_scale": True,
    "dsd_net_theta_min": 0.5,
    "dsd_net_theta_max": 15.0,
    "dsd_use_nvmin": True,
    "dsd_use_nvmax": True,
    "dsd_nvmin": 0.05,
    "dsd_nvmax": 0.35,
    "dsd_use_ngmin": True,
    "dsd_use_ngmax": True,
    "dsd_ngmin": -0.03,
    "dsd_ngmax": 0.0,
    "dsd_use_dnmin": True,
    "dsd_dnmin": 10,
    "dsd_use_dnmax": True,
    "dsd_dnmax": 60,
    "dsd_use_dfmin": True,
    "dsd_dfmin": 35,
    "dsd_use_dfmax": True,
    "dsd_dfmax": 400,
    "dsd_use_otm": True,
    "dsd_otm_min": 0.10,
    "dsd_use_otm_long": True,
    "dsd_otm_long_min": 0.10,
    "dsd_use_dratio": True,
    "dsd_dratio": 0.25,
    "dsd_use_rss": True,
    "dsd_rss": 0.08,
    "dsd_use_rsl": True,
    "dsd_rsl": 0.05,
    "dsd_use_minoi": True,
    "dsd_minoi": 100,
    "dsd_relax_exclude_otm": False,
}


def _dsd_reset_strict_filters() -> None:
    for k, v in _DSD_REC_STRICT.items():
        st.session_state[k] = v


def _dsd_apply_relaxed_filters() -> None:
    for k, v in _DSD_REC_RELAXED.items():
        st.session_state[k] = v


def _dsd_options_from_stored_blob(blob: dict) -> dss.DiagonalSearchOptions:
    names = {f.name for f in dataclasses.fields(dss.DiagonalSearchOptions)}
    d = dataclasses.asdict(dss.DiagonalSearchOptions())
    for k in names:
        if k in blob:
            d[k] = blob[k]
    return dss.DiagonalSearchOptions(**d)


def _dsd_apply_diagonal_options_to_session_state(o: dss.DiagonalSearchOptions, ticker: str) -> None:
    """Zosúladí widgety Pokročilých (a spot pre ticker) s uloženými ``DiagonalSearchOptions``."""
    tk = str(ticker).strip().upper()
    st.session_state["dsd_use_dt"] = o.delta_tolerance is not None
    if o.delta_tolerance is not None:
        st.session_state["dsd_delta_tol"] = float(o.delta_tolerance)
    st.session_state["dsd_theta_scale"] = bool(o.theta_scale_contracts)
    st.session_state["dsd_use_tmin"] = o.net_theta_min is not None
    if o.net_theta_min is not None:
        st.session_state["dsd_net_theta_min"] = float(o.net_theta_min)
    st.session_state["dsd_use_tmax"] = o.net_theta_max is not None
    if o.net_theta_max is not None:
        st.session_state["dsd_net_theta_max"] = float(o.net_theta_max)
    st.session_state["dsd_use_nvmin"] = o.net_vega_min is not None
    if o.net_vega_min is not None:
        st.session_state["dsd_nvmin"] = float(o.net_vega_min)
    st.session_state["dsd_use_nvmax"] = o.net_vega_max is not None
    if o.net_vega_max is not None:
        st.session_state["dsd_nvmax"] = float(o.net_vega_max)
    st.session_state["dsd_use_ngmin"] = o.net_gamma_min is not None
    if o.net_gamma_min is not None:
        st.session_state["dsd_ngmin"] = float(o.net_gamma_min)
    st.session_state["dsd_use_ngmax"] = o.net_gamma_max is not None
    if o.net_gamma_max is not None:
        st.session_state["dsd_ngmax"] = float(o.net_gamma_max)
    st.session_state["dsd_use_dnmin"] = o.dte_near_min is not None
    if o.dte_near_min is not None:
        st.session_state["dsd_dnmin"] = int(o.dte_near_min)
    st.session_state["dsd_use_dnmax"] = o.dte_near_max is not None
    if o.dte_near_max is not None:
        st.session_state["dsd_dnmax"] = int(o.dte_near_max)
    st.session_state["dsd_use_dfmin"] = o.dte_far_min is not None
    if o.dte_far_min is not None:
        st.session_state["dsd_dfmin"] = int(o.dte_far_min)
    st.session_state["dsd_use_dfmax"] = o.dte_far_max is not None
    if o.dte_far_max is not None:
        st.session_state["dsd_dfmax"] = int(o.dte_far_max)
    st.session_state["dsd_use_otm"] = o.short_otm_min is not None
    if o.short_otm_min is not None:
        st.session_state["dsd_otm_min"] = float(o.short_otm_min)
    st.session_state["dsd_use_otm_long"] = o.long_otm_min is not None
    if o.long_otm_min is not None:
        st.session_state["dsd_otm_long_min"] = float(o.long_otm_min)
    st.session_state["dsd_use_dratio"] = o.max_debit_to_strike_width_ratio is not None
    if o.max_debit_to_strike_width_ratio is not None:
        st.session_state["dsd_dratio"] = float(o.max_debit_to_strike_width_ratio)
    st.session_state["dsd_use_ivsl"] = bool(o.require_iv_short_ge_long)
    st.session_state["dsd_iv_margin"] = float(o.iv_short_ge_long_margin) if o.require_iv_short_ge_long else 0.0
    st.session_state["dsd_use_rss"] = o.max_rel_spread_short is not None
    if o.max_rel_spread_short is not None:
        st.session_state["dsd_rss"] = float(o.max_rel_spread_short)
    st.session_state["dsd_use_rsl"] = o.max_rel_spread_long is not None
    if o.max_rel_spread_long is not None:
        st.session_state["dsd_rsl"] = float(o.max_rel_spread_long)
    st.session_state["dsd_use_minoi"] = o.min_open_interest is not None
    if o.min_open_interest is not None:
        st.session_state["dsd_minoi"] = int(o.min_open_interest)
    st.session_state["dsd_use_minvol"] = o.min_volume is not None
    if o.min_volume is not None:
        st.session_state["dsd_minvol"] = int(o.min_volume)
    st.session_state["dsd_rank_mode"] = o.rank_mode
    if o.strike_proximity_leg is None:
        st.session_state["dsd_strike_prox_leg"] = "none"
    else:
        st.session_state["dsd_strike_prox_leg"] = str(o.strike_proximity_leg)
    st.session_state["dsd_relax_exclude_otm"] = bool(getattr(o, "relax_exclude_otm", False))
    st.session_state["dsd_opt_shape"] = "calendar" if bool(getattr(o, "require_same_strike", False)) else "diagonal"
    if o.spot is not None and float(o.spot) > 0:
        st.session_state[f"dsd_spot_{tk}"] = float(o.spot)


def _dsd_drain_rehydrate_after_relaxation() -> None:
    """
    Po úspešnom hľadaní so zjemnením sa musia widgety (Pokročilé) zosúladiť s effective_opt.
    V tom istom behu po vykreslení widgetov to spôsobí StreamlitAPIException (kľúč je už viazaný na widget).
    Po ``st.rerun()`` sa tento hook spustí **pred** vytvorením widgetov.
    """
    tkv = st.session_state.pop("dsd_pending_rehydrate_after_relax", None)
    if not tkv:
        return
    tk = str(tkv).strip().upper()
    blob = (st.session_state.get("dsd_per_ticker_opts") or {}).get(tk)
    if blob:
        o = _dsd_options_from_stored_blob(blob)
        _dsd_apply_diagonal_options_to_session_state(o, tk)


def _dsd_drain_pending_dte_suggestion() -> None:
    """
    DTE z tlačidla „Upraviť“ — **pred** widgetmi s kľúčmi ``dsd_use_dnmin``/``dsd_dn*``.
    Inak: ``StreamlitAPIException`` (session_state po inštancii widgetu sa nesmie meniť týmto kľúčom).
    """
    p = st.session_state.pop("dsd_pending_dte_suggestion", None)
    if not p or not isinstance(p, dict):
        return
    try:
        d_n = int(p["dte_near"])
        d_f = int(p["dte_far"])
    except (KeyError, TypeError, ValueError):
        return
    st.session_state["dsd_use_dnmin"] = True
    st.session_state["dsd_use_dnmax"] = True
    st.session_state["dsd_use_dfmin"] = True
    st.session_state["dsd_use_dfmax"] = True
    st.session_state["dsd_dnmin"] = d_n
    st.session_state["dsd_dnmax"] = d_n
    st.session_state["dsd_dfmin"] = d_f
    st.session_state["dsd_dfmax"] = d_f


def _dsd_on_ticker_change() -> None:
    tk = str(st.session_state.get("dsd_ticker") or "").strip().upper()
    blob = (st.session_state.get("dsd_per_ticker_opts") or {}).get(tk)
    if not blob:
        return
    o = _dsd_options_from_stored_blob(blob)
    _dsd_apply_diagonal_options_to_session_state(o, tk)


def _dsd_canonical_column_name_no_times100(col: str) -> str:
    return re.sub(r"\s*×100\s*$", "", str(col).strip()).strip()


def _dsd_strip_times100_display_for_storage(df: pd.DataFrame) -> pd.DataFrame:
    """Pred zápisom do DB: stĺpce delta/theta/vega delí 100 a odstráni príponu ×100 (kanonické názvy + jednotky reťazca)."""
    out = df.copy()
    ren = {c: _dsd_canonical_column_name_no_times100(c) for c in out.columns if "×100" in str(c)}
    if ren:
        out = out.rename(columns=ren)
    for c in list(out.columns):
        lc = str(c).lower()
        if "čistá delta" in lc and "theta" not in lc and "vega" not in lc:
            out[c] = pd.to_numeric(out[c], errors="coerce") / 100.0
        elif "čistá theta" in lc and "vega" not in lc:
            out[c] = pd.to_numeric(out[c], errors="coerce") / 100.0
        elif "čistá vega" in lc and "gamma" not in lc:
            out[c] = pd.to_numeric(out[c], errors="coerce") / 100.0
    return out


def _dsd_reorder_saved_columns_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """Ticker a meta hneď za checkboxmi — široká tabuľka inak schová symbol vpravo."""
    meta_first = ["Ticker", "Snímka uloženia", "Stratégia ID", "Uložené", "ID"]
    present = [c for c in meta_first if c in df.columns]
    rest = [c for c in df.columns if c not in present]
    return df[present + rest]


def _dsd_apply_times100_display_for_saved(df: pd.DataFrame) -> pd.DataFrame:
    """Uložené záznamy v DB sú v surovej škále; v tabuľke zobrazíme rovnako ako pri hľadaní (×100)."""
    out = df.copy()
    for c in list(out.columns):
        cstr = str(c)
        lc = cstr.lower()
        if "×100" in cstr or "x100" in lc:
            continue
        if "čistá delta" in lc and "theta" not in lc and "vega" not in lc:
            new_c = "Čistá delta ×100"
            out[new_c] = pd.to_numeric(out[c], errors="coerce") * 100.0
            out = out.drop(columns=[c])
        elif "čistá theta" in lc and "vega" not in lc:
            new_c = "Čistá theta (+ príjem / − strata) ×100"
            out[new_c] = pd.to_numeric(out[c], errors="coerce") * 100.0
            out = out.drop(columns=[c])
        elif "čistá vega" in lc and "gamma" not in lc:
            new_c = "Čistá vega ×100"
            out[new_c] = pd.to_numeric(out[c], errors="coerce") * 100.0
            out = out.drop(columns=[c])
    return out


def _default_spot_for_ticker(tk: str) -> float:
    sym = db.get_symbol(str(tk).strip().upper())
    if not sym:
        return 0.0
    try:
        v = float(sym.get("spot") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def _spread_table_column_config(df: pd.DataFrame) -> dict:
    """Formát a šírky stĺpcov (výsledky hľadania aj uložené)."""
    cfg: dict = {}
    for c in df.columns:
        if c in ("Uložiť", "Zmazať", "Do Buildera"):
            _help = (
                "Označ na uloženie"
                if c == "Uložiť"
                else ("Odošli tento riadok do Spread Buildera (presne jeden)" if c == "Do Buildera" else "Označ na zmazanie z DB")
            )
            cfg[c] = st.column_config.CheckboxColumn(c, default=False, help=_help)
        elif c == "ID":
            cfg[c] = st.column_config.NumberColumn(c, format="%d", disabled=True)
        elif c == "Ticker":
            cfg[c] = st.column_config.TextColumn(
                "Ticker",
                width="large",
                help="Podkladový symbol pri uložení riadku.",
            )
        elif c in ("Uložené", "Snímka uloženia", "Stratégia ID"):
            cfg[c] = st.column_config.TextColumn(c, width="small")
        elif c == "Stratégia":
            cfg[c] = st.column_config.TextColumn(c, width="large")
        elif c == "Typ":
            cfg[c] = st.column_config.TextColumn(c, width="small")
        elif "DTE" in c:
            cfg[c] = st.column_config.NumberColumn(c, format="%d", width="small")
        elif "expirácia" in c.lower():
            cfg[c] = st.column_config.TextColumn(c, width="small")
        elif "strike" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.1f", width="small")
        elif "bid" in c.lower() or "ask" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.2f", width="small")
        elif "Debit" in c or "kredit" in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.2f", width="small")
        elif "delta" in c.lower() and "theta" not in c.lower():
            cfg[c] = st.column_config.NumberColumn(
                c, format="%.2f", width="small", help="Z reťazca × 100 (čitateľnejšie)."
            )
        elif "theta" in c.lower():
            cfg[c] = st.column_config.NumberColumn(
                c, format="%.2f", width="medium", help="Z reťazca × 100 (čitateľnejšie)."
            )
        elif "vega" in c.lower() and "gamma" not in c.lower():
            cfg[c] = st.column_config.NumberColumn(
                c, format="%.2f", width="small", help="Z reťazca × 100 (čitateľnejšie)."
            )
        elif "gamma" in c.lower() and "vega" not in c.lower():
            cfg[c] = st.column_config.NumberColumn(c, format="%.4f", width="small")
        elif c == "Skóre":
            cfg[c] = st.column_config.NumberColumn(c, format="%.4f", width="small")
        elif "APR" in c:
            cfg[c] = st.column_config.NumberColumn(
                c,
                format="%.1f",
                width="small",
                help="Orientačné: (čisté theta v USD/deň) / |debit| × 365 × 100. Pri kredite (záp. debit) interpretuj opatrne.",
            )
        else:
            cfg[c] = st.column_config.TextColumn(c, width="medium")
    return cfg


_dsd_inject_tabular_css_once()
_dsd_drain_rehydrate_after_relaxation()
_dsd_drain_pending_dte_suggestion()
_dsd_drain_pending_delta_tolerance()
_dsd_drain_pending_strike_band_suggestion()
_dsd_drain_pending_otm_tuning_suggestion()
st.title("Hľadanie delty — diagonály a kalendáre")
_flash_ok = st.session_state.pop("dsd_flash_success", None)
if _flash_ok:
    st.success(_flash_ok)
st.caption(
    "**Návod:** Najprv import v **DB Grékov**. Vyber **ticker**, **dátum snímky**, **Call/Put**, **Long/Short** a **diagonálu alebo kalendár**; nastav cieľ delty a filtre, spusti **hľadanie**. "
    "**Kalendár** = rovnaký strike na skoršej aj neskoršej expirácii. Výsledky sú z lokálnych `.db` súborov; detailný postup je v expandéri **Manuál**."
)
st.caption(
    "Dáta z **DB Grékov** (`data/option_chains/*.db`). Dva kontrakty rovnakého typu (Call alebo Put): "
    "**skoršia** expirácia = skorší dátum v kalendári, **neskoršia** = neskorší. Gréky sú za long 1 kontrakt; váhy zodpovedajú long/short nohám."
)
st.caption(_DSD_REC_NOTE)

_DSD_MANUAL = Path(__file__).resolve().parent.parent / "docs" / "hladanie-delty-diagonaly.md"
if _DSD_MANUAL.is_file():
    with st.expander("Manuál — ako používať túto stránku", expanded=False):
        st.markdown(_DSD_MANUAL.read_text(encoding="utf-8"))
else:
    st.caption(
        "_Text manuálu sa nenašiel v repozitári (`docs/hladanie-delty-diagonaly.md`)._"
    )

tickers = odb.list_chain_tickers()
if not tickers:
    st.info("Najprv importuj reťazce v **DB Grékov**.")
    st.stop()

# Prepínače stratégie (Call/Put × Long/Short × diagonála/kalendár) — migrácia zo starého ``dsd_strat``.
if "dsd_opt_right" not in st.session_state:
    _leg = str(st.session_state.get("dsd_strat", "long_call_diagonal")).strip()
    st.session_state["dsd_opt_right"] = "Put" if "put" in _leg else "Call"
    st.session_state["dsd_opt_ls"] = "short" if _leg.startswith("short_") else "long"
if "dsd_opt_shape" not in st.session_state:
    st.session_state["dsd_opt_shape"] = "diagonal"

c1, c2, c3 = st.columns([1, 1, 1])
with c1:
    ticker = st.selectbox(
        "Ticker",
        options=tickers,
        key="dsd_ticker",
        on_change=_dsd_on_ticker_change,
        help="Pri zmene tickera sa načítajú **Pokročilé filtre** uložené pri poslednom úspešnom hľadaní pre daný symbol (ak existujú).",
    )
with c2:
    dates = dss.list_as_of_dates(ticker)
    if not dates:
        st.warning("Pre tento ticker nie sú v DB žiadne snímky.")
        st.stop()
    as_of = st.selectbox("Dátum snímky (as-of)", options=dates, key="dsd_asof")
with c3:
    st.radio(
        "Call / Put",
        ["Call", "Put"],
        horizontal=True,
        key="dsd_opt_right",
    )
    st.radio(
        "Smer stratégie",
        ["long", "short"],
        horizontal=True,
        key="dsd_opt_ls",
        format_func=lambda x: (
            "Long (+ďaleká exp. −blízka exp.)" if x == "long" else "Short (−ďaleká exp. +blízka exp.)"
        ),
    )
    st.radio(
        "Tvar spreadu",
        ["diagonal", "calendar"],
        horizontal=True,
        key="dsd_opt_shape",
        format_func=lambda x: (
            "Diagonála (rôzne striky)" if x == "diagonal" else "Kalendár (rovnaký strike)"
        ),
        help="**Kalendár** ponechá len kombinácie, kde je strike na skoršej a neskoršej expirácii **rovnaký**.",
    )

_right = str(st.session_state.get("dsd_opt_right", "Call"))
_ls = str(st.session_state.get("dsd_opt_ls", "long"))
_shape = str(st.session_state.get("dsd_opt_shape", "diagonal"))
strategy = f"{_ls}_{'call' if _right == 'Call' else 'put'}_diagonal"
_require_same_strike = _shape == "calendar"

st.markdown(
    dss.STRATEGIES[strategy].label_sk
    + (" · **Kalendár:** rovnaký strike na oboch expiráciách." if _require_same_strike else "")
    + " — v kalendári: **skoršia** expirácia = skorší dátum, **neskoršia** = neskorší dátum (viď DTE filtre nižšie, ktorá noha je short/long podľa váh)."
)

c4, c5, c6 = st.columns([1, 1, 1])
with c4:
    target_d = st.number_input(
        "Cieľová čistá delta",
        value=0.0,
        step=0.01,
        format="%.4f",
        help=(
            "**Cieľ** v jednotkách **reťazca** (0–1), nie horná medz dát. V tabuľke výsledkov je **Čistá delta ×100** len na čítanie. "
            "Neutrálna delta = **0** (predvolene). Triedenie: najprv najmenšia |čistá delta − cieľ|, potom vyššia čistá theta. "
            "Pás ±2 okolo nuly: cieľ **0** a v Pokročilých **Tolerancia delty** max. **2**."
        ),
        key="dsd_target",
    )
with c5:
    top_n = st.number_input("Max. počet výsledkov", min_value=5, max_value=200, value=40, step=5, key="dsd_topn")
with c6:
    max_k = st.number_input(
        "Max. strike-ov na expiráciu (výkon)",
        min_value=15,
        max_value=120,
        value=55,
        step=5,
        key="dsd_maxk",
    )

spot_display = st.number_input(
    "Spot (pre OTM short; z Symbolov)",
    min_value=0.0,
    value=float(_default_spot_for_ticker(ticker)),
    step=0.25,
    format="%.2f",
    help="Ak je > 0, dajú sa filtre **min. OTM short** a **min. OTM long** (pomer k spotu). Hodnota z **Symbolov**; môžeš prepísať. **0** = OTM filtre sa neaplikujú.",
    key=f"dsd_spot_{str(ticker).strip().upper()}",
)

use_strike_band = st.checkbox(
    "Obmedziť rozsah strike (obidve nohy musia byť v intervale)",
    value=False,
    key="dsd_strike_band",
)
c7, c8 = st.columns(2)
with c7:
    strike_od = st.number_input(
        "Strike od",
        min_value=0.0,
        value=100.0,
        step=1.0,
        format="%.1f",
        disabled=not use_strike_band,
        key="dsd_strike_min",
    )
with c8:
    strike_do = st.number_input(
        "Strike do",
        min_value=0.0,
        value=500.0,
        step=1.0,
        format="%.1f",
        disabled=not use_strike_band,
        key="dsd_strike_max",
    )

with st.expander("Pokročilé filtre a režim triedenia", expanded=False):
    st.markdown(_DSD_REC_NOTE)
    _b1, _b2 = st.columns(2)
    with _b1:
        if st.button("Striktné predvolby (klasika)", type="secondary", key="dsd_reset_strict"):
            _dsd_reset_strict_filters()
            st.rerun()
    with _b2:
        if st.button("Širšie filtre (ETF / kratší reťazec)", type="secondary", key="dsd_reset_relaxed"):
            _dsd_apply_relaxed_filters()
            st.rerun()
    rank_mode = st.radio(
        "Triedenie",
        options=["legacy", "score", "theta_delta_debit"],
        horizontal=True,
        format_func=lambda x: (
            "Klasické (delta → theta)"
            if x == "legacy"
            else ("Skóre (potom delta)" if x == "score" else "Theta → delta → debit")
        ),
        key="dsd_rank_mode",
    )
    ex1, ex2 = st.columns(2)
    with ex1:
        use_delta_tol = st.checkbox(
            "Tolerancia |čistá delta − cieľ| (odporúč.: ±2 okolo cieľa)",
            value=True,
            key="dsd_use_dt",
        )
        delta_tol = st.number_input(
            "Max. odchýlka delty (odporúč. 2)",
            min_value=0.0,
            value=2.0,
            step=0.1,
            format="%.4f",
            disabled=not use_delta_tol,
            key="dsd_delta_tol",
        )
    with ex2:
        theta_scale_contracts = st.checkbox(
            "Theta prah × 100 (striktné predvolby: min/max **3–8** pri ×100)",
            value=True,
            help="Ak je zapnuté, prahy Min./Max. čistá theta sa porovnávajú s **net_theta × 100**.",
            key="dsd_theta_scale",
        )
        use_theta_min = st.checkbox("Min. čistá theta (striktné: **3** pri ×100)", value=True, key="dsd_use_tmin")
        use_theta_max = st.checkbox("Max. čistá theta (striktné: **8** pri ×100)", value=True, key="dsd_use_tmax")
        net_theta_min = st.number_input(
            "Min. čistá theta",
            value=3.0,
            step=0.1,
            format="%.4f",
            disabled=not use_theta_min,
            key="dsd_net_theta_min",
        )
        net_theta_max = st.number_input(
            "Max. čistá theta",
            value=8.0,
            step=0.1,
            format="%.4f",
            disabled=not use_theta_max,
            key="dsd_net_theta_max",
        )
    ex3, ex4 = st.columns(2)
    with ex3:
        st.markdown("**Čistá vega** (striktné: **0,10–0,20** v jednotkách reťazca / akciu)")
        use_nvmin = st.checkbox("Min.", value=True, key="dsd_use_nvmin")
        net_vega_min = st.number_input(
            "vega min",
            value=0.10,
            step=0.01,
            format="%.4f",
            disabled=not use_nvmin,
            key="dsd_nvmin",
            label_visibility="collapsed",
            help="Z importu, **na 1 akciu**. Pri 0 výsledkoch skús širšie filtre alebo zníž minimum.",
        )
        use_nvmax = st.checkbox("Max.", value=True, key="dsd_use_nvmax")
        net_vega_max = st.number_input(
            "vega max",
            value=0.20,
            step=0.01,
            format="%.4f",
            disabled=not use_nvmax,
            key="dsd_nvmax",
            label_visibility="collapsed",
        )
    with ex4:
        st.markdown("**Čistá gamma** (odporúč.: **−0,03 … 0**)")
        use_ngmin = st.checkbox("Min.", value=True, key="dsd_use_ngmin")
        net_gamma_min = st.number_input(
            "gamma min",
            value=-0.03,
            step=0.001,
            format="%.4f",
            disabled=not use_ngmin,
            key="dsd_ngmin",
            label_visibility="collapsed",
        )
        use_ngmax = st.checkbox("Max.", value=True, key="dsd_use_ngmax")
        net_gamma_max = st.number_input(
            "gamma max",
            value=0.0,
            step=0.001,
            format="%.4f",
            disabled=not use_ngmax,
            key="dsd_ngmax",
            label_visibility="collapsed",
        )
    st.markdown("**DTE (dni od snímky)** (striktné: skoršia **40–55**, neskoršia **90–140**)")
    d1, d2, d3, d4 = st.columns(4)
    with d1:
        use_dnmin = st.checkbox("Skoršia min", value=True, key="dsd_use_dnmin", help="Skorší dátum expirácie v páre (nižší DTE vs. neskoršia noha, ak sú obe v budúcnosti).")
        dte_near_min = st.number_input("dte_n_min", min_value=0, value=40, step=1, disabled=not use_dnmin, key="dsd_dnmin", label_visibility="collapsed")
    with d2:
        use_dnmax = st.checkbox("Skoršia max", value=True, key="dsd_use_dnmax", help="Horná hranica DTE pre skoršiu expiráciu v kalendári.")
        dte_near_max = st.number_input("dte_n_max", min_value=0, value=55, step=1, disabled=not use_dnmax, key="dsd_dnmax", label_visibility="collapsed")
    with d3:
        use_dfmin = st.checkbox("Neskoršia min", value=True, key="dsd_use_dfmin", help="Spodná hranica DTE pre neskoršiu expiráciu v kalendári.")
        dte_far_min = st.number_input("dte_f_min", min_value=0, value=90, step=1, disabled=not use_dfmin, key="dsd_dfmin", label_visibility="collapsed")
    with d4:
        use_dfmax = st.checkbox("Neskoršia max", value=True, key="dsd_use_dfmax", help="Horná hranica DTE pre neskoršiu expiráciu v kalendári.")
        dte_far_max = st.number_input("dte_f_max", min_value=0, value=140, step=1, disabled=not use_dfmax, key="dsd_dfmax", label_visibility="collapsed")
    if dss.STRATEGIES[strategy].w_near < 0:
        st.caption(
            "Pri **long call/put** diagonáli je noha **Short** na **skoršej** expirácii a **Long** na neskoršej — "
            "pásma *Skoršia min/max* = stĺpec **Short — DTE**, *Neskoršia* = **Long — DTE**."
        )
    else:
        st.caption(
            "Pri **short call/put** diagonáli je **Short** na **neskoršej** expirácii a **Long** na skoršej — "
            "pásma *Neskoršia min/max* = stĺpec **Short — DTE**; *Skoršia* = **Long — DTE**."
        )
    st.selectbox(
        "Strike k spotu (|K−spot|) — ktorá noha má *menšie* |strike − spot| (vyžaduje spot > 0)",
        options=["long", "short", "none"],
        key="dsd_strike_prox_leg",
        format_func=lambda x: {
            "long": "Long (predvolené) — táto noha bližšie k spotu",
            "short": "Short — táto noha bližšie k spotu",
            "none": "Bez filtra",
        }[x],
    )
    st.caption(
        "Bez **spotu** (hore) sa tento filter neaplikuje. Pri **OTM call** je zvyčajne *nižší* strike *bližšie* k spotu; "
        "u **long call** diagonálu je long na neskoršej expirácii — ak je long strike vyšší ako short, long často **nie** je bližšie. "
        "Nastavenie sa pri **automatickom zjemnení** filtrov nemení (rovnako ako DTE)."
    )
    st.checkbox(
        "Pri 0 výsledkoch **nezjemňovať** OTM short/long (iba postupné zjemnenie po filtroch)",
        value=False,
        key="dsd_relax_exclude_otm",
        help="Kombinovaná 2. fáza **OTM nikdy nemení** (vždy podľa vstupu). Zapni, ak nechceš znižovať OTM ani v **1. fáze** (postupne po jednom filtri).",
    )
    liq1, liq2, liq3 = st.columns(3)
    with liq1:
        use_otm = st.checkbox(
            "Min. OTM short (odporúč.: **10 %** = 0,10; treba spot > 0)",
            value=True,
            key="dsd_use_otm",
        )
        short_otm_min = st.number_input(
            "Min. OTM",
            min_value=0.0,
            value=0.10,
            step=0.01,
            format="%.4f",
            disabled=not use_otm or float(spot_display) <= 0,
            help="Call: (strike_short − spot)/spot; Put: (spot − strike_short)/spot. **10 %** = zadaj **0,10**. Vyžaduje spot > 0.",
            key="dsd_otm_min",
        )
        use_otm_long = st.checkbox(
            "Min. OTM long (put/call rovnaký vzorec ako short; **odporúč. zapnuté** pri long diagonáli)",
            value=True,
            key="dsd_use_otm_long",
        )
        long_otm_min = st.number_input(
            "Min. OTM long",
            min_value=0.0,
            value=0.10,
            step=0.01,
            format="%.4f",
            disabled=not use_otm_long or float(spot_display) <= 0,
            help="Call: (strike_long − spot)/spot; Put: (spot − strike_long)/spot. Vyžaduje spot > 0. Vypnutím ponecháš aj **ITM** dlhú nohu (ak sú v DB).",
            key="dsd_otm_long_min",
        )
    with liq2:
        use_debit_ratio = st.checkbox(
            "Max. |debit|/šírka strike (odporúč.: **25 %** = 0,25)",
            value=True,
            key="dsd_use_dratio",
        )
        max_debit_to_strike_width_ratio = st.number_input(
            "Pomer",
            min_value=0.0,
            value=0.25,
            step=0.01,
            disabled=not use_debit_ratio,
            key="dsd_dratio",
        )
    with liq3:
        use_iv_sl = st.checkbox("IV short ≥ IV long", value=False, key="dsd_use_ivsl")
        iv_margin = st.number_input(
            "Marža (short − long ≥)",
            min_value=0.0,
            value=0.0,
            step=0.005,
            format="%.4f",
            disabled=not use_iv_sl,
            key="dsd_iv_margin",
        )
    spr1, spr2 = st.columns(2)
    with spr1:
        use_rss = st.checkbox("Max. rel. spread short (odporúč.: **0,08**)", value=True, key="dsd_use_rss")
        max_rel_spread_short = st.number_input("short", min_value=0.0, value=0.08, step=0.01, disabled=not use_rss, key="dsd_rss", label_visibility="collapsed")
    with spr2:
        use_rsl = st.checkbox("Max. rel. spread long (odporúč.: **0,05**)", value=True, key="dsd_use_rsl")
        max_rel_spread_long = st.number_input("long", min_value=0.0, value=0.05, step=0.01, disabled=not use_rsl, key="dsd_rsl", label_visibility="collapsed")
    vol1, vol2 = st.columns(2)
    with vol1:
        use_minoi = st.checkbox("Min. open interest (odporúč.: **100** obe nohy)", value=True, key="dsd_use_minoi")
        min_open_interest = st.number_input(
            "OI",
            min_value=0,
            value=100,
            step=10,
            disabled=not use_minoi,
            key="dsd_minoi",
            label_visibility="collapsed",
            help="Ak je OI v DB prázdny (CSV ho nemá), riadok sa **týmto** filtrom nevyradí; ak je vyplnený, musí byť ≥ prah na oboch nohách.",
        )
    with vol2:
        use_minvol = st.checkbox("Min. volume (obidve nohy)", value=False, key="dsd_use_minvol")
        min_volume = st.number_input("Volume", min_value=0, value=10, step=5, disabled=not use_minvol, key="dsd_minvol", label_visibility="collapsed")

st.checkbox(
    "Pri hľadaní uložiť kompletný protokol aj do súboru (`data/delta_search_debug/`)",
    value=False,
    key="dsd_debug_protocol_disk",
    help="Protokol (Markdown) sa po každom hľadaní vždy uloží do session a zobrazí v expanderi nižšie — vhodné na kopírovanie. Zapnutím pridáš zápis timestampovaného .md na disk.",
)
_dsd_run_search_now = bool(st.session_state.pop("dsd_pending_run_search", False))
if st.button("Hľadať", type="primary", key="dsd_run") or _dsd_run_search_now:
    try:
        smin = float(strike_od) if use_strike_band else None
        smax = float(strike_do) if use_strike_band else None
        spot_f = float(spot_display)
        _prox = str(st.session_state.get("dsd_strike_prox_leg", "long"))
        if _prox == "none":
            _strike_leg = None
        elif _prox == "short":
            _strike_leg = "short"
        else:
            _strike_leg = "long"
        search_opts = dss.DiagonalSearchOptions(
            spot=spot_f if spot_f > 0 else None,
            rank_mode=rank_mode,  # type: ignore[arg-type]
            delta_tolerance=float(delta_tol) if use_delta_tol else None,
            net_theta_min=float(net_theta_min) if use_theta_min else None,
            net_theta_max=float(net_theta_max) if use_theta_max else None,
            theta_scale_contracts=bool(theta_scale_contracts),
            net_vega_min=float(net_vega_min) if use_nvmin else None,
            net_vega_max=float(net_vega_max) if use_nvmax else None,
            net_gamma_min=float(net_gamma_min) if use_ngmin else None,
            net_gamma_max=float(net_gamma_max) if use_ngmax else None,
            dte_near_min=int(dte_near_min) if use_dnmin else None,
            dte_near_max=int(dte_near_max) if use_dnmax else None,
            dte_far_min=int(dte_far_min) if use_dfmin else None,
            dte_far_max=int(dte_far_max) if use_dfmax else None,
            short_otm_min=float(short_otm_min) if use_otm and spot_f > 0 else None,
            long_otm_min=float(long_otm_min) if use_otm_long and spot_f > 0 else None,
            max_debit_to_strike_width_ratio=float(max_debit_to_strike_width_ratio) if use_debit_ratio else None,
            max_rel_spread_short=float(max_rel_spread_short) if use_rss else None,
            max_rel_spread_long=float(max_rel_spread_long) if use_rsl else None,
            min_open_interest=int(min_open_interest) if use_minoi else None,
            min_volume=int(min_volume) if use_minvol else None,
            require_iv_short_ge_long=bool(use_iv_sl),
            iv_short_ge_long_margin=float(iv_margin) if use_iv_sl else 0.0,
            strike_proximity_leg=_strike_leg,
            relax_exclude_otm=bool(st.session_state.get("dsd_relax_exclude_otm", False)),
            require_same_strike=bool(_require_same_strike),
        )
        _precheck = dss.diagonal_search_precheck_warnings_markdown(
            ticker, as_of_date=as_of, strategy=strategy, opt=search_opts
        )
        if _precheck:
            st.warning(_precheck)
        with st.spinner("Počítam kombinácie expirácií a strike-ov…"):
            res, filter_log, effective_opt = dss.progressive_filter_search(
                ticker,
                as_of_date=as_of,
                strategy=strategy,
                target_net_delta=float(target_d),
                top_n=int(top_n),
                max_strikes_per_expiry=int(max_k),
                strike_min=smin,
                strike_max=smax,
                options=search_opts,
                max_relax_iterations=3,
            )
        st.session_state["dsd_last_filter_log"] = filter_log
        try:
            _full_proto = dss.build_delta_search_protocol_markdown(
                ticker=ticker,
                as_of_date=as_of,
                strategy=strategy,
                target_net_delta=float(target_d),
                top_n=int(top_n),
                max_strikes_per_expiry=int(max_k),
                strike_min=smin,
                strike_max=smax,
                initial_options=search_opts,
                effective_options=effective_opt,
                filter_log=filter_log,
                result=res,
            )
        except Exception as _proto_exc:
            _full_proto = f"# Protokol (chýba)\n\nNepodarilo sa zostaviť protokol: `{_proto_exc}`"
        st.session_state["dsd_last_protocol_md"] = _full_proto
        st.session_state["dsd_last_protocol_path"] = ""
        if st.session_state.get("dsd_debug_protocol_disk", False):
            try:
                _p_saved = dss.write_delta_search_protocol_to_data_dir(_full_proto, str(ticker))
                st.session_state["dsd_last_protocol_path"] = str(_p_saved)
            except OSError as _wexc:
                st.session_state["dsd_last_protocol_path"] = f"(chyba zápisu: {_wexc})"
        if res.empty:
            st.session_state["dsd_last_results"] = None
            st.session_state["dsd_last_meta"] = {}
            st.session_state["dsd_post_search_is_empty"] = True
            st.session_state["dsd_empty_context"] = {
                "ticker": str(ticker).strip(),
                "as_of": str(as_of).strip(),
                "strategy": strategy,
                "initial_opt": dataclasses.asdict(search_opts),
                "effective_opt": dataclasses.asdict(effective_opt),
            }
        else:
            st.session_state["dsd_post_search_is_empty"] = False
            st.session_state.pop("dsd_empty_context", None)
            st.session_state["dsd_last_results"] = res
            tk_u = str(ticker).strip().upper()
            st.session_state.setdefault("dsd_per_ticker_opts", {})[tk_u] = dataclasses.asdict(effective_opt)
            _succ = (
                f"Nájdených **{len(res)}** kombinácií — zoradené podľa **skóre** (stĺpec Skóre), potom presnosť delty."
                if rank_mode == "score"
                else (
                    f"Nájdených **{len(res)}** kombinácií — zoradené podľa **čistej theta** (najvyššia prvá), "
                    f"potom **odchýlky delty** od cieľa, potom **debitu** (nižší skôr)."
                    if rank_mode == "theta_delta_debit"
                    else f"Nájdených **{len(res)}** najlepších kombinácií — zoradené podľa **odchýlky delty**, potom **čistej theta**."
                )
            )
            st.session_state["dsd_last_meta"] = {
                "ticker": ticker,
                "as_of": as_of,
                "strategy": strategy,
                "rank_mode": rank_mode,
                "spot": spot_f if spot_f > 0 else None,
                "relax_summary": (filter_log.summary_sk() or None) if filter_log.any_relaxed else None,
                "filter_log_steps": [dataclasses.asdict(s) for s in filter_log.steps],
            }
            if filter_log.steps:
                _last_ok = filter_log.steps[-1]
                st.success(
                    f"Brána prešla až po **{_last_ok.name}**. "
                    "Ak chceš, môžeš vyladiť ďalší filter podľa protokolu nižšie."
                )
            else:
                st.success("DTE prešlo a hľadanie našlo výsledky bez zjemnenia ďalších filtrov.")
            if filter_log.any_relaxed:
                st.session_state["dsd_flash_success"] = _succ
                st.session_state["dsd_pending_rehydrate_after_relax"] = tk_u
                st.rerun()
            st.success(_succ)
    except Exception as exc:
        st.error(f"Chyba: {type(exc).__name__}: {exc}")
        st.stop()

_dsd_render_empty_search_panel()

_dsd_proto = st.session_state.get("dsd_last_protocol_md")
if _dsd_proto:
    with st.expander("📋 Kompletný protokol posledného hľadania (Markdown — kopíruj alebo stiahni)", expanded=False):
        _pp = (st.session_state.get("dsd_last_protocol_path") or "").strip()
        if _pp and not _pp.startswith("("):
            st.caption(f"Uložený súbor: `{_pp}`")
        elif _pp:
            st.caption(_pp)
        st.download_button(
            label="Stiahnuť protokol ako .md",
            data=_dsd_proto.encode("utf-8"),
            file_name="delta_search_protokol.md",
            mime="text/markdown",
            key="dsd_proto_download",
        )
        st.code(_dsd_proto, language="markdown")

res = st.session_state.get("dsd_last_results")
meta = st.session_state.get("dsd_last_meta") or {}
last_log = st.session_state.get("dsd_last_filter_log")
if res is not None and not res.empty:
    _rm = meta.get("rank_mode") or "legacy"
    _tried = (
        "skóre"
        if _rm == "score"
        else ("theta → delta → debit" if _rm == "theta_delta_debit" else "delta → theta")
    )
    _rsum = (meta.get("relax_summary") or "").strip()
    if _rsum:
        st.info(_rsum)
    _fsteps = meta.get("filter_log_steps") or []
    if _fsteps:
        with st.expander("Protokol posledného hľadania (automatické zjemnenie)", expanded=False):
            for i, row in enumerate(_fsteps, start=1):
                st.markdown(
                    f"{i}. **{row.get('name', '')}** — {row.get('original')} → **{row.get('relaxed_to')}** "
                    f"(riadkov: {row.get('rows_after', '')})"
                )
    if last_log:
        fs = getattr(last_log, "failure_steps", None) or []
        if last_log.final_rows > 0:
            if fs:
                st.success(
                    f"Brána prešla: **{fs[0].label if fs else 'DTE'}** a hľadanie pokračovalo ďalej."
                )
            else:
                st.success("Brána prešla: DTE aj ďalšie filtre našli výsledky bez zlyhania.")
        else:
            if fs:
                st.error(f"Brána zlyhala: **{fs[0].label}** (`{fs[0].field}`).")
            else:
                st.error("Brána zlyhala: bez výsledku, ale bez detailného trace v poslednom logu.")
        with st.expander("Brány — prehľad prešli / neprešli", expanded=False):
            gate_lines: list[str] = []
            failed_fields = {str(s.field) for s in fs}
            step_names = [str(getattr(s, "name", "")).strip() for s in (getattr(last_log, "steps", None) or []) if str(getattr(s, "name", "")).strip()]
            if last_log.final_rows > 0 and step_names:
                for name in step_names:
                    gate_lines.append(f"- ✅ **{name}**")
            elif last_log.final_rows > 0:
                gate_lines.append("- ✅ **DTE**")
            elif fs:
                for row in fs:
                    status = "❌" if row.field in failed_fields else "✅"
                    gate_lines.append(f"- {status} **{row.label}** (`{row.field}`)")
            else:
                gate_lines.append("- ❌ Bez detailu o bráne v logu")
            st.markdown("\n".join(gate_lines))
    _sid = str(meta.get("strategy") or strategy or "")
    if _sid in dss.STRATEGIES:
        st.markdown(
            f"**Stratégia (rovnaká pre celú tabuľku):** {dss.STRATEGIES[_sid].label_sk}"
        )
    st.caption(
        f"_Posledné hľadanie: **{meta.get('ticker', '')}** · snímka **{meta.get('as_of', '')}** · "
        f"triedenie **{_tried}** — **Uložiť** je vpravo v poslednom stĺpci; potvrď tlačidlom nižšie._"
    )
    st.caption(
        "**Debit/kredit ($/1 lot ×100)** = (Long ask − Short bid) × **100** pri otvorení jedného kontraktu "
        "(kladné = platíš debit, záporné = berieš kredit v USD za lot)."
    )
    st.caption(
        "**Čistá delta**, **čistá theta** a **čistá vega** v tabuľke sú **hodnota z reťazca × 100** (lepšia čitateľnosť). "
        "Filtre v Pokročilých stále používajú **pôvodnú škálu** z DB (bez tohto násobku v stĺpci)."
    )
    st.caption(
        "**Čistá theta:** **+** = decay v tvoj prospech (zjednodušene ako *denný príjem* z theta v týchto jednotkách), "
        "**−** = decay proti tebe (*denná strata*). **Riadky so zápornou čistou thetou hľadanie nezobrazuje** (Pokročilé stále umožnia zužovať hornú/dolnú hranu medzi *kladnými*). Ide o model z reťazca, nie hotovosť."
    )
    st.caption(
        "**APR % (rát.):** hrubý pomer očakávaného denného theta (USD/deň na 1 lot) k absolútnej veľkosti debetu; na porovnanie kandidátov, nie zaručený výnos."
    )
    _spot_meta = meta.get("spot")
    if _spot_meta is not None:
        try:
            _spot_num = float(_spot_meta)
        except (TypeError, ValueError):
            _spot_num = None
        if _spot_num is not None and _spot_num > 0:
            _tk = str(meta.get("ticker") or "").strip().upper() or "—"
            st.markdown(
                f"**Spot podkladu ({_tk}):** **{_spot_num:,.2f}** USD — referencia k strikom v tabuľke."
            )
        else:
            st.caption("Spot podkladu pri tomto hľadaní nie je k dispozícii (skontroluj vstup spotu vo formulári alebo dáta v DB).")
    else:
        st.caption("Spot podkladu pri tomto hľadaní nie je v meta (zvyčajne treba zadať spot vo formulári).")
    edit_df = res.copy()
    edit_df["Uložiť"] = False
    _cols = [c for c in edit_df.columns if c != "Uložiť"] + ["Uložiť"]
    edit_df = edit_df[_cols]
    edited = st.data_editor(
        edit_df,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in edit_df.columns if c != "Uložiť"],
        column_config=_spread_table_column_config(edit_df),
        key=_dsd_data_editor_key("dsd_res", edit_df),
    )
    if st.button("Uložiť označené riadky do lokálnej DB", type="secondary", key="dsd_save_rows"):
        picked = edited.loc[edited["Uložiť"] == True].drop(columns=["Uložiť"], errors="ignore")
        picked = picked.drop(
            columns=[c for c in picked.columns if str(c).startswith("APR %")],
            errors="ignore",
        )
        if picked.empty:
            st.warning("Nie je označený žiadny riadok (stĺpec **Uložiť**).")
        else:
            n = sdiag.save_rows(
                meta.get("ticker", ticker),
                meta.get("as_of", as_of),
                meta.get("strategy", strategy),
                _dsd_strip_times100_display_for_storage(picked),
            )
            st.success(f"Uložených **{n}** riadkov do `{sdiag.db_path()}`.")
            st.rerun()

st.divider()
st.markdown("##### Uložené diagonály")
st.caption(
    f"Súbor: `{sdiag.db_path()}` — mimo `journal.db`. Stĺpec **Ticker** je symbol podkladu z momentu uloženia."
)
saved_df = sdiag.list_saved()
if saved_df.empty:
    st.info("Zatiaľ nemáš uložené žiadne riadky z tejto stránky.")
else:
    del_df = _dsd_apply_times100_display_for_saved(saved_df.copy())
    del_df = _dsd_reorder_saved_columns_for_display(del_df)
    del_df.insert(0, "Zmazať", False)
    del_df.insert(1, "Do Buildera", False)
    edited_saved = st.data_editor(
        del_df,
        use_container_width=True,
        hide_index=True,
        disabled=[c for c in del_df.columns if c not in ("Zmazať", "Do Buildera")],
        column_config=_spread_table_column_config(del_df),
        key=_dsd_data_editor_key("dsd_sav", del_df),
    )
    c_cmp, c_sb, c_del = st.columns([1, 1, 1])
    with c_cmp:
        if st.button(
            "Porovnať zaškrtnuté (2+ v Do Buildera)",
            type="secondary",
            help="Zaškrtni aspoň dva riadky v stĺpci Do Buildera — zobrazí sa poradie a stručné dôvody pre 1. a 2. miesto pred odoslaním do Buildera.",
            key="dsd_cmp_saved",
        ):
            selected = edited_saved.loc[edited_saved["Do Buildera"] == True]
            n_sel = int(len(selected))
            if n_sel < 2:
                st.session_state.pop("_dsd_last_compare", None)
                st.warning("Na porovnanie potrebuješ aspoň **2** riadky so zaškrknutým **Do Buildera**.")
            else:
                drop = [c for c in ("Zmazať", "Do Buildera") if c in selected.columns]
                sub = selected.drop(columns=drop, errors="ignore")
                dfc, md, protocol = sdc.compare_saved_diagonals(sub)
                st.session_state["_dsd_last_compare"] = {
                    "md": md,
                    "table": dfc,
                    "protocol": protocol,
                }
                st.session_state["dsd_compare_agent_chat"] = []
                _dsd_save_compare_agent_chat([])
    with c_sb:
        if st.button("Odoslať do Spread Buildera", type="primary", key="dsd_send_sb"):
            send_rows = edited_saved.loc[edited_saved["Do Buildera"] == True]
            if len(send_rows) != 1:
                st.error("Označ **presne jeden** riadok v stĺpci **Do Buildera** (rovnaký postup ako CSV Varianty → top 1).")
            else:
                row_sb = send_rows.iloc[0]
                tk0 = str(row_sb.get("Ticker") or "").strip().upper()
                if not tk0:
                    st.error("V riadku chýba **Ticker**.")
                else:
                    sk = row_sb.get("Short — strike")
                    lk = row_sb.get("Long — strike")
                    hint = None
                    try:
                        if pd.notna(sk) and pd.notna(lk):
                            hint = (float(sk) + float(lk)) / 2.0
                    except (TypeError, ValueError):
                        hint = None
                    tk, spot, iv = ticker_spot_iv_for_diagonal_send(tk0, strike_hint=hint)
                    legs, leg_err, csv_notice = diagonal_legs_from_saved_display_row(
                        row_sb, spot=float(spot), iv=float(iv), contracts=1
                    )
                    if leg_err:
                        st.error(leg_err)
                    else:
                        parts = [f"Načítané z **uložených diagonál** ({tk})."]
                        if csv_notice:
                            parts.append(str(csv_notice))
                        note = " ".join(parts)
                        st.session_state["_sb_pending_patch"] = {
                            "op": "csv_calendar_variant",
                            "ticker": tk,
                            "spot": float(spot),
                            "iv": float(iv),
                            "legs": legs,
                            "notice": note,
                        }
                        try:
                            st.switch_page("pages/spread_builder.py")
                        except Exception:
                            st.success(
                                "Údaje sú pripravené v session. Otvor v menu **Spread Builder** — nohy sa doplnia automaticky."
                            )
    with c_del:
        if st.button("Odstrániť označené z DB", key="dsd_delete_saved"):
            ids = edited_saved.loc[edited_saved["Zmazať"] == True, "ID"].dropna().astype(int).tolist()
            if not ids:
                st.warning("Označ aspoň jeden riadok v stĺpci **Zmazať**.")
            else:
                k = sdiag.delete_by_ids(ids)
                st.session_state.pop("_dsd_last_compare", None)
                st.session_state["dsd_compare_agent_chat"] = []
                _dsd_save_compare_agent_chat([])
                st.success(f"Odstránených záznamov: **{k}**.")
                st.rerun()
    if st.session_state.get("_dsd_last_compare"):
        with st.expander("Porovnanie — odporúčané 1. a 2. miesto (pred odoslaním do Buildera)", expanded=True):
            p = st.session_state["_dsd_last_compare"]
            st.markdown(p.get("md") or "")
            _tdf = p.get("table")
            if _tdf is not None and not (hasattr(_tdf, "empty") and _tdf.empty):
                st.caption(
                    "Porovnanie je **kompozitné**: bázové skóre (z hľadania alebo heuristika) **+ kvalita Short — bid** — pri nízkom bide (napr. 0,20) "
                    "môže iný riadok so silnejším bidom preskočiť čisto číselne lepšie Skóre. Pri zmene zaškrtnutia klikni **Porovnať** znova."
                )
                st.dataframe(_tdf, use_container_width=True, hide_index=True)
        _proto = (st.session_state.get("_dsd_last_compare") or {}).get("protocol") or ""
        if _proto.strip():
            with st.expander("Protokol — parametre, metodika, vstup a výsledok (Markdown na skopírovanie)", expanded=False):
                st.caption(
                    "Obsahuje čas (UTC), popis kompozitu, CSV vstupných riadkov a tabuľku výsledku. Skopíruj z poľa nižšie alebo stiahni `.md`."
                )
                st.text_area(
                    "Protokol (Markdown)",
                    value=_proto,
                    height=420,
                    key="dsd_compare_protocol_ta",
                    label_visibility="collapsed",
                )
                st.download_button(
                    label="Stiahnuť protokol (.md)",
                    data=_proto.encode("utf-8"),
                    file_name=f"porovnanie_ulozenych_diagonal_{date.today().strftime('%Y%m%d')}.md",
                    mime="text/markdown; charset=utf-8",
                    key="dsd_compare_protocol_dl",
                )
        if _proto.strip():
            with st.expander("🤖 Konzultácia s agentom (porovnanie viacerých záznamov)", expanded=False):
                st.caption(
                    "Rovnaký princíp ako v **Spread Builderi** (model + prvá analýza + chat), ale kontextom je **celý protokol** z porovnania, "
                    "nie jeden rozvinutý spread. Pri novom kliku na **Porovnať** sa chat vymaže, aby sedel k čerstvému protokolu."
                )
                _cm = list(ai_agent.AVAILABLE_MODELS.keys())
                _cl = [ai_agent.AVAILABLE_MODELS[m]["label"] for m in _cm]
                _sm = st.session_state.get("selected_claude_model", "claude-sonnet-4-6")
                _ix = _cm.index(_sm) if _sm in _cm else 1
                _aiq = st.text_area(
                    "Úvodná otázka / doplňujúci kontext (voliteľné)",
                    placeholder="napr. Mám uprednostniť likviditu pred 1. miestom v tabuľke?",
                    height=68,
                    key="dsd_cmp_ai_question",
                )
                c_ai1, c_ai2 = st.columns([2, 1])
                with c_ai1:
                    _si2 = st.selectbox(
                        "Model",
                        options=range(len(_cm)),
                        format_func=lambda i: _cl[i],
                        index=_ix,
                        key="dsd_cmp_ai_model",
                    )
                    st.session_state["selected_claude_model"] = _cm[_si2]
                with c_ai2:
                    st.write("")
                    st.write("")
                    _run_cmp_ai = st.button("🚀 Nová analýza (protokol)", type="primary", key="dsd_cmp_ai_run", use_container_width=True)
                c_clr1, c_clr2 = st.columns([3, 1])
                with c_clr2:
                    if st.button("🗑 Vymazať chat", key="dsd_cmp_ai_clear", use_container_width=True):
                        st.session_state["dsd_compare_agent_chat"] = []
                        _dsd_save_compare_agent_chat([])
                        st.rerun()
                if _run_cmp_ai:
                    with st.spinner("Agent analyzuje porovnanie…"):
                        try:
                            _prompt = ai_agent.build_diagonal_compare_analysis_prompt(
                                (st.session_state.get("_dsd_last_compare") or {}).get("protocol") or "",
                                user_note=(_aiq or "").strip(),
                            )
                            _clt = ai_agent._load_client()
                            _m = st.session_state.get("selected_claude_model")
                            _minfo = ai_agent.AVAILABLE_MODELS.get(_m, {})
                            _mtok = _minfo.get("max_tokens", 1200)
                            _msg = _clt.messages.create(
                                model=_m,
                                max_tokens=_mtok,
                                messages=[{"role": "user", "content": _prompt}],
                            )
                            _ar = _msg.content[0].text
                            st.session_state["dsd_compare_agent_chat"] = [{"role": "assistant", "content": _ar}]
                            _dsd_save_compare_agent_chat(st.session_state["dsd_compare_agent_chat"])
                            _tk = "?"
                            try:
                                _r0 = (st.session_state.get("_dsd_last_compare") or {}).get("table")
                                if _r0 is not None and not getattr(_r0, "empty", True) and "Ticker" in _r0.columns:
                                    _tk = str(_r0.iloc[0].get("Ticker") or "").strip() or "?"
                            except Exception:
                                pass
                            db.add_note(
                                title=f"🤖 AI: Porovnanie diagonál {_tk} [{date.today().strftime('%d.%m.%Y')}]",
                                content=_ar,
                                group_id=None,
                            )
                            st.success("Analýza hotová — skopíruj z chatu nižšie; uložené aj v **Konzultáciách** v denníku.")
                        except Exception as e:
                            st.error(f"Chyba: {e}")
                _hcmp = st.session_state.get("dsd_compare_agent_chat") or []
                if _hcmp:
                    st.markdown("---")
                    with st.expander("💬 Analýza a chat — rozbaľ", expanded=True):
                        render_ai_chat_markdown(_hcmp)
                    _fu = st.chat_input("Doplňujúca otázka k tomuto porovnaniu…", key="dsd_cmp_chat_followup")
                    if _fu:
                        _hcmp.append({"role": "user", "content": _fu})
                        with st.spinner("Agent odpovedá…"):
                            try:
                                _r2 = ai_agent.chat_diagonal_compare(
                                    _hcmp, model=st.session_state.get("selected_claude_model")
                                )
                                _hcmp.append({"role": "assistant", "content": _r2})
                                st.session_state["dsd_compare_agent_chat"] = _hcmp
                                _dsd_save_compare_agent_chat(_hcmp)
                            except Exception as e:
                                st.error(f"Chyba: {e}")
                        st.rerun()
        if st.button("Skryť výsledok porovnania", key="dsd_cmp_dismiss"):
            st.session_state.pop("_dsd_last_compare", None)
            st.session_state["dsd_compare_agent_chat"] = []
            _dsd_save_compare_agent_chat([])
            st.rerun()

st.divider()
st.markdown("##### Poznámky")
st.markdown(
    "- **Čistá theta** = vážený súčet z reťazca (Barchart). **Kladné číslo** = theta v tvoj prospech (zjednodušene *denný príjem* z decay), "
    "**záporné** = proti tebe (*denná strata*). Nie je to účtovný PnL ani hotovosť.\n"
    "- Diagonál vyžaduje **dve expirácie**; ak máš len jednu, importuj ďalší reťazec.\n"
    "- **Short bid** a **Long ask** môžu byť prázdne, ak v DB chýba strana **options** CSV — vtedy je prázdny aj **Debit/kredit** (za lot).\n"
    "- **Uloženie:** označ stĺpec **Uložiť** pri riadkoch výsledkov a klikni na uloženie; v sekcii **Uložené diagonály** môžeš záznamy zmazať stĺpcom **Zmazať**.\n"
    "- **Spread Builder:** v uložených môžeš **Porovnať** 2+ riadky (stĺpec **Do Buildera**): uvidíš **1. a 2. miesto** a stručné dôvody, potom nechaj v *Do Buildera* **len jeden** riadok a klikni **Odoslať** — rovnaký postup ako z **CSV Varianty**.\n"
    "- **DTE** = dni do expirácie od zvolenej snímky (as-of).\n"
    "- **Obmedzenie strike:** započítavajú sa len kontrakty, kde **obidve** nohy (short aj long) majú strike v zadanom intervale.\n"
    "- **Pokročilé filtre:** v expanderi môžeš prepnúť triedenie na **skóre**, nastaviť **spot** (pre OTM short), toleranciu delty, min./max. theta (voliteľne ×100), DTE, likviditu (OI/volume), rel. spready, pomer debit/šírka strike a IV short ≥ long — všetko z lokálnej DB, bez externého API. **Min. OI:** ak v importe chýba stĺpec OI, prázdne hodnoty sa už neberú ako 0 — filter sa na ne nevzťahuje.\n"
    "- **Predvolby:** predvolené sú **striktné** filtre (DTE 40–55 / 90–140, theta 3–8 pri ×100, vega 0,10–0,20 …). "
    "Pri **0 výsledkoch** uvidíš stručný návrh zjemnenia a v expanderi **Detail** diagnostiku z DB. "
    "Tlačidlá **Striktné predvolby** a **Širšie filtre (ETF)** v Pokročilých nastavia oba balíky naraz."
)
