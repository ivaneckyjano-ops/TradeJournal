import streamlit as st
import pandas as pd
import numpy as np

from core import database as db
from core.csv_spread_variant import (
    calendar_legs_from_variant_row,
    norm_header,
    series_to_norm_dict,
    underlying_ticker_from_norm,
    verbally_assess_top1,
)
from core.page_context import set_tradejournal_page

db.init_db()
set_tradejournal_page("csv_variants")

st.title("CSV Varianty")
st.caption(
    "Nahraj CSV s variantmi spreadu a apka vyberie top výsledky priamo tu. "
    "Podporuje európske čísla s čiarkou, percentá aj dopočet `Net Debit` z `Ask2 - Bid1`."
)


def _parse_number(value) -> float:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return float("nan")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    s = str(value).strip().replace("\u00a0", " ").replace("%", "").replace(" ", "")
    if not s:
        return float("nan")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _minmax(s: pd.Series) -> pd.Series:
    lo, hi = s.min(), s.max()
    if pd.isna(lo) or pd.isna(hi) or hi == lo:
        return pd.Series(0.5, index=s.index)
    return (s - lo) / (hi - lo)


def _rank_variants(df: pd.DataFrame, strategy: str) -> pd.DataFrame:
    norm = df.copy()
    norm.columns = [norm_header(c) for c in norm.columns]

    def col(name: str) -> pd.Series:
        if name not in norm.columns:
            return pd.Series(np.nan, index=norm.index)
        return norm[name].map(_parse_number)

    debit = col("net_debit")
    if not debit.notna().any():
        ask2 = col("ask2")
        bid1 = col("bid1")
        if ask2.notna().any() and bid1.notna().any():
            debit = ask2 - bid1
        else:
            raise ValueError("V CSV chýba `Net Debit` a nedá sa dopočítať z `Ask2 - Bid1`.")

    skew = col("iv_skew")
    theta = col("net_theta")
    delta = col("net_delta")

    if strategy == "cheap":
        score = -debit
    elif strategy == "skew":
        score = skew.fillna(skew.min())
    elif strategy == "theta":
        score = theta.fillna(theta.min())
    elif strategy == "balanced":
        debit_better = 1.0 - _minmax(debit)
        skew_better = _minmax(skew.fillna(skew.min()))
        theta_better = _minmax(theta.fillna(theta.min()))
        abs_delta = delta.abs()
        if abs_delta.notna().any() and abs_delta.max() > 0:
            delta_better = 1.0 - _minmax(abs_delta.fillna(abs_delta.max()))
        else:
            delta_better = pd.Series(0.5, index=norm.index)
        score = 0.40 * debit_better + 0.30 * skew_better + 0.20 * theta_better + 0.10 * delta_better
    else:
        raise ValueError(f"Neznáma stratégia: {strategy}")

    ranked = df.copy()
    ranked["_score"] = score
    ranked["_net_debit"] = debit
    return ranked.sort_values("_score", ascending=False, kind="mergesort")


def _sb_iv_from_symbol(sym: dict | None) -> float:
    if not sym:
        return 0.30
    raw = sym.get("iv_pct")
    if raw is None:
        return 0.30
    try:
        fv = float(raw)
    except (TypeError, ValueError):
        return 0.30
    if fv > 1.0:
        fv = fv / 100.0
    return float(min(max(fv, 0.01), 5.0))


def _row_symbol_upper(row: pd.Series) -> str:
    """Ticker zo stĺpcov Symbol / Ticker / Underlying / … (rovnaká logika ako pri odoslaní do Buildera)."""
    return underlying_ticker_from_norm(series_to_norm_dict(row))


def _unique_symbols_from_df(df: pd.DataFrame) -> list[str]:
    out: list[str] = []
    for _, r in df.iterrows():
        t = _row_symbol_upper(r)
        if t and t not in out:
            out.append(t)
    return sorted(out)


def _ticker_spot_iv_for_row(row: pd.Series, *, manual_ticker: str = "") -> tuple[str, float, float]:
    norm = series_to_norm_dict(row)
    tk = (manual_ticker or "").strip().upper() or underlying_ticker_from_norm(norm)
    sym = db.get_symbol(tk) if tk else None
    spot = float(sym.get("spot") or 0) if sym else 0.0
    iv = _sb_iv_from_symbol(sym)
    pcell = norm.get("price", "").strip()
    if pcell:
        pv = _parse_number(pcell)
        if not np.isnan(pv) and float(pv) > 0:
            spot = float(pv)
    if spot <= 0:
        for key in ("leg1_strike", "strike", "long_strike", "k"):
            v = norm.get(key, "").strip()
            if v:
                try:
                    spot = float(str(v).replace(",", "."))
                    if spot > 0:
                        break
                except ValueError:
                    pass
    if spot <= 0:
        spot = 200.0
    return tk, spot, iv


uploaded = st.file_uploader(
    "CSV súbor",
    type=["csv"],
    help="Nahraj export variantov spreadu. Podporuje aj CSV zo screenshotu alebo z Excelu.",
)

col1, col2, col3 = st.columns([2, 1, 1])
with col1:
    strategy = st.selectbox(
        "Hodnotenie",
        options=["balanced", "cheap", "skew", "theta"],
        index=0,
        format_func=lambda x: {
            "balanced": "Balanced",
            "cheap": "Najnižší debit",
            "skew": "Najvyšší IV skew",
            "theta": "Najvyššia theta",
        }[x],
    )
with col2:
    top_n = st.number_input("Top N", min_value=1, max_value=20, value=3, step=1)
with col3:
    show_score = st.checkbox("Zobraziť score", value=False)

