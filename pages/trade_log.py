from typing import Optional

import streamlit as st
import pandas as pd
from datetime import date, datetime

from core import database as db
from core import ibkr
from core.page_context import set_tradejournal_page

db.init_db()
set_tradejournal_page("trade_log")


# ─── Helper funkcie ───────────────────────────────────────────────────────────
def _cell_float(v, default: float = 0.0) -> float:
    """Hodnota z data_editor (niekedy jednoprvkový list); NaN → default."""
    if isinstance(v, (list, tuple)) and len(v) == 1:
        v = v[0]
    if v is None:
        return default
    try:
        if pd.isna(v):
            return default
    except (ValueError, TypeError):
        pass
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _build_df(trades: list[dict], show_pnl: bool = False) -> pd.DataFrame:
    rows = []
    for t in trades:
        pnl = db.compute_pnl(t) if show_pnl else None
        th_v = t.get("theta_at_entry")
        comm = t.get("commission") or 0.0
        rows.append({
            "ID": t["id"],
            "Group": t.get("group_id", "") or "",
            "Ticker": t["ticker"],
            "Stratégia": t.get("strategy", ""),
            "Noha": t.get("leg_type", ""),
            "Typ": t.get("option_type", ""),
            "Strike": t.get("strike"),
            "Expiry": t.get("expiry", ""),
            "Kontrakty": t.get("contracts", 1),
            "Entry": t.get("entry_price"),
            "Exit": t.get("exit_price") if show_pnl else None,
            "Komisia": comm if show_pnl else None,
            "P&L čistý ($)": pnl,
            "Θ (entry) $/deň": f"${float(th_v):+.3f}" if th_v is not None else "—",
            "Entry dátum": t.get("entry_date", ""),
            "Exit dátum": t.get("exit_date", "") if show_pnl else None,
        })
    df = pd.DataFrame(rows)
    if show_pnl:
        df = df.sort_values("Entry dátum", na_position="last").reset_index(drop=True)
        df["P&L kumulatív ($)"] = df["P&L čistý ($)"].cumsum()
        df = df.drop(columns=["Θ (entry) $/deň"], errors="ignore")
    else:
        df = df.drop(columns=["Exit", "Komisia", "P&L čistý ($)", "Exit dátum"], errors="ignore")
    return df


def _col_config(pnl: bool = False) -> dict:
    cfg = {
        "Strike": st.column_config.NumberColumn(format="$%.2f"),
        "Entry": st.column_config.NumberColumn(format="$%.2f"),
    }
    if pnl:
        cfg["Exit"] = st.column_config.NumberColumn(format="$%.2f")
        cfg["Komisia"] = st.column_config.NumberColumn(format="$%.2f")
        cfg["P&L čistý ($)"] = st.column_config.NumberColumn(format="$%.2f")
        cfg["P&L kumulatív ($)"] = st.column_config.NumberColumn(format="$%.2f")
    return cfg


STRATEGIES = [
    "Long Call", "Long Put", "Short Call", "Short Put",
    "Covered Call", "Cash-Secured Put",
    "Bull Call Spread", "Bear Put Spread", "Bull Put Spread", "Bear Call Spread",
    "Diagonal", "Calendar Spread",
    "Iron Condor", "Straddle", "Strangle", "Butterfly",
    "Iné",
]

# Rovnaká hodnota ako pri „Pridať obchod“ — zoznam skupín je v záložke Skupiny
GROUP_NONE_LABEL = "— (bez skupiny) —"


def _group_select_options(trades_for_orphans: list[dict]) -> list[str]:
    """Skupiny z tabuľky Skupiny + prípadné group_id z obchodov, ktoré v Skupinách ešte nie sú."""
    registered = db.get_group_names()
    reg_set = set(registered)
    extra = sorted(
        {
            (t.get("group_id") or "").strip()
            for t in trades_for_orphans
            if (t.get("group_id") or "").strip() and (t.get("group_id") or "").strip() not in reg_set
        }
    )
    return [GROUP_NONE_LABEL] + registered + extra


def _group_id_from_select(cell_value: str) -> Optional[str]:
    s = (cell_value or "").strip()
    if not s or s == GROUP_NONE_LABEL:
        return None
    return s

