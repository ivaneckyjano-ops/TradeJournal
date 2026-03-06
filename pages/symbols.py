import streamlit as st
import pandas as pd
from datetime import date

from core import database as db

db.init_db()

SECTORS = [
    "—", "Technology", "Consumer Discretionary", "Consumer Staples",
    "Healthcare", "Financials", "Energy", "Utilities",
    "Real Estate", "Materials", "Industrials", "Communication Services",
    "Iné",
]

ASSET_TYPES = ["Stock", "ETF", "Index Options", "Futures", "Crypto", "Iné"]

EARN_LABELS = ["1. Earnings", "2. Earnings", "3. Earnings", "4. Earnings"]


def _date_or_none(d) -> str | None:
    """Vráti ISO string alebo None pre date_input hodnotu."""
    if d is None:
        return None
    try:
        return d.isoformat()
    except Exception:
        return None


def _parse_date(s: str | None):
    """Vráti date objekt alebo None."""
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


st.title("📌 Symboly")
st.caption(
    "Centrálna správa tickerov — definuj symboly raz a vyberaj ich z dropdownu "
    "v celom denníku (Trade Log, Roll Simulátor, Kalendár, Dashboard)."
)

tab_add, tab_manage = st.tabs(["Pridať symbol", "Prehľad a úprava"])

# ─── Tab: Pridať symbol ───────────────────────────────────────────────────────
with tab_add:
    st.subheader("Nový symbol")

    with st.form("add_symbol_form", clear_on_submit=True):
        # ── Základné info ──────────────────────────────────────────────────────
        c1, c2, c3 = st.columns([1, 2, 1])
        with c1:
            s_ticker = st.text_input("Ticker *", placeholder="napr. AMZN").upper().strip()
        with c2:
            s_company = st.text_input("Názov spoločnosti", placeholder="napr. Amazon.com, Inc.")
        with c3:
            s_type = st.selectbox("Typ aktíva", ASSET_TYPES)

        c4, c5 = st.columns(2)
        with c4:
            s_sector = st.selectbox("Sektor", SECTORS)
        with c5:
            s_iv = st.number_input(
                "IV Rank / Percentil (%)",
                min_value=0.0, max_value=100.0, value=0.0, step=0.5,
                help="Aktuálny IV rank pre rýchlu orientáciu (nepovinné)",
            )

        # ── Investor Relations odkaz ───────────────────────────────────────────
        s_ir = st.text_input(
            "Investor Relations URL",
            placeholder="napr. https://ir.aboutamazon.com",
            help="Odkaz na IR stránku spoločnosti — otvorí sa priamo z denníka",
        )

        # ── 4 Earnings termíny ─────────────────────────────────────────────────
        st.markdown("**Earnings termíny** (nepovinné — automaticky sa zobrazia v Kalendári)")
        ec1, ec2, ec3, ec4 = st.columns(4)
        earn_inputs = []
        for i, (col, lbl) in enumerate(zip([ec1, ec2, ec3, ec4], EARN_LABELS)):
            with col:
                earn_inputs.append(
                    st.date_input(
                        lbl,
                        value=None,
                        min_value=date(2020, 1, 1),
                        max_value=date(date.today().year + 3, 12, 31),
                        key=f"new_earn_{i}",
                    )
                )

        s_desc = st.text_area(
            "Poznámky k symbolu (nepovinné)",
            placeholder="napr. 'Obchodujem len diagonály, IV rank > 40%, zatvárať 21 DTE'",
            height=80,
        )

        submitted = st.form_submit_button("Pridať symbol", type="primary", use_container_width=True)

    if submitted:
        if not s_ticker:
            st.error("Ticker je povinný.")
        else:
            earn_dates = [_date_or_none(d) for d in earn_inputs]
            sid = db.add_symbol(
                ticker=s_ticker,
                company_name=s_company,
                sector=s_sector if s_sector != "—" else "",
                asset_type=s_type,
                description=s_desc,
                earnings_date=earn_dates[0],
                earnings_date_2=earn_dates[1],
                earnings_date_3=earn_dates[2],
                earnings_date_4=earn_dates[3],
                ir_url=s_ir.strip() or None,
                iv_rank=s_iv if s_iv > 0 else None,
            )
            if sid > 0:
                st.success(f"Symbol **{s_ticker}** pridaný!")
                # Automaticky vytvor earnings eventy v Kalendári
                added_earn = 0
                for i, ed in enumerate(earn_dates):
                    if ed:
                        db.add_event(
                            date=ed,
                            event_type="earnings",
                            title=f"Earnings: {s_ticker}",
                            ticker=s_ticker,
                            description=s_company or "",
                        )
                        added_earn += 1
                if added_earn:
                    st.info(f"{added_earn} earnings dátum(y) automaticky pridané do Kalendára.")
                st.rerun()
            else:
                st.warning(
                    f"Symbol **{s_ticker}** už existuje. "
                    "Uprav ho v záložke **Prehľad a úprava**."
                )

    # Rýchly prehľad existujúcich symbolov
    existing = db.get_symbols()
    if existing:
        st.divider()
        st.caption(f"Aktuálne symboly ({len(existing)}):")
        cols = st.columns(min(len(existing), 6))
        for i, sym in enumerate(existing):
            with cols[i % 6]:
                iv_txt = f" · IV {sym['iv_rank']:.0f}%" if sym.get("iv_rank") else ""
                next_earn = sym.get("earnings_date") or ""
                earn_txt = f" · E: {next_earn}" if next_earn else ""
                ir_badge = " 🔗" if sym.get("ir_url") else ""
                st.markdown(
                    f"<div style='background:#1e293b; border-radius:8px; padding:8px 10px; "
                    f"margin:4px 0; text-align:center'>"
                    f"<b style='font-size:1.1rem;color:#60a5fa'>{sym['ticker']}{ir_badge}</b><br>"
                    f"<span style='font-size:0.7rem;color:#94a3b8'>"
                    f"{(sym.get('company_name') or '')[:20]}</span><br>"
                    f"<span style='font-size:0.7rem;color:#64748b'>"
                    f"{sym.get('asset_type','')}{iv_txt}{earn_txt}</span>"
                    "</div>",
                    unsafe_allow_html=True,
                )


