"""
Parsovanie denných historických exportov z Barchartu (tabuľka so slovenskými hlavičkami)
a výpočet korelácie výnosov medzi dvoma tickermi.

Očakávané stĺpce (niektoré voliteľné): Čas/Time/Date, Najnovšie/Latest/Close, príp. objem/volume …
"""

from __future__ import annotations

import io
import re
import json
from typing import BinaryIO, Literal

import numpy as np
import pandas as pd

CorrMethod = Literal["pearson", "spearman"]


def _norm_header(h: str) -> str:
    s = str(h).strip().replace("\ufeff", "")
    s = s.lower()
    for a, b in (
        ("á", "a"), ("ä", "a"), ("č", "c"), ("ď", "d"), ("é", "e"), ("í", "i"),
        ("ľ", "l"), ("ĺ", "l"), ("ň", "n"), ("ó", "o"), ("ô", "o"), ("ö", "o"),
        ("ő", "o"), ("ŕ", "r"), ("š", "s"), ("ť", "t"), ("ú", "u"), ("ý", "y"),
        ("ž", "z"),
    ):
        s = s.replace(a, b)
    s = re.sub(r"\s+", " ", s)
    return s


def _find_col(norm_columns: list[str], *candidates: str) -> int | None:
    cand = {_norm_header(c) for c in candidates}
    for i, col in enumerate(norm_columns):
        if col in cand:
            return i
    return None


def read_barchart_history_csv(
    source: str | bytes | BinaryIO,
    *,
    encoding: str = "utf-8-sig",
) -> pd.DataFrame:
    """
    Načíta CSV a vráti DataFrame s ``date`` (datetime) a ``close`` (float).
    Voliteľne ``open``, ``high``, ``low``, ``volume`` ak sú v súbore.
    """
    if isinstance(source, str):
        raw = source
    else:
        if isinstance(source, bytes):
            raw = source.decode(encoding, errors="replace")
        else:
            raw = source.read()
            if isinstance(raw, bytes):
                raw = raw.decode(encoding, errors="replace")
    df = pd.read_csv(io.StringIO(raw), sep=None, engine="python", dtype=str, encoding_errors="replace")
    if df.empty or len(df.columns) < 2:
        raise ValueError("CSV je prázdny alebo má príliš málo stĺpcov.")

    cols_raw = [str(c) for c in df.columns]
    cols_n = [_norm_header(c) for c in cols_raw]

    i_date = _find_col(cols_n, "cas", "čas", "time", "datum", "date")
    if i_date is None:
        raise ValueError("Nenašiel som stĺpec dátumu (očakávam napr. **Čas** / **Time** / **Date**).")

    i_close = _find_col(
        cols_n,
        "najnovsie",
        "najnovšie",
        "latest",
        "close",
        "last",
        "zavierka",
        "uzávierka",
    )
    if i_close is None:
        raise ValueError(
            "Nenašiel som stĺpec uzávierky (očakávam napr. **Najnovšie** / **Latest** / **Close**)."
        )

    out = pd.DataFrame()
    out["date"] = pd.to_datetime(df.iloc[:, i_date], errors="coerce", dayfirst=False)
    close_raw = df.iloc[:, i_close].astype(str).str.replace(",", ".", regex=False)
    out["close"] = pd.to_numeric(close_raw, errors="coerce")

    i_vol = _find_col(cols_n, "objem", "volume", "vol")
    if i_vol is not None:
        vol_raw = df.iloc[:, i_vol].astype(str).str.replace(",", "", regex=False)
        out["volume"] = pd.to_numeric(vol_raw, errors="coerce")

    out = out.loc[out["date"].notna() & out["close"].notna()].copy()
    out = out.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    if out.empty:
        raise ValueError("Po parsovaní neostali žiadne platné riadky (dátum + close).")
    return out.reset_index(drop=True)


def align_close_series(
    a: pd.DataFrame,
    b: pd.DataFrame,
    *,
    max_trading_days: int | None = 504,
) -> tuple[pd.Series, pd.Series, pd.DataFrame]:
    """
    Vnútorné spojenie podľa dátumu, zoradenie, voliteľne posledných ``max_trading_days`` riadkov.
    Vráti (close_a, close_b, merged_frame s stĺpcami date, close_a, close_b).
    """
    if "close" not in a.columns or "close" not in b.columns or "date" not in a.columns or "date" not in b.columns:
        raise ValueError("DataFrame musí mať stĺpce date a close.")
    x = a[["date", "close"]].rename(columns={"close": "close_a"})
    y = b[["date", "close"]].rename(columns={"close": "close_b"})
    m = x.merge(y, on="date", how="inner").sort_values("date")
    if m.empty:
        raise ValueError("Žiadne spoločné obchodné dni medzi oboma súbormi.")
    if max_trading_days is not None and max_trading_days > 0 and len(m) > max_trading_days:
        m = m.tail(int(max_trading_days)).reset_index(drop=True)
    return m["close_a"], m["close_b"], m


