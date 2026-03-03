"""
Ottoneu Power Rankings Dashboard
=================================
Interactive Dash app — reads the Excel workbook produced by
ottoneu_power_rankings.py and renders:

  1. League Overview  – stacked SPTS bar chart + Hit vs Pit scatter
  2. Team Report Card – stat tiles, radar chart, and player detail tables

Run:
    python ottoneu_dashboard.py
Then open http://127.0.0.1:8050 in your browser.
"""

import os
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from dash import Dash, dcc, html, Input, Output, dash_table
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
# Relative path so the app works both locally and when deployed
XLSX_PATH = Path(__file__).parent / "ottoneu_power_rankings.xlsx"

# Colour palette
C_HIT  = "#3B82F6"   # blue
C_SP   = "#10B981"   # green
C_RP   = "#F59E0B"   # amber
C_BG   = "#0F172A"   # dark navy background
C_CARD = "#1E293B"   # card background
C_TEXT = "#F1F5F9"   # light text
C_MUTED= "#94A3B8"   # muted label text
C_GRID = "#334155"   # chart gridlines

SLOT_ORDER = ["C", "1B", "2B", "SS", "MIF", "3B", "OF", "Util"]

# ── Load data ─────────────────────────────────────────────────────────────────
if not XLSX_PATH.is_file():
    raise FileNotFoundError(
        f"Workbook not found: {XLSX_PATH}\n"
        "Run ottoneu_power_rankings.py first to generate it."
    )

summary  = pd.read_excel(XLSX_PATH, sheet_name="Summary")
hitters  = pd.read_excel(XLSX_PATH, sheet_name="Hitters")
sp_data  = pd.read_excel(XLSX_PATH, sheet_name="SP_Detail")
rp_data  = pd.read_excel(XLSX_PATH, sheet_name="RP_Detail")
try:
    roster_data = pd.read_excel(XLSX_PATH, sheet_name="Roster")
except Exception:
    roster_data = pd.DataFrame()

# ── Team name display mapping (truncated CSV names → full display names) ──────
TEAM_NAMES = {
    "Even Baked": "Even Baked Alaska",
    "Logan &amp": "Logan & Logan Attorneys",
    "NTX Gambit": "NTX Gambit",
    "Naylor? I ": "Naylor? I Hardly Know Her",
    "Mirkwood S": "Mirkwood Spiders",
    "Continenta": "Continental Fire Tigers",
    "Hit the Kw": "Hit The Kwan",
    "Smoking He": "Smoking Heaters",
    "The Sandma": "The Sandman",
    "H-Town Ram": "H-Town Rampage",
    "The Big Lu": "The Big Luzinski",
    "Apple Cinn": "Apple Cinnamon Churious",
}

for _df in (summary, hitters, sp_data, rp_data, roster_data):
    if "Team" in _df.columns:
        _df["Team"] = _df["Team"].replace(TEAM_NAMES)

HAS_SALARY_DATA = all(c in summary.columns
                       for c in ["Total_Salary", "Avg_Age", "Sal_Hit", "Sal_SP", "Sal_RP"])

summary  = summary.sort_values("Total_SPTS", ascending=False).reset_index(drop=True)
summary["Rank"] = summary.index + 1

teams = summary["Team"].tolist()

# Per-slot SPTS totals for each team (for position radar)
POS_SLOTS = ["C", "1B", "2B", "SS", "3B", "MIF", "OF"]
slot_spts = (
    hitters.groupby(["Team", "Slot"])["SPTS"]
    .sum()
    .unstack(fill_value=0)
    .reindex(columns=POS_SLOTS, fill_value=0)
)
slot_league_avg = slot_spts.mean()
slot_league_max = slot_spts.max().replace(0, 1)  # avoid div-by-zero
slot_league_min = slot_spts.min()                # floor for min-max scaling

# Pitching radar pre-computation (K, BB avoidance, HR avoidance, SV, HLD)
# Min-max scale all 5 radar cols so league avg always sits near 50%
_PIT_MAX = summary[["Pit_K", "Pit_BB", "Pit_HR", "SV", "HLD"]].max().replace(0, 1)
_PIT_MIN = summary[["Pit_K", "Pit_BB", "Pit_HR", "SV", "HLD"]].min()

# ── Percentile ranks for conditional tile colouring ───────────────────────────
# pct_rank[col][team] = 0..1 where 1.0 = best in league for that stat
_HIGHER_BETTER = ["Total_SPTS", "Hit_SPTS", "SP_SPTS", "RP_SPTS",
                  "Hit_wOBA", "Hit_OPS", "Hit_HR", "Hit_SB", "Hit_2B", "Hit_BB",
                  "Pit_K", "SV", "HLD", "Total_IP"]
_LOWER_BETTER  = ["Pit_BB", "Pit_HR", "Pit_FIP", "Avg_Age"]

_idx = summary.set_index("Team")
pct_rank: dict = {}
for _col in _HIGHER_BETTER:
    if _col in _idx.columns:
        pct_rank[_col] = _idx[_col].rank(pct=True)
for _col in _LOWER_BETTER:
    if _col in _idx.columns:
        pct_rank[_col] = 1 - _idx[_col].rank(pct=True)
# Batting average (derived)
_ba = _idx.apply(lambda r: r["Hit_H"] / r["Hit_AB"] if r["Hit_AB"] > 0 else 0, axis=1)
pct_rank["BA"] = _ba.rank(pct=True)


def pct_color(col: str, team: str) -> str:
    """Green (high/good) → neutral (mid) → red (low/bad) gradient by percentile."""
    v = float(pct_rank.get(col, pd.Series(dtype=float)).get(team, 0.5))
    v = max(0.0, min(1.0, v))
    # Anchors: red #EF4444=(239,68,68) → white #E5E7EB=(229,231,235) → green #22C55E=(34,197,94)
    if v <= 0.5:
        t = v / 0.5
        r = round(239 + t * (229 - 239))
        g = round(68  + t * (231 - 68))
        b = round(68  + t * (235 - 68))
    else:
        t = (v - 0.5) / 0.5
        r = round(229 + t * (34  - 229))
        g = round(231 + t * (197 - 231))
        b = round(235 + t * (94  - 235))
    return f"#{r:02X}{g:02X}{b:02X}"


# ── Helpers ───────────────────────────────────────────────────────────────────

def rank_color(rank: int, n: int) -> str:
    """Green → amber → red gradient based on rank."""
    frac = (rank - 1) / max(n - 1, 1)
    if frac < 0.33:
        return "#22C55E"
    if frac < 0.66:
        return "#F59E0B"
    return "#EF4444"


