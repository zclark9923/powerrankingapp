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

summary  = summary.sort_values("Total_SPTS", ascending=False).reset_index(drop=True)
summary["Rank"] = summary.index + 1

teams = summary["Team"].tolist()

# League averages (for radar normalisation)
RADAR_COLS = ["Hit_SPTS", "SP_SPTS", "RP_SPTS", "SV", "HLD", "Total_IP"]
league_avg = summary[RADAR_COLS].mean()
league_max = summary[RADAR_COLS].max()


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
        html.P(value, style={"margin": "2px 0", "fontSize": "26px",
                              "fontWeight": "700", "color": color}),
        html.P(sub,   style={"margin": "0", "fontSize": "11px", "color": C_MUTED}),
    ], style={
        "background": C_CARD,
        "borderRadius": "10px",
        "padding": "14px 18px",
        "minWidth": "120px",
        "flex": "1",
    })


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
        legend=dict(orientation="h", y=1.04, x=0,
                    bgcolor="rgba(0,0,0,0)", font_size=12),
        yaxis=dict(autorange="reversed", gridcolor=C_GRID,
                   tickfont=dict(size=12)),
        xaxis=dict(gridcolor=C_GRID, title="Total SPTS"),
        margin=dict(l=10, r=20, t=40, b=10),
        height=max(380, len(df) * 28),
        title=dict(text="Power Rankings — Projected SPTS by Component",
                   font_size=14, x=0.5),
    )
    return fig


def build_scatter_fig(df: pd.DataFrame) -> go.Figure:
    fig = px.scatter(
        df, x="Hit_SPTS", y="Pit_SPTS",
        text="Team", color="Total_SPTS",
        color_continuous_scale=["#EF4444", "#F59E0B", "#22C55E"],
        hover_data={"Total_SPTS": ":.1f", "Hit_SPTS": ":.1f", "Pit_SPTS": ":.1f"},
        labels={"Hit_SPTS": "Hitting SPTS", "Pit_SPTS": "Pitching SPTS"},
    )
    fig.update_traces(
        textposition="top center",
        marker=dict(size=10, line=dict(width=1, color="white")),
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
    row = summary[summary["Team"] == team].iloc[0]
    # Normalise each value as % of league max (0–1 scale)
    vals = [(row[c] / league_max[c] * 100) if league_max[c] > 0 else 0
            for c in RADAR_COLS]
    labels = ["Hitting", "SP", "RP", "Saves", "Holds", "Innings"]
    # Close the polygon
    vals_closed   = vals + [vals[0]]
    labels_closed = labels + [labels[0]]

    fig = go.Figure()
    # League average reference
    avg_vals = [(league_avg[c] / league_max[c] * 100) if league_max[c] > 0 else 0
                for c in RADAR_COLS]
    fig.add_trace(go.Scatterpolar(
        r=avg_vals + [avg_vals[0]],
        theta=labels_closed,
        name="League Avg",
        fill="toself",
        fillcolor="rgba(148,163,184,0.1)",
        line=dict(color=C_MUTED, dash="dot", width=1),
    ))
    fig.add_trace(go.Scatterpolar(
        r=vals_closed,
        theta=labels_closed,
        name=team,
        fill="toself",
        fillcolor="rgba(59,130,246,0.25)",
        line=dict(color=C_HIT, width=2),
    ))
    fig.update_layout(
        polar=dict(
            bgcolor=C_CARD,
            radialaxis=dict(visible=True, range=[0, 100],
                            gridcolor=C_GRID, tickfont_color=C_MUTED,
                            ticksuffix="%"),
            angularaxis=dict(gridcolor=C_GRID, tickfont_color=C_TEXT),
        ),
        paper_bgcolor=C_BG,
        font_color=C_TEXT,
        showlegend=True,
        legend=dict(bgcolor="rgba(0,0,0,0)", font_size=11),
        margin=dict(l=40, r=40, t=40, b=40),
        height=340,
        title=dict(text="Strength Profile (% of league best)",
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
    cols = ["Slot", "Name", "Pos", "G_proj", "G_used", "SPTS/G", "SPTS"]
    df = df[cols].round({"SPTS/G": 2, "SPTS": 1, "G_used": 1})
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c, "type": "numeric",
                  "format": {"specifier": ",.1f"}}
                 if c in ("SPTS/G", "SPTS", "G_used") else {"name": c, "id": c}
                 for c in cols],
        sort_action="native",
        **_TABLE_STYLE,
    )