def correlation_from_closes(
    close_a: pd.Series,
    close_b: pd.Series,
    *,
    method: CorrMethod = "pearson",
    return_kind: Literal["simple", "log"] = "log",
) -> tuple[float, int, pd.Series, pd.Series]:
    """
    Korelácia **denných výnosov** (nie úrovní cien).

    Vráti (korelácia, počet pozorovaní po spárovaní, séría r_a, séría r_b).
    """
    a = pd.to_numeric(close_a, errors="coerce")
    b = pd.to_numeric(close_b, errors="coerce")
    if return_kind == "log":
        r_a = np.log(a / a.shift(1))
        r_b = np.log(b / b.shift(1))
    else:
        r_a = a.pct_change()
        r_b = b.pct_change()
    ok = r_a.notna() & r_b.notna()
    r_a = r_a.loc[ok]
    r_b = r_b.loc[ok]
    n = int(len(r_a))
    if n < 5:
        raise ValueError(f"Po výpočte výnosov ostalo len **{n}** spoločných dní — treba aspoň ~5.")
    if method == "spearman":
        corr = float(r_a.corr(r_b, method="spearman"))
    else:
        corr = float(r_a.corr(r_b, method="pearson"))
    if np.isnan(corr):
        raise ValueError("Korelácia je NaN (konštantné výnosy?).")
    return corr, n, r_a, r_b


def hist_dataframe_to_series_json(df: pd.DataFrame) -> tuple[str, str, str]:
    """
    Serializácia ``date`` + ``close`` do JSON pre ``ticker_hist_snapshots``.
    Vráti ``(series_json, first_date_iso, last_date_iso)``.
    """
    if df.empty or "date" not in df.columns or "close" not in df.columns:
        raise ValueError("DataFrame musí mať stĺpce date a close.")
    d0 = df.sort_values("date").reset_index(drop=True)
    rec: list[dict[str, object]] = []
    for _, row in d0.iterrows():
        dt = row["date"]
        if hasattr(dt, "strftime"):
            ds = dt.strftime("%Y-%m-%d")
        else:
            ds = str(dt)[:10]
        rec.append({"d": ds, "c": float(row["close"])})
    blob = json.dumps(rec, ensure_ascii=False)
    return blob, rec[0]["d"], rec[-1]["d"]


def hist_series_json_to_dataframe(series_json: str) -> pd.DataFrame:
    data = json.loads(series_json)
    if not data:
        return pd.DataFrame(columns=["date", "close"])
    rows = [{"date": pd.Timestamp(str(x["d"])[:10]), "close": float(x["c"])} for x in data]
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def _n_obs_single_close(close: pd.Series, max_trading_days: int | None) -> int:
    s = pd.to_numeric(close, errors="coerce").dropna()
    if s.empty:
        return 0
    if max_trading_days is not None and int(max_trading_days) > 0 and len(s) > int(max_trading_days):
        s = s.tail(int(max_trading_days))
    r = np.log(s.astype(float) / s.astype(float).shift(1))
    return int(r.notna().sum())


def correlation_matrix_pairwise(
    frames: dict[str, pd.DataFrame],
    *,
    max_trading_days: int | None = 504,
    method: CorrMethod = "pearson",
    return_kind: Literal["simple", "log"] = "log",
) -> tuple[list[str], list[list[float | None]], list[list[int | None]]]:
    """
    Pearson/Spearman korelácia log-výnosov po pároch (rôzne páry môžu mať rôzny počet dní).

    Vráti ``(tickers_zoradené, matica_korelácií, matica_n_obs)``. Pri chybe páru je ``None``.
    """
    if len(frames) < 2:
        raise ValueError("Potrebuj aspoň **2** série.")
    tickers = sorted(frames.keys(), key=lambda x: str(x).upper())
    n = len(tickers)
    mat: list[list[float | None]] = [[None] * n for _ in range(n)]
    nmat: list[list[int | None]] = [[None] * n for _ in range(n)]
    for i, ti in enumerate(tickers):
        ca = frames[ti]["close"]
        nmat[i][i] = _n_obs_single_close(ca, max_trading_days)
        mat[i][i] = 1.0
    for i in range(n):
        for j in range(i + 1, n):
            ti, tj = tickers[i], tickers[j]
            try:
                ca, cb, _merged = align_close_series(
                    frames[ti], frames[tj], max_trading_days=max_trading_days
                )
                corr, n_obs, _, _ = correlation_from_closes(
                    ca, cb, method=method, return_kind=return_kind
                )
                mat[i][j] = mat[j][i] = float(corr)
                nmat[i][j] = nmat[j][i] = int(n_obs)
            except (ValueError, TypeError, KeyError):
                mat[i][j] = mat[j][i] = None
                nmat[i][j] = nmat[j][i] = None
    return tickers, mat, nmat


