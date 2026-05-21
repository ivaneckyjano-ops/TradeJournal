"""
Import a prehľad lokálnej SQLite databázy reťazcov opcií (Barchart CSV alebo IBKR sync).
Jeden súbor DB na ticker: data/option_chains/<TICKER>.db
"""

from __future__ import annotations

import io
from datetime import date
from collections import defaultdict

import pandas as pd
import streamlit as st

from core import option_chain_db as odb
from core.page_context import set_tradejournal_page

set_tradejournal_page("option_chain_greeks")

st.title("Databáza Grékov")
st.caption(
    "**Návod:** **Import CSV** — Barchart (options + greeks). **IBKR** — jeden ticker, Call/Put, expirácie z TWS. **Prehľad** — uložené reťazce (diagonály a pod.)."
)
st.caption(
    "Samostatná databáza **mimo journal.db**: jeden súbor `data/option_chains/<TICKER>.db` na symbol. "
    "Barchart: názov s `options-exp` alebo `volatility-greeks-exp`, dátum expirácie a snímka `-MM-DD-YYYY.csv`."
)

option_chain_sections = ["Import CSV", "IBKR", "Prehľad"]
option_chain_section = st.selectbox("Sekcia", option_chain_sections, key="option_chain_section")


def _status_label(has_options: bool, has_greeks: bool, in_db: bool) -> str:
    if in_db:
        return "V databáze" if (has_options and has_greeks) else "Neúplné v DB"
    if has_options and has_greeks:
        return "Spárované"
    if has_options:
        return "Chýbajú gréky"
    if has_greeks:
        return "Chýbajú opcie"
    return "Prázdne"


def _status_color(label: str) -> str:
    return {
        "V databáze": "🟢",
        "Neúplné v DB": "🔴",
        "Spárované": "🔵",
        "Chýbajú gréky": "🟠",
        "Chýbajú opcie": "🟠",
        "Prázdne": "⚪",
    }.get(label, "⚪")


def _human_col(name: str) -> str:
    mapping = {
        "ticker": "Ticker",
        "kind": "Typ súboru",
        "expiry": "Expirácia",
        "as_of_date": "Dátum snímky",
        "options": "Súbor opcií",
        "greeks": "Súbor grékov",
        "status": "Stav",
        "rows": "Riadkov v DB",
    }
    return mapping.get(name, name)


def _status_style(label: str) -> str:
    return {
        "V databáze": "background-color: #d9f7d9;",
        "Neúplné v DB": "background-color: #ffd6d6;",
        "Spárované": "background-color: #dcecff;",
        "Chýbajú gréky": "background-color: #fff0c2;",
        "Chýbajú opcie": "background-color: #fff0c2;",
        "Prázdne": "background-color: #efefef;",
    }.get(label, "")


def _snapshot_status_for_group(ticker: str, expiry: str, as_of_date: str) -> tuple[int, int, int]:
    try:
        df = odb.list_snapshot_status(ticker)
        match = df[(df["expiry"] == expiry) & (df["as_of_date"] == as_of_date)]
        if match.empty:
            return 0, 0, 0
        row = match.iloc[0]
        return int(row["rows"]), int(row.get("rows_with_options", 0)), int(row.get("rows_with_greeks", 0))
    except Exception:
        return 0, 0, 0


def _side_needed(rows_with_options: int, rows_with_greeks: int) -> str:
    if rows_with_options > 0 and rows_with_greeks == 0:
        return "greeks"
    if rows_with_greeks > 0 and rows_with_options == 0:
        return "options"
    return ""


def _store_uploaded_groups(uploaded):
    grouped: dict[tuple[str, str, str], dict[str, dict[str, object]]] = defaultdict(dict)
    for uf in uploaded:
        meta = odb.parse_barchart_option_filename(uf.name)
        if not meta:
            continue
        try:
            uf.seek(0)
        except Exception:
            pass
        grouped[(meta.ticker, meta.expiry, meta.as_of_date)][meta.kind] = {
            "name": uf.name,
            "bytes": uf.getvalue() if hasattr(uf, "getvalue") else uf.read(),
        }
    st.session_state["ocg_uploaded_groups"] = grouped
    st.session_state.pop("ocg_import_report", None)


