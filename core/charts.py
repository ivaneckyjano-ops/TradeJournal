"""
Plotly chart buildery pre TradeJournal.
"""
from __future__ import annotations

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from typing import Optional

from core.probability import SDLines, lognormal_prices, calc_sd_lines


# ─── Farby ────────────────────────────────────────────────────────────────────
C_1SD = "rgba(46, 204, 113, 0.25)"
C_1SD_LINE = "rgba(46, 204, 113, 0.9)"
C_2SD = "rgba(52, 152, 219, 0.12)"
C_2SD_LINE = "rgba(52, 152, 219, 0.7)"
C_SPOT = "rgba(241, 196, 15, 0.9)"
C_STRIKE = "rgba(231, 76, 60, 0.85)"


def sd_lines_chart(
    sd: SDLines,
    ticker: str = "Ticker",
    strikes: Optional[list[float]] = None,
    strike_labels: Optional[list[str]] = None,
) -> go.Figure:
    """
    Graf s horizontálnymi SD líniami a voliteľnými strike čiarami.
    Vizualizuje rozdeľovacie pásma okolo aktuálnej ceny.
    """
    fig = go.Figure()

    x_range = [0, 1]  # placeholder os X (dte → expiry)

    # 2SD pásmo
    fig.add_hrect(
        y0=sd.lower_2sd, y1=sd.upper_2sd,
        fillcolor=C_2SD, line_width=0,
        annotation_text="2SD  ~95%", annotation_position="top right",
        annotation_font_color=C_2SD_LINE,
    )
    # 1SD pásmo
    fig.add_hrect(
        y0=sd.lower_1sd, y1=sd.upper_1sd,
        fillcolor=C_1SD, line_width=0,
        annotation_text="1SD  ~68%", annotation_position="top right",
        annotation_font_color=C_1SD_LINE,
    )

    # SD hraničné čiary
    for y, color, dash, label in [
        (sd.upper_2sd, C_2SD_LINE, "dot", f"+2SD  ${sd.upper_2sd:.2f}"),
        (sd.lower_2sd, C_2SD_LINE, "dot", f"−2SD  ${sd.lower_2sd:.2f}"),
        (sd.upper_1sd, C_1SD_LINE, "dash", f"+1SD  ${sd.upper_1sd:.2f}"),
        (sd.lower_1sd, C_1SD_LINE, "dash", f"−1SD  ${sd.lower_1sd:.2f}"),
    ]:
        fig.add_hline(y=y, line_color=color, line_dash=dash, line_width=1.5,
                      annotation_text=label, annotation_position="left",
                      annotation_font_size=11)

    # Aktuálna cena
    fig.add_hline(y=sd.spot, line_color=C_SPOT, line_width=2,
                  annotation_text=f"Spot  ${sd.spot:.2f}",
                  annotation_position="right",
                  annotation_font_color=C_SPOT)

    # Strike čiary
    if strikes:
        labels = strike_labels or [f"Strike ${s:.0f}" for s in strikes]
        for s, lbl in zip(strikes, labels):
            fig.add_hline(y=s, line_color=C_STRIKE, line_dash="dashdot", line_width=1.8,
                          annotation_text=lbl, annotation_position="right",
                          annotation_font_color=C_STRIKE, annotation_font_size=11)

    fig.update_layout(
        title=f"{ticker}  |  IV: {sd.iv*100:.1f}%  |  DTE: {sd.dte}  |  SD pohyb: ±${sd.sd_move:.2f}",
        yaxis_title="Cena ($)",
        xaxis=dict(visible=False),
        height=420,
        margin=dict(l=90, r=180, t=60, b=20),
        plot_bgcolor="rgba(20,20,30,0.95)",
        paper_bgcolor="rgba(20,20,30,0.0)",
        font_color="#e0e0e0",
    )
    return fig


