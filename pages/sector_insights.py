"""
Sektory — OCR tabuliek výkonnosti (Barchart) a jednoduché odporúčania k diverzifikácii.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import streamlit as st

from core import barchart_historical_csv as bhc
from core import database as db
from core import sector_insights_engine as sie
from core import sector_performance_ocr as spo
from core.page_context import set_tradejournal_page
from core.sector_select_options import barchart_insight_sector_guide_markdown

db.init_db()
set_tradejournal_page("sector_insights")


def _si_corr_matrix_to_float_df(mat: list[list[object]], tickers: list[str]) -> pd.DataFrame:
    rows: list[list[float]] = []
    for row in mat:
        out_row: list[float] = []
        for v in row:
            if v is None:
                out_row.append(np.nan)
            else:
                try:
                    out_row.append(float(v))
                except (TypeError, ValueError):
                    out_row.append(np.nan)
        rows.append(out_row)
    return pd.DataFrame(rows, index=tickers, columns=tickers)


def _si_render_corr_heatmap(df: pd.DataFrame, *, title: str) -> None:
    """Semafor: ``RdYlGn_r`` — vyššia korelácia červenšia, nižšia zelenšia; maska skryje horný trojuholník a diagonálu."""
    mask = np.triu(np.ones(df.shape, dtype=bool))
    fig, ax = plt.subplots(figsize=(10, 8))
    sns.heatmap(
        df,
        mask=mask,
        annot=True,
        cmap="RdYlGn_r",
        center=0.4,
        vmin=0.2,
        vmax=0.7,
        fmt=".3f",
        ax=ax,
        linewidths=0.5,
        linecolor="white",
    )
    ax.set_title(title)
    plt.tight_layout()
    st.pyplot(fig, clear_figure=True)


st.title("Sektory — insight z tabuliek")
st.caption(
    "**Návod:** (1) V **Symboly** nastav **sektory** z rovnakého zoznamu ako tu. (2) Nahraj **krátky a dlhý** screenshot tabuľky výkonnosti (OCR). "
    "(3) Pozri mapovanie portfólia na riadky tabuľky a textové odporúčania. Expandér **Korelácia** = samostatné CSV z Barchartu."
)
st.caption(
    "Nahraj **dva** screenshoty rovnakej logiky ako na Barchart (krátkodobý a dlhodobý výber stĺpcov). "
    "Aplikácia spraví OCR lokálne (Tesseract) a uloží snímky do denníka. Odporúčania vychádzajú z **podobnosti "
    "výkonnostných vektorov** v tabuľke, nie z korelácie cien."
)

with st.expander("Prehľad sektorov (Barchart → rovnaký zoznam ako v Symboly)", expanded=False):
    st.markdown(barchart_insight_sector_guide_markdown())

with st.expander("Korelácia uzatváracích cien (CSV z Barchartu, DB, matica)", expanded=False):
    _si_ext = st.session_state.pop("si_ext_notice", None)
    if _si_ext:
        st.success(_si_ext)
    st.caption(
        "Stiahni z Barchartu **denné** CSV (**Time** / **Čas** + **Latest** / **Najnovšie**). "
        "Korelácia = **Pearson na log-výnosoch**; páry používajú **prienik** obchodných dní (v okne **max. dní** od najnovšieho)."
    )
    max_d = st.number_input(
        "Max. obchodných dní v okne (pár aj matica)",
        min_value=30,
        max_value=3000,
        value=504,
        step=1,
        key="si_corr_max_days",
        help="504 ≈ 2 roky.",
    )

    st.markdown("##### Rýchle porovnanie dvoch CSV")
    la, lb = st.columns(2)
    with la:
        tk_a = st.text_input("Ticker / označenie A", value="A", key="si_corr_label_a")
        up_a = st.file_uploader("CSV — A", type=["csv", "txt"], key="si_corr_csv_a")
        save_a = st.checkbox("Po výpočte uložiť A do DB", key="si_corr_save_a")
    with lb:
        tk_b = st.text_input("Ticker / označenie B", value="B", key="si_corr_label_b")
        up_b = st.file_uploader("CSV — B", type=["csv", "txt"], key="si_corr_csv_b")
        save_b = st.checkbox("Po výpočte uložiť B do DB", key="si_corr_save_b")
    if st.button("Vypočítať pár", type="primary", key="si_corr_run"):
        if up_a is None or up_b is None:
            st.warning("Nahraj **oba** CSV.")
        else:
            try:
                da = bhc.read_barchart_history_csv(up_a.getvalue())
                dbb = bhc.read_barchart_history_csv(up_b.getvalue())
                ca, cb, merged = bhc.align_close_series(da, dbb, max_trading_days=int(max_d))
                corr, n_obs, _, _ = bhc.correlation_from_closes(ca, cb, method="pearson")
                d0 = merged["date"].min()
                d1 = merged["date"].max()
                st.metric("Pearson (log-výnosy)", f"{corr:.3f}")
                st.caption(
                    f"Spoločných dní: **{len(merged)}** · výnosov: **{n_obs}** · **{d0.date()}** — **{d1.date()}**"
                )
                tka = str(tk_a).strip().upper() or "A"
                tkb = str(tk_b).strip().upper() or "B"
                norm = pd.DataFrame(
                    {
                        tka: merged["close_a"] / float(merged["close_a"].iloc[0]),
                        tkb: merged["close_b"] / float(merged["close_b"].iloc[0]),
                    },
                    index=merged["date"],
                )
                st.line_chart(norm, height=220)
                if save_a:
                    sj, f0, f1 = bhc.hist_dataframe_to_series_json(da)
                    sid = db.insert_ticker_hist_snapshot(
                        tka, sj, bar_count=len(da), first_date=f0, last_date=f1, note="Z párového porovnania"
                    )
                    st.success(f"Uložené **{tka}** (snímok id **{sid}**).")
                if save_b:
                    sj, f0, f1 = bhc.hist_dataframe_to_series_json(dbb)
                    sid = db.insert_ticker_hist_snapshot(
                        tkb, sj, bar_count=len(dbb), first_date=f0, last_date=f1, note="Z párového porovnania"
                    )
                    st.success(f"Uložené **{tkb}** (snímok id **{sid}**).")
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")

    st.divider()
    st.markdown("##### Uložiť jeden CSV do databázy (bez výpočtu)")
    u1, u2, u3 = st.columns([2, 2, 2])
    with u1:
        tk_one = st.text_input("Ticker (symbol)", placeholder="MSFT", key="si_hist_ticker")
    with u2:
        up_one = st.file_uploader("CSV histórie", type=["csv", "txt"], key="si_hist_csv")
    with u3:
        note_one = st.text_input("Poznámka (voliteľné)", key="si_hist_note")
    if st.button("Uložiť sériu", key="si_hist_save"):
        if not (tk_one or "").strip() or up_one is None:
            st.warning("Zadaj ticker a nahraj CSV.")
        else:
            try:
                df1 = bhc.read_barchart_history_csv(up_one.getvalue())
                sj, f0, f1 = bhc.hist_dataframe_to_series_json(df1)
                sid = db.insert_ticker_hist_snapshot(
                    tk_one.strip().upper(),
                    sj,
                    bar_count=len(df1),
                    first_date=f0,
                    last_date=f1,
                    note=note_one or None,
                )
                st.success(f"Uložené id **{sid}** · **{len(df1)}** dní · {f0} … {f1}")
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")

    with st.expander("🗑 Odstrániť symbol / snímok z histórie (korelácia)", expanded=False):
        _hist_del = st.session_state.pop("si_hist_del_notice", None)
        if _hist_del:
            st.success(_hist_del)
        st.caption(
            "Ak je v zozname zlý upload (napr. zlý ticker pri uložení CSV) alebo poškodené dáta, zmaž záznam a **nahraj CSV znova**. "
            "Snímky sú v tabuľke `ticker_hist_snapshots`. **Uložené korelačné matice** sú zvlášť (`ticker_corr_matrix_runs`) — po vymazaní histórie môžu ostať; sekcia nižšie ich dočistí. "
            "Záložka **Symboly** sa tým nemení."
        )
        _del_list = db.list_ticker_hist_snapshots_latest_per_ticker()
        if not _del_list:
            st.info("Zatiaľ **žiadne** uložené série v DB — môžeš ešte vyčistiť **uložené matice** podľa symbolu (ak ti tam ostal zlý ticker).")
        else:
            _del_labels = [
                f"{s['ticker']}: id **{s['id']}** · {s['bar_count']}d · {str(s['created_at'])[:10]}"
                for s in _del_list
            ]
            _del_by_label = {_del_labels[i]: _del_list[i] for i in range(len(_del_list))}
            _pick_lbl = st.selectbox(
                "Vyber riadok (najnovší snímok na tento symbol):",
                options=_del_labels,
                key="si_hist_del_pick",
            )
            dc1, dc2 = st.columns(2)
            with dc1:
                if st.button("Zmazať tento 1 snímok", key="si_hist_del_one", type="secondary"):
                    _row = _del_by_label.get(_pick_lbl)
                    if _row:
                        n = db.delete_ticker_hist_snapshot(int(_row["id"]))
                        if n:
                            st.session_state["si_hist_del_notice"] = (
                                f"Snímok **{int(_row['id'])}** (`{_row.get('ticker', '')}`) odstránený. "
                                "Ak ešte existovali staršie snímky tohto tickera, v zozname bude opäť zobrazený **predchádzajúci**."
                            )
                        else:
                            st.session_state["si_hist_del_notice"] = "Záznam sa nenašiel (možno už bol zmazaný)."
                    st.rerun()
            with dc2:
                if st.button("Zmazať VŠETKY snímky pre tento ticker", key="si_hist_del_all", type="primary"):
                    _row = _del_by_label.get(_pick_lbl)
                    if _row:
                        tk0 = str(_row.get("ticker") or "").strip().upper()
                        n2 = db.delete_ticker_hist_snapshots_by_ticker(tk0)
                        n3 = db.delete_ticker_corr_matrix_runs_containing_ticker(tk0)
                        st.session_state["si_hist_del_notice"] = (
                            f"História snímok: **{n2}** záznam(ov) pre **{tk0}**. "
                            f"Uložené korelačné matice odstránené: **{n3}** (všetky, ktoré obsahovali tento symbol)."
                        )
                    st.rerun()
        st.markdown("---")
        st.markdown("**Uložené korelačné matice** (ak ťa otravujú ešte po vymazaní snímok)")
        _t_purge = st.text_input(
            "Symbol (ticker) na vyčistenie mát v „Uložené matice“",
            value="",
            key="si_corr_purge_ticker",
            placeholder="TXN",
            help="Zmaže celé záznamy (riadky) v `ticker_corr_matrix_runs`, ak zoznam tickerov v matici obsahuje tento symbol.",
        )
        if st.button("Zmazať uložené matice obsahujúce tento symbol", key="si_corr_purge_runs", type="secondary"):
            tku = (_t_purge or "").strip().upper()
            if not tku:
                st.session_state["si_hist_del_notice"] = "Zadaj symbol (napr. **TXN**)."
            else:
                npur = db.delete_ticker_corr_matrix_runs_containing_ticker(tku)
                st.session_state["si_hist_del_notice"] = (
                    f"Z uložených korelačných mátic odstránené **{npur}** záznam(ov) obsahujúcich **{tku}**."
                )
            st.rerun()

    st.divider()
    st.markdown("##### Korelačná matica z uložených snímok")
    snaps = db.list_ticker_hist_snapshots_latest_per_ticker()
    st.caption(
        "V zozname je **najnovší** uložený snímok pre každý ticker (nie 300 posledných riadkov z DB, aby nezmizli staršie tickery)."
    )
    if len(snaps) < 2:
        st.info("V DB musia byť aspoň **2** uložené série — najprv ich pridaj vyššie.")
    else:
        labels = [f"{s['id']}: {s['ticker']} · {s['bar_count']}d · {str(s['created_at'])[:10]}" for s in snaps]
        id_by_label = {labels[i]: snaps[i]["id"] for i in range(len(labels))}
        pick = st.multiselect(
            "Vyber snímky (každý ticker najviac raz)",
            options=labels,
            key="si_mat_pick",
            help="Z každého tickeru vyber jeden riadok, inak výpočet odmietne.",
        )
        if st.button("Vypočítať maticu", key="si_mat_run"):
            try:
                ids = [id_by_label[x] for x in pick]
                if len(ids) < 2:
                    st.warning("Vyber aspoň **2** snímky.")
                else:
                    frames: dict[str, pd.DataFrame] = {}
                    for sid in ids:
                        row = db.get_ticker_hist_snapshot(sid)
                        if not row:
                            continue
                        tk = str(row["ticker"]).upper()
                        if tk in frames:
                            raise ValueError(f"Ticker **{tk}** je vybraný dvakrát — nechaj len jeden snímok.")
                        frames[tk] = bhc.hist_series_json_to_dataframe(row["series_json"])
                    if len(frames) < 2:
                        st.error("Nepodarilo sa načítať dáta.")
                    else:
                        tks, mat, nobs = bhc.correlation_matrix_pairwise(
                            frames, max_trading_days=int(max_d), method="pearson", return_kind="log"
                        )
                        st.session_state["si_last_matrix"] = {
                            "tickers": tks,
                            "matrix": mat,
                            "n_obs": nobs,
                            "max_days": int(max_d),
                        }
            except Exception as e:
                st.error(f"{type(e).__name__}: {e}")
        lm = st.session_state.get("si_last_matrix")
        if lm and lm.get("tickers") and lm.get("matrix"):
            _tks_lm = lm["tickers"]
            _mat_lm = lm["matrix"]
            _df_lm = _si_corr_matrix_to_float_df(_mat_lm, _tks_lm)
            _si_render_corr_heatmap(
                _df_lm,
                title="Semafor korelácie uzatváracích cien (log-výnosy, Pearson)",
            )
            st.caption(
                "Farebná škála **RdYlGn_r** (`center=0.4`, `vmin=0.2`, `vmax=0.7`) — vyššia korelácia **červenšia**, nižšia **zelenšia**; **1.0** na diagonále pri **vmax**."
            )
            _no_lm = lm.get("n_obs")
            if _no_lm:
                st.caption("Počty pozorovaní (výnosov) pre každý pár:")
                st.dataframe(
                    pd.DataFrame(_no_lm, index=_tks_lm, columns=_tks_lm).astype(object),
                    use_container_width=True,
                )
        if lm and lm.get("tickers"):
            mtit = st.text_input("Názov uloženej matice", value="Moja matica", key="si_mat_title")
            if st.button("Uložiť túto maticu do DB", key="si_mat_save"):
                try:
                    rid = db.insert_ticker_corr_matrix_run(
                        mtit,
                        lm["tickers"],
                        lm["matrix"],
                        max_days=int(lm.get("max_days") or max_d),
                        method="pearson",
                        return_kind="log",
                        n_obs=lm.get("n_obs"),
                    )
                    st.success(f"Uložené id **{rid}**.")
                except Exception as e:
                    st.error(f"{type(e).__name__}: {e}")

    st.divider()
    st.markdown("##### Uložené matice")
    runs = db.list_ticker_corr_matrix_runs(40)
    if not runs:
        st.caption("Zatiaľ žiadna uložená matica.")
    else:
        run_labels = [
            f"{r['id']}: {(r.get('title') or '—')[:24]} · {len(r.get('tickers') or [])}× · {str(r['created_at'])[:16]}"
            for r in runs
        ]
        rmap = {run_labels[i]: runs[i]["id"] for i in range(len(run_labels))}
        chosen = st.selectbox("Otvoriť maticu", options=run_labels, key="si_run_pick")
        rr = db.get_ticker_corr_matrix_run(rmap[chosen])
        if rr:
            st.caption(
                f"**{rr.get('title') or '—'}** · max. dní **{rr['max_days']}** · {rr['method']} / {rr['return_kind']} · {rr['created_at']}"
            )
            tks = rr.get("tickers") or []
            mat = rr.get("matrix") or []
            if tks and mat:
                _dfmr = _si_corr_matrix_to_float_df(mat, tks)
                _tit = str(rr.get("title") or "Uložená korelačná matica")
                _si_render_corr_heatmap(_dfmr, title=f"Semafor korelácie — {_tit}")
                st.caption("Škála **RdYlGn_r** (`vmin=0.2`, `vmax=0.7`, `center=0.4`).")
            no = rr.get("n_obs")
            if no:
                st.dataframe(pd.DataFrame(no, index=tks, columns=tks), use_container_width=True)
            if tks and mat:
                st.markdown("##### Rozšíriť o ďalšie tickery")
                st.caption(
                    "Nemusíš skladať maticu od nuly: pôvodný blok **všetkých pôvodných parov** ostane z tohto uloženia. "
                    "Dopočítajú sa korelácie s **novými** a medzi **novými** (Pearson / log-výnosy, rovnaké **max. dní** ako v tomto zázname). "
                    "Pre staré tickery sa na nové páry berie **najnovší** snímok z DB — ak chceš inú dĺžku histórie, nechaj v DB len ten snímok alebo urob novú plnú maticu. "
                    "**Korelácia** nie zo záložky Symboly: nový symbol musí mať nahraté **CSV** vyššie (uložená história v DB)."
                )
                _snaps_x = db.list_ticker_hist_snapshots_latest_per_ticker()
                _labels_x = [
                    f"{s['id']}: {s['ticker']} · {s['bar_count']}d · {str(s['created_at'])[:10]}"
                    for s in _snaps_x
                ]
                _idmap_x = {_labels_x[i]: _snaps_x[i]["id"] for i in range(len(_labels_x))}
                _have = {str(x).strip().upper() for x in tks}
                _add_opts = [
                    lb
                    for lb, s in zip(_labels_x, _snaps_x)
                    if str(s.get("ticker") or "").strip().upper() not in _have
                ]
                if not _add_opts:
                    st.info(
                        "Nie je čo pridať: v DB buď **nie je** iný ticker s históriou, alebo **všetky** tickery s uloženým CSV "
                        "už máš v tejto matici. **Symbol v Symboly nestačí** — musí byť sekcia **„Uložiť sériu“** (denné CSV z Barchartu). "
                        "Potom sa v zozname objaví **najnovší** snímok na tento symbol."
                    )
                else:
                    _ext_key = f"si_mat_ext_{int(rr['id'])}"
                    _ext_pick = st.multiselect(
                        "Pridať snímky (iba tickery, ktoré ešte v matici nie sú)",
                        options=_add_opts,
                        key=f"{_ext_key}_multiselect",
                    )
                    if st.button(
                        "Dopočítať a načítať rozšírenú maticu do pracovného stavu",
                        key=f"{_ext_key}_run",
                    ):
                        if not _ext_pick:
                            st.warning("Vyber aspoň jeden snímok.")
                        else:
                            try:
                                frames: dict[str, pd.DataFrame] = {}
                                new_tk_list: list[str] = []
                                for lb in _ext_pick:
                                    sid = int(_idmap_x[lb])
                                    hrow = db.get_ticker_hist_snapshot(sid)
                                    if not hrow:
                                        continue
                                    tku = str(hrow.get("ticker") or "").strip().upper()
                                    if not tku:
                                        continue
                                    if tku in frames:
                                        raise ValueError(
                                            f"Ticker **{tku}** je v rozšírení viackrát — nechaj jeden snímok."
                                        )
                                    new_tk_list.append(tku)
                                    frames[tku] = bhc.hist_series_json_to_dataframe(
                                        str(hrow.get("series_json") or "")
                                    )
                                if not new_tk_list:
                                    st.warning("Nepodarilo sa načítať nové snímky.")
                                else:
                                    old_latest = db.get_latest_ticker_hist_snapshot_rows(
                                        [str(x) for x in tks], max_scan=500
                                    )
                                    miss = [
                                        str(x).strip().upper()
                                        for x in tks
                                        if str(x).strip().upper() not in old_latest
                                    ]
                                    if miss:
                                        st.error(
                                            "V DB chýba **najnovší** snímok pre: **"
                                            + "**, **".join(miss)
                                            + "**. Ulož sériu vyššie, potom skús znova."
                                        )
                                    else:
                                        for x in tks:
                                            u = str(x).strip().upper()
                                            frames[u] = bhc.hist_series_json_to_dataframe(
                                                str(old_latest[u].get("series_json") or "")
                                            )
                                        r_method = str(rr.get("method") or "pearson")
                                        r_ret = str(rr.get("return_kind") or "log")
                                        if r_method not in ("pearson", "spearman"):
                                            r_method = "pearson"
                                        t_new, m_new, n_new = bhc.extend_correlation_matrix(
                                            tks,
                                            mat,
                                            no,
                                            new_tk_list,
                                            frames,
                                            max_trading_days=int(rr.get("max_days") or max_d),
                                            method="spearman" if r_method == "spearman" else "pearson",
                                            return_kind="simple" if r_ret == "simple" else "log",
                                        )
                                        st.session_state["si_last_matrix"] = {
                                            "tickers": t_new,
                                            "matrix": m_new,
                                            "n_obs": n_new,
                                            "max_days": int(rr.get("max_days") or max_d),
                                        }
                                        st.session_state["si_ext_notice"] = (
                                            f"Matica rozšírená na **{len(t_new)}** symbolov — náhľad hore v tejto sekcii. "
                                            "Môžeš ju uložiť znova pod novým názvom."
                                        )
                                        st.rerun()
                            except Exception as e:
                                st.error(f"{type(e).__name__}: {e}")
            if st.button("Zmazať túto maticu", key="si_run_del"):
                db.delete_ticker_corr_matrix_run(int(rr["id"]))
                st.rerun()

_SI_MANUAL = Path(__file__).resolve().parent.parent / "docs" / "sektory-insight.md"
if _SI_MANUAL.is_file():
    with st.expander("Manuál — ako používať túto stránku", expanded=False):
        st.markdown(_SI_MANUAL.read_text(encoding="utf-8"))
else:
    st.caption("_Text manuálu sa nenašiel (`docs/sektory-insight.md`)._")

if not spo.ocr_stack_available():
    st.warning(
        "Python balíky pre OCR nie sú dostupné (`opencv-python-headless`, `pytesseract`). "
        "V projekte spusti `pip install -r requirements.txt`. Na systéme musí byť nainštalovaný **tesseract-ocr**."
    )
else:
    st.caption(
        "**Široký nízky screenshot** (veľa stĺpcov): obrázok sa pred OCR zväčší podľa výšky riadkov "
        "a pri veľkej šírke sa číta po prúžkoch — mal by sa prečítať celý riadok. Ak nie, skús ostrejší výrez alebo text v poli nižšie."
    )


def _sector_for_ticker(tk: str) -> str | None:
    row = db.get_symbol(tk)
    if not row:
        return None
    s = row.get("sector")
    return str(s).strip() if s else None


def _render_upload_block(horizon: str, title: str) -> None:
    st.subheader(title)
    key_base = f"si_{horizon}"
    up = st.file_uploader(
        "Obrázok tabuľky (PNG/JPG)",
        type=["png", "jpg", "jpeg", "webp", "tif", "tiff"],
        key=f"{key_base}_up",
    )
    raw_text = st.text_area(
        "Alebo priamy text (vlož po OCR inde alebo prepíš)",
        height=120,
        key=f"{key_base}_txt",
        help="Parsovanie len po kliknutí na tlačidlo — inak by sa tabuľka pri každom obnovení resetovala.",
    )
    c1, c2 = st.columns(2)
    with c1:
        do_ocr = st.button(
            "Spustiť OCR z obrázka",
            key=f"{key_base}_ocr",
            disabled=up is None or not spo.ocr_stack_available(),
        )
    with c2:
        parse_only = st.button("Parsovať text z poľa", key=f"{key_base}_parse")

    if do_ocr and up is not None and spo.ocr_stack_available():
        try:
            text = spo.ocr_image_bytes_to_text(up.getvalue())
            st.session_state[f"{key_base}_last_ocr"] = text
            df = spo.parse_sector_performance_text(text)
            st.session_state[f"{key_base}_df"] = df
            st.success(f"OCR: rozpoznaných **{len(df)}** riadkov (výpis je v expanderi nižšie).")
        except Exception as e:
            st.error(f"OCR zlyhal: {e}")

    if parse_only:
        df = spo.parse_sector_performance_text(raw_text)
        st.session_state[f"{key_base}_df"] = df
        st.success(f"Z textu: **{len(df)}** riadkov.")

    ocr_preview = st.session_state.get(f"{key_base}_last_ocr")
    if ocr_preview:
        with st.expander("Surový text z posledného OCR"):
            st.code(ocr_preview[:8000] + ("…" if len(ocr_preview) > 8000 else ""), language="text")

    if f"{key_base}_df" in st.session_state:
        _edf = st.session_state[f"{key_base}_df"]
        _sig = "|".join(str(c) for c in _edf.columns) + f"|{len(_edf)}"
        if len(_edf) > 0 and "sector" in _edf.columns:
            _sig += "|" + "|".join(_edf["sector"].astype(str).head(5).tolist())
        ed_key = f"{key_base}_ed_{hashlib.sha256(_sig.encode('utf-8')).hexdigest()[:16]}"
        st.session_state[f"{key_base}_df"] = st.data_editor(
            _edf,
            use_container_width=True,
            num_rows="dynamic",
            key=ed_key,
        )

    note = st.text_input("Poznámka k uloženiu (voliteľné)", key=f"{key_base}_note")
    if st.button(f"Uložiť snímok ({horizon})", key=f"{key_base}_save"):
        edf = st.session_state.get(f"{key_base}_df")
        if edf is None or getattr(edf, "empty", True):
            st.error("Nie sú dáta na uloženie — najprv OCR alebo parsovanie.")
        else:
            payload = spo.dataframe_to_payload_rows(edf)
            sid = db.insert_sector_performance_snapshot(horizon, payload, note or None)
            st.success(f"Uložené (id **{sid}**).")
            st.rerun()


st.divider()
ca, cb = st.columns(2)
with ca:
    _render_upload_block("short", "Krátkodobý snímok")
with cb:
    _render_upload_block("long", "Dlhodobý snímok")

st.divider()
st.subheader("Report a posledné snímky")

sh = db.get_latest_sector_performance_snapshot("short")
lo = db.get_latest_sector_performance_snapshot("long")
if not sh:
    st.info("Zatiaľ nemáš uložený **krátkodobý** snímok.")
if sh:
    st.caption(f"Krátkodobý: **{sh['created_at']}**" + (f" — {sh.get('note')}" if sh.get("note") else ""))
if lo:
    st.caption(f"Dlhodobý: **{lo['created_at']}**" + (f" — {lo.get('note')}" if lo.get("note") else ""))

if sh:
    short_df = spo.payload_rows_to_dataframe(sh["payload"])
    long_df = spo.payload_rows_to_dataframe(lo["payload"]) if lo else None

    open_trades = db.get_open_trades()
    weights = sie.portfolio_sector_weights(open_trades, _sector_for_ticker)

    st.subheader("Skontroluj ticker (diverzifikácia)")
    st.caption(
        "Zadaj ticker, ktorý máš v **Symboly** so sektorom. Použijú sa **otvorené** nohy z denníka a **posledný krátkodobý** snímok "
        "(kosínus podobnosti výkonnostných stĺpcov v tabuľke — orientačné, nie investičná rada)."
    )
    ec1, ec2 = st.columns([3, 1])
    with ec1:
        _tdiv = st.text_input("Ticker", placeholder="AMZN", key="si_tdiv_tk", help="Musí existovať v Symboly so zvoleným sektorom.")
    with ec2:
        _tdiv_go = st.button("Vyhodnotiť", key="si_tdiv_go", type="primary")
    if _tdiv_go:
        ev = sie.evaluate_ticker_diversification(
            _tdiv,
            short_df,
            long_df,
            weights,
            _sector_for_ticker,
            open_trades=open_trades,
        )
        if ev.get("error"):
            st.error(ev["error"])
        else:
            if ev.get("verdict"):
                st.info(ev["verdict"])
            _lines = ev.get("lines") or []
            if _lines:
                st.markdown("\n".join(_lines))
            with st.expander("Technické detaily", expanded=False):
                st.json({k: v for k, v in ev.items() if k != "lines"})

    rep = sie.build_insight_report(short_df, long_df, weights)

    if rep.get("errors"):
        for e in rep["errors"]:
            st.error(e)

    st.markdown(rep.get("similarity_note", ""))

    with st.expander("Mapovanie portfólia → tabuľka", expanded=False):
        for row in rep.get("portfolio_mapped", []):
            st.write(
                f"- **{row['portfolio_sector']}** ({row['weight']:.1%}) → "
                f"`{row['table_sector'] or '— nespárované —'}`"
            )

    if rep.get("warnings"):
        st.markdown("#### Varovania")
        st.markdown("\n\n".join(rep["warnings"]))

    if rep.get("concentration"):
        st.markdown("#### Klastre (podobné správanie v krátkom snímku)")
        st.markdown("\n".join(f"- {c}" for c in rep["concentration"]))

    if rep.get("diversifiers"):
        st.markdown("#### Možní diverzifikátori (nízka podobnosť k tvojim tabuľkovým sektorom)")
        st.markdown("\n".join(f"- {d}" for d in rep["diversifiers"]))

    if rep.get("momentum_long"):
        st.markdown("#### Dlhodobý výkon (z dlhého snímku)")
        st.markdown("\n".join(f"- {m}" for m in rep["momentum_long"]))

    hist = db.list_sector_performance_snapshots(15)
    if hist:
        st.markdown("**Posledné uložené snímky**")
        for h in hist:
            st.caption(f"id {h['id']} · {h['horizon']} · {h['created_at']}")