def _get_uploaded_blob_for_group(key: tuple[str, str, str], side: str):
    groups = st.session_state.get("ocg_uploaded_groups", {})
    return groups.get(key, {}).get(side)


def _upload_side_to_filelike(side: object):
    """Skupiny v session_state sú dict {name, bytes}; import_pair_from_uploads očakáva .name a .read()."""
    if side is None:
        return None
    if isinstance(side, dict) and "bytes" in side:
        name = str(side.get("name") or "")
        raw = side["bytes"]
        data = raw if isinstance(raw, bytes) else bytes(raw) if raw is not None else b""

        class _BytesAsUpload:
            def __init__(self) -> None:
                self.name = name
                self._bio = io.BytesIO(data)

            def read(self, n: int = -1):
                return self._bio.read() if n == -1 else self._bio.read(n)

            def seek(self, pos: int, whence: int = 0):
                return self._bio.seek(pos, whence)

        return _BytesAsUpload()
    return side


def _side_display_name(side: object | None) -> str:
    if not side:
        return "—"
    if isinstance(side, dict):
        return str(side.get("name") or "—")
    return str(getattr(side, "name", None) or "—")


def _validate_reimport_file(
    *,
    file_name: str,
    expected_ticker: str,
    expected_expiry: str,
    expected_as_of: str,
    expected_side: str,
) -> str | None:
    meta = odb.parse_barchart_option_filename(file_name or "")
    if not meta:
        return (
            "Súbor sa nedá rozpoznať ako Barchart export. "
            "Očakávam názov s `options-exp` alebo `volatility-greeks-exp` a dátumom `-MM-DD-YYYY.csv`."
        )
    if meta.ticker != expected_ticker:
        return f"Nesedí ticker: súbor má `{meta.ticker}`, očakávané je `{expected_ticker}`."
    if meta.expiry != expected_expiry:
        return f"Nesedí expirácia: súbor má `{meta.expiry}`, očakávané je `{expected_expiry}`."
    if meta.as_of_date != expected_as_of:
        return f"Nesedí dátum snímky: súbor má `{meta.as_of_date}`, očakávané je `{expected_as_of}`."
    if meta.kind != expected_side:
        return f"Nesedí typ súboru: chýba `{expected_side}`, ale súbor je `{meta.kind}`."
    return None


def _render_import_report(report: dict) -> None:
    """Zobrazí výsledok posledného importu (session_state), aby nezmizol po rerune."""
    summary = report.get("summary", "")
    ok_lines = report.get("ok", [])
    err_lines = report.get("err", [])
    skip_lines = report.get("skip", [])
    if err_lines:
        st.error(summary or "Import skončil s chybami.")
    elif ok_lines:
        st.success(summary or "Import prebehol.")
    elif skip_lines:
        st.warning(summary or "Nič sa neimportovalo — pozri dôvody nižšie.")
    else:
        st.info(summary or "Import sa nespustil alebo nebolo čo spracovať.")
    if ok_lines:
        with st.expander("Úspešné importy", expanded=bool(ok_lines) and not err_lines):
            for line in ok_lines:
                st.markdown(f"- {line}")
    if skip_lines:
        with st.expander("Preskočené skupiny", expanded=bool(skip_lines) and not ok_lines):
            for line in skip_lines:
                st.markdown(f"- {line}")
    if err_lines:
        with st.expander("Chyby", expanded=True):
            for line in err_lines:
                st.markdown(f"- {line}")