# ─── Tab: Prehľad a úprava ────────────────────────────────────────────────────
with tab_manage:
    st.subheader("Všetky symboly")

    symbols = db.get_symbols()
    all_trades = db.get_all_trades()

    if not symbols:
        st.info("Žiadne symboly. Pridaj ich v záložke **Pridať symbol**.")
    else:
        # Súhrnná tabuľka
        rows = []
        for s in symbols:
            trade_count = sum(1 for t in all_trades if t.get("ticker", "").upper() == s["ticker"])
            open_count  = sum(1 for t in all_trades if t.get("ticker", "").upper() == s["ticker"] and t.get("status") == "Open")
            earn_dates = ", ".join(
                d for d in [
                    s.get("earnings_date"), s.get("earnings_date_2"),
                    s.get("earnings_date_3"), s.get("earnings_date_4"),
                ] if d
            )
            rows.append({
                "Ticker": s["ticker"],
                "Názov": s.get("company_name") or "—",
                "Typ": s.get("asset_type") or "—",
                "Sektor": s.get("sector") or "—",
                "IV Rank (%)": s.get("iv_rank"),
                "Earnings termíny": earn_dates or "—",
                "IR": "🔗" if s.get("ir_url") else "—",
                "Otv. pozície": open_count,
                "Všetky obchody": trade_count,
            })
        df_sym = pd.DataFrame(rows)
        st.dataframe(
            df_sym, hide_index=True, use_container_width=True,
            column_config={
                "IV Rank (%)": st.column_config.NumberColumn(format="%.1f %%"),
                "Otv. pozície": st.column_config.NumberColumn(),
                "Všetky obchody": st.column_config.NumberColumn(),
            },
        )

        st.divider()
        st.subheader("Editácia")

        for sym in symbols:
            ticker_trades = [t for t in all_trades if t.get("ticker", "").upper() == sym["ticker"]]
            open_cnt = sum(1 for t in ticker_trades if t.get("status") == "Open")
            ir_icon = " 🔗" if sym.get("ir_url") else ""

            with st.expander(
                f"**{sym['ticker']}**{ir_icon} &nbsp;·&nbsp; "
                f"{sym.get('company_name') or '—'} "
                f"&nbsp;·&nbsp; {sym.get('asset_type', '')} "
                f"&nbsp;·&nbsp; {open_cnt} otv. pozícií",
                expanded=False,
            ):
                # IR odkaz (kliknuteľný ak existuje)
                if sym.get("ir_url"):
                    st.markdown(
                        f"🔗 [Investor Relations — {sym['ticker']}]({sym['ir_url']})",
                        unsafe_allow_html=False,
                    )

                with st.form(f"edit_sym_{sym['id']}"):
                    # ── Základné polia ─────────────────────────────────────────
                    ec1, ec2, ec3 = st.columns([1, 2, 1])
                    with ec1:
                        e_ticker = st.text_input(
                            "Ticker", value=sym["ticker"], key=f"st_{sym['id']}"
                        ).upper().strip()
                    with ec2:
                        e_company = st.text_input(
                            "Názov", value=sym.get("company_name") or "",
                            key=f"sc_{sym['id']}"
                        )
                    with ec3:
                        e_type = st.selectbox(
                            "Typ", ASSET_TYPES,
                            index=ASSET_TYPES.index(sym["asset_type"])
                            if sym.get("asset_type") in ASSET_TYPES else 0,
                            key=f"sat_{sym['id']}"
                        )

                    ec4, ec5 = st.columns(2)
                    with ec4:
                        cur_sector = sym.get("sector") or "—"
                        sector_idx = SECTORS.index(cur_sector) if cur_sector in SECTORS else 0
                        e_sector = st.selectbox(
                            "Sektor", SECTORS, index=sector_idx,
                            key=f"ss_{sym['id']}"
                        )
                    with ec5:
                        e_iv = st.number_input(
                            "IV Rank (%)", min_value=0.0, max_value=100.0,
                            value=float(sym.get("iv_rank") or 0.0), step=0.5,
                            key=f"siv_{sym['id']}"
                        )

                    # ── Investor Relations URL ─────────────────────────────────
                    e_ir = st.text_input(
                        "Investor Relations URL",
                        value=sym.get("ir_url") or "",
                        placeholder="https://ir.aboutamazon.com",
                        key=f"sir_{sym['id']}",
                    )

                    # ── 4 Earnings termíny ─────────────────────────────────────
                    st.markdown("**Earnings termíny**")
                    earn_cols = st.columns(4)
                    earn_keys = ["earnings_date", "earnings_date_2",
                                 "earnings_date_3", "earnings_date_4"]
                    e_earn_dates = []
                    for i, (ecol, lbl, key) in enumerate(
                        zip(earn_cols, EARN_LABELS, earn_keys)
                    ):
                        with ecol:
                            e_earn_dates.append(
                                st.date_input(
                                    lbl,
                                    value=_parse_date(sym.get(key)),
                                    min_value=date(2020, 1, 1),
                                    max_value=date(date.today().year + 3, 12, 31),
                                    key=f"se{i}_{sym['id']}",
                                )
                            )

                    e_desc = st.text_area(
                        "Poznámky", value=sym.get("description") or "",
                        height=70, key=f"sd_{sym['id']}"
                    )

                    col_s, col_d = st.columns(2)
                    with col_s:
                        save_btn = st.form_submit_button(
                            "Uložiť", type="primary", use_container_width=True
                        )
                    with col_d:
                        del_btn = st.form_submit_button(
                            "Zmazať symbol", type="secondary", use_container_width=True
                        )

                if save_btn:
                    new_earn = [_date_or_none(d) for d in e_earn_dates]
                    db.update_symbol(
                        symbol_id=sym["id"],
                        ticker=e_ticker,
                        company_name=e_company,
                        sector=e_sector if e_sector != "—" else "",
                        asset_type=e_type,
                        description=e_desc,
                        earnings_date=new_earn[0],
                        earnings_date_2=new_earn[1],
                        earnings_date_3=new_earn[2],
                        earnings_date_4=new_earn[3],
                        ir_url=e_ir.strip() or None,
                        iv_rank=e_iv if e_iv > 0 else None,
                    )
                    st.success(f"Symbol **{e_ticker}** aktualizovaný.")
                    st.rerun()

                if del_btn:
                    db.delete_symbol(sym["id"])
                    st.warning(
                        f"Symbol **{sym['ticker']}** zmazaný. "
                        "Existujúce obchody s týmto tickerom ostávajú nezmenené."
                    )
                    st.rerun()

                # Otvorené pozície pre tento symbol
                open_trades_sym = [t for t in ticker_trades if t.get("status") == "Open"]
                if open_trades_sym:
                    st.markdown(f"**Otvorené pozície ({len(open_trades_sym)}):**")
                    leg_rows = []
                    for t in open_trades_sym:
                        leg_rows.append({
                            "ID": t["id"],
                            "Skupina": t.get("group_id") or "—",
                            "Noha": t.get("leg_type", ""),
                            "Typ": t.get("option_type", ""),
                            "Strike": t.get("strike"),
                            "Expiry": t.get("expiry", ""),
                            "Kontrakty": t.get("contracts", 1),
                        })
                    st.dataframe(
                        pd.DataFrame(leg_rows), hide_index=True, use_container_width=True,
                        column_config={
                            "Strike": st.column_config.NumberColumn(format="$%.2f")
                        },
                    )