def stat_tile(label: str, value: str, sub: str = "", color: str = C_TEXT) -> html.Div:
    return html.Div([
        html.P(label, style={"margin": "0", "fontSize": "11px",
                              "color": C_MUTED, "textTransform": "uppercase",
                              "letterSpacing": "0.08em"}),
        html.P(value, className="stat-tile-value", style={"margin": "2px 0", "fontSize": "26px",
                              "fontWeight": "700", "color": color}),
        html.P(sub,   style={"margin": "0", "fontSize": "11px", "color": C_MUTED}),
    ], style={
        "background": C_CARD,
        "borderRadius": "10px",
        "padding": "14px 18px",
        "minWidth": "120px",
        "flex": "1",
    })


def tile_group(label: str, tiles: list) -> html.Div:
    """Labelled group of stat tiles."""
    return html.Div([
        html.P(label, style={"margin": "0 0 6px", "fontSize": "10px",
                              "fontWeight": "700", "color": C_MUTED,
                              "textTransform": "uppercase", "letterSpacing": "0.12em"}),
        html.Div(tiles, style={"display": "flex", "gap": "8px", "flexWrap": "wrap"}),
    ], style={"marginBottom": "12px"})


def build_overview_fig(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Bar(
        name="Hitting",
        y=df["Team"], x=df["Hit_SPTS"],
        orientation="h", marker_color=C_HIT,
        hovertemplate="<b>%{y}</b><br>Hit SPTS: %{x:,.1f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Starting Pitching",
        y=df["Team"], x=df["SP_SPTS"],
        orientation="h", marker_color=C_SP,
        hovertemplate="<b>%{y}</b><br>SP SPTS: %{x:,.1f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        name="Relief Pitching",
        y=df["Team"], x=df["RP_SPTS"],
        orientation="h", marker_color=C_RP,
        hovertemplate="<b>%{y}</b><br>RP SPTS: %{x:,.1f}<extra></extra>",
    ))
    fig.update_layout(
        barmode="stack",
        paper_bgcolor=C_BG, plot_bgcolor=C_BG,
        font_color=C_TEXT,
        legend=dict(orientation="h", yanchor="top", y=-0.18,
                    xanchor="center", x=0.5,
                    bgcolor="rgba(0,0,0,0)", font_size=12),
        yaxis=dict(autorange="reversed", gridcolor=C_GRID,
                   tickfont=dict(size=12), ticksuffix="  "),
        xaxis=dict(gridcolor=C_GRID, title="Total SPTS"),
        margin=dict(l=180, r=20, t=30, b=90),
        height=max(400, len(df) * 32),
        title=dict(text="Power Rankings — Projected SPTS by Component",
                   font_size=14, x=0.5),
    )
    return fig


def build_scatter_fig(df: pd.DataFrame) -> go.Figure:
    fig = px.scatter(
        df, x="Hit_SPTS", y="Pit_SPTS",
        color="Total_SPTS",
        color_continuous_scale=["#EF4444", "#F59E0B", "#22C55E"],
        hover_name="Team",
        hover_data={"Total_SPTS": ":.1f", "Hit_SPTS": ":.1f", "Pit_SPTS": ":.1f"},
        labels={"Hit_SPTS": "Hitting SPTS", "Pit_SPTS": "Pitching SPTS"},
    )
    fig.update_traces(
        marker=dict(size=12, line=dict(width=1, color="white")),
    )
    avg_hit = df["Hit_SPTS"].mean()
    avg_pit = df["Pit_SPTS"].mean()
    fig.add_hline(y=avg_pit, line_dash="dot", line_color=C_MUTED, opacity=0.5)
    fig.add_vline(x=avg_hit, line_dash="dot", line_color=C_MUTED, opacity=0.5)
    fig.update_layout(
        paper_bgcolor=C_BG, plot_bgcolor=C_BG,
        font_color=C_TEXT,
        coloraxis_showscale=False,
        title=dict(text="Hitting vs Pitching SPTS", font_size=14, x=0.5),
        xaxis=dict(gridcolor=C_GRID),
        yaxis=dict(gridcolor=C_GRID),
        margin=dict(l=10, r=20, t=40, b=10),
        height=380,
    )
    return fig


def build_radar_fig(team: str) -> go.Figure:
    """Position-slot strength radar — raw SPTS per slot vs league average."""
    team_row = slot_spts.loc[team] if team in slot_spts.index else pd.Series(0, index=POS_SLOTS)

    # OF has 5 slots; divide by 5 to put it on a per-slot basis like every other position
    OF_DIV = {s: (5 if s == "OF" else 1) for s in POS_SLOTS}

    vals     = [team_row[s] / OF_DIV[s] for s in POS_SLOTS]
    avg_vals = [float(slot_league_avg[s]) / OF_DIV[s] for s in POS_SLOTS]

    # Radial axis ceiling: highest per-slot value in league, padded 10%
    raw_max  = max(float(slot_league_max[s]) / OF_DIV[s] for s in POS_SLOTS)
    radar_max = max(100, round(raw_max * 1.12 / 25) * 25)

    labels = POS_SLOTS
    hover_team = [
        f"{s}: {vals[i]:,.0f} SPTS" + (" (÷5)" if s == "OF" else "") +
        f"  |  lg avg: {avg_vals[i]:,.0f} SPTS"
        for i, s in enumerate(POS_SLOTS)
    ]
    hover_avg  = [
        f"{s} (lg avg): {avg_vals[i]:,.0f} SPTS" + (" (÷5)" if s == "OF" else "") +
        f"  |  this team: {vals[i]:,.0f} SPTS"
        for i, s in enumerate(POS_SLOTS)
    ]

    closed = labels + [labels[0]]
    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=avg_vals + [avg_vals[0]],
        theta=closed,
        name="League Avg",
        text=hover_avg + [hover_avg[0]],
        hoverinfo="text",
        fill="toself",
        fillcolor="rgba(148,163,184,0.1)",
        line=dict(color=C_MUTED, dash="dot", width=1),
    ))
    fig.add_trace(go.Scatterpolar(
        r=vals + [vals[0]],
        theta=closed,
        name=team,
        text=hover_team + [hover_team[0]],
        hoverinfo="text",
        fill="toself",
        fillcolor="rgba(59,130,246,0.25)",
        line=dict(color=C_HIT, width=2),
    ))
    fig.update_layout(
        polar=dict(
            bgcolor=C_CARD,
            radialaxis=dict(visible=True, range=[0, radar_max],
                            gridcolor=C_GRID, tickfont_color=C_MUTED),
            angularaxis=dict(gridcolor=C_GRID, tickfont_color=C_TEXT,
                             tickfont_size=13),
        ),
        paper_bgcolor=C_BG,
        font_color=C_TEXT,
        showlegend=True,
        legend=dict(bgcolor="rgba(0,0,0,0)", font_size=11),
        margin=dict(l=40, r=40, t=40, b=40),
        height=340,
        title=dict(text="Position Strength (SPTS per slot)",
                   font_size=13, x=0.5),
    )
    return fig