def bell_curve_chart(
    spot: float,
    iv: float,
    dte: int,
    ticker: str = "Ticker",
    strikes: Optional[list[float]] = None,
    strike_labels: Optional[list[str]] = None,
) -> go.Figure:
    """
    Bell curve (log-normálna distribúcia cien pri expirácii)
    s vyznačenými SD pásmami a strike čiarami.
    """
    prices, densities = lognormal_prices(spot, iv, dte)
    sd = calc_sd_lines(spot, iv, dte)

    fig = go.Figure()

    # Výplne pod krivkou
    def _fill_between(lo, hi, color, name):
        mask = (prices >= lo) & (prices <= hi)
        px = np.concatenate([[lo], prices[mask], [hi]])
        py = np.concatenate([[0], densities[mask], [0]])
        fig.add_trace(go.Scatter(
            x=px, y=py, fill="tozeroy",
            fillcolor=color, line=dict(width=0),
            name=name, showlegend=True,
            hoverinfo="skip",
        ))

    _fill_between(sd.lower_2sd, sd.upper_2sd, C_2SD, "2SD ~95%")
    _fill_between(sd.lower_1sd, sd.upper_1sd, C_1SD, "1SD ~68%")

    # Krivka
    fig.add_trace(go.Scatter(
        x=prices, y=densities,
        line=dict(color="#e0e0e0", width=2),
        name="Distribúcia", showlegend=False,
    ))

    # Vertikálne čiary pre SD
    for x, color, label in [
        (sd.lower_2sd, C_2SD_LINE, f"−2SD ${sd.lower_2sd:.1f}"),
        (sd.upper_2sd, C_2SD_LINE, f"+2SD ${sd.upper_2sd:.1f}"),
        (sd.lower_1sd, C_1SD_LINE, f"−1SD ${sd.lower_1sd:.1f}"),
        (sd.upper_1sd, C_1SD_LINE, f"+1SD ${sd.upper_1sd:.1f}"),
        (spot, C_SPOT, f"Spot ${spot:.1f}"),
    ]:
        fig.add_vline(x=x, line_color=color, line_width=1.5, line_dash="dash",
                      annotation_text=label, annotation_position="top",
                      annotation_font_size=10)

    # Strike čiary
    if strikes:
        labels = strike_labels or [f"${s:.0f}" for s in strikes]
        for s, lbl in zip(strikes, labels):
            fig.add_vline(x=s, line_color=C_STRIKE, line_width=2, line_dash="dashdot",
                          annotation_text=lbl, annotation_position="top right",
                          annotation_font_color=C_STRIKE, annotation_font_size=11)

    fig.update_layout(
        title=f"{ticker}  |  Bell Curve pri expirácii  |  DTE: {dte}  |  IV: {iv*100:.1f}%",
        xaxis_title="Cena ($)",
        yaxis_title="Hustota pravdepodobnosti",
        height=400,
        margin=dict(l=60, r=60, t=60, b=40),
        plot_bgcolor="rgba(20,20,30,0.95)",
        paper_bgcolor="rgba(20,20,30,0.0)",
        font_color="#e0e0e0",
        legend=dict(orientation="h", y=-0.2),
    )
    return fig