if not uploaded:
    st.info("Nahraj CSV a hneď sa zobrazí výber top variantov.")
    st.stop()

try:
    raw = pd.read_csv(uploaded, sep=None, engine="python", dtype=str)
except Exception as exc:
    st.error(f"CSV sa nepodarilo spracovať: {exc}")
    st.stop()

if raw.empty:
    st.warning("CSV neobsahuje žiadne dátové riadky.")
    st.stop()

_unique_syms = _unique_symbols_from_df(raw)
_filtered = raw
if _unique_syms:
    st.markdown("##### Vylúčenie z posudzovania")
    st.caption(
        "Riadky s vybraným **symbolom** sa vôbec nezarátajú do hodnotenia (rebríček, top 1, Spread Builder). "
        "Riadky bez rozpoznaného tickeru ostávajú v dátach."
    )
    _exclude_pick = st.multiselect(
        "Symboly úplne vynechať z CSV",
        options=_unique_syms,
        default=[],
        key="csv_variants_exclude_symbols",
        help="Zhoduje sa so stĺpcom Symbol, Ticker alebo Underlying (veľkosť písmen nezáleží).",
    )
    if _exclude_pick:
        _ex_set = {str(x).strip().upper() for x in _exclude_pick}

        def _keep_for_ranking(r: pd.Series) -> bool:
            t = _row_symbol_upper(r)
            if not t:
                return True
            return t not in _ex_set

        _mask = raw.apply(_keep_for_ranking, axis=1)
        _filtered = raw.loc[_mask].copy()
        _n_out = int((~_mask).sum())
        st.caption(
            f"Vylúčených **{_n_out}** riadkov ({len(_exclude_pick)} symbol(y)); "
            f"na posudzovanie zostáva **{len(_filtered)}** riadkov."
        )
else:
    st.caption(
        "V CSV sa nenašiel rozpoznateľný stĺpec **Symbol** / **Ticker** / **Underlying** — vylúčenie podľa symbolu nie je k dispozícii."
    )

if _filtered.empty:
    st.warning("Po vylúčení symbolov nezostali žiadne riadky na posudzovanie. Zruš výber v multiselecte.")
    st.stop()

try:
    ranked = _rank_variants(_filtered, strategy)
except Exception as exc:
    st.error(f"CSV sa nepodarilo spracovať: {exc}")
    st.stop()

if ranked.empty:
    st.warning("Po filtri nezostali žiadne riadky na hodnotenie.")
    st.stop()

top = ranked.head(int(top_n)).copy()
if not show_score:
    top = top.drop(columns=["_score"], errors="ignore")

st.success(f"Načítaných {len(ranked)} variantov. Zobrazených top {len(top)}.")
st.caption(
    "Do Buildera: **kalendár** = jeden strike (napr. len **Leg1 Strike**); **diagonál** = dva striky "
    "(**Leg1 Strike** + **Leg2 Strike**, alebo **Short strike** / **Long strike**)."
)
st.dataframe(top, use_container_width=True, hide_index=True)

top1 = ranked.iloc[0]
st.markdown("##### Top 1 — slovné zhodnotenie")
st.info(verbally_assess_top1(top1, ranked, strategy))

_sug_csv_ticker = underlying_ticker_from_norm(series_to_norm_dict(top1))
_csv_fname = getattr(uploaded, "name", "variant") or "variant"
_csv_tk_key = f"csv_variant_manual_ticker_{_csv_fname}"
if _sug_csv_ticker:
    st.caption(
        f"Z CSV je rozpoznaný ticker **{_sug_csv_ticker}**. Ak treba iný (napr. nie je v záložke Symboly), zadaj ho nižšie — **má prednosť** pred CSV."
    )
else:
    st.caption(
        "V CSV **nie je** rozpoznateľný stĺpec s tickerom (Symbol, Ticker, Underlying, Stock, Root, …). "
        "**Zadaj ticker podkladu** nižšie — už sa **nepoužije** prvý symbol z DB."
    )
st.text_input(
    "Ticker podkladu (voliteľné doplnenie / oprava)",
    key=_csv_tk_key,
    placeholder=_sug_csv_ticker or "napr. MRVL",
    help="Prepíše ticker z CSV alebo ho doplní, keď v CSV chýba. Spot vie ísť z ceny v CSV (Price~) aj bez záznamu v Symboly.",
)

if st.button(
    "Odoslať top 1 do Spread Buildera",
    type="primary",
    help="Ticker = pole vyššie alebo CSV; spot z ceny v CSV alebo Symboly. Kalendárny spread z prvého riadku rebríčka.",
):
    _man_tk = (st.session_state.get(_csv_tk_key) or "").strip().upper()
    tk, spot, iv = _ticker_spot_iv_for_row(top1, manual_ticker=_man_tk)
    legs, leg_err, csv_exp_notice = calendar_legs_from_variant_row(
        top1,
        spot=spot,
        iv=iv,
        contracts=1,
    )
    if leg_err:
        st.error(leg_err)
    elif not tk:
        st.error(
            "Chýba **ticker podkladu**. Doplň ho v poli vyššie alebo pridaj do CSV stĺpec "
            "**Symbol** / **Ticker** / **Underlying** / **Stock** / **Root** s kódom (napr. MRVL)."
        )
    else:
        _parts = [
            f"Načítaný **top 1** z CSV variantov ({tk}).",
            verbally_assess_top1(top1, ranked, strategy),
        ]
        if csv_exp_notice:
            _parts.append(csv_exp_notice)
        note = " ".join(_parts)
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
                "Údaje sú pripravené v session. Otvor v menu **Spread Builder** — kalendár sa doplní automaticky."
            )
