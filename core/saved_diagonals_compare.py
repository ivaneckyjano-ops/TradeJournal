"""
Porovnanie uložených riadkov diagonál (rovnaký tvar stĺpcov ako v UI po ×100) —
heuristické poradie + krátke slovenské zdôvodnenie, bez LLM a bez externých API.
"""

from __future__ import annotations

import io
import math
from datetime import datetime, timezone
from typing import Any, Optional

import numpy as np
import pandas as pd


def _n(v: Any) -> float:
    if v is None or (isinstance(v, float) and (math.isnan(v) or np.isnan(v))):
        return float("nan")
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


def _find_col(columns: list, *, need: str, avoid: frozenset[str] = frozenset()) -> Optional[str]:
    for c in columns:
        s = str(c).lower()
        if need in s and not any(a in s for a in avoid):
            return str(c)
    return None


def _resolve_columns(df: pd.DataFrame) -> dict[str, Optional[str]]:
    c = [str(x) for x in df.columns]
    id_c = "ID" if "ID" in df.columns else (next((x for x in c if "id" == str(x).lower()), None) or c[0] if c else None)
    short_bid = next(
        (
            x
            for x in c
            if "short" in str(x).lower() and "bid" in str(x).lower() and "debit" not in str(x).lower()
        ),
        None,
    )
    return {
        "id": id_c,
        "ticker": next((x for x in c if str(x).lower() == "ticker"), None),
        "delta": _find_col(c, need="čistá delta", avoid=frozenset({"theta", "vega"})),
        "theta": _find_col(c, need="čistá theta", avoid=frozenset({"vega"})),
        "vega": _find_col(c, need="čistá vega", avoid=frozenset({"gamma"})),
        "debit": next((x for x in c if "debit" in str(x).lower() or "kredit" in str(x).lower()), None),
        "skore": next((x for x in c if "skóre" in str(x).lower() or str(x) == "Skóre"), None),
        "short_bid": short_bid,
        "short_strike": next(
            (x for x in c if "short" in str(x).lower() and "strike" in str(x).lower()), None
        ),
        "long_strike": next(
            (x for x in c if "long" in str(x).lower() and "strike" in str(x).lower()), None
        ),
    }


def _short_bid_quality_0_100(sbid: float) -> float:
    """
    0 = žiadna/mizerná likvidita na short nohe, 100 = solídny bid (USD/akcia).
    0,20 $ je už „tenké“; pod ~0,15 rátame ako veľmi slabé.
    """
    if math.isnan(sbid) or sbid < 0:
        return 5.0
    if sbid >= 1.4:
        return 100.0
    if sbid >= 0.9:
        return 90.0
    if sbid >= 0.6:
        return 78.0
    if sbid >= 0.45:
        return 64.0
    if sbid >= 0.35:
        return 52.0
    if sbid >= 0.25:
        return 40.0
    if sbid >= 0.20:
        return 30.0
    if sbid >= 0.12:
        return 18.0
    return 8.0


def _minmax_0_100(xs: list[float], i: int) -> float:
    a = [x for x in xs if not math.isnan(x)]
    if not a:
        return 50.0
    lo, hi = min(a), max(a)
    if abs(hi - lo) < 1e-12:
        return 50.0
    v = xs[i]
    if math.isnan(v):
        return 50.0
    return (v - lo) / (hi - lo) * 100.0


def _median(vals: list[float]) -> float:
    a = [v for v in vals if not math.isnan(v)]
    if not a:
        return float("nan")
    a.sort()
    m = len(a) // 2
    if len(a) % 2:
        return a[m]
    return (a[m - 1] + a[m]) / 2.0


def _df_to_csv_block(df: pd.DataFrame) -> str:
    """CSV pre protokol — zaokrúhlené floaty, aby v exporte nevznikali artefakty floatu."""
    buf = io.StringIO()
    try:
        df.to_csv(buf, index=False, float_format="%.6g")
    except (TypeError, ValueError):
        buf = io.StringIO()
        df.to_csv(buf, index=False)
    return buf.getvalue().rstrip()