def pnl_timeline_chart(trades: list[dict]) -> go.Figure:
    """Kumulatívny P&L graf zo zoznamu uzavretých obchodov."""
    import pandas as pd
    from core.database import compute_pnl

    closed = [t for t in trades if t.get("status") == "Closed" and t.get("exit_date")]
    if not closed:
        fig = go.Figure()
        fig.update_layout(
            title="Žiadne uzavreté obchody",
            height=300,
            plot_bgcolor="rgba(20,20,30,0.95)",
            paper_bgcolor="rgba(20,20,30,0.0)",
            font_color="#e0e0e0",
        )
        return fig

    rows = []
    for t in closed:
        pnl_val = compute_pnl(t)
        if pnl_val is None:
            continue
        # Normalizuj dátum (YYYYMMDD alebo YYYY-MM-DD)
        raw_d = str(t.get("exit_date", ""))
        if len(raw_d) == 8 and "-" not in raw_d:
            norm_d = f"{raw_d[:4]}-{raw_d[4:6]}-{raw_d[6:8]}"
        else:
            norm_d = raw_d
        rows.append({
            "exit_date": norm_d,
            "pnl": round(pnl_val),
            "ticker": t.get("ticker", ""),
            "strategy": t.get("strategy", ""),
            "leg_type": t.get("leg_type", ""),
            "strike": t.get("strike", 0),
            "trade_id": t.get("id", ""),
        })

    if not rows:
        fig = go.Figure()
        fig.update_layout(title="Žiadne P&L dáta", height=300)
        return fig

    df = pd.DataFrame(rows)
    df["exit_date"] = pd.to_datetime(df["exit_date"], errors="coerce")
    df = df.dropna(subset=["exit_date"]).sort_values("exit_date")
    
    # Pridaj dnešný bod ak nie je v dátach, aby graf nekončil včerajškom
    from datetime import date as _d
    today_dt = pd.to_datetime(_d.today())
    if not df.empty and df["exit_date"].max() < today_dt:
        last_cum = df["pnl"].sum()
        new_row = pd.DataFrame([{
            "exit_date": today_dt, 
            "cumulative_pnl": last_cum,
            "pnl": 0,
            "ticker": "Dnes",
            "strategy": "",
            "leg_type": "",
            "strike": 0,
            "trade_id": ""
        }])
        df = pd.concat([df, new_row], ignore_index=True)

    df["cumulative_pnl"] = df["pnl"].cumsum()

    # Farba čiary podľa poslednej hodnoty
    last_val = df["cumulative_pnl"].iloc[-1]
    line_color = "#2ecc71" if last_val >= 0 else "#e74c3c"
    fill_color = "rgba(46,204,113,0.12)" if last_val >= 0 else "rgba(231,76,60,0.10)"

    fig = go.Figure()

    # Priebežný P&L (jednotlivé obchody - stĺpcový)
    bar_colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in df["pnl"]]
    fig.add_trace(go.Bar(
        x=df["exit_date"],
        y=df["pnl"],
        name="P&L / obchod",
        marker_color=bar_colors,
        opacity=0.45,
        yaxis="y2",
        hovertemplate=(
            "<b>%{x|%d.%m.%Y}</b><br>"
            "Obchod P&L: <b>$%{y:+,d}</b><br>"
            "<extra></extra>"
        ),
    ))

    # Kumulatívna čiara
    hover_text = [
        f"{row['ticker']} {row['leg_type']} ${row['strike']:.0f} | #{row['trade_id']}"
        for _, row in df.iterrows()
    ]
    fig.add_trace(go.Scatter(
        x=df["exit_date"],
        y=df["cumulative_pnl"],
        mode="lines+markers",
        line=dict(color=line_color, width=2.5),
        marker=dict(size=7, color=line_color),
        fill="tozeroy",
        fillcolor=fill_color,
        name="Kumulatívny P&L",
        text=hover_text,
        hovertemplate=(
            "<b>%{x|%d.%m.%Y}</b><br>"
            "Kum. P&L: <b>$%{y:+,d}</b><br>"
            "%{text}<extra></extra>"
        ),
    ))

    fig.add_hline(y=0, line_color="rgba(255,255,255,0.25)", line_width=1)

    fig.update_layout(
        title=f"Kumulatívny P&L — celkom: <b>${last_val:+,.0f}</b>",
        xaxis_title="Dátum",
        yaxis=dict(title="Kum. P&L ($)", side="left"),
        yaxis2=dict(title="P&L / obchod ($)", side="right", overlaying="y", showgrid=False),
        height=350,
        margin=dict(l=70, r=70, t=55, b=40),
        plot_bgcolor="rgba(20,20,30,0.95)",
        paper_bgcolor="rgba(20,20,30,0.0)",
        font_color="#e0e0e0",
        legend=dict(orientation="h", y=-0.25),
        barmode="overlay",
    )
    return fig