def build_pit_radar_fig(team: str) -> go.Figure:
    """Pitching radar — per-axis min-max (0–100) on SPTS-converted values.
    Scoring: IP×5, K×2, BB cost×3, HR cost×13, SV×5, HLD×4.
    BB/HR axes inverted before scaling so outward always = better.
    League avg ring = actual average position on each axis.
    """
    MULT_IP  = 5.0
    MULT_K   = 2.0
    MULT_BB  = 3.0
    MULT_HR  = 13.0
    MULT_SV  = 5.0
    MULT_HLD = 4.0

    row     = summary[summary["Team"] == team].iloc[0]
    avg_row = summary.mean(numeric_only=True)

    # Raw SPTS-unit series across all teams
    s_ip  = summary["Total_IP"] * MULT_IP
    s_k   = summary["Pit_K"]    * MULT_K
    s_bb  = summary["Pit_BB"]   * MULT_BB   # cost — lower is better → invert
    s_hr  = summary["Pit_HR"]   * MULT_HR   # cost — lower is better → invert
    s_sv  = summary["SV"]       * MULT_SV
    s_hld = summary["HLD"]      * MULT_HLD

    # Invert cost axes so higher = better on every axis
    s_bb_inv  = s_bb.max()  - s_bb
    s_hr_inv  = s_hr.max()  - s_hr

    def _mm(series, val):
        lo, hi = float(series.min()), float(series.max())
        rng = hi - lo if hi != lo else 1.0
        return (float(val) - lo) / rng * 100

    t_ip  = float(row["Total_IP"]) * MULT_IP
    t_k   = float(row["Pit_K"])    * MULT_K
    t_bb  = float(row["Pit_BB"])   * MULT_BB
    t_hr  = float(row["Pit_HR"])   * MULT_HR
    t_sv  = float(row["SV"])       * MULT_SV
    t_hld = float(row["HLD"])      * MULT_HLD

    a_ip  = float(avg_row["Total_IP"]) * MULT_IP
    a_k   = float(avg_row["Pit_K"])    * MULT_K
    a_bb  = float(avg_row["Pit_BB"])   * MULT_BB
    a_hr  = float(avg_row["Pit_HR"])   * MULT_HR
    a_sv  = float(avg_row["SV"])       * MULT_SV
    a_hld = float(avg_row["HLD"])      * MULT_HLD

    vals = [
        _mm(s_k,      t_k),
        _mm(s_ip,     t_ip),
        _mm(s_bb_inv, s_bb.max() - t_bb),
        _mm(s_hr_inv, s_hr.max() - t_hr),
        _mm(s_sv,     t_sv),
        _mm(s_hld,    t_hld),
    ]
    avg_vals = [
        _mm(s_k,      a_k),
        _mm(s_ip,     a_ip),
        _mm(s_bb_inv, s_bb.max() - a_bb),
        _mm(s_hr_inv, s_hr.max() - a_hr),
        _mm(s_sv,     a_sv),
        _mm(s_hld,    a_hld),
    ]
    labels = ["K Pts", "IP Pts", "BB Cost", "HR Cost", "SV Pts", "HLD Pts"]

    hover_team = [
        f"K Pts: {t_k:,.0f}  (lg avg {a_k:,.0f})",
        f"IP Pts: {t_ip:,.0f}  (lg avg {a_ip:,.0f})",
        f"BB Cost: {t_bb:,.0f} pts lost  (lg avg {a_bb:,.0f})",
        f"HR Cost: {t_hr:,.0f} pts lost  (lg avg {a_hr:,.0f})",
        f"SV Pts: {t_sv:,.0f}  (lg avg {a_sv:,.0f})",
        f"HLD Pts: {t_hld:,.0f}  (lg avg {a_hld:,.0f})",
    ]
    hover_avg = [
        f"K Pts (lg avg): {a_k:,.0f}  |  this team: {t_k:,.0f}",
        f"IP Pts (lg avg): {a_ip:,.0f}  |  this team: {t_ip:,.0f}",
        f"BB Cost (lg avg): {a_bb:,.0f} pts lost  |  this team: {t_bb:,.0f}",
        f"HR Cost (lg avg): {a_hr:,.0f} pts lost  |  this team: {t_hr:,.0f}",
        f"SV Pts (lg avg): {a_sv:,.0f}  |  this team: {t_sv:,.0f}",
        f"HLD Pts (lg avg): {a_hld:,.0f}  |  this team: {t_hld:,.0f}",
    ]

    closed = labels + [labels[0]]
    fig = go.Figure()
    fig.add_trace(go.Scatterpolar(
        r=avg_vals + [avg_vals[0]], theta=closed,
        name="League Avg",
        text=hover_avg + [hover_avg[0]], hoverinfo="text",
        fill="toself", fillcolor="rgba(148,163,184,0.1)",
        line=dict(color=C_MUTED, dash="dot", width=1),
    ))
    fig.add_trace(go.Scatterpolar(
        r=vals + [vals[0]], theta=closed,
        name=team,
        text=hover_team + [hover_team[0]], hoverinfo="text",
        fill="toself", fillcolor="rgba(16,185,129,0.2)",
        line=dict(color=C_SP, width=2),
    ))
    fig.update_layout(
        polar=dict(
            bgcolor=C_CARD,
            radialaxis=dict(visible=True, range=[0, 100],
                            gridcolor=C_GRID, tickfont_color=C_MUTED,
                            ticksuffix="%"),
            angularaxis=dict(gridcolor=C_GRID, tickfont_color=C_TEXT,
                             tickfont_size=12),
        ),
        paper_bgcolor=C_BG, font_color=C_TEXT,
        showlegend=True,
        legend=dict(bgcolor="rgba(0,0,0,0)", font_size=11),
        margin=dict(l=40, r=40, t=40, b=40),
        height=340,
        title=dict(text="Pitching SPTS by Component (0–100 per axis, BB/HR inverted)",
                   font_size=13, x=0.5),
    )
    return fig


