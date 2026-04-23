"""
Odporúčania k diverzifikácii na základe **podobnosti výkonnostných profilov** sektorov
(vyfotená tabuľka), nie štatistickej korelácie cien. Portfólio mapuješ cez stĺpec *sector* v Symboly.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

from core.journal_sectors import (
    CANONICAL_SECTOR_TO_SP500_SLOVAK_ROW,
    canonical_journal_sector,
    journal_sector_from_table_row_name,
)
from core.sector_performance_ocr import normalize_sector_dataframe

PERF_COLS = ("pct_1d", "pct_5d", "pct_1m", "pct_3m", "pct_1y")

_GARBAGE_SECTOR = re.compile(
    r"^(s\s*&\s*p\.?\s*|s\s*&\s*p\s*\d*|sp\d*|index\s+s\s*&\s*p\s*s?)$",
    re.IGNORECASE,
)


def _is_spurious_ocr_sector(name: str) -> bool:
    """Krátke / rozbité fragmenty z OCR (S&P, …), ktoré kazia klastre a zobrazenie."""
    t = (name or "").strip()
    if not t:
        return True
    if len(t) <= 3:
        return True
    tl = re.sub(r"\s+", " ", t)
    if _GARBAGE_SECTOR.match(tl):
        return True
    if len(tl) <= 8 and re.fullmatch(r"s\s*&\s*p[\s\d]*", tl, flags=re.IGNORECASE):
        return True
    return False


def _insight_sector_label(raw: str) -> str:
    """Zobrazenie v reportoch: slovenský S&P názov z mapy, inak GICS kanón, inak pôvodný text."""
    s = str(raw or "").strip()
    if not s:
        return s
    j = journal_sector_from_table_row_name(s)
    if j:
        return CANONICAL_SECTOR_TO_SP500_SLOVAK_ROW.get(j, j)
    return s


def prepare_sector_df_for_insights(df: pd.DataFrame | None) -> pd.DataFrame:
    """
    Pred kosínusovou podobnosťou: odstráni zjavný OCR šum, zlepší názvy cez ``journal_sector_from_table_row_name``
    a zlúči riadky, ktoré mapujú na rovnaký kanonický sektor (priemer výkonnostných stĺpcov).
    """
    if df is None:
        return pd.DataFrame(columns=["sector", "pct_1d", "pct_5d", "pct_1m", "pct_3m", "pct_1y"])
    dfn = normalize_sector_dataframe(df)
    if dfn.empty:
        return dfn
    ok = ~dfn["sector"].astype(str).map(_is_spurious_ocr_sector)
    dfn = dfn.loc[ok].copy()
    if dfn.empty:
        return dfn
    jn = dfn["sector"].astype(str).map(lambda x: journal_sector_from_table_row_name(str(x)))
    labels = []
    for i in range(len(dfn)):
        j = jn.iloc[i] if pd.notna(jn.iloc[i]) else None
        if j:
            labels.append(CANONICAL_SECTOR_TO_SP500_SLOVAK_ROW.get(j, j))
        else:
            labels.append(str(dfn["sector"].iloc[i]))
    dfn["_grp"] = labels
    cols = [c for c in PERF_COLS if c in dfn.columns]
    if not cols:
        return dfn.drop(columns=["_grp"], errors="ignore")
    out = dfn.groupby("_grp", as_index=False)[cols].mean()
    out = out.rename(columns={"_grp": "sector"})
    return normalize_sector_dataframe(out)


def _sector_row_index(df: pd.DataFrame) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, s in enumerate(df["sector"].astype(str)):
        out[s.strip().lower()] = i
    return out


def match_table_sector(portfolio_sector: str, df: pd.DataFrame) -> Optional[str]:
    """
    Nájde riadok v OCR tabuľke zodpovedajúci sektoru z **Symboly** (rovnaký zoznam ako v denníku).

    Názvy z Barchart (užšie odvetvia) sa mapujú na štítky z výberu v záložke Symboly.
    """
    if df is None or df.empty or not (portfolio_sector or "").strip():
        return None
    ps_raw = portfolio_sector.strip()
    if ps_raw == "Neznámy sektor":
        return None

    pj = canonical_journal_sector(ps_raw)
    if pj is not None:
        for _, row in df.iterrows():
            tname = str(row["sector"]).strip()
            if not tname:
                continue
            rj = journal_sector_from_table_row_name(tname)
            if rj == pj:
                return tname

    # Voľný text v Symboly mimo zoznamu — pôvodná heuristika substringov
    ps = ps_raw.lower()
    idx = _sector_row_index(df)
    if ps in idx:
        return str(df.iloc[idx[ps]]["sector"])
    best: Optional[str] = None
    for key, i in idx.items():
        if ps in key or key in ps:
            cand = str(df.iloc[i]["sector"])
            if best is None or len(cand) < len(best):
                best = cand
    return best


def cosine_similarity_sectors(df: pd.DataFrame) -> pd.DataFrame:
    dfn = normalize_sector_dataframe(df)
    if dfn.empty:
        return pd.DataFrame()
    cols = [c for c in PERF_COLS if c in dfn.columns]
    X = dfn[cols].to_numpy(dtype=float)
    X = np.nan_to_num(X, nan=0.0)
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms = np.where(norms < 1e-12, 1.0, norms)
    Xn = X / norms
    sim = Xn @ Xn.T
    names = dfn["sector"].astype(str).tolist()
    return pd.DataFrame(sim, index=names, columns=names)


def _union_find_clusters(pairs: list[tuple[str, str]], nodes: list[str]) -> list[list[str]]:
    parent = {n: n for n in nodes}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        pa, pb = find(a), find(b)
        if pa != pb:
            parent[pb] = pa

    for a, b in pairs:
        union(a, b)
    buckets: dict[str, list[str]] = defaultdict(list)
    for n in nodes:
        buckets[find(n)].append(n)
    return [sorted(v) for v in buckets.values() if len(v) > 1]


def portfolio_sector_weights(
    open_trades: list[dict],
    sector_for_ticker: Callable[[str], Optional[str]],
) -> dict[str, float]:
    """Váhy podľa Σ |entry|×kontrakty×100 na ticker, potom agregácia na sektor."""
    by_ticker: dict[str, float] = defaultdict(float)
    total = 0.0
    for t in open_trades:
        tk = str(t.get("ticker") or "").strip().upper()
        if not tk:
            continue
        try:
            c = float(t.get("contracts") or 1)
            e = float(t.get("entry_price") or 0)
        except (TypeError, ValueError):
            continue
        w = abs(e) * c * 100.0
        by_ticker[tk] += w
        total += w
    if total <= 1e-12:
        return {}
    sec_w: dict[str, float] = defaultdict(float)
    for tk, w in by_ticker.items():
        sec = (sector_for_ticker(tk) or "").strip() or "Neznámy sektor"
        sec_w[sec] += w / total
    return dict(sorted(sec_w.items(), key=lambda x: -x[1]))


def build_insight_report(
    short_df: pd.DataFrame,
    long_df: Optional[pd.DataFrame],
    portfolio_weights: dict[str, float],
    *,
    sim_threshold_high: float = 0.92,
    concentration_single: float = 0.40,
    concentration_cluster: float = 0.45,
    hhi_warn: float = 0.22,
) -> dict:
    """
    Vráti slovník s textami pre UI a pomocné štruktúry.

    * ``similarity_note``: nejde o Pearsonovu koreláciu časových radov.
    """
    short_df = prepare_sector_df_for_insights(short_df)
    long_df = prepare_sector_df_for_insights(long_df) if long_df is not None else None

    out: dict = {
        "similarity_note": (
            "Podobnosť je počítaná z vektoru krátkodobých výkonov sektorov v tabuľke (kosínusová "
            "podobnosť po normalizácii), nie z historickej korelácie cien."
        ),
        "concentration": [],
        "clusters": [],
        "warnings": [],
        "diversifiers": [],
        "momentum_long": [],
        "portfolio_mapped": [],
        "errors": [],
    }

    if short_df.empty:
        out["errors"].append("Chýba krátkodobá tabuľka (alebo sa nepodarilo rozpoznať riadky).")
        return out

    sim = cosine_similarity_sectors(short_df)
    sectors = sim.index.tolist()
    high_pairs: list[tuple[str, str]] = []
    for i, a in enumerate(sectors):
        for j in range(i + 1, len(sectors)):
            b = sectors[j]
            if float(sim.iloc[i, j]) >= sim_threshold_high:
                high_pairs.append((a, b))

    clusters = _union_find_clusters(high_pairs, sectors)
    out["clusters"] = clusters

    if clusters:
        pretty_groups = []
        for g in clusters[:12]:
            seen: set[str] = set()
            parts: list[str] = []
            for raw in g:
                lab = _insight_sector_label(raw)
                k = lab.casefold()
                if k in seen:
                    continue
                seen.add(k)
                parts.append(lab)
            pretty_groups.append(", ".join(parts))
        out["concentration"].append(
            f"Skupiny podobného krátkodobého správania (kosínus ≥ {sim_threshold_high:.2f}): "
            + "; ".join(pretty_groups)
            + ("…" if len(clusters) > 12 else "")
        )

    # Mapovanie portfólia na riadky tabuľky
    mapped: dict[str, str] = {}
    for sec, w in (portfolio_weights or {}).items():
        m = match_table_sector(sec, short_df)
        if m:
            mapped[sec] = m
        out["portfolio_mapped"].append({"portfolio_sector": sec, "weight": w, "table_sector": m})

    # Koncentrácia v jednom sektore
    if portfolio_weights:
        top_sec, top_w = max(portfolio_weights.items(), key=lambda x: x[1])
        if top_w >= concentration_single:
            out["warnings"].append(
                f"Vysoká váha v jednom sektore z denníka (**{top_sec}** ≈ **{top_w:.0%}** orientačne podľa |prémie|)."
            )
        hhi = sum(w * w for w in portfolio_weights.values())
        if hhi >= hhi_warn:
            out["warnings"].append(
                f"Koncentrácia (HHI váh) je **{hhi:.2f}** — zváž rozptyl medzi nezávislejšie sektory."
            )

        # Váha v „klastri“
        table_to_cluster: dict[str, int] = {}
        for ci, g in enumerate(clusters):
            for s in g:
                table_to_cluster[s] = ci
        cluster_w: dict[int, float] = defaultdict(float)
        unmapped_w = 0.0
        for sec, w in portfolio_weights.items():
            ts = mapped.get(sec)
            if ts and ts in table_to_cluster:
                cluster_w[table_to_cluster[ts]] += w
            elif ts is None:
                unmapped_w += w
        for ci, w in cluster_w.items():
            if w >= concentration_cluster:
                g = clusters[ci]
                labs = []
                seen2: set[str] = set()
                for raw in g[:14]:
                    lab = _insight_sector_label(raw)
                    k = lab.casefold()
                    if k in seen2:
                        continue
                    seen2.add(k)
                    labs.append(lab)
                tail = "…" if len(g) > 14 else ""
                out["warnings"].append(
                    f"Veľa expozície v jednom „správaní“ sektorov: **{w:.0%}** v skupine "
                    f"[{', '.join(labs)}{tail}]."
                )
        if unmapped_w >= 0.25:
            out["warnings"].append(
                f"**{unmapped_w:.0%}** váh: sektor z **Symboly** (výber z rovnakého zoznamu ako v denníku) sa nepodarilo "
                "priradiť k žiadnemu riadku v OCR tabuľke. Užšie názvy z Barchart (napr. *Electronic Technology*) sa "
                "mapujú na *Technology* atď. — skontroluj OCR riadky alebo vyber správny sektor v **Symboly**; "
                "ak v tabuľke chýba celý sektor z portfólia, doplníš riadok v editore po OCR."
            )

    # Diverzifikátory: nízka priemerná podobnosť k portfóliu, kladný dlhý horizont
    if portfolio_weights and not sim.empty:
        table_secs_in_pf = list({mapped[s] for s in mapped if mapped[s]})
        if table_secs_in_pf:
            others = [s for s in sectors if s not in table_secs_in_pf]
            scores = []
            for s in others:
                avg_sim = float(np.mean([float(sim.loc[s, p]) for p in table_secs_in_pf]))
                scores.append((s, avg_sim))
            scores.sort(key=lambda x: x[1])
            n_pick = min(8, len(scores))
            for s, avs in scores[:n_pick]:
                long_note = ""
                if long_df is not None and not long_df.empty:
                    lm = match_table_sector(s, long_df)
                    if lm:
                        mrow = long_df[long_df["sector"].astype(str) == lm]
                        if not mrow.empty and pd.notna(mrow.iloc[0].get("pct_1y")):
                            long_note = (
                                f" (1r v dlhšej tabuľke: {float(mrow.iloc[0]['pct_1y']):+.1f} %)"
                            )
                out["diversifiers"].append(
                    f"**{_insight_sector_label(s)}** — nízka podobnosť k tvojim tabuľkovým sektorom "
                    f"(priemer ≈ {avs:.2f}){long_note}."
                )

    if long_df is not None and not long_df.empty:
        dfn = long_df.copy()
        if "pct_1y" in dfn.columns:
            dfn = dfn.dropna(subset=["pct_1y"])
            if not dfn.empty:
                top = dfn.nlargest(5, "pct_1y")
                for _, r in top.iterrows():
                    out["momentum_long"].append(
                        f"**{_insight_sector_label(str(r['sector']))}** — 1r **{float(r['pct_1y']):+.1f} %** (dlhodobý snímok)."
                    )

    return out


def evaluate_ticker_diversification(
    ticker: str,
    short_df: pd.DataFrame,
    long_df: Optional[pd.DataFrame],
    portfolio_weights: dict[str, float],
    sector_for_ticker: Callable[[str], Optional[str]],
    *,
    open_trades: Optional[list[dict]] = None,
    sim_threshold_high: float = 0.92,
) -> dict[str, Any]:
    """
    Rýchle vyhodnotenie tickera z pohľadu **sektora** a **podobnosti výkonu v krátkodobej tabuľke**
    voči tomu, čo už máš v otvorených nohách (váhy podľa |prémia|×100).

    Potrebuje uložený krátkodobý snímok (rovnaký dataframe ako report) a záznam v **Symboly** so sektorom.
    """
    out: dict[str, Any] = {
        "error": None,
        "lines": [],
        "verdict": None,
        "ticker": "",
        "sector_symboly": None,
        "table_row": None,
        "weight_same_sector": None,
        "avg_sim_to_portfolio_table": None,
        "in_open_trades": False,
    }
    t = (ticker or "").strip().upper()
    out["ticker"] = t
    if not t:
        out["error"] = "Zadaj ticker (napr. AMZN)."
        return out

    short_df = prepare_sector_df_for_insights(short_df)
    if short_df.empty:
        out["error"] = "Chýba krátkodobá tabuľka — ulož **krátkodobý** snímok (OCR), potom skús znova."
        return out

    sec = sector_for_ticker(t)
    if sec is None:
        out["error"] = (
            f"Ticker **{t}** nie je v záložke **Symboly** — pridaj ho a nastav **sektor** z rovnakého zoznamu ako v manuáli."
        )
        return out
    sec = sec.strip()
    if not sec or sec == "—":
        out["error"] = f"Pre **{t}** v Symboly nie je nastavený sektor — vyber ho v zozname sektorov."
        return out

    out["sector_symboly"] = sec
    m_t = match_table_sector(sec, short_df)
    out["table_row"] = m_t

    pj = canonical_journal_sector(sec)
    tw = 0.0
    for s, w in (portfolio_weights or {}).items():
        cj = canonical_journal_sector(s)
        if pj and cj == pj:
            tw += float(w)
        elif not pj and (s or "").strip().lower() == sec.lower():
            tw += float(w)
    out["weight_same_sector"] = tw

    ot = open_trades or []
    out["in_open_trades"] = any(str(x.get("ticker") or "").strip().upper() == t for x in ot)

    lines: list[str] = []

    sim = cosine_similarity_sectors(short_df)
    sectors = sim.index.tolist()
    high_pairs: list[tuple[str, str]] = []
    for i, a in enumerate(sectors):
        for j in range(i + 1, len(sectors)):
            b = sectors[j]
            if float(sim.iloc[i, j]) >= sim_threshold_high:
                high_pairs.append((a, b))
    clusters = _union_find_clusters(high_pairs, sectors)

    mapped: dict[str, str] = {}
    for s2, w in (portfolio_weights or {}).items():
        mm = match_table_sector(s2, short_df)
        if mm:
            mapped[s2] = mm
    table_secs_pf = list({mapped[k] for k in mapped if mapped[k]})

    avg_sim: Optional[float] = None
    if m_t and table_secs_pf and m_t in sim.index:
        peers = [p for p in table_secs_pf if p != m_t]
        if peers:
            sims = [float(sim.loc[m_t, p]) for p in peers if p in sim.columns]
            if sims:
                avg_sim = float(np.mean(sims))
        elif m_t in table_secs_pf:
            lines.append(
                "Portfólio v tabuľke mapuje len na **rovnaký riadok** ako tento ticker — "
                "„priemerná podobnosť k iným“ by bola zavádzajúca; zváž koncentráciu v sektore (váha vyššie)."
            )
            out["verdict"] = (
                "Z pohľadu tabuľky ide skôr o **prehĺbenie** rovnakého „správania“ ako o diverzifikáciu."
            )
    out["avg_sim_to_portfolio_table"] = avg_sim

    lines.append(f"**Symboly:** `{sec}`" + (f" → riadok v krátkodobej tabuľke: `{m_t}`" if m_t else " → **nespárované** s tabuľkou (skontroluj OCR alebo názov sektora)."))

    if out["in_open_trades"]:
        lines.append("Ticker už máš v **otvorených** obchodoch — ide o kontrolu expozície, nie čistý „nový“ nápad.")

    if portfolio_weights:
        lines.append(f"Podiel **rovnakého sektora** (evidencia) v otvorených nohách: **{tw:.1%}** (orientácia z |prémia|×100).")
    else:
        lines.append("V denníku nemáš **otvorené** nohy — váhy sektorov sú 0 %; vyhodnotenie je len o tabuľke a sektore tickera.")

    if avg_sim is not None and table_secs_pf:
        lines.append(
            f"Priemerná **kosínusová podobnosť** výkonu sektora tickera k **ostatným** tabuľkovým riadkom z portfólia: **{avg_sim:.2f}** "
            "(1 = rovnaký smer vektoru krátkych výnosov ako v tabuľke)."
        )
        if out.get("verdict") is None:
            if avg_sim >= 0.82:
                out["verdict"] = "Podobné krátkodobé správanie ako už zastúpené sektory v tabuľke — diverzifikácia z tohto uhla **slabšia**."
            elif avg_sim <= 0.58:
                out["verdict"] = "Odlišnejší výkonnostný profil v tabuľke oproti tvojim zastúpeným sektorom — z tohto uhla **vhodnejší diverzifikátor**."
            else:
                out["verdict"] = "Stredná podobnosť — zváž aj koncentráciu v sektore (váha vyššie) a fundament."
    elif m_t and not table_secs_pf:
        lines.append("Portfólio sa nepodarilo namapovať na žiadny riadok tabuľky — priemernú podobnosť neviem vypočítať.")
        if out.get("verdict") is None:
            out["verdict"] = "Doplň / oprav OCR tabuľku alebo sektory v Symboly, aby sedeli s riadkami."
    elif not m_t:
        if out.get("verdict") is None:
            out["verdict"] = "Najprv spáruj sektor tickera s tabuľkou (OCR / editor)."
    elif out.get("verdict") is None:
        out["verdict"] = "Nedostatok dát na podobnosť."

    if tw >= 0.38:
        lines.append("⚠ Už máš **vysoký** podiel tohto sektora v otvorených nohách — pridanie rovnakého sektora zvyšuje koncentráciu.")

    if m_t and clusters and portfolio_weights:
        table_to_cluster: dict[str, int] = {}
        for ci, g in enumerate(clusters):
            for sname in g:
                table_to_cluster[sname] = ci
        ci_t = table_to_cluster.get(m_t)
        if ci_t is not None:
            cluster_w = 0.0
            for s2, w in portfolio_weights.items():
                ts = mapped.get(s2)
                if ts and table_to_cluster.get(ts) == ci_t:
                    cluster_w += float(w)
            if cluster_w >= 0.42:
                g = clusters[ci_t]
                _shown: list[str] = []
                _seen_lab: set[str] = set()
                for x in g[:10]:
                    lab = _insight_sector_label(str(x))
                    k = lab.casefold()
                    if k in _seen_lab:
                        continue
                    _seen_lab.add(k)
                    _shown.append(lab)
                lines.append(
                    f"⚠ Veľa váhy (**{cluster_w:.0%}**) už je v **rovnakom klastri** tabuľkového správania ako tento ticker "
                    f"[{', '.join(_shown[:6])}{'…' if len(g) > 6 else ''}] — pozor na „skrytú“ koncentráciu."
                )

    long_df = normalize_sector_dataframe(long_df) if long_df is not None else None
    if long_df is not None and not long_df.empty:
        jlab = journal_sector_from_table_row_name(m_t) if m_t else None
        if not jlab:
            jlab = pj or canonical_journal_sector(sec) or sec
        lm = match_table_sector(jlab, long_df)
        if lm:
            mrow = long_df[long_df["sector"].astype(str) == lm]
            if not mrow.empty and pd.notna(mrow.iloc[0].get("pct_1y")):
                lines.append(f"Dlhodobý snímok — **1r** pre zodpovedajúci riadok: **{float(mrow.iloc[0]['pct_1y']):+.1f} %**.")

    out["lines"] = lines
    return out