def compare_saved_diagonals(
    rows: pd.DataFrame,
) -> tuple[pd.DataFrame, str, str]:
    """
    Zoradí ``rows`` a vráti:

    - tabuľku s **kompozitným** skóre 0–100 a „prečo“
    - **stručný** Markdown (Top 1 a 2)
    - **plný protokol** Markdown (metodika, vstupné CSV, výsledok — vhodné na kopírovanie)

    **Kompozit** = váha *bázy* (Skóre alebo heuristika, v sade 0–100) + váha
    **kvality Short — bid** (abs. USD/akcia z dát; nízky bid = horšia plniteľnosť, „tenký“ vankúš).
    Ak stĺpec *Short — bid* chýba, likviditná zložka je neutrálna.
    """
    if rows is None or rows.empty:
        return (
            pd.DataFrame(),
            "_Žiadne riadky na porovnanie._",
            "# Protokol — porovnanie uložených diagonál\n\n*Žiadne riadky.*\n",
        )

    m = _resolve_columns(rows)
    if not m["id"] or m["id"] not in rows.columns:
        m["id"] = "ID" if "ID" in rows.columns else str(rows.columns[0])

    # Vylúčiť riadky kde Long strike < Short strike — neplatná konfigurácia diagonály.
    ssk_col = m.get("short_strike")
    lsk_col = m.get("long_strike")
    filtered_invalid_ids: list = []
    if ssk_col and lsk_col and ssk_col in rows.columns and lsk_col in rows.columns:
        ss_num = pd.to_numeric(rows[ssk_col], errors="coerce")
        ls_num = pd.to_numeric(rows[lsk_col], errors="coerce")
        inv_mask = ss_num.notna() & ls_num.notna() & (ls_num < ss_num)
        if inv_mask.any():
            id_col = m["id"] if m["id"] and m["id"] in rows.columns else None
            filtered_invalid_ids = (
                rows.loc[inv_mask, id_col].tolist() if id_col else rows.index[inv_mask].tolist()
            )
            rows = rows.loc[~inv_mask].reset_index(drop=True)
            if rows.empty:
                warn = (
                    f"⚠️ Všetky riadky boli vyfiltrované (Long strike < Short strike): "
                    f"ID {', '.join(str(x) for x in filtered_invalid_ids)}. Žiadne platné riadky na porovnanie."
                )
                return (
                    pd.DataFrame(),
                    warn,
                    f"# Protokol — porovnanie uložených diagonál\n\n{warn}\n",
                )

    ids = []
    thetas, debits, dltas, vegas, skores, tickers, short_bids = [], [], [], [], [], [], []
    sbc = m.get("short_bid")
    for _, row in rows.iterrows():
        ids.append(row.get(m["id"]))
        tcol, dcol, dlcol, vcol, scol = m["theta"], m["debit"], m["delta"], m["vega"], m["skore"]
        thetas.append(_n(row.get(tcol)) if tcol else float("nan"))
        debits.append(_n(row.get(dcol)) if dcol else float("nan"))
        dltas.append(_n(row.get(dlcol)) if dlcol else float("nan"))
        vegas.append(_n(row.get(vcol)) if vcol else float("nan"))
        skores.append(_n(row.get(scol)) if scol and scol in row.index else float("nan"))
        if sbc and sbc in row.index:
            short_bids.append(_n(row.get(sbc)))
        else:
            short_bids.append(float("nan"))
        tt = m["ticker"] or ""
        tickers.append(
            str(row.get(tt)).strip().upper() if tt and not pd.isna(row.get(tt)) else ""
        )

    med_th = _median(thetas)
    med_db = _median([abs(d) for d in debits if not math.isnan(d)])
    med_dlt = _median([abs(d) for d in dltas if not math.isnan(d)])

    use_search_score = m["skore"] is not None and not all(math.isnan(s) for s in skores)

    def _heuristic(
        th: float, db: float, dlt: float, vg: float
    ) -> float:
        thv = 0.0 if math.isnan(th) else th
        ab = abs(db) if not math.isnan(db) else 1e9
        eff = thv / (ab + 1.0) if ab < 1e8 else 0.0
        dltb = 0.0 if math.isnan(dlt) else abs(dlt)
        vg_v = 0.0 if math.isnan(vg) else vg
        return eff * 1_000.0 - dltb * 0.2 + vg_v * 0.1

    primary: list[float] = []
    for i in range(len(ids)):
        th, db, dlt, sk, vg = thetas[i], debits[i], dltas[i], skores[i], vegas[i]
        if use_search_score and not math.isnan(sk):
            primary.append(float(sk))
        else:
            primary.append(_heuristic(th, db, dlt, vg))

    W_BASE = 0.52
    W_BID = 0.48
    composite: list[float] = []
    for i in range(len(ids)):
        pnorm = _minmax_0_100(primary, i)
        bq = _short_bid_quality_0_100(short_bids[i]) if sbc else 50.0
        composite.append(W_BASE * pnorm + W_BID * bq)

    scores: list[tuple[float, int, float, float, float]] = []
    for i in range(len(ids)):
        pnorm = _minmax_0_100(primary, i)
        bq = _short_bid_quality_0_100(short_bids[i]) if sbc else 50.0
        comp = composite[i]
        pri = primary[i]
        scores.append((comp, i, pri, pnorm, bq))

    scores.sort(key=lambda x: (-x[0], -x[4] if not math.isnan(x[4]) else 0, x[1]))

    def _sb_note(sv: float) -> str:
        if math.isnan(sv) or not sbc:
            return "bez dát o short bide (neutr. 50 % v kompozite)"
        if sv < 0.25:
            return "slabý short bid — tenký „vankúš“ na cene, skontroľuj real-time spread v platforme"
        if sv < 0.4:
            return "short bid stredne nízky — môže byť veľký % spread; vhodné overiť hĺbku trhu"
        return "short bid v rozumnej hladine oproti typu striku"

    reasons_by_rank: list[str] = []
    for _r, row_t in enumerate(scores, start=1):
        comp, j, _pri, _pn, bq = row_t[0], row_t[1], row_t[2], row_t[3], row_t[4]
        th, db, dlt, sk, vg = thetas[j], debits[j], dltas[j], skores[j], vegas[j]
        sbj = short_bids[j] if sbc else float("nan")
        parts: list[str] = []
        if use_search_score and not math.isnan(sk):
            parts.append("báza: **Skóre** z hľadania (v sade znormované) + **likvidita short bida**")
        else:
            parts.append("báza: heuristika theta/|debit| + **likvidita short bida**")
        if sbc and not math.isnan(sbj):
            parts.append(f"short bid {sbj:.2f} $ → kval. **{bq:.0f}**/100; {_sb_note(sbj)}")
        elif sbc:
            parts.append("short bid chýba — v kompozite penalizované")
        if not math.isnan(med_th) and not math.isnan(th) and th >= med_th - 1e-9:
            parts.append("theta aspoň na mediáne sady")
        if not math.isnan(med_db) and not math.isnan(db) and abs(db) + 1e-6 < med_db:
            parts.append("nižší |debit| oproti mediánu sady")
        if not parts:
            parts.append("kompozitné skóre v tabuľke (Bázové / Short bid kval.)")
        reasons_by_rank.append("; ".join(parts[:3]))

    ticker_col = m["ticker"]
    theta_col = m["theta"]
    debit_col = m["debit"]
    delta_col = m["delta"]
    short_h = []
    for ridx, row_t in enumerate(scores, start=1):
        comp, j, pri, pnorm, bq = row_t[0], row_t[1], row_t[2], row_t[3], row_t[4]
        d = {
            m["id"]: ids[j],
            "Poradie": ridx,
            "Kompozit 0–100": round(float(comp), 1),
            "Bázové 0–100": round(float(pnorm), 1),
            "Short bid (kval.) 0–100": round(float(bq), 1),
            "Skóre/heur. (pôv.)": _fmt(pri),
        }
        if ticker_col and ticker_col in rows.columns:
            d["Ticker"] = rows.iloc[j].get(ticker_col, "")
        if sbc and sbc in rows.columns:
            bvv = _n(rows.iloc[j].get(sbc))
            d["Short bid $"] = f"{bvv:.2f}" if not math.isnan(bvv) else "—"
        d["Dôvod (struč.)"] = reasons_by_rank[ridx - 1]
        for label, col in (("Θ(×100)", theta_col), ("Debit $", debit_col), ("|Δ|×100", delta_col)):
            if col and col in rows.columns:
                v = _n(rows.iloc[j].get(col))
                d[label] = f"{v:.4f}" if not math.isnan(v) else "—"
        short_h.append(d)
    out_detail = pd.DataFrame(short_h)

    n = len(scores)
    tset = {t for t in tickers if t}
    warn_tk = ""
    if len(tset) > 1:
        warn_tk = (
            f"\n\n*Upozornenie: v sade je viac tickeroch ({', '.join(sorted(tset))}) — porovnanie je len z daných stĺpcov, medzi inými podkladmi ostro neporovnávaj.*\n"
        )

    all_have_sk = use_search_score and all(not math.isnan(skores[i]) for i in range(len(ids)))
    if sbc:
        wtxt = (
            f"**Kompozit 0–100** = cca **{W_BASE*100:.0f}%** bázové (Skóre alebo heur., v sade 0–100) "
            f"+ **{W_BID*100:.0f}%** **kvalita Short — bid** (0–100; zodpovedá tomu, či je u short nohy dostatok „ceny v order booku“; pod ~0,25 $/ak. považuj trh za stlačený). "
        )
    else:
        wtxt = "**Poznámka:** v uložených riadkoch chýba stĺpec *Short — bid* — do kompozitu sa dosadí neutrál. "
    if all_have_sk:
        btxt = "Báza: z **Skóre** z pôvodného hľadania. "
    elif use_search_score:
        btxt = "Báza: **Skóre** alebo heuristika tam, kde v uložení chýba. "
    else:
        btxt = "Báza: heuristika z theta a |debitu|. "

    inv_note = ""
    if filtered_invalid_ids:
        inv_note = (
            f"\n\n⚠️ **Vyfiltrované** riadky (Long strike < Short strike — neplatná konfigurácia): "
            f"ID {', '.join(str(x) for x in filtered_invalid_ids)}. Tieto sa do porovnania nezapočítali."
        )

    lines: list[str] = [
        wtxt + btxt + f"Počet v porovnaní: **{n}**.{warn_tk}{inv_note}",
        "",
    ]

    def _low_bid_note(j: int) -> str | None:
        if not sbc:
            return None
        sbj = short_bids[j]
        if math.isnan(sbj) or sbj < 0.0:
            return "Short bid **chýba alebo 0** — dáta môžu byť neúplné; pred real trade skontroluj kótovanie u brokera."
        if sbj < 0.30:
            return f"**Likvidita:** *Short — bid* len **{sbj:.2f}** $/ak. — môže to znamenať veľký *spread* alebo zlú plniteľnosť; „vankúš“ na priaznivý sklz je obmedzený."
        return None

    def _block(choice: int, j: int) -> None:
        th, db, dlt, sk, tid = thetas[j], debits[j], dltas[j], skores[j], ids[j]
        bwarn = _low_bid_note(j)
        bullet: list[str] = []
        if bwarn:
            bullet.append(bwarn)
        if sbc and not math.isnan(short_bids[j]) and short_bids[j] >= 0.35:
            bullet.append("na short nohe je v dátach **prijateľnejší** bid oproti „tenkým“ 0,15–0,25 (nižší reálny ohlad, ak sedí cena a spread)")
        if not math.isnan(med_th) and not math.isnan(th) and th >= med_th - 1e-9:
            bullet.append("čistá theta v sade nie je horšia ako bežné kandidáty (decay/profit stránka reťazca)")
        if not math.isnan(med_db) and not math.isnan(db) and abs(db) + 1e-6 < med_db:
            bullet.append("abs. **debit (lot)** pod mediánom v tejto sade")
        if not math.isnan(med_dlt) and not math.isnan(dlt) and abs(dlt) + 0.1 < med_dlt:
            bullet.append("**|čistá delta ×100|** o niečo kompaktnejšia oproti stredu sady")
        if not bullet:
            bullet.append("kombinácia **Kompozit** / stĺpec Dôvod — žiadna jedna noha extrém nevybočila")
        lines.append(f"### {choice}. miesto — **ID** `{tid}`")
        for b in bullet[:4]:
            lines.append(f"- {b}")
        lines.append("")

    if n >= 1:
        j0 = scores[0][1]
        _block(1, j0)
    if n >= 2:
        j1 = scores[1][1]
        _block(2, j1)

    lines.append(
        "_Odoslanie do Buildera: v stĺpci **Do Buildera** ponechaj stále **práve jeden** riadok._"
    )
    summary_md = "\n".join(lines).strip()
    summary_in_protocol = summary_md.replace(inv_note, "", 1).replace("\n\n\n", "\n\n").strip() if inv_note else summary_md
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    proto: list[str] = [
        "# Protokol — porovnanie uložených diagonál (TradeJournal)",
        "",
        f"- **Čas (UTC):** `{ts}`",
        f"- **Počet porovnávaných záznamov:** {n}",
    ]
    if filtered_invalid_ids:
        proto.append(
            f"- **Vyfiltrované** (Long strike < Short strike): "
            f"ID {', '.join(str(x) for x in filtered_invalid_ids)}"
        )
    proto += [
        "",
        "## 1. Spôsob porovnania (metodika)",
        "",
        "### 1.1 Kompozit 0–100",
        f"- Vzorec: **Kompozit = {W_BASE} × B + {W_BID} × L**",
        "  - **B** (bázová zložka, 0–100): v rámci *tejto sady* riadkov sa primárne skóre **min–max** škáluje na 0–100 (ak majú všetci rovnaké číslo, dosadí sa 50).",
        "  - **L** (likvidita short nohy, 0–100): z absolútnej ceny **Short — bid** v $/akcii (v importe z Barchartu), podľa prahov v kóde (`_short_bid_quality_0_100`); ~0,20 $ → nízka L.",
        "- Ak stĺpec *Short — bid* **chýba**, do výpočtu L sa dosadí **50** (neutrál).",
        "",
        "### 1.2 Primárne skóre (pred normalizáciou na B)",
    ]
    if all_have_sk:
        proto.append("- Pre každý riadok: hodnota stĺpca **Skóre** z pôvodného hľadania (ak je prázdna, heuristika z čistej theta / |debitu| + doladenie).")
    elif use_search_score:
        proto.append("- Kde existuje **Skóre** z uloženia, berie sa ono; kde chýba, heuristika: th/(|debit|+1) v upravenej škále + jemné |delta|/vega (viď kód).")
    else:
        proto.append(
            "- Heuristika: **čistá theta (×100)** a **|debit lot|** — pomer th/(|debit|+1) s váhami, menšia |čistá delta×100| a vega mierne zvyšujú skóre."
        )
    proto.extend(
        [
            "",
            "### 1.2.1 Význam čísel (Skóre a sada)",
            "- Stĺpec **Skóre** z pôvodného hľadania **nie je** pevná škála 0–100. Pri veľmi malej |čistá delta| môžu byť hodnoty veľké (vo výpočte v `core/diagonal_spread_search` je mimo iného člen úmerný 1/|net delta|+ε). V kompozite rozhoduje až **B** = min–max *v tejto sade* — teda **relatívne poradie**, nie „absolútna veľkosť“ čísla v tabuľke.",
            "- **Kompozit** porovnáva kandidátov **navzájom v danej sade**; s inou výberkou riadkov môže iný kandidát skončiť vyššie, aj keď číselné Gréka sú rovnaké (iná normalizácia B).",
            "",
            "### 1.3 Triedenie",
            "- Riadky sa zoradia podľa **Kompozitu** (zostupne); pri rovnosti kvalita **L** a potom pôvodný index.",
            "",
            "## 2. Vstupné dáta (parametre porovnaných záznamov)",
            "",
            "Nižšie presná kópia stĺpcov, ktoré boli v porovnaní (uložené diagonály, bez stĺpcov Zmazať / Do Buildera).",
            "",
            "```text",
            _df_to_csv_block(rows),
            "```",
            "",
            "## 3. Výsledok: zhrnutie a odporúčania 1.–2. miesto",
            "",
        ]
    )
    if inv_note:
        proto.append("*(O vyfiltrovaných ID pozri hlavičku; text nižšie to neopakuje — v UI stručnom zhrnutí zostáva plná poznámka.)*")
        proto.append("")
    proto.append(summary_in_protocol)
    proto.extend(
        [
            "",
            "## 4. Tabuľka výsledku (kompozit, dôvody) — CSV",
            "",
            "```text",
            _df_to_csv_block(out_detail),
            "```",
            "",
            "---",
            "_TradeJournal — `core/saved_diagonals_compare.py`_",
        ]
    )
    return out_detail, summary_md, "\n".join(proto).strip()


def _fmt(x: float) -> str:
    if math.isnan(x):
        return "—"
    s = f"{x:.4f}"
    if len(s) > 12:
        s = f"{x:.2e}"
    return s