def build_spts_donut(team: str) -> go.Figure:
    row = summary[summary["Team"] == team].iloc[0]
    fig = go.Figure(go.Pie(
        labels=["Hitting", "SP", "RP"],
        values=[row["Hit_SPTS"], row["SP_SPTS"], row["RP_SPTS"]],
        hole=0.55,
        marker_colors=[C_HIT, C_SP, C_RP],
        textinfo="label+percent",
        textfont_size=12,
        hovertemplate="%{label}: %{value:,.1f} SPTS<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor=C_BG,
        font_color=C_TEXT,
        showlegend=False,
        margin=dict(l=10, r=10, t=30, b=10),
        height=300,
        title=dict(text="SPTS Mix", font_size=13, x=0.5),
        annotations=[dict(
            text=f"<b>{row['Total_SPTS']:,.0f}</b><br>Total",
            font_size=15, showarrow=False,
            font_color=C_TEXT,
        )],
    )
    return fig


# ── Roster & Salary figures ────────────────────────────────────────────────

def build_salary_bar_fig() -> go.Figure:
    """Stacked horizontal salary bar — Hit / SP / RP per team, sorted by rank."""
    if not HAS_SALARY_DATA:
        fig = go.Figure()
        fig.update_layout(paper_bgcolor=C_BG, font_color=C_MUTED,
                          annotations=[dict(text="Re-run ottoneu_power_rankings.py to load salary data",
                                            showarrow=False, font_color=C_MUTED)])
        return fig
    df = summary.sort_values("Total_SPTS", ascending=True)
    fig = go.Figure()
    for col, label, color in [
        ("Sal_Hit", "Hitting",    C_HIT),
        ("Sal_SP",  "Starting P", C_SP),
        ("Sal_RP",  "Relief P",   C_RP),
    ]:
        fig.add_trace(go.Bar(
            y=df["Team"], x=df[col],
            name=label, orientation="h",
            marker_color=color,
            customdata=df["Total_Salary"],
            hovertemplate=(
                "<b>%{y}</b><br>"
                + label + ": $%{x:,.0f}<br>"
                + "Total: $%{customdata:,.0f}"
                + "<extra></extra>"
            ),
        ))
    fig.update_layout(
        barmode="stack",
        paper_bgcolor=C_BG, plot_bgcolor=C_BG, font_color=C_TEXT,
        margin=dict(l=160, r=20, t=40, b=70),
        height=420,
        xaxis=dict(gridcolor=C_GRID, tickprefix="$", title="Total Committed Salary"),
        yaxis=dict(gridcolor=C_GRID),
        legend=dict(orientation="h", yanchor="bottom", y=-0.2,
                    xanchor="center", x=0.5, bgcolor="rgba(0,0,0,0)"),
        title=dict(text="Salary Commitment by Role", font_size=13, x=0.5),
    )
    return fig


def build_salary_scatter_fig() -> go.Figure:
    """Total Salary vs Projected SPTS — one dot per team (value chart)."""
    if not HAS_SALARY_DATA:
        return go.Figure()
    df = summary.copy()
    df["SPTS_per_$"] = df.apply(
        lambda r: round(r["Total_SPTS"] / r["Total_Salary"], 2)
        if r.get("Total_Salary", 0) > 0 else 0, axis=1
    )
    fig = go.Figure()
    for _, row in df.iterrows():
        fig.add_trace(go.Scatter(
            x=[row["Total_Salary"]], y=[row["Total_SPTS"]],
            mode="markers+text",
            text=[row["Team"]], textposition="top center",
            textfont=dict(size=10, color=C_MUTED),
            marker=dict(size=12, color=rank_color(int(row["Rank"]), len(df)),
                        line=dict(width=1, color=C_GRID)),
            hovertemplate=(
                f"<b>{row['Team']}</b><br>"
                f"Salary: ${row['Total_Salary']:,.0f}<br>"
                f"SPTS: {row['Total_SPTS']:,.0f}<br>"
                f"Efficiency: {row['SPTS_per_$']:.2f} SPTS/$<extra></extra>"
            ),
            showlegend=False,
        ))
    fig.update_layout(
        paper_bgcolor=C_BG, plot_bgcolor=C_BG, font_color=C_TEXT,
        margin=dict(l=50, r=30, t=40, b=50),
        height=420,
        xaxis=dict(gridcolor=C_GRID, tickprefix="$", title="Total Committed Salary"),
        yaxis=dict(gridcolor=C_GRID, title="Projected SPTS"),
        title=dict(text="Salary vs Projected SPTS (Value)", font_size=13, x=0.5),
    )
    return fig


def build_age_bar_fig() -> go.Figure:
    """Average roster age per team, sorted by age."""
    if not HAS_SALARY_DATA:
        return go.Figure()
    df = summary.sort_values("Avg_Age", ascending=True)
    league_avg = float(summary["Avg_Age"].mean())
    colors = [pct_color("Avg_Age", t) for t in df["Team"]]
    fig = go.Figure(go.Bar(
        x=df["Team"], y=df["Avg_Age"],
        marker_color=colors,
        hovertemplate="%{x}<br>Avg Age: %{y:.1f}<extra></extra>",
    ))
    fig.add_hline(y=league_avg, line_dash="dot", line_color=C_MUTED,
                  annotation_text=f"Lg avg {league_avg:.1f}",
                  annotation_font_color=C_MUTED)
    fig.update_layout(
        paper_bgcolor=C_BG, plot_bgcolor=C_BG, font_color=C_TEXT,
        margin=dict(l=40, r=20, t=40, b=100),
        height=320,
        xaxis=dict(gridcolor=C_GRID, tickangle=-35),
        yaxis=dict(gridcolor=C_GRID, title="Avg Age", range=[
            max(0, df["Avg_Age"].min() - 1), df["Avg_Age"].max() + 1]),
        showlegend=False,
        title=dict(text="Average Roster Age (younger = greener)", font_size=13, x=0.5),
    )
    return fig


def build_roster_pos_bar(team: str) -> go.Figure:
    """Average salary by position/role for a single team."""
    if roster_data.empty:
        return go.Figure()
    df = roster_data[roster_data["Team"] == team].copy()
    if df.empty:
        return go.Figure()
    agg = (
        df.groupby("Pos")["Salary"]
        .agg(avg="mean", count="count")
        .reset_index()
        .sort_values("avg", ascending=True)
    )
    color_map = {
        "C": C_HIT, "1B": C_HIT, "2B": C_HIT, "SS": C_HIT,
        "3B": C_HIT, "OF": C_HIT, "Util": C_HIT, "TWP": C_HIT,
        "SP": C_SP,  "RP": C_RP,
    }
    colors = [color_map.get(p, C_MUTED) for p in agg["Pos"]]
    fig = go.Figure(go.Bar(
        x=agg["avg"], y=agg["Pos"], orientation="h",
        marker_color=colors,
        customdata=agg["count"],
        hovertemplate="<b>%{y}</b><br>Avg Salary: $%{x:,.1f}<br>Players: %{customdata}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor=C_BG, plot_bgcolor=C_BG, font_color=C_TEXT,
        margin=dict(l=60, r=20, t=40, b=40),
        height=300,
        xaxis=dict(gridcolor=C_GRID, tickprefix="$", title="Avg Salary per Player"),
        yaxis=dict(gridcolor=C_GRID),
        showlegend=False,
        title=dict(text="Avg Salary by Position / Role", font_size=13, x=0.5),
    )
    return fig


def _photo_md(mid) -> str:
    """Return an HTML img tag for an MLB headshot given an MLBAMID."""
    try:
        return (
            f'<img src="https://img.mlbstatic.com/mlb-photos/image/upload/'
            f'w_40,q_100/v1/people/{int(mid)}/headshot/67/current" '
            f'style="border-radius:6px;width:40px;height:auto;">'
        )
    except (ValueError, TypeError):
        return ""


def _photo_col_def() -> dict:
    """Column definition for the Photo column."""
    return {"name": "", "id": "Photo", "presentation": "markdown"}


_PHOTO_CELL_STYLE = [
    {"if": {"column_id": "Photo"}, "width": "52px", "minWidth": "52px",
     "maxWidth": "52px", "padding": "0px 4px", "textAlign": "center"},
    {"if": {"column_id": "Name"}, "width": "260px", "minWidth": "200px",
     "maxWidth": "300px", "textAlign": "center"},
    {"if": {"column_id": "Slot"}, "width": "55px", "minWidth": "55px",
     "maxWidth": "55px", "textAlign": "center"},
]

# Raw CSS injected into photo tables to kill the inner div padding that
# the Python style props cannot reach
_PHOTO_CSS = [
    {"selector": "td.dash-cell", "rule": "padding-top: 0 !important; padding-bottom: 0 !important; line-height: 1;"},
    {"selector": "td.dash-cell div.dash-cell-value", "rule": "padding: 0 !important; line-height: 1;"},
    {"selector": "td.dash-cell img", "rule": "display: block; margin: 0 auto;"},
]


def roster_table(team: str) -> dash_table.DataTable:
    df = roster_data[roster_data["Team"] == team].copy()
    df = df.sort_values("Salary", ascending=False)
    df["SPTS/$"] = df.apply(
        lambda r: round(float(r["SPTS"]) / float(r["Salary"]), 1)
        if r.get("Salary", 0) > 0 else 0.0, axis=1
    )
    # Build headshot column from MLBAMID
    if "MLBAMID" in df.columns:
        df["Photo"] = df["MLBAMID"].apply(_photo_md)
    else:
        df["Photo"] = ""
    cols = ["Photo", "Name", "Role", "Pos", "Age", "Salary", "SPTS", "SPTS/$"]
    df = df[[c for c in cols if c in df.columns]].fillna({"Age": "-"})
    column_defs = []
    for c in [c for c in cols if c in df.columns]:
        if c == "Photo":
            column_defs.append({"name": "", "id": c, "presentation": "markdown"})
        elif c in ("SPTS", "SPTS/$"):
            column_defs.append({"name": c, "id": c, "type": "numeric", "format": {"specifier": ",.1f"}})
        elif c in ("Salary", "Age"):
            column_defs.append({"name": c, "id": c, "type": "numeric", "format": {"specifier": ",.0f"}})
        else:
            column_defs.append({"name": c, "id": c})
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=column_defs,
        sort_action="native",
        style_cell={"verticalAlign": "middle", "textAlign": "center", "padding": "0px 6px"},
        style_cell_conditional=_PHOTO_CELL_STYLE,
        markdown_options={"html": True},
        css=_PHOTO_CSS,
        **{**_TABLE_STYLE,
           "style_data": {**_TABLE_STYLE["style_data"], "fontSize": "24px", "padding": "0px 6px"},
           "page_size": 30},
    )


_TABLE_STYLE = dict(
    style_table={"overflowX": "auto", "borderRadius": "8px"},
    style_header={
        "backgroundColor": "#0F172A",
        "color": C_MUTED,
        "fontWeight": "600",
        "fontSize": "11px",
        "textTransform": "uppercase",
        "letterSpacing": "0.05em",
        "border": "none",
    },
    style_data={
        "backgroundColor": C_CARD,
        "color": C_TEXT,
        "fontSize": "13px",
        "border": f"1px solid {C_GRID}",
    },
    style_data_conditional=[
        {"if": {"row_index": "odd"},
         "backgroundColor": "#263248"},
    ],
    page_size=20,
)


def hitter_table(team: str) -> dash_table.DataTable:
    df = hitters[hitters["Team"] == team].copy()
    df["Slot_order"] = df["Slot"].map(
        {s: i for i, s in enumerate(SLOT_ORDER)}).fillna(99)
    df = df.sort_values(["Slot_order", "SPTS/G"], ascending=[True, False])
    if "MLBAMID" in df.columns:
        df["Photo"] = df["MLBAMID"].apply(_photo_md)
    else:
        df["Photo"] = ""
    stat_cols = ["Slot", "Name", "Pos", "G_proj", "G_used", "SPTS/G", "SPTS"]
    df = df[[c for c in ["Photo"] + stat_cols if c in df.columns]].round(
        {"SPTS/G": 2, "SPTS": 1, "G_used": 1}
    )
    cols = [c for c in ["Photo"] + stat_cols if c in df.columns]
    column_defs = [_photo_col_def() if c == "Photo"
                   else {"name": c, "id": c, "type": "numeric",
                         "format": {"specifier": ",.1f"}} if c in ("SPTS/G", "SPTS", "G_used")
                   else {"name": c, "id": c}
                   for c in cols]
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=column_defs,
        sort_action="native",
        style_cell={"verticalAlign": "middle", "textAlign": "center", "padding": "0px 6px"},
        style_cell_conditional=_PHOTO_CELL_STYLE,
        markdown_options={"html": True},
        css=_PHOTO_CSS,
        **{**_TABLE_STYLE, "style_data": {**_TABLE_STYLE["style_data"], "fontSize": "24px", "padding": "0px 6px"}},
    )