if option_chain_section == "Import CSV":
    st.caption(
        "**Návod:** Nahraj **Barchart CSV** (options + greeks pre rovnakú expiráciu a dátum snímky). Skontroluj náhľad, zvoľ **Importovať všetko** alebo **len chýbajúce**. "
        "Formát názvu súboru je v popise stránky hore."
    )
    st.markdown("##### Nahrať súbory")
    uploaded = st.file_uploader(
        "Jeden alebo viac CSV (môžeš nahrať naraz options aj greeks pre viac expirácií)",
        type=["csv"],
        accept_multiple_files=True,
        key="ocg_upload_multi",
        help="Rovnaké pravidlá ako pri skripte scripts/import_barchart_option_chains.py",
    )
    if uploaded:
        _store_uploaded_groups(uploaded)
    stored_groups = st.session_state.get("ocg_uploaded_groups", {})
    if uploaded and not stored_groups:
        st.warning(
            "Z nahratých súborov sa nepodarilo zostaviť žiadnu skupinu. "
            "Názov musí obsahovať `options-exp` alebo `volatility-greeks-exp`, dátum expirácie a na konci snímku `-MM-DD-YYYY.csv`."
        )
    report_prev = st.session_state.get("ocg_import_report")
    if report_prev:
        st.markdown("##### Posledný výsledok importu")
        _render_import_report(report_prev)

    if not stored_groups:
        st.caption("Po nahratí platných CSV sa zobrazí náhľad a tlačidlá importu.")
    else:
        st.markdown("##### Náhľad a import do DB")
        st.caption(
            "Skupiny sa párujú podľa tickera, expirácie a dátumu snímky z názvu súboru. "
            "Vyber režim importu a potvrď jedným z tlačidiel."
        )
        groups = stored_groups
        only_incomplete = st.checkbox(
            "Zobraziť iba nekompletné skupiny v tabuľke",
            value=False,
            key="ocg_only_incomplete",
            help="Len skryje riadky v náhľade; import stále ide zo všetkých nahratých skupín (podľa zvoleného tlačidla).",
        )
        reinstate = st.checkbox(
            "Pri „Importovať všetko“ najprv zmazať existujúcu snímku v DB",
            value=True,
            key="ocg_delete_before_reimport",
            help="Pred prepísaním vymaže riadky danej snímky v DB.",
        )
        preview_rows = []
        for key, slot in sorted(groups.items()):
            has_o = "options" in slot
            has_g = "greeks" in slot
            rows, rows_with_options, rows_with_greeks = _snapshot_status_for_group(key[0], key[1], key[2])
            in_db = rows > 0
            db_complete = in_db and rows_with_options > 0 and rows_with_greeks > 0
            label = _status_label(has_o, has_g, db_complete)
            if only_incomplete and db_complete:
                continue
            preview_rows.append(
                {
                    "ticker": key[0],
                    "expiry": key[1],
                    "as_of_date": key[2],
                    "options": _side_display_name(slot.get("options")) if has_o else "—",
                    "greeks": _side_display_name(slot.get("greeks")) if has_g else "—",
                    "status": f"{_status_color(label)} {label}",
                    "rows": rows,
                    "rows_with_options": rows_with_options,
                    "rows_with_greeks": rows_with_greeks,
                }
            )
        if preview_rows:
            preview_df = pd.DataFrame(preview_rows).rename(columns=_human_col)
            st.dataframe(preview_df, use_container_width=True, hide_index=True)
        else:
            st.info(
                "V náhľade nie je žiadny riadok (pravdepodobne sú všetky skupiny už kompletné a máš zapnutý filter). "
                "Import môžeš aj tak spustiť — spracujú sa všetky nahraté skupiny."
            )
        c1, c2 = st.columns(2)
        with c1:
            import_all = st.button("Importovať všetko", type="primary", key="ocg_confirm_import")
        with c2:
            import_missing = st.button("Importovať len chýbajúce / doplniť", key="ocg_confirm_missing")
        if import_all or import_missing:
            progress = st.progress(0.0)
            ok_lines: list[str] = []
            err_lines: list[str] = []
            skip_lines: list[str] = []
            items = list(groups.items())
            for i, (_key, slot) in enumerate(items):
                fo = _upload_side_to_filelike(slot.get("options"))
                fg = _upload_side_to_filelike(slot.get("greeks"))
                if not fo and not fg:
                    skip_lines.append(
                        f"`{_key}`: žiadny súbor (options ani greeks) — nie je čo importovať."
                    )
                    progress.progress((i + 1) / max(len(items), 1))
                    continue
                rows, rows_with_options, rows_with_greeks = _snapshot_status_for_group(_key[0], _key[1], _key[2])
                in_db = rows > 0
                db_complete = in_db and rows_with_options > 0 and rows_with_greeks > 0
                try:
                    if import_missing and db_complete:
                        skip_lines.append(
                            f"`{_key}`: preskočené — režim „len chýbajúce“ a táto snímka je už v DB kompletne (options aj gréky)."
                        )
                        progress.progress((i + 1) / max(len(items), 1))
                        continue
                    if reinstate and import_all and in_db:
                        odb.delete_snapshot(_key[0], _key[1], _key[2])
                        in_db = False
                    if import_missing and in_db and (rows_with_options > 0 and rows_with_greeks > 0):
                        skip_lines.append(
                            f"`{_key}`: preskočené — v DB už sú zapísané options aj gréky; na prepísanie použi „Importovať všetko“."
                        )
                        progress.progress((i + 1) / max(len(items), 1))
                        continue
                    if not fo and not fg:
                        skip_lines.append(f"`{_key}`: prázdne súbory.")
                        progress.progress((i + 1) / max(len(items), 1))
                        continue
                    t, n = odb.import_pair_from_uploads(fo, fg)
                    ok_lines.append(f"**{t}** · exp {_key[1]} · snímka {_key[2]} → **{n}** riadkov")
                except Exception as exc:
                    err_lines.append(f"`{_key}`: `{type(exc).__name__}`: {exc}")
                progress.progress((i + 1) / max(len(items), 1))
            progress.empty()
            n_ok, n_err, n_skip = len(ok_lines), len(err_lines), len(skip_lines)
            summary = (
                f"Súhrn: **{n_ok}** úspešných, **{n_skip}** preskočených, **{n_err}** chýb "
                f"(zo **{len(items)}** nahratých skupín)."
            )
            if not ok_lines and not err_lines and not skip_lines and not items:
                summary = "Žiadne skupiny na spracovanie (prázdny stav nahratých súborov)."
            elif not ok_lines and not err_lines and not skip_lines and items:
                summary += (
                    " Skontroluj, či sú v CSV dáta a či názvy súborov zodpovedajú formátu Barchart exportu."
                )
            st.session_state["ocg_import_report"] = {
                "summary": summary,
                "ok": ok_lines,
                "err": err_lines,
                "skip": skip_lines,
            }
            st.rerun()