def sp_table(team: str) -> dash_table.DataTable:
    df = sp_data[sp_data["Team"] == team].copy()
    df = df.sort_values("SPTS_used", ascending=False)
    cols = ["Name", "Proj_Starts", "Used_Starts", "IP_per_GS", "IP_used",
            "SPTS_proj", "SPTS_used"]
    df = df[cols].round(2)
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c, "type": "numeric",
                  "format": {"specifier": ",.1f"}}
                 if c != "Name" else {"name": c, "id": c}
                 for c in cols],
        sort_action="native",
        **_TABLE_STYLE,
    )


def rp_table(team: str) -> dash_table.DataTable:
    df = rp_data[rp_data["Team"] == team].copy()
    df = df.sort_values("SPTS_used", ascending=False)
    cols = ["Name", "IP_used", "SPTS_proj", "SPTS_used",
            "SV_proj", "SV_used", "HLD_proj", "HLD_used"]
    df = df[cols].round(1)
    return dash_table.DataTable(
        data=df.to_dict("records"),
        columns=[{"name": c, "id": c, "type": "numeric",
                  "format": {"specifier": ",.1f"}}
                 if c != "Name" else {"name": c, "id": c}
                 for c in cols],
        sort_action="native",
        **_TABLE_STYLE,
    )


# ── Layout ────────────────────────────────────────────────────────────────────
app = Dash(__name__, title="Ottoneu Power Rankings")

app.layout = html.Div(style={
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
    ], style={"marginBottom": "28px"}),

    # ── League overview charts
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
    ], style={"display": "flex", "gap": "16px", "marginBottom": "28px",
              "alignItems": "flex-start"}),

    # ── Team selector
    html.Div([
        html.Label("Select a team for the Report Card  ↓",
                   style={"color": C_MUTED, "fontSize": "12px",
                          "marginBottom": "6px", "display": "block"}),
        dcc.Dropdown(
            id="team-dropdown",
            options=[{"label": t, "value": t} for t in teams],
            value=teams[0],
            clearable=False,
            style={"width": "300px", "color": "#0F172A"},
        ),
    ], style={"marginBottom": "20px"}),

    # ── Report card
    html.Div(id="report-card"),
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
        stat_tile("Rank",        f"#{rank}",              f"of {n} teams", r_col),
        stat_tile("Total SPTS",  f"{row['Total_SPTS']:,.0f}", "projected"),
        stat_tile("Hitting",     f"{row['Hit_SPTS']:,.0f}",   "SPTS"),
        stat_tile("Starting P",  f"{row['SP_SPTS']:,.0f}",   "SPTS"),
        stat_tile("Relief P",    f"{row['RP_SPTS']:,.0f}",   "SPTS"),
        stat_tile("Batting Avg", f"{ba:.3f}",                "H/AB (capped)"),
        stat_tile("Total IP",    f"{row['Total_IP']:,.0f}",  "capped"),
        stat_tile("SV / HLD",    f"{row['SV']:.0f} / {row['HLD']:.0f}", "capped"),
    ], style={
        "display": "flex", "gap": "10px", "flexWrap": "wrap",
        "marginBottom": "20px",
    })

    charts = html.Div([
        html.Div(dcc.Graph(figure=build_radar_fig(team),
                           config={"displayModeBar": False}),
                 style={"flex": "1", "background": C_CARD,
                        "borderRadius": "12px", "padding": "12px"}),
        html.Div(dcc.Graph(figure=build_spts_donut(team),
                           config={"displayModeBar": False}),
                 style={"flex": "1", "background": C_CARD,
                        "borderRadius": "12px", "padding": "12px"}),
    ], style={"display": "flex", "gap": "16px", "marginBottom": "20px"})

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


server = app.server  # expose the Flask server for gunicorn

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8050))
    print(f"Dashboard running → http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