def sp_table(team: str) -> dash_table.DataTable:
    df = sp_data[sp_data["Team"] == team].copy()
    df = df.sort_values("SPTS_used", ascending=False)
    if "MLBAMID" in df.columns:
        df["Photo"] = df["MLBAMID"].apply(_photo_md)
    else:
        df["Photo"] = ""
    stat_cols = ["Name", "Proj_Starts", "Used_Starts", "IP_per_GS", "IP_used",
                 "SPTS_proj", "SPTS_used"]
    cols = [c for c in ["Photo"] + stat_cols if c in df.columns]
    df = df[cols].round({c: 1 for c in stat_cols if c != "Name"})
    column_defs = [_photo_col_def() if c == "Photo"
                   else {"name": c, "id": c, "type": "numeric",
                         "format": {"specifier": ",.1f"}} if c != "Name"
                   else {"name": c, "id": c}
                   for c in cols]
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=column_defs,
        sort_action="native",
        style_cell={"verticalAlign": "middle", "textAlign": "center", "padding": "0px 6px"},
        style_cell_conditional=_PHOTO_CELL_STYLE,
        markdown_options={"html": True},
        css=_PHOTO_CSS,
        **{**_TABLE_STYLE, "style_data": {**_TABLE_STYLE["style_data"], "fontSize": "24px", "padding": "0px 6px"}},
    )