st.title("Trade Log")
st.caption(
    "**Návod:** Použi štyri záložky nižšie — **Pridať** každú nohu zvlášť, **Otvorené** na uzávierku nohy, **Upraviť / Zoskupiť** na hromadné zmeny a skupiny, **Uzavreté** len na prehľad P&L. "
    "**Stratégia** pri novom obchode je v *Pridať obchod*; šablóny spreadov v **Spread Builder**; názvy skupín v **Skupiny**."
)

tab_add, tab_open, tab_edit, tab_closed = st.tabs([
    "Pridať obchod", "Otvorené pozície", "Upraviť / Zoskupiť", "Uzavreté pozície"
])

# ─── Tab: Pridať obchod ───────────────────────────────────────────────────────
with tab_add:
    st.caption(
        "**Návod:** Vyber ticker (najprv ho pridaj v **Symboly**), stratégiu a **skupinu** zo zoznamu zo záložky **Skupiny**. "
        "Každá noha stratégie = samostatné odoslanie formulára. Voliteľne IV pri vstupe pre výpočty v iných stránkach."
    )
    st.subheader("Zadanie nového obchodu")

    with st.form("add_trade_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            _sym_tickers = db.get_symbol_tickers()
            if _sym_tickers:
                _sym_opts = _sym_tickers + ["— vlastný ticker —"]
                _sym_sel = st.selectbox("Ticker *", _sym_opts, help="Symboly spravuješ v záložke Symboly")
                if _sym_sel == "— vlastný ticker —":
                    ticker = st.text_input("Zadaj ticker", value="").upper().strip()
                else:
                    ticker = _sym_sel
            else:
                ticker = st.text_input("Ticker *", value="", placeholder="napr. AMZN").upper().strip()
                st.caption("💡 Pridaj symboly v záložke **Symboly** pre rýchly výber.")
        with c2:
            strategy = st.selectbox("Stratégia *", STRATEGIES)
        with c3:
            group_names = [GROUP_NONE_LABEL] + db.get_group_names()
            group_sel = st.selectbox("Skupina (Group ID)", group_names,
                                     help="Skupiny spravuješ v záložke Skupiny")
            group_id = group_sel if group_sel != GROUP_NONE_LABEL else ""

        st.markdown("**Noha (Leg)**")
        c4, c5, c6, c7 = st.columns(4)
        with c4:
            leg_type = st.selectbox("Typ nohy", ["Short", "Long"])
        with c5:
            option_type = st.selectbox("Opcia", ["Call", "Put"])
        with c6:
            strike = st.number_input("Strike ($) *", min_value=0.0, step=0.5)
        with c7:
            expiry_date = st.date_input("Expiry *", value=date.today(), min_value=date.today())

        c8, c9, c10, c10b = st.columns([1, 1, 1, 1])
        with c8:
            contracts = st.number_input("Kontrakty", min_value=1, value=1, step=1)
        with c9:
            entry_price = st.number_input("Entry cena (prémia) *", min_value=0.0, step=0.01)
        with c10:
            entry_date = st.date_input("Dátum vstupu", value=date.today())
        with c10b:
            commission_input = st.number_input(
                "Komisia ($)", min_value=0.0, step=0.01, value=0.0,
                help="Celková komisia brokera za otvorenie (napr. 0.65 × počet kontraktov)"
            )

        st.markdown("**Voliteľné — IV a Theta**")
        iv_input = st.number_input(
            "IV pri vstupe (napr. 0.30)",
            min_value=0.0,
            max_value=5.0,
            step=0.01,
            value=0.0,
            help="Uloží sa do denníka ako desatinný zlomok (0,30 = 30 %).",
        )

        use_theta_entry = st.checkbox("Doplniť Θ pri vstupe (USD/deň za nohu, napr. z TWS)", value=False)
        theta_entry_input = st.number_input(
            "Theta pri vstupe ($/deň)",
            min_value=-9999.0,
            max_value=9999.0,
            step=0.001,
            format="%.3f",
            value=0.0,
            disabled=not use_theta_entry,
            help="Súčet theta pre túto nohu v dolároch za deň (ako v portfóliu TWS).",
        )

        use_delta_entry = st.checkbox("Doplniť Δ pri vstupe (z TWS / vlastná poznámka)", value=False)
        delta_entry_input = st.number_input(
            "Delta pri vstupe (−1 … 1)",
            min_value=-1.0,
            max_value=1.0,
            step=0.01,
            format="%.4f",
            value=0.0,
            disabled=not use_delta_entry,
            help="Delta kontraktu pri otvorení. Zapni checkbox vyššie, ak chceš hodnotu uložiť.",
        )

        submitted = st.form_submit_button("Uložiť obchod", type="primary", use_container_width=True)

    if submitted:
        if not ticker or strike <= 0 or entry_price <= 0:
            st.error("Vyplň: Ticker, Strike a Entry cenu.")
        else:
            expiry_str = expiry_date.strftime("%Y%m%d")

            trade_id = db.add_trade(
                ticker=ticker,
                strategy=strategy,
                leg_type=leg_type,
                option_type=option_type,
                strike=strike,
                expiry=expiry_str,
                contracts=int(contracts),
                entry_price=entry_price,
                entry_date=entry_date.isoformat(),
                group_id=group_id if group_id else None,
                iv_at_entry=iv_input if iv_input > 0 else None,
                pop_at_entry=None,
                commission=commission_input if commission_input > 0 else None,
                delta_at_entry=float(delta_entry_input) if use_delta_entry else None,
                theta_at_entry=float(theta_entry_input) if use_theta_entry else None,
            )
            st.success(f"Obchod #{trade_id} uložený — {ticker} {leg_type} {option_type} ${strike:.0f}.")
            st.rerun()


# ─── Tab: Otvorené pozície ────────────────────────────────────────────────────
with tab_open:
    st.caption(
        "**Návod:** Prehľad otvorených nôh zoskupených podľa **Group ID**. V expandéri uzavrieš alebo zmažeš jednu nohu — "
        "úprava viacerých polí je v záložke **Upraviť / Zoskupiť**."
    )
    open_trades = db.get_open_trades()

    if not open_trades:
        st.info("Žiadne otvorené pozície.")
    else:
        # Zoskup podľa group_id
        grouped: dict[str, list] = {}
        for t in open_trades:
            gid = (t.get("group_id") or "").strip() or "— bez skupiny —"
            grouped.setdefault(gid, []).append(t)

        st.caption(f"{len(open_trades)} otvorených nôh · {len(grouped)} skupín/stratégií")

        for gid, legs in sorted(grouped.items()):
            # Súhrn skupiny
            tickers = ", ".join(sorted({t["ticker"] for t in legs}))
            strategies = ", ".join(sorted({t.get("strategy","") for t in legs if t.get("strategy")}))
            expiries = sorted([t.get("expiry","") for t in legs if t.get("expiry")])
            dte_list = []
            for t in legs:
                exp = t.get("expiry","")
                if exp:
                    try:
                        exp_d = datetime.strptime(exp, "%Y%m%d").date()
                        dte_list.append((datetime.now().date() - exp_d).days * -1)
                    except Exception:
                        pass
            nearest_dte = min(dte_list) if dte_list else None
            dte_badge = f"⏳ {nearest_dte} dní" if nearest_dte is not None else ""

            # Farba podľa DTE
            if nearest_dte is not None and nearest_dte <= 21:
                icon = "🔴"
            elif nearest_dte is not None and nearest_dte <= 45:
                icon = "🟡"
            else:
                icon = "🟢"

            header = f"{icon} **{gid}** &nbsp;·&nbsp; {tickers} &nbsp;·&nbsp; {strategies} &nbsp;·&nbsp; {len(legs)} nôh &nbsp;·&nbsp; {dte_badge}"

            with st.expander(header, expanded=True):
                # Tabuľka nôh
                leg_rows = []
                for t in sorted(legs, key=lambda x: x.get("expiry","")):
                    exp_raw = t.get("expiry","")
                    exp_fmt = exp_raw
                    dte_val = None
                    if exp_raw:
                        try:
                            exp_d = datetime.strptime(exp_raw, "%Y%m%d").date()
                            exp_fmt = exp_d.strftime("%d.%m.%Y")
                            dte_val = (exp_d - datetime.now().date()).days
                        except Exception:
                            pass
                    _iv_e = t.get("iv_at_entry")
                    _d_e = t.get("delta_at_entry")
                    _th_e = t.get("theta_at_entry")
                    leg_rows.append({
                        "ID": t["id"],
                        "Noha": t.get("leg_type",""),
                        "Typ": t.get("option_type",""),
                        "Strike $": t.get("strike"),
                        "Expiry": exp_fmt,
                        "DTE": dte_val,
                        "Kontr.": t.get("contracts",1),
                        "Entry $": t.get("entry_price"),
                        "IV entry": f"{float(_iv_e) * 100:.1f} %" if _iv_e is not None else "—",
                        "Δ vstup": f"{float(_d_e):.4f}" if _d_e is not None else "—",
                        "Θ vstup": f"${float(_th_e):+.3f}/deň" if _th_e is not None else "—",
                    })
                st.dataframe(
                    pd.DataFrame(leg_rows),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Strike $": st.column_config.NumberColumn(format="$%.2f"),
                        "Entry $":  st.column_config.NumberColumn(format="$%.2f"),
                        "DTE":      st.column_config.NumberColumn(format="%d dní"),
                    },
                )

                # Uzavrieť nohu
                with st.expander("Uzavrieť / Zmazať nohu"):
                    leg_opts = {
                        f"#{t['id']} {t.get('leg_type','')} {t.get('option_type','')} ${t.get('strike',0):.0f} exp {t.get('expiry','')}": t["id"]
                        for t in legs
                    }
                    with st.form(f"close_form_{gid.replace(' ','_')}"):
                        sel_leg = st.selectbox("Noha", list(leg_opts.keys()), key=f"cl_sel_{gid}")
                        cc1, cc2 = st.columns(2)
                        with cc1:
                            exit_p = st.number_input("Exit cena", min_value=0.0, step=0.01, key=f"cl_ep_{gid}")
                        with cc2:
                            exit_d = st.date_input("Exit dátum", value=date.today(), key=f"cl_ed_{gid}")
                        cb1, cb2 = st.columns(2)
                        with cb1:
                            close_btn = st.form_submit_button("Uzavrieť", type="primary", use_container_width=True)
                        with cb2:
                            del_btn = st.form_submit_button("Zmazať", type="secondary", use_container_width=True)

                    if close_btn:
                        db.close_trade(leg_opts[sel_leg], exit_p, exit_d.isoformat())
                        st.success(f"Noha #{leg_opts[sel_leg]} uzavretá za ${exit_p:.2f}")
                        st.rerun()
                    if del_btn:
                        db.delete_trade(leg_opts[sel_leg])
                        st.warning("Noha zmazaná.")
                        st.rerun()