elif option_chain_section == "IBKR":
    from core import ibkr as ibkr_ocg
    from core.option_chain_ibkr_sync import (
        parse_expiry_text,
        sync_chain_snapshot,
        validate_expiries_against_secdef,
    )

    st.caption(
        "**IBKR:** jeden ticker, len **Call** alebo **Put**, zoznam expirácií a počet strike-ov okolo ATM. "
        "Vyžaduje pripojený TWS / IB Gateway. **Najprv** „Skontrolovať expirácie“; ak niektorá dátum nie je v reťazci, "
        "zaškrtni súhlas a potom „Synchronizovať do DB“."
    )
    cib = ibkr_ocg.is_connected()
    if cib:
        st.success("IBKR je pripojený.")
    else:
        st.warning("IBKR **nie je** pripojený — spusti TWS alebo IB Gateway a v aplikácii sa pripoj.")

    t_ib = st.text_input("Ticker", max_chars=12, key="ocg_ib_ticker", placeholder="SPY").strip().upper()
    side_ib = st.radio("Strana", ["Call", "Put"], horizontal=True, key="ocg_ib_side")
    exp_ib = st.text_area(
        "Expirácie (YYYY-MM-DD; čiarky alebo nový riadok)",
        height=100,
        key="ocg_ib_exp",
        placeholder="2026-06-19\n2026-07-17",
    )
    nstrikes_ib = st.number_input(
        "Počet strike-ov okolo ATM", min_value=1, max_value=300, value=11, step=1, key="ocg_ib_nst"
    )
    pause_ib = st.number_input(
        "Pauza medzi kontrakty (s)", min_value=0.0, max_value=3.0, value=0.2, step=0.05, key="ocg_ib_pause"
    )
    asof_ib = st.date_input("Dátum snímky (as-of)", value=date.today(), key="ocg_ib_asof")

    c1, c2 = st.columns(2)
    with c1:
        btn_check_ib = st.button("Skontrolovať expirácie", key="ocg_ib_btn_check")
    with c2:
        btn_sync_ib = st.button("Synchronizovať do DB", type="primary", key="ocg_ib_btn_sync")

    snap_ib = st.session_state.get("ocg_ib_val")
    form_changed_ib = (
        snap_ib is None
        or snap_ib.get("ticker") != t_ib
        or snap_ib.get("exps_raw") != exp_ib
        or snap_ib.get("side") != side_ib
        or int(snap_ib.get("nstrikes", -1)) != int(nstrikes_ib)
        or float(snap_ib.get("pause", -1.0)) != float(pause_ib)
        or snap_ib.get("asof") != asof_ib.isoformat()
    )

    if btn_check_ib:
        if not cib:
            st.error("Najprv pripoj IBKR.")
        elif not t_ib:
            st.error("Zadaj ticker.")
        else:
            raw_ib = parse_expiry_text(exp_ib)
            if not raw_ib:
                st.error("Zadaj aspoň jednu expiráciu.")
            else:
                v_ib, m_ib, err_ib = validate_expiries_against_secdef(t_ib, raw_ib)
                st.session_state["ocg_ib_val"] = {
                    "ticker": t_ib,
                    "exps_raw": exp_ib,
                    "valid": v_ib,
                    "missing": m_ib,
                    "err": err_ib,
                    "side": side_ib,
                    "nstrikes": int(nstrikes_ib),
                    "pause": float(pause_ib),
                    "asof": asof_ib.isoformat(),
                }
                st.rerun()

    snap_ib = st.session_state.get("ocg_ib_val")
    if snap_ib and snap_ib.get("ticker") == t_ib and snap_ib.get("exps_raw") == exp_ib:
        if snap_ib.get("err"):
            st.error(f"SecDef: {snap_ib['err']}")
        else:
            st.info(f"**Platné** expirácie ({len(snap_ib['valid'])}): `{', '.join(snap_ib['valid']) or '—'}`")
            if snap_ib.get("missing"):
                st.warning(
                    f"**Nie sú v IBKR reťazci** ({len(snap_ib['missing'])}): `{', '.join(snap_ib['missing'])}`"
                )
                st.checkbox(
                    "Beriem to na vedomie — pri synchronizácii použiť **iba platné** expirácie",
                    key="ocg_ib_allow_partial",
                )

    if btn_sync_ib:
        if not cib:
            st.error("Najprv pripoj IBKR.")
        elif not t_ib:
            st.error("Zadaj ticker.")
        elif form_changed_ib:
            st.error("Údaje sa zmenili oproti poslednej kontrole — klikni znova **Skontrolovať expirácie**.")
        else:
            raw_ib = parse_expiry_text(exp_ib)
            v_ib, m_ib, err_ib = validate_expiries_against_secdef(t_ib, raw_ib)
            if err_ib:
                st.error(f"SecDef: {err_ib}")
            elif not v_ib:
                st.error("Žiadna platná expirácia.")
            elif m_ib and not st.session_state.get("ocg_ib_allow_partial", False):
                st.error(
                    "Sú expirácie, ktoré IBKR v tomto reťazci nemá — zaškrtni súhlas vyššie "
                    "(po **Skontrolovať expirácie**) alebo uprav zoznam."
                )
            else:
                with st.spinner(f"Synchronizujem {t_ib} …"):
                    res_ib = sync_chain_snapshot(
                        t_ib,
                        right="call" if side_ib == "Call" else "put",
                        expiries_yyyy_mm_dd=v_ib,
                        strike_count=int(nstrikes_ib),
                        as_of_yyyy_mm_dd=asof_ib.isoformat(),
                        pause_s=float(pause_ib),
                    )
                for w in res_ib.warnings:
                    st.warning(w)
                for e in res_ib.errors:
                    st.error(e)
                if res_ib.rows_written:
                    st.success(
                        f"Zapísaných **{res_ib.rows_written}** riadkov do `{odb.db_path_for_ticker(t_ib)}`. "
                        f"Expirácie: {', '.join(res_ib.expiries_processed)}"
                    )
                elif res_ib.ok:
                    st.success("Hotovo.")
                else:
                    st.warning("Nič sa neimportovalo — skontroluj chyby vyššie alebo trhové dáta.")