def rp_table(team: str) -> dash_table.DataTable:
    df = rp_data[rp_data["Team"] == team].copy()
    df = df.sort_values("SPTS_used", ascending=False)
    if "MLBAMID" in df.columns:
        df["Photo"] = df["MLBAMID"].apply(_photo_md)
    else:
        df["Photo"] = ""
    stat_cols = ["Name", "IP_used", "SPTS_proj", "SPTS_used",
                 "SV_proj", "SV_used", "HLD_proj", "HLD_used"]
    cols = [c for c in ["Photo"] + stat_cols if c in df.columns]
    df = df[cols].round({c: 1 for c in stat_cols if c != "Name"})
    column_defs = [_photo_col_def() if c == "Photo"
                   else {"name": c, "id": c, "type": "numeric",
                         "format": {"specifier": ",.1f"}} if c != "Name"
                   else {"name": c, "id": c}
                   for c in cols]
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=column_defs,
        sort_action="native",
        style_cell={"verticalAlign": "middle", "textAlign": "center", "padding": "0px 6px"},
        style_cell_conditional=_PHOTO_CELL_STYLE,
        markdown_options={"html": True},
        css=_PHOTO_CSS,
        **{**_TABLE_STYLE, "style_data": {**_TABLE_STYLE["style_data"], "fontSize": "24px", "padding": "0px 6px"}},
    )


# ── Comparison helper ────────────────────────────────────────────────────────
def _team_comparison_col(team: str) -> html.Div:
    """Tiles + both radar charts for one team — used in the comparison tab."""
    row   = summary[summary["Team"] == team].iloc[0]
    rank  = int(row["Rank"])
    n     = len(summary)
    r_col = rank_color(rank, n)
    ba    = row["Hit_H"] / row["Hit_AB"] if row["Hit_AB"] > 0 else 0

    tiles = html.Div([
        tile_group("Overall", [
            stat_tile("Rank",       f"#{rank}",                   f"of {n} teams",      r_col),
            stat_tile("Total SPTS", f"{row['Total_SPTS']:,.0f}",  "projected",          pct_color("Total_SPTS", team)),
            stat_tile("Hitting",    f"{row['Hit_SPTS']:,.0f}",    "SPTS",               pct_color("Hit_SPTS",   team)),
            stat_tile("Starting P", f"{row['SP_SPTS']:,.0f}",     "SPTS",               pct_color("SP_SPTS",    team)),
            stat_tile("Relief P",   f"{row['RP_SPTS']:,.0f}",     "SPTS",               pct_color("RP_SPTS",    team)),
        ]),
        tile_group("Batting", [
            stat_tile("Avg",        f"{ba:.3f}",                  "H/AB (capped)",      pct_color("BA",         team)),
            stat_tile("BB (bat)",   f"{row.get('Hit_BB',   0):,.0f}", "walks",          pct_color("Hit_BB",     team)),
            stat_tile("2B",         f"{row.get('Hit_2B',   0):,.0f}", "doubles",        pct_color("Hit_2B",     team)),
            stat_tile("HR",         f"{row.get('Hit_HR',   0):,.0f}", "hitter HR",      pct_color("Hit_HR",     team)),
            stat_tile("SB",         f"{row.get('Hit_SB',   0):,.0f}", "stolen bases",   pct_color("Hit_SB",     team)),
            stat_tile("OPS",        f"{row.get('Hit_OPS',  0):.3f}", "on-base + slug",  pct_color("Hit_OPS",    team)),
            stat_tile("wOBA",       f"{row.get('Hit_wOBA', 0):.3f}", "weighted on-base",pct_color("Hit_wOBA",   team)),
        ]),
        tile_group("Pitching", [
            stat_tile("Total IP",              f"{row['Total_IP']:,.0f}",    "capped",             pct_color("Total_IP",   team)),
            stat_tile("Pitcher Strikeouts",    f"{row.get('Pit_K',   0):,.0f}", "strikeouts",      pct_color("Pit_K",      team)),
            stat_tile("Pitcher Walks Allowed", f"{row.get('Pit_BB',  0):,.0f}", "walks allowed",   pct_color("Pit_BB",     team)),
            stat_tile("Pitcher HR Allowed",    f"{row.get('Pit_HR',  0):,.0f}", "HR allowed",      pct_color("Pit_HR",     team)),
            stat_tile("FIP",                   f"{row.get('Pit_FIP', 0):.2f}",  "IP-weighted avg", pct_color("Pit_FIP",    team)),
            stat_tile("SV / HLD",              f"{row['SV']:.0f} / {row['HLD']:.0f}", "capped",   pct_color("SV",         team)),
        ]),
    ], style={"marginBottom": "16px"})

    radars = html.Div([
        html.Div(dcc.Graph(figure=build_radar_fig(team),
                           config={"displayModeBar": False}),
                 style={"background": C_CARD, "borderRadius": "12px",
                        "padding": "12px", "marginBottom": "12px"}),
        html.Div(dcc.Graph(figure=build_pit_radar_fig(team),
                           config={"displayModeBar": False}),
                 style={"background": C_CARD, "borderRadius": "12px",
                        "padding": "12px"}),
    ])

    return html.Div([
        html.H2(team, style={"margin": "0 0 14px", "fontSize": "18px",
                             "fontWeight": "700", "color": C_TEXT}),
        tiles, radars,
    ], style={"flex": "1", "minWidth": "0"})


_TAB_STYLE = {
    "backgroundColor": C_CARD, "color": C_MUTED,
    "borderColor": C_GRID, "fontWeight": "600", "padding": "10px 20px",
}
_TAB_SEL = {
    "backgroundColor": C_BG, "color": C_TEXT,
    "borderTop": f"2px solid {C_SP}", "fontWeight": "700", "padding": "10px 20px",
}


# ── Layout ────────────────────────────────────────────────────────────────────
app = Dash(__name__, title="Ottoneu Power Rankings",
          meta_tags=[{"name": "viewport", "content": "width=device-width, initial-scale=1"}])