# ─── Tab: Upraviť / Zoskupiť ─────────────────────────────────────────────────
with tab_edit:
    st.caption(
        "**Návod:** Uprav bunky v tabuľke a stlač **Uložiť zmeny**. Stĺpec **Skupina** = výber z **Skupiny** + existujúce hodnoty. "
        "Nižšie môžeš rozdeliť kontrakty alebo hromadne priradiť skupinu podľa ID nôh."
    )
    all_edit_trades = db.get_all_trades()

    if not all_edit_trades:
        st.info("Žiadne obchody.")
    else:
        # ── 1. Prepínač Open / Closed ───────────────────────────────────────
        edit_filter = st.radio(
            "Zobraz",
            ["Otvorené", "Uzavreté", "Všetky"],
            horizontal=True,
            key="edit_filter_radio",
        )
        if edit_filter == "Otvorené":
            edit_trades_filtered = [t for t in all_edit_trades if t.get("status") == "Open"]
        elif edit_filter == "Uzavreté":
            edit_trades_filtered = [t for t in all_edit_trades if t.get("status") == "Closed"]
        else:
            edit_trades_filtered = all_edit_trades

        group_opts_editor = _group_select_options(edit_trades_filtered)
        group_opts_all = _group_select_options(all_edit_trades)

        st.caption(f"{len(edit_trades_filtered)} záznam(ov) · Uprav priamo v tabuľke, potom klikni **Uložiť zmeny**.")

        edit_rows = []
        for t in edit_trades_filtered:
            raw_gid = (t.get("group_id") or "").strip()
            gid_cell = raw_gid if raw_gid in group_opts_editor else GROUP_NONE_LABEL
            edit_rows.append({
                "ID": t["id"],
                "Ticker": t["ticker"],
                "Status": t.get("status", "Open"),
                "Noha": t.get("leg_type", "Short"),
                "Typ": t.get("option_type", "Call"),
                "Strike": float(t.get("strike", 0.0)),
                "Expiry": t.get("expiry", ""),
                "Kontrakty": int(t.get("contracts", 1)),
                "Entry $": float(t.get("entry_price", 0.0)),
                "Exit $": float(t.get("exit_price", 0.0)) if t.get("exit_price") else 0.0,
                "Komisia $": float(t.get("commission") or 0.0),
                "Exit Date": t.get("exit_date", ""),
                "Skupina": gid_cell,
                "Stratégia": t.get("strategy") or "",
            })

        _edit_df = pd.DataFrame(edit_rows)
        for _num_col in ("Strike", "Entry $", "Exit $", "Komisia $"):
            _edit_df[_num_col] = _edit_df[_num_col].astype("float64")

        edited_df = st.data_editor(
            _edit_df,
            use_container_width=True,
            hide_index=True,
            disabled=["ID"],
            column_config={
                "Status": st.column_config.SelectboxColumn("Status", options=["Open", "Closed"]),
                "Noha": st.column_config.SelectboxColumn("Noha", options=["Short", "Long"]),
                "Typ": st.column_config.SelectboxColumn("Typ", options=["Call", "Put"]),
                "Stratégia": st.column_config.SelectboxColumn("Stratégia", options=STRATEGIES),
                "Strike": st.column_config.NumberColumn("Strike", format="$%.2f", step=0.01),
                "Entry $": st.column_config.NumberColumn("Entry $", format="$%.2f", step=0.01),
                "Exit $": st.column_config.NumberColumn("Exit $", format="$%.2f", step=0.01),
                "Komisia $": st.column_config.NumberColumn("Komisia $", format="$%.2f", step=0.01,
                                                            help="Celková komisia brokera (entry + exit)"),
                "Expiry": st.column_config.TextColumn("Expiry", help="Formát: YYYYMMDD"),
                "Exit Date": st.column_config.TextColumn("Exit Date", help="Formát: YYYY-MM-DD"),
                "Skupina": st.column_config.SelectboxColumn(
                    "Skupina",
                    options=group_opts_editor,
                    help="Zoznam zo záložky Skupiny",
                ),
            },
            key="edit_table_v2",
        )

        if st.button("Uložiť zmeny", type="primary", key="save_edit_btn_v2"):
            changed = 0
            orig_map = {t["id"]: t for t in all_edit_trades}
            for _, row in edited_df.iterrows():
                tid = int(row["ID"])
                orig = orig_map.get(tid, {})
                
                # Sledujeme zmeny
                updates = {}
                if row["Ticker"] != orig.get("ticker"): updates["ticker"] = row["Ticker"]
                if row["Status"] != orig.get("status"): updates["status"] = row["Status"]
                if row["Noha"] != orig.get("leg_type"): updates["leg_type"] = row["Noha"]
                if row["Typ"] != orig.get("option_type"): updates["option_type"] = row["Typ"]
                _strike = _cell_float(row["Strike"])
                if _strike != float(orig.get("strike", 0)): updates["strike"] = _strike
                if row["Expiry"] != orig.get("expiry"): updates["expiry"] = row["Expiry"]
                if int(row["Kontrakty"]) != int(orig.get("contracts", 1)): updates["contracts"] = int(row["Kontrakty"])
                _entry_p = _cell_float(row["Entry $"])
                if _entry_p != float(orig.get("entry_price", 0)): updates["entry_price"] = _entry_p
                
                # Exit cena a dátum
                new_exit_p = _cell_float(row["Exit $"])
                if new_exit_p != float(orig.get("exit_price") or 0.0): 
                    updates["exit_price"] = new_exit_p if new_exit_p > 0 else None
                
                new_exit_d = (row["Exit Date"] or "").strip() or None
                if new_exit_d != orig.get("exit_date"): updates["exit_date"] = new_exit_d
                
                new_group = _group_id_from_select(row["Skupina"])
                if new_group != (orig.get("group_id") or None): updates["group_id"] = new_group
                
                if row["Stratégia"] != orig.get("strategy"): updates["strategy"] = row["Stratégia"]

                new_comm = _cell_float(row.get("Komisia $"))
                if new_comm != float(orig.get("commission") or 0.0):
                    updates["commission"] = new_comm if new_comm > 0 else None

                if updates:
                    db.update_trade(trade_id=tid, **updates)
                    changed += 1
            
            if changed:
                st.success(f"Uložené — zmenených {changed} záznam(ov).")
                st.rerun()
            else:
                st.info("Žiadne zmeny.")

        st.divider()

        # ── 1b. Rozdeliť pozíciu ────────────────────────────────────────────
        st.subheader("Rozdeliť pozíciu na samostatné nohy")
        st.caption("Napr. Long 205 Call ×2 → noha A (Diagonal skupina 1) + noha B (skupina 2)")

        multi_trades = [t for t in all_edit_trades if int(t.get("contracts", 1)) > 1]
        if not multi_trades:
            st.info("Žiadna pozícia s viac ako 1 kontraktom.")
        else:
            split_options = {
                f"#{t['id']} | {t['ticker']} {t.get('leg_type','')} {t.get('option_type','')} "
                f"${t.get('strike',0):.0f} ×{t.get('contracts',1)} kontr.": t["id"]
                for t in multi_trades
            }
            split_label = st.selectbox("Vyber pozíciu na rozdelenie", list(split_options.keys()), key="split_sel")
            split_id = split_options[split_label]
            split_trade_obj = next((t for t in multi_trades if t["id"] == split_id), None)
            n_contracts = int(split_trade_obj.get("contracts", 2)) if split_trade_obj else 2

            st.markdown(f"Rozdelí sa na **{n_contracts}** nôh po 1 kontrakte. Vyber skupinu pre každú nohu:")
            split_group_inputs = []
            for i in range(n_contracts):
                raw = (split_trade_obj.get("group_id") or "").strip()
                split_default = raw if raw in group_opts_all else GROUP_NONE_LABEL
                gsel = st.selectbox(
                    f"Skupina — noha {i + 1}",
                    group_opts_all,
                    index=group_opts_all.index(split_default) if split_default in group_opts_all else 0,
                    key=f"split_gid_{i}",
                )
                split_group_inputs.append("" if gsel == GROUP_NONE_LABEL else gsel)

            if st.button("Rozdeliť pozíciu", type="primary", key="split_btn"):
                new_ids = db.split_trade(split_id, split_group_inputs)
                st.success(f"Pozícia #{split_id} rozdelená na nohy: {new_ids}")
                st.rerun()

        st.divider()

        # ── 2. Rýchle hromadné Group ID ────────────────────────────────────
        st.subheader("Rýchle hromadné priradenie skupiny")
        st.caption("Zadaj ID nôh oddelené čiarkou a vyber skupinu zo zoznamu (záložka Skupiny).")

        rc1, rc2 = st.columns([2, 3])
        with rc1:
            bulk_ids_input = st.text_input("ID nôh (napr. 1,2,3)", placeholder="1,2,3", key="bulk_ids_v1")
        with rc2:
            bulk_group_input = st.selectbox("Skupina", group_opts_all, key="bulk_grp_v1")

        if st.button("Priradiť", type="primary", key="quick_group_btn"):
            try:
                ids_list = [int(x.strip()) for x in bulk_ids_input.split(",") if x.strip()]
                if not ids_list:
                    st.warning("Zadaj aspoň jedno ID.")
                else:
                    gid_apply = "" if bulk_group_input == GROUP_NONE_LABEL else bulk_group_input.strip()
                    db.bulk_set_group_id(ids_list, gid_apply)
                    lbl = gid_apply or GROUP_NONE_LABEL
                    st.success(f"Skupina **{lbl}** priradená nohám: {ids_list}")
                    st.rerun()
            except ValueError:
                st.error("Neplatné ID — zadaj čísla oddelené čiarkou.")

        st.divider()

        # ── 3. Rýchla zmena Group ID ────────────────────────────────────────
        st.subheader("Rýchle priradenie skupiny (kompaktný formulár)")
        rc1, rc2, rc3 = st.columns([2, 3, 1])
        with rc1:
            bulk_ids_input2 = st.text_input("ID nôh (napr. 11,12)", placeholder="11,12,14", key="bulk_ids_v2")
        with rc2:
            bulk_group_input2 = st.selectbox("Skupina", group_opts_all, key="bulk_grp_v2")
        with rc3:
            st.write("")
            st.write("")
            if st.button("Priradiť", type="primary", key="quick_group_btn_v2", use_container_width=True):
                try:
                    ids_list = [int(x.strip()) for x in bulk_ids_input2.split(",") if x.strip()]
                    if ids_list:
                        gid_apply = "" if bulk_group_input2 == GROUP_NONE_LABEL else bulk_group_input2.strip()
                        db.bulk_set_group_id(ids_list, gid_apply)
                        lbl = gid_apply or GROUP_NONE_LABEL
                        st.success(f"Skupina **{lbl}** priradená nohám: {ids_list}")
                        st.rerun()
                except ValueError:
                    st.error("Neplatné ID.")