elif option_chain_section == "Prehľad":
    st.caption(
        "**Návod:** Vyber **ticker** a prípadne filtre expirácie / dátumu snímky — prehliadaš dáta v lokálnej `data/option_chains/<TICKER>.db`, nie v hlavnom journal.db."
    )
    tickers = odb.list_chain_tickers()
    if not tickers:
        st.info(
            "Zatiaľ nie je žiadna DB — záložka **Import CSV**, **IBKR** alebo skript "
            "`scripts/import_barchart_option_chains.py` / `scripts/sync_option_chains_ibkr.py`."
        )
    else:
        ticker = st.selectbox("Ticker", options=tickers, key="ocg_view_ticker")
        st.caption(f"Súbor: `{odb.db_path_for_ticker(ticker)}`")
        snaps = odb.list_distinct_snapshots(ticker)
        if snaps.empty:
            st.warning("Tabuľka je prázdna.")
        else:
            exp_opts = ["(všetky)"] + sorted(snaps["expiry"].unique().tolist(), reverse=True)
            as_opts = ["(všetky)"] + sorted(snaps["as_of_date"].unique().tolist(), reverse=True)
            c1, c2 = st.columns(2)
            with c1:
                exp_pick = st.selectbox("Expirácia", options=exp_opts, key="ocg_f_exp")
            with c2:
                as_pick = st.selectbox("Snímka (as-of)", options=as_opts, key="ocg_f_as")
            exp_f = None if exp_pick == "(všetky)" else exp_pick
            as_f = None if as_pick == "(všetky)" else as_pick
            df = odb.read_chain(ticker, expiry=exp_f, as_of_date=as_f)
            status_df = odb.list_snapshot_status(ticker)
            if not status_df.empty and "expiry" in status_df.columns and "as_of_date" in status_df.columns:
                _se = pd.to_datetime(status_df["expiry"], errors="coerce")
                _sa = pd.to_datetime(status_df["as_of_date"], errors="coerce")
                status_df = status_df.copy()
                status_df["dte"] = (_se - _sa).dt.days
                _c = [x for x in status_df.columns if x != "dte"]
                _i = _c.index("as_of_date") + 1
                status_df = status_df[_c[:_i] + ["dte"] + _c[_i:]]
            if not status_df.empty:
                status_df["Stav"] = status_df.apply(
                    lambda r: _status_label(
                        bool(r["rows_with_options"]),
                        bool(r["rows_with_greeks"]),
                        int(r["rows"]) > 0,
                    ),
                    axis=1,
                )
                status_df["StavFarba"] = status_df["Stav"].map(_status_color)
                st.markdown("##### Stav importu")
                status_df["Môže sa doplniť"] = status_df.apply(
                    lambda r: "Áno" if int(r["rows"]) > 0 and (int(r["rows_with_options"]) == 0 or int(r["rows_with_greeks"]) == 0) else "Nie",
                    axis=1,
                )
                st.dataframe(
                    status_df.rename(
                        columns={
                            "expiry": "Expirácia",
                            "as_of_date": "Dátum snímky",
                            "dte": "DTE (k snímke)",
                            "rows": "Riadkov",
                            "has_options": "Options",
                            "has_greeks": "Greeks",
                            "rows_with_options": "Riadkov s options",
                            "rows_with_greeks": "Riadkov s greeks",
                            "Stav": "Stav",
                            "StavFarba": "Farba",
                            "Môže sa doplniť": "Môže sa doplniť",
                        }
                    ),
                    use_container_width=True,
                    hide_index=True,
                )
                st.caption("🟢 = kompletne spárované, 🔴 = iba časť dát v DB, 🟠 = čaká sa na druhý súbor.")
                for _, r in status_df.iterrows():
                    if int(r["rows"]) <= 0:
                        continue
                    if int(r["rows_with_options"]) > 0 and int(r["rows_with_greeks"]) > 0:
                        continue
                    side = _side_needed(int(r["rows_with_options"]), int(r["rows_with_greeks"]))
                    label = "greeks" if side == "greeks" else "options"
                    cols = st.columns([2, 2, 2, 1])
                    cols[0].write(f"**{r['expiry']}** · `{r['as_of_date']}`")
                    cols[1].write(f"chýba: **{label}**")
                    row_file = cols[2].file_uploader(
                        f"CSV pre {label}",
                        type=["csv"],
                        key=f"reimp_file_{ticker}_{r['expiry']}_{r['as_of_date']}",
                        label_visibility="collapsed",
                    )
                    if cols[3].button("Reimport", key=f"reimport_{ticker}_{r['expiry']}_{r['as_of_date']}"):
                        try:
                            if row_file is not None:
                                reason = _validate_reimport_file(
                                    file_name=getattr(row_file, "name", "") or "",
                                    expected_ticker=ticker,
                                    expected_expiry=r["expiry"],
                                    expected_as_of=r["as_of_date"],
                                    expected_side=side,
                                )
                                if reason:
                                    st.error(f"Opätovný import sa nepodaril: {reason}")
                                    continue
                                try:
                                    row_file.seek(0)
                                except Exception:
                                    pass
                                df_side = pd.read_csv(row_file, sep=None, engine="python", dtype=str)
                                if df_side.empty:
                                    st.error("Opätovný import sa nepodaril: CSV je prázdny (bez dátových riadkov).")
                                    continue
                                source_name = getattr(row_file, "name", f"{label}.csv")
                            else:
                                blob = _get_uploaded_blob_for_group((ticker, r["expiry"], r["as_of_date"]), side)
                                if not blob:
                                    st.warning("Vyber CSV pri riadku (alebo nahraj v hornej časti) a klikni Reimport.")
                                    continue
                                reason = _validate_reimport_file(
                                    file_name=blob["name"],
                                    expected_ticker=ticker,
                                    expected_expiry=r["expiry"],
                                    expected_as_of=r["as_of_date"],
                                    expected_side=side,
                                )
                                if reason:
                                    st.error(f"Opätovný import sa nepodaril: {reason}")
                                    continue
                                df_side = pd.read_csv(io.BytesIO(blob["bytes"]), sep=None, engine="python", dtype=str)
                                if df_side.empty:
                                    st.error("Opätovný import sa nepodaril: CSV je prázdny (bez dátových riadkov).")
                                    continue
                                source_name = blob["name"]
                            n = odb.import_snapshot_side_only(
                                ticker,
                                r["expiry"],
                                r["as_of_date"],
                                side,
                                source_name=source_name,
                                df=df_side,
                            )
                            st.success(f"Reimport hotový: {n} riadkov.")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Opätovný import sa nepodaril: {type(exc).__name__}: {exc}")
            if not df.empty and "expiry" in df.columns and "as_of_date" in df.columns:
                _e = pd.to_datetime(df["expiry"], errors="coerce")
                _a = pd.to_datetime(df["as_of_date"], errors="coerce")
                df = df.copy()
                _insert_at = list(df.columns).index("as_of_date") + 1
                df.insert(_insert_at, "dte", (_e - _a).dt.days)
            pretty = df.rename(
                columns={
                    "expiry": "Expirácia",
                    "as_of_date": "Dátum snímky",
                    "dte": "DTE (k snímke)",
                    "strike": "Strike",
                    "option_type": "Typ",
                    "bid": "Bid",
                    "mid": "Mid",
                    "ask": "Ask",
                    "last_price": "Posledná cena",
                    "moneyness_pct": "Moneyness",
                    "iv": "Implikovaná volatilita",
                    "delta": "Delta",
                    "gamma": "Gamma",
                    "theta": "Theta",
                    "vega": "Vega",
                    "rho": "Rho",
                    "theor": "Teória",
                    "volume": "Objem",
                    "open_interest": "Open Interest",
                    "vol_oi_ratio": "Pomer Objem/OI",
                    "itm_prob": "Pravdepodobnosť ITM",
                    "source_options_csv": "CSV opcií",
                    "source_greeks_csv": "CSV grékov",
                    "imported_at": "Importované",
                }
            )
            st.dataframe(pretty, use_container_width=True, hide_index=True)
            st.caption(
                f"Zobrazených **{len(df)}** riadkov. **DTE (k snímke)** = kalendárne dni do expirácie "
                "od dátumu snímky (sťahovania dát), nie od dneška."
            )