app.layout = html.Div(className="page-wrap", style={
    "background": C_BG, "minHeight": "100vh",
    "fontFamily": "'Inter', 'Segoe UI', sans-serif",
    "color": C_TEXT, "padding": "24px",
}, children=[

    # ── Header
    html.Div([
        html.H1("⚾ Ottoneu 2026 Power Rankings",
                style={"margin": "0", "fontSize": "24px", "fontWeight": "800"}),
        html.P("Lineup-constrained projected SPTS  ·  ZiPS",
               style={"margin": "4px 0 0", "color": C_MUTED, "fontSize": "13px"}),
    ], style={"marginBottom": "24px"}),

    dcc.Tabs(id="main-tabs", value="rankings",
             style={"marginBottom": "24px"},
             colors={"border": C_GRID, "primary": C_SP, "background": C_CARD},
             children=[

        # ── Tab 1: Power Rankings ─────────────────────────────────────────
        dcc.Tab(label="Power Rankings", value="rankings",
                style=_TAB_STYLE, selected_style=_TAB_SEL,
                children=[

            # Overview charts
            html.Div([
                html.Div(dcc.Graph(id="overview-bar",
                                   figure=build_overview_fig(summary),
                                   config={"displayModeBar": False}),
                         style={"flex": "2", "background": C_CARD,
                                "borderRadius": "12px", "padding": "12px"}),
                html.Div(dcc.Graph(id="scatter-fig",
                                   figure=build_scatter_fig(summary),
                                   config={"displayModeBar": False}),
                         style={"flex": "1", "background": C_CARD,
                                "borderRadius": "12px", "padding": "12px"}),
            ], className="flex-row", style={"display": "flex", "gap": "16px", "marginBottom": "28px",
                      "alignItems": "flex-start"}),

            # Team selector
            html.Div([
                html.Label("Select a team for the Report Card  ↓",
                           style={"color": C_MUTED, "fontSize": "12px",
                                  "marginBottom": "6px", "display": "block"}),
                dcc.Dropdown(
                    id="team-dropdown",
                    options=[{"label": t, "value": t} for t in teams],
                    value=teams[0],
                    clearable=False,
                    style={"width": "300px", "maxWidth": "100%", "color": "#0F172A"},
                ),
            ], className="dropdown-wrap", style={"marginBottom": "20px"}),

            # Report card
            html.Div(id="report-card"),
        ]),

        # ── Tab 2: Compare Teams ──────────────────────────────────────────
        dcc.Tab(label="Compare Teams", value="compare",
                style=_TAB_STYLE, selected_style=_TAB_SEL,
                children=[

            html.Div([
                html.Div([
                    html.Label("Team A",
                               style={"color": C_MUTED, "fontSize": "12px",
                                      "marginBottom": "6px", "display": "block"}),
                    dcc.Dropdown(
                        id="compare-a",
                        options=[{"label": t, "value": t} for t in teams],
                        value=teams[0],
                        clearable=False,
                        style={"color": "#0F172A"},
                    ),
                ], style={"flex": "1"}),
                html.Div([
                    html.Label("Team B",
                               style={"color": C_MUTED, "fontSize": "12px",
                                      "marginBottom": "6px", "display": "block"}),
                    dcc.Dropdown(
                        id="compare-b",
                        options=[{"label": t, "value": t} for t in teams],
                        value=teams[1] if len(teams) > 1 else teams[0],
                        clearable=False,
                        style={"color": "#0F172A"},
                    ),
                ], style={"flex": "1"}),
            ], className="flex-row", style={"display": "flex", "gap": "24px",
                      "marginBottom": "24px"}),

            html.Div(id="compare-output"),
        ]),

        # ── Tab 3: Roster & Salary ────────────────────────────────────────
        dcc.Tab(label="Roster & Salary", value="roster",
                style=_TAB_STYLE, selected_style=_TAB_SEL,
                children=[

            # League-wide salary & value charts
            html.Div([
                html.Div(dcc.Graph(figure=build_salary_bar_fig(),
                                   config={"displayModeBar": False}),
                         style={"flex": "1", "background": C_CARD,
                                "borderRadius": "12px", "padding": "12px"}),
                html.Div(dcc.Graph(figure=build_salary_scatter_fig(),
                                   config={"displayModeBar": False}),
                         style={"flex": "1", "background": C_CARD,
                                "borderRadius": "12px", "padding": "12px"}),
            ], className="flex-row", style={"display": "flex", "gap": "16px", "marginBottom": "24px",
                      "alignItems": "flex-start"}),

            # Age bar (league-wide)
            html.Div(
                dcc.Graph(figure=build_age_bar_fig(),
                          config={"displayModeBar": False}),
                style={"background": C_CARD, "borderRadius": "12px",
                       "padding": "12px", "marginBottom": "28px"},
            ),

            # Team deep-dive selector
            html.Div([
                html.Label("Select a team for roster breakdown  \u2193",
                           style={"color": C_MUTED, "fontSize": "12px",
                                  "marginBottom": "6px", "display": "block"}),
                dcc.Dropdown(
                    id="roster-team-dropdown",
                    options=[{"label": t, "value": t} for t in teams],
                    value=teams[0],
                    clearable=False,
                    style={"width": "300px", "maxWidth": "100%", "color": "#0F172A"},
                ),
            ], className="dropdown-wrap", style={"marginBottom": "20px"}),

            html.Div(id="roster-team-output"),
        ]),
    ]),
])


# ── Callbacks ─────────────────────────────────────────────────────────────────