# ─── Tab: Uzavreté pozície ────────────────────────────────────────────────────
with tab_closed:
    st.caption(
        "**Návod:** Uzavreté nohy podľa skupiny so súhrnom P&L. Len prehľad — spätná zmena stavu je v **Upraviť / Zoskupiť**."
    )
    closed_trades = db.get_closed_trades()

    if not closed_trades:
        st.info("Žiadne uzavreté obchody.")
    else:
        total_pnl = sum(db.compute_pnl(t) or 0 for t in closed_trades)
        wins_all = sum(1 for t in closed_trades if (db.compute_pnl(t) or 0) > 0)
        wr_all = wins_all / len(closed_trades) * 100 if closed_trades else 0

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Celkový P&L", f"${total_pnl:+.2f}")
        m2.metric("Uzavretých nôh", len(closed_trades))
        m3.metric("Win Rate", f"{wr_all:.1f}%")
        m4.metric("Wins / Losses", f"{wins_all} / {len(closed_trades)-wins_all}")

        st.divider()

        # Zoskup uzavreté podľa group_id
        closed_grouped: dict[str, list] = {}
        for t in closed_trades:
            gid = (t.get("group_id") or "").strip() or "— bez skupiny —"
            closed_grouped.setdefault(gid, []).append(t)

        st.caption(f"Zobrazené podľa skupín ({len(closed_grouped)} skupín)")

        for gid, legs in sorted(closed_grouped.items(), key=lambda x: -(sum(db.compute_pnl(t) or 0 for t in x[1]))):
            group_pnl = sum(db.compute_pnl(t) or 0 for t in legs)
            tickers = ", ".join(sorted({t["ticker"] for t in legs}))
            pnl_icon = "🟢" if group_pnl >= 0 else "🔴"
            header = f"{pnl_icon} **{gid}** &nbsp;·&nbsp; {tickers} &nbsp;·&nbsp; {len(legs)} nôh &nbsp;·&nbsp; P&L: **${group_pnl:+.2f}**"

            with st.expander(header, expanded=False):
                c_rows = []
                for t in sorted(legs, key=lambda x: x.get("exit_date") or ""):
                    pnl_v = db.compute_pnl(t)
                    exp_raw = t.get("expiry","")
                    exp_fmt = exp_raw
                    if exp_raw:
                        try:
                            exp_fmt = datetime.strptime(exp_raw, "%Y%m%d").strftime("%d.%m.%Y")
                        except Exception:
                            pass
                    c_rows.append({
                        "ID": t["id"],
                        "Noha": t.get("leg_type",""),
                        "Typ": t.get("option_type",""),
                        "Strike $": t.get("strike"),
                        "Expiry": exp_fmt,
                        "Kontr.": t.get("contracts",1),
                        "Entry $": t.get("entry_price"),
                        "Exit $": t.get("exit_price"),
                        "Komisia $": t.get("commission") or 0.0,
                        "P&L čistý $": round(pnl_v, 2) if pnl_v is not None else None,
                        "Exit dátum": t.get("exit_date",""),
                    })
                df_g = pd.DataFrame(c_rows)
                st.dataframe(
                    df_g,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Strike $":    st.column_config.NumberColumn(format="$%.2f"),
                        "Entry $":     st.column_config.NumberColumn(format="$%.2f"),
                        "Exit $":      st.column_config.NumberColumn(format="$%.2f"),
                        "Komisia $":   st.column_config.NumberColumn(format="$%.2f"),
                        "P&L čistý $": st.column_config.NumberColumn(format="$%.2f"),
                    },
                )
                st.caption(f"Celkový P&L skupiny: **${group_pnl:+.2f}**")