def _cell_to_float_log_corr(v: object) -> float | None:
    if v is None:
        return None
    try:
        if isinstance(v, (str, int, float, np.floating, np.integer)):
            if pd.isna(v):  # type: ignore[arg-type]
                return None
    except (TypeError, ValueError):
        pass
    try:
        return float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def extend_correlation_matrix(
    old_tickers: list[str],
    old_matrix: list[list[object]],
    old_n_obs: list[list[object]] | None,
    new_tickers: list[str],
    frames: dict[str, pd.DataFrame],
    *,
    max_trading_days: int | None = 504,
    method: CorrMethod = "pearson",
    return_kind: Literal["simple", "log"] = "log",
) -> tuple[list[str], list[list[float | None]], list[list[int | None]]]:
    """
    Obohatí uloženú maticu o nové tickery: hornoľavý blok (staré×staré) ostané z **old_matrix**,
    nové riadky/stĺpce sa dopočítajú z ``frames`` (párovo, rovnako ako ``correlation_matrix_pairwise``).

    Kľúče v ``frames`` očakávame ako **veľké písmená** (AAPL, …) a v súlade s ``old_tickers``/``new_tickers``.
    """
    old_u = [str(t).strip().upper() for t in old_tickers if str(t).strip()]
    new_u = sorted([str(t).strip().upper() for t in new_tickers if str(t).strip()], key=str.upper)
    n = len(old_u)
    m = len(new_u)
    if m < 1:
        raise ValueError("Potrebuj aspoň jeden nový ticker na rozšírenie matice.")
    for ftk in old_u + new_u:
        u = str(ftk).strip().upper()
        if u not in frames or frames[u].empty:
            raise ValueError(f"Chýbajú dáta (close) pre ticker **{u}**.")
    if n < 1:
        raise ValueError("Pôvodná matica musí mať aspoň jeden ticker.")
    if len(old_matrix) != n or any((len(r) if r is not None else 0) != n for r in old_matrix):
        raise ValueError("Rozmer pôvodnej matice nezodpovedá počtu pôvodných tickerov.")
    all_tk: list[str] = list(old_u) + new_u
    N = n + m
    mat: list[list[float | None]] = [[None] * N for _ in range(N)]
    nmat: list[list[int | None]] = [[None] * N for _ in range(N)]

    for i in range(n):
        for j in range(n):
            mat[i][j] = _cell_to_float_log_corr(old_matrix[i][j])
    if old_n_obs is not None and len(old_n_obs) == n and all(
        (len(r) if r is not None else 0) == n for r in old_n_obs
    ):
        for i in range(n):
            for j in range(n):
                v = old_n_obs[i][j]
                if v is None:
                    continue
                try:
                    fv = float(v)
                    nmat[i][j] = None if np.isnan(fv) else int(round(fv))
                except (TypeError, ValueError):
                    nmat[i][j] = None
    for k in range(m):
        idx = n + k
        mat[idx][idx] = 1.0
        ca = frames[new_u[k]]["close"]
        nmat[idx][idx] = _n_obs_single_close(ca, max_trading_days)

    def _pair(
        tia: str, tib: str, ii: int, ji: int
    ) -> None:
        try:
            ca, cb, _ = align_close_series(frames[tia], frames[tib], max_trading_days=max_trading_days)
            corr, nobs, _, _ = correlation_from_closes(ca, cb, method=method, return_kind=return_kind)
            mat[ii][ji] = mat[ji][ii] = float(corr)
            nmat[ii][ji] = nmat[ji][ii] = int(nobs)
        except (ValueError, TypeError, KeyError):
            mat[ii][ji] = mat[ji][ii] = None
            nmat[ii][ji] = nmat[ji][ii] = None

    for i in range(n):
        for k in range(m):
            _pair(old_u[i], new_u[k], i, n + k)

    for k1 in range(m):
        for k2 in range(k1 + 1, m):
            i1, i2 = n + k1, n + k2
            _pair(new_u[k1], new_u[k2], i1, i2)

    return all_tk, mat, nmat