@app.callback(Output("report-card", "children"), Input("team-dropdown", "value"))
def render_report_card(team: str):
    row   = summary[summary["Team"] == team].iloc[0]
    rank  = int(row["Rank"])
    n     = len(summary)
    r_col = rank_color(rank, n)
    ba    = row["Hit_H"] / row["Hit_AB"] if row["Hit_AB"] > 0 else 0

    tiles = html.Div([
        tile_group("Overall", [
            stat_tile("Rank",       f"#{rank}",                   f"of {n} teams",      r_col),
            stat_tile("Total SPTS", f"{row['Total_SPTS']:,.0f}",  "projected",          pct_color("Total_SPTS", team)),
            stat_tile("Hitting",    f"{row['Hit_SPTS']:,.0f}",    "SPTS",               pct_color("Hit_SPTS",   team)),
            stat_tile("Starting P", f"{row['SP_SPTS']:,.0f}",     "SPTS",               pct_color("SP_SPTS",    team)),
            stat_tile("Relief P",   f"{row['RP_SPTS']:,.0f}",     "SPTS",               pct_color("RP_SPTS",    team)),
        ]),
        tile_group("Batting", [
            stat_tile("Avg",        f"{ba:.3f}",                  "H/AB (capped)",      pct_color("BA",         team)),
            stat_tile("BB (bat)",   f"{row.get('Hit_BB',   0):,.0f}", "walks",          pct_color("Hit_BB",     team)),
            stat_tile("2B",         f"{row.get('Hit_2B',   0):,.0f}", "doubles",        pct_color("Hit_2B",     team)),
            stat_tile("HR",         f"{row.get('Hit_HR',   0):,.0f}", "hitter HR",      pct_color("Hit_HR",     team)),
            stat_tile("SB",         f"{row.get('Hit_SB',   0):,.0f}", "stolen bases",   pct_color("Hit_SB",     team)),
            stat_tile("OPS",        f"{row.get('Hit_OPS',  0):.3f}", "on-base + slug",  pct_color("Hit_OPS",    team)),
            stat_tile("wOBA",       f"{row.get('Hit_wOBA', 0):.3f}", "weighted on-base",pct_color("Hit_wOBA",   team)),
        ]),
        tile_group("Pitching", [
            stat_tile("Total IP",              f"{row['Total_IP']:,.0f}",    "capped",             pct_color("Total_IP",   team)),
            stat_tile("Pitcher Strikeouts",    f"{row.get('Pit_K',   0):,.0f}", "strikeouts",      pct_color("Pit_K",      team)),
            stat_tile("Pitcher Walks Allowed", f"{row.get('Pit_BB',  0):,.0f}", "walks allowed",   pct_color("Pit_BB",     team)),
            stat_tile("Pitcher HR Allowed",    f"{row.get('Pit_HR',  0):,.0f}", "HR allowed",      pct_color("Pit_HR",     team)),
            stat_tile("FIP",                   f"{row.get('Pit_FIP', 0):.2f}",  "IP-weighted avg", pct_color("Pit_FIP",    team)),
            stat_tile("SV / HLD",              f"{row['SV']:.0f} / {row['HLD']:.0f}", "capped",   pct_color("SV",         team)),
        ]),
    ], style={"marginBottom": "20px"})

    charts = html.Div([
        html.Div(dcc.Graph(figure=build_radar_fig(team),
                           config={"displayModeBar": False}),
                 style={"flex": "1", "background": C_CARD,
                        "borderRadius": "12px", "padding": "12px"}),
        html.Div(dcc.Graph(figure=build_pit_radar_fig(team),
                           config={"displayModeBar": False}),
                 style={"flex": "1", "background": C_CARD,
                        "borderRadius": "12px", "padding": "12px"}),
        html.Div(dcc.Graph(figure=build_spts_donut(team),
                           config={"displayModeBar": False}),
                 style={"flex": "1", "background": C_CARD,
                        "borderRadius": "12px", "padding": "12px"}),
    ], className="flex-row", style={"display": "flex", "gap": "16px", "marginBottom": "20px"})

    def section(title, content):
        return html.Div([
            html.H3(title, style={"margin": "0 0 10px",
                                   "fontSize": "14px", "fontWeight": "600",
                                   "color": C_MUTED, "textTransform": "uppercase",
                                   "letterSpacing": "0.07em"}),
            content,
        ], style={"background": C_CARD, "borderRadius": "12px",
                  "padding": "16px", "marginBottom": "16px"})

    tables = html.Div([
        section(f"{team} — Lineup", hitter_table(team)),
        section(f"{team} — Starting Pitchers", sp_table(team)),
        section(f"{team} — Relief Pitchers", rp_table(team)),
    ])

    return html.Div([
        html.H2(team, style={"margin": "0 0 16px", "fontSize": "20px",
                              "fontWeight": "700"}),
        tiles, charts, tables,
    ])


# ── Click bar chart → set dropdown ───────────────────────────────────────────
@app.callback(
    Output("team-dropdown", "value"),
    Input("overview-bar", "clickData"),
    prevent_initial_call=True,
)
def sync_dropdown_from_bar(click_data):
    if click_data and click_data.get("points"):
        return click_data["points"][0]["y"]
    return teams[0]


# ── Compare Teams callback ────────────────────────────────────────────────────
@app.callback(
    Output("compare-output", "children"),
    Input("compare-a", "value"),
    Input("compare-b", "value"),
)
def render_comparison(team_a: str, team_b: str):
    return html.Div([
        _team_comparison_col(team_a),
        html.Div(className="compare-divider", style={
            "width": "1px", "background": C_GRID,
            "alignSelf": "stretch", "margin": "0 8px",
        }),
        _team_comparison_col(team_b),
    ], className="flex-row", style={"display": "flex", "gap": "16px", "alignItems": "flex-start"})


# ── Roster & Salary team deep-dive ───────────────────────────────────────────
@app.callback(
    Output("roster-team-output", "children"),
    Input("roster-team-dropdown", "value"),
)
def render_roster_team(team: str):
    if not team:
        return html.Div()
    row = summary[summary["Team"] == team].iloc[0]

    total_sal   = float(row.get("Total_Salary", 0))
    sal_hit     = float(row.get("Sal_Hit", 0))
    sal_sp      = float(row.get("Sal_SP",  0))
    sal_rp      = float(row.get("Sal_RP",  0))
    avg_age     = float(row.get("Avg_Age", 0))
    n_players   = int(row.get("N_Bat", 0)) + int(row.get("N_Pit", 0))
    spts_per_sal = row["Total_SPTS"] / total_sal if total_sal > 0 else 0.0

    if HAS_SALARY_DATA:
        tiles = html.Div([
            tile_group("Roster Overview", [
                stat_tile("Total Salary",  f"${total_sal:,.0f}",   f"{n_players} players"),
                stat_tile("Avg Age",       f"{avg_age:.1f}",        "years",
                          pct_color("Avg_Age", team)),
                stat_tile("SPTS / $1",     f"{spts_per_sal:.2f}",  "efficiency"),
                stat_tile("Hitter $",      f"${sal_hit:,.0f}",     "batters",   C_HIT),
                stat_tile("SP $",          f"${sal_sp:,.0f}",      "starters",  C_SP),
                stat_tile("RP $",          f"${sal_rp:,.0f}",      "relievers", C_RP),
            ]),
        ], style={"marginBottom": "16px"})
    else:
        tiles = html.Div(
            html.P("Salary data not available — re-run ottoneu_power_rankings.py.",
                   style={"color": C_MUTED, "fontSize": "13px"}),
            style={"marginBottom": "16px"}
        )

    charts = html.Div([
        html.Div(dcc.Graph(figure=build_roster_pos_bar(team),
                           config={"displayModeBar": False}),
                 style={"flex": "1", "background": C_CARD,
                        "borderRadius": "12px", "padding": "12px"}),
    ], style={"display": "flex", "gap": "16px", "marginBottom": "16px"})

    table_section = html.Div([
        html.H3("Full Roster", style={
            "margin": "0 0 10px", "fontSize": "14px", "fontWeight": "600",
            "color": C_MUTED, "textTransform": "uppercase",
            "letterSpacing": "0.07em",
        }),
        roster_table(team) if not roster_data.empty else
        html.P("No roster data available.", style={"color": C_MUTED}),
    ], style={"background": C_CARD, "borderRadius": "12px", "padding": "16px"})

    return html.Div([
        html.H2(team, style={"margin": "0 0 16px", "fontSize": "20px",
                             "fontWeight": "700"}),
        tiles, charts, table_section,
    ])


server = app.server  # exposed for gunicorn: `gunicorn ottoneu_dashboard:server`

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    print(f"Dashboard running → http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
