"""
Ottoneu Power Rankings – Lineup-Constrained
============================================
Applies actual Ottoneu lineup rules before summing projected SPTS:

    HITTERS  (per day):  C · 1B · 2B · SS · MIF · 3B · OF×5 · Util  (12 slots), 140 games
    SP       (per week): 10 starts/week  →  210 total starts/season (21 weeks)
    RP       (per day):  capped at 500 IP/appearances

Position data is pulled from the FanGraphs 2025 batting leaderboard (single
API request) and merged by FanGraphs PlayerId.
"""

import re
import warnings
import requests
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Season constants ─────────────────────────────────────────────────────────
SEASON_WEEKS   = 21          # 21-week scoring season
SEASON_DAYS    = 140         # regular-season game-days (no playoffs)
SP_STARTS_CAP  = 10 * SEASON_WEEKS   # 210 starts / season
RP_APPS_CAP    = 350                 # RP innings/appearances cap
IP_PER_START   = 5.25                 # fallback innings per SP outing (used only when GS = 0)

# ── File paths ────────────────────────────────────────────────────────────────
# Roster files  – leaderboard exports that contain the Fantasy/team column
HITTER_ROSTER_FILE  = r"C:\Users\Rachel\Downloads\fangraphs-leaderboards (25).csv"
PITCHER_ROSTER_FILE = r"C:\Users\Rachel\Downloads\fangraphs-leaderboards (26).csv"
# Projection files – ZiPS/Steamer exports that contain the stat projections
HITTER_PROJ_FILE    = r"C:\Users\Rachel\Downloads\fangraphs-leaderboard-projections (8).csv"
PITCHER_PROJ_FILE   = r"C:\Users\Rachel\Downloads\fangraphs-leaderboard-projections (9).csv"
OUT_PATH     = Path(r"C:\Users\Rachel\Desktop\Scripts\ottoneu_power_rankings.csv")
OUT_XLSX     = OUT_PATH.with_suffix(".xlsx")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def extract_team(html: str) -> str:
    m = re.search(r'">(.+?)</a>', str(html))
    return m.group(1).strip() if m else "Free Agent"


def player_eligible(pos_str: str, slot: str) -> bool:
    """True if a player whose position string can fill the given lineup slot.
    DH players are Util-only (no positional home); all other non-pitchers
    are also Util-eligible.
    """
    p = str(pos_str).upper().strip()
    if slot == "C":
        return p == "C"
    if slot == "1B":
        return p == "1B"
    if slot == "2B":
        return p == "2B"
    if slot == "SS":
        return p == "SS"
    if slot == "MIF":
        return p in {"2B", "SS"}
    if slot == "3B":
        return p == "3B"
    if slot == "OF":
        return p == "OF"
    if slot == "Util":
        return p not in {"P", "SP", "RP"}
    return False


def optimal_lineup_spts(roster: pd.DataFrame) -> tuple:
    """
    For each position slot (140 game-days; 700 total for the 5 OF slots),
    greedily stack the best-SPTS/G eligible players for their full projected
    games, then fall to backups until the slot is filled to 155 days.
    No player is double-counted across slots.

    Slots are processed in scarcity order so scarce-position players
    (C, 3B, SS) are claimed before flex/multi-pos slots drain them.

    Returns (total_constrained_SPTS, constrained_AB, constrained_H, lineup_detail_list).
    """
    if roster.empty:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, []

    roster = roster.copy().reset_index(drop=True)

    # Projected games = season SPTS / SPTS-per-game (floor at 1)
    roster["proj_G"] = (
        roster["SPTS"] / roster["SPTS/G"].replace(0, np.nan)
    ).clip(lower=1).fillna(1).round()
    roster["rem_G"] = roster["proj_G"].copy()

    # Scarcity order: positional slots first, flex last
    SLOT_ORDER = [
        ("C",    140),
        ("3B",   140),
        ("SS",   140),
        ("2B",   140),
        ("1B",   140),
        ("MIF",  140),
        ("OF",   700),   # 5 OF slots × 140 game-days
        ("Util", 140),
    ]

    total_spts  = 0.0
    total_ab    = 0.0
    total_h     = 0.0
    total_2b    = 0.0
    total_bb    = 0.0
    total_hr    = 0.0
    total_sb    = 0.0
    woba_sum    = 0.0
    ops_sum     = 0.0
    weight_sum  = 0.0
    lineup_rows = []

    for slot, cap in SLOT_ORDER:
        eligible_idx = roster.index[
            roster["Pos"].apply(lambda p: player_eligible(p, slot)) &
            (roster["rem_G"] > 0)
        ]
        eligible = roster.loc[eligible_idx].sort_values("SPTS/G", ascending=False)

        remaining = float(cap)
        for idx in eligible.index:
            if remaining <= 0:
                break
            row    = roster.loc[idx]
            used_G = min(row["rem_G"], remaining)
            pts    = used_G * row["SPTS/G"]
            frac   = used_G / row["proj_G"] if row["proj_G"] > 0 else 0
            total_spts              += pts
            total_ab                += row.get("AB",   0) * frac
            total_h                 += row.get("H",    0) * frac
            total_2b                += row.get("2B",   0) * frac
            total_bb                += row.get("BB",   0) * frac
            total_hr                += row.get("HR",   0) * frac
            total_sb                += row.get("SB",   0) * frac
            woba_sum                += row.get("wOBA", 0) * used_G
            ops_sum                 += row.get("OPS",  0) * used_G
            weight_sum              += used_G
            roster.at[idx, "rem_G"] -= used_G
            remaining               -= used_G
            lineup_rows.append({
                "Slot":   slot,
                "Name":   row["Name"],
                "Pos":    row["Pos"],
                "SPTS/G": row["SPTS/G"],
                "G_proj": int(row["proj_G"]),
                "G_used": round(used_G, 1),
                "SPTS":   round(pts, 1),
            })

    woba_wavg = woba_sum / weight_sum if weight_sum > 0 else 0
    ops_wavg  = ops_sum  / weight_sum if weight_sum > 0 else 0
    return (round(total_spts, 1), round(total_ab, 1), round(total_h, 1),
            round(total_2b, 1), round(total_bb, 1), round(total_hr, 1),
            round(total_sb, 1), round(woba_wavg, 3), round(ops_wavg, 3), lineup_rows)


def constrained_pitcher_spts(team_pit: pd.DataFrame) -> tuple:
    """
    Apply the SP (10/week) and RP (5/day) caps.
    Returns (sp_spts, rp_spts, sv_constrained, hld_constrained, sp_detail, rp_detail).
    SV and HLD are scaled by the same usage fraction applied to each RP's SPTS,
    so they reflect only the innings that fit under the cap.
    """
    # Classify RP: closer/setup usage or short IP = reliever
    is_rp = (
        (team_pit["SV"]  > 2) |
        (team_pit["HLD"] > 5) |
        (team_pit["IP"]  < 70)
    )

    # Force dual-eligibility SP/RP to count as SP only
    if "Pos" in team_pit.columns:
        pos_flags = team_pit["Pos"].astype(str).str.upper()
        dual_sp   = pos_flags.str.contains("SP") & pos_flags.str.contains("RP")
        is_rp = is_rp & ~dual_sp
    sp_df = team_pit[~is_rp].copy()
    rp_df = team_pit[is_rp].copy()

    # ── SP: 210-start bucket (greedy cascade by SPTS/IP) ──────────────────
    # Proj_Starts = GS directly; IP/start = IP/GS per pitcher (fallback to IP_PER_START if GS=0)
    sp_df["IP_per_GS"]    = sp_df.apply(
        lambda r: r["IP"] / r["GS"] if r.get("GS", 0) > 0 else IP_PER_START, axis=1
    )
    sp_df["Proj_Starts"]  = sp_df.apply(
        lambda r: r["GS"] if r.get("GS", 0) > 0 else (r["IP"] / IP_PER_START), axis=1
    ).clip(lower=0.01)
    sp_df["SPTS_per_IP"]  = (sp_df["SPTS"] / sp_df["IP"].replace(0, np.nan)).fillna(0)
    sp_df = sp_df.sort_values("SPTS_per_IP", ascending=False).reset_index(drop=True)

    bucket = float(SP_STARTS_CAP)
    sp_spts, sp_detail = 0.0, []
    sp_k, sp_bb, sp_hr = 0.0, 0.0, 0.0
    sp_fip_ip_sum, sp_fip_ip_total = 0.0, 0.0
    for _, row in sp_df.iterrows():
        if bucket <= 0:
            break
        usable = min(row["Proj_Starts"], bucket)
        frac   = usable / row["Proj_Starts"]
        ip_used = row["IP"] * frac
        pts    = row["SPTS"] * frac
        sp_spts  += pts
        sp_k     += row.get("SO", row.get("K", 0)) * frac
        sp_bb    += row.get("BB", 0) * frac
        sp_hr    += row.get("HR", 0) * frac
        if row.get("FIP", 0) > 0:
            sp_fip_ip_sum   += row["FIP"] * ip_used
            sp_fip_ip_total += ip_used
        bucket   -= usable
        sp_detail.append({
            "Name":        row["Name"],
            "Role":        "SP",
            "Proj_Starts": round(row["Proj_Starts"], 1),
            "Used_Starts": round(usable, 1),
            "IP_per_GS":   round(row["IP_per_GS"], 2),
            "IP_used":     round(ip_used, 1),
            "SPTS_proj":   row["SPTS"],
            "SPTS_used":   round(pts, 1),
        })

    # ── RP: 500-IP bucket ─────────────────────────────────────────────────
    rp_df["Proj_Apps"]    = rp_df["IP"].clip(lower=1)
    rp_df["SPTS_per_App"] = rp_df["SPTS"] / rp_df["Proj_Apps"]
    rp_df = rp_df.sort_values("SPTS_per_App", ascending=False).reset_index(drop=True)

    bucket_rp = float(RP_APPS_CAP)
    rp_spts, rp_detail = 0.0, []
    sv_constrained, hld_constrained = 0.0, 0.0
    rp_k, rp_bb, rp_hr = 0.0, 0.0, 0.0
    rp_fip_ip_sum, rp_fip_ip_total = 0.0, 0.0
    for _, row in rp_df.iterrows():
        if bucket_rp <= 0:
            break
        usable = min(row["Proj_Apps"], bucket_rp)
        frac   = usable / row["Proj_Apps"]
        pts    = row["SPTS"] * frac
        rp_spts           += pts
        sv_constrained    += row.get("SV",  0) * frac
        hld_constrained   += row.get("HLD", 0) * frac
        rp_k              += row.get("SO", row.get("K", 0)) * frac
        rp_bb             += row.get("BB",  0) * frac
        rp_hr             += row.get("HR",  0) * frac
        if row.get("FIP", 0) > 0:
            rp_fip_ip_sum   += row["FIP"] * usable
            rp_fip_ip_total += usable
        bucket_rp         -= usable
        rp_detail.append({
            "Name":      row["Name"],
            "Role":      "RP",
            "Proj_Apps": round(row["Proj_Apps"], 1),
            "Used_Apps": round(usable, 1),
            "IP_used":   round(usable, 1),
            "SPTS_proj": row["SPTS"],
            "SPTS_used": round(pts, 1),
            "SV_proj":   row.get("SV",  0),
            "SV_used":   round(row.get("SV",  0) * frac, 1),
            "HLD_proj":  row.get("HLD", 0),
            "HLD_used":  round(row.get("HLD", 0) * frac, 1),
        })

    _fip_ip = sp_fip_ip_total + rp_fip_ip_total
    pit_fip = round((sp_fip_ip_sum + rp_fip_ip_sum) / max(_fip_ip, 1), 2)

    return (sp_spts, rp_spts,
            round(sv_constrained, 1), round(hld_constrained, 1),
            round(sp_k + rp_k, 1), round(sp_bb + rp_bb, 1), round(sp_hr + rp_hr, 1),
            pit_fip,
            sp_detail, rp_detail)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 – Load roster (team assignments) + projections, then merge
# ─────────────────────────────────────────────────────────────────────────────
for label, path in (
    ("Hitter roster",      HITTER_ROSTER_FILE),
    ("Pitcher roster",     PITCHER_ROSTER_FILE),
    ("Hitter projections", HITTER_PROJ_FILE),
    ("Pitcher projections",PITCHER_PROJ_FILE),
):
    if not Path(path).is_file():
        raise FileNotFoundError(f"{label} file not found: {path}")

print("Loading roster files…")
hit_roster = pd.read_csv(HITTER_ROSTER_FILE)
pit_roster  = pd.read_csv(PITCHER_ROSTER_FILE)

for df in (hit_roster, pit_roster):
    df["Team"] = df["Fantasy"].apply(extract_team)

# Keep only rostered players — retain Name + MLBAMID for the full roster frame
# Include pitcher stat cols for role classification of stash/no-projection players
hit_roster_full = (
    hit_roster[hit_roster["Team"] != "Free Agent"]
    [["Name", "PlayerId", "Team", "$", "MLBAMID"]].copy()
    .rename(columns={"$": "Salary"})
)
pit_roster_full = (
    pit_roster[pit_roster["Team"] != "Free Agent"]
    [["Name", "PlayerId", "Team", "$", "MLBAMID", "IP", "SV", "HLD"]].copy()
    .rename(columns={"$": "Salary", "IP": "Ros_IP", "SV": "Ros_SV", "HLD": "Ros_HLD"})
)
for col in ("Ros_IP", "Ros_SV", "Ros_HLD"):
    pit_roster_full[col] = pd.to_numeric(pit_roster_full[col], errors="coerce").fillna(0)
for _rf in (hit_roster_full, pit_roster_full):
    _rf["PlayerId"] = _rf["PlayerId"].astype(str).str.strip()
    _rf["MLBAMID"]  = pd.to_numeric(_rf["MLBAMID"], errors="coerce")

# Slim frames used only for inner-join merge below
hit_roster_ids = hit_roster_full[["PlayerId", "Team", "Salary"]].copy()
pit_roster_ids  = pit_roster_full[["PlayerId", "Team", "Salary"]].copy()

print("Loading projection files…")
hit_proj = pd.read_csv(HITTER_PROJ_FILE)
pit_proj  = pd.read_csv(PITCHER_PROJ_FILE)

if "PlayerId" not in hit_proj.columns or "PlayerId" not in pit_proj.columns:
    raise KeyError(
        "Projection CSVs must contain a 'PlayerId' column to merge with roster data. "
        f"Hitter proj cols: {list(hit_proj.columns)[:10]}  "
        f"Pitcher proj cols: {list(pit_proj.columns)[:10]}"
    )

hit_proj["PlayerId"] = hit_proj["PlayerId"].astype(str).str.strip()
pit_proj["PlayerId"]  = pit_proj["PlayerId"].astype(str).str.strip()

# Drop the MLB-team column from projections so it doesn't collide with the
# fantasy Team column coming from the roster merge
hit_proj = hit_proj.drop(columns=["Team"], errors="ignore")
pit_proj  = pit_proj.drop(columns=["Team"], errors="ignore")

# ── Inner-join frames: projection-matched players only (used for SPTS calc) ─
hit_df = hit_proj.merge(hit_roster_ids, on="PlayerId", how="inner")
pit_df  = pit_proj.merge(pit_roster_ids,  on="PlayerId", how="inner")

print(f"  -> {len(hit_df):,} rostered hitters matched to projections.")
print(f"  -> {len(pit_df):,} rostered pitchers matched to projections.")

# ── Full roster frames: ALL rostered players (left join — zero SPTS if no projection) ─
_HIT_PROJ_COLS = [c for c in ("PlayerId", "SPTS", "SPTS/G", "AB", "H", "2B", "BB",
                               "HR", "SB", "wOBA", "OPS") if c in hit_proj.columns]
_PIT_PROJ_COLS = [c for c in ("PlayerId", "SPTS", "IP", "SV", "HLD", "GS",
                               "SO", "BB", "HR", "FIP") if c in pit_proj.columns]
hit_full = hit_roster_full.merge(hit_proj[_HIT_PROJ_COLS], on="PlayerId", how="left")
pit_full = pit_roster_full.merge(pit_proj[_PIT_PROJ_COLS], on="PlayerId", how="left")

print(f"  -> {len(hit_full):,} total rostered hitters (incl. no-projection players).")
print(f"  -> {len(pit_full):,} total rostered pitchers (incl. no-projection players).")

for col in ("SPTS", "SPTS/G", "AB", "H", "2B", "BB", "HR", "SB", "wOBA", "OPS"):
    if col in hit_df.columns:
        hit_df[col] = pd.to_numeric(hit_df[col], errors="coerce").fillna(0)
    else:
        hit_df[col] = 0.0
    hit_full[col] = pd.to_numeric(hit_full.get(col, 0), errors="coerce").fillna(0)

for col in ("SPTS", "IP", "SV", "HLD", "GS", "SO", "BB", "HR", "FIP"):
    if col in pit_df.columns:
        pit_df[col] = pd.to_numeric(pit_df[col], errors="coerce").fillna(0)
    else:
        pit_df[col] = 0.0
        if col == "GS":
            print(f"  ⚠ 'GS' column not found in pitcher projections — IP/start will default to {IP_PER_START}.")
            print(f"     Columns found: {list(pit_df.columns)}")
    pit_full[col] = pd.to_numeric(pit_full.get(col, 0), errors="coerce").fillna(0)

# ─────────────────────────────────────────────────────────────────────────────
# Step 2 – Fetch position data via MLB Stats API (uses MLBAMID, free/no-auth)
# ─────────────────────────────────────────────────────────────────────────────
print("Fetching position data from MLB Stats API…")

# MLB primary position → Ottoneu-compatible position string
MLB_POS_MAP = {
    "C":   "C",
    "1B":  "1B",
    "2B":  "2B",
    "SS":  "SS",
    "3B":  "3B",
    "LF":  "OF",
    "CF":  "OF",
    "RF":  "OF",
    "OF":  "OF",
    "DH":  "DH",    # Util-eligible but no positional slot
    "TWP": "DH",    # two-way player hitting role → DH
    "P":   "P",
}
POS_FALLBACK = "OF"  # default for unknown hitters

if "MLBAMID" not in hit_df.columns:
    raise KeyError(
        "'MLBAMID' column not found in hitter projections CSV. "
        "Make sure the FanGraphs projection export includes the MLBAM ID field."
        f"\n  Columns found: {list(hit_df.columns)}"
    )

# Collect MLBAMIDs from ALL rostered players (roster files always have MLBAMID)
hit_mlbam_ids = hit_full["MLBAMID"].dropna().astype(int).unique().tolist()
pit_mlbam_ids = pit_full["MLBAMID"].dropna().astype(int).unique().tolist()
all_mlbam_ids = list(set(hit_mlbam_ids + pit_mlbam_ids))
pos_by_mlbam: dict = {}
age_by_mlbam: dict = {}

BATCH = 300  # MLB API supports large batches
MLB_API = "https://statsapi.mlb.com/api/v1/people"

for i in range(0, len(all_mlbam_ids), BATCH):
    batch = all_mlbam_ids[i : i + BATCH]
    try:
        resp = requests.get(
            MLB_API,
            params={
                "personIds": ",".join(map(str, batch)),
                "fields":    "people,id,primaryPosition,abbreviation,currentAge",
            },
            timeout=20,
        )
        resp.raise_for_status()
        for person in resp.json().get("people", []):
            abbr = person.get("primaryPosition", {}).get("abbreviation", "")
            pos_by_mlbam[person["id"]] = MLB_POS_MAP.get(abbr, POS_FALLBACK)
            age_by_mlbam[person["id"]] = person.get("currentAge")
    except Exception as e:
        print(f"  ⚠ MLB API batch {i//BATCH+1} failed: {e}")

print(f"  -> {len(pos_by_mlbam):,} player positions + ages loaded.")

# Name → MLBAMID lookup built from both roster files (used for Excel headshots)
_name_to_mlbam: dict = {}

def _apply_pos(df, fallback=POS_FALLBACK):
    df["MLBAMID"] = pd.to_numeric(df["MLBAMID"], errors="coerce")
    df["Pos"] = df["MLBAMID"].apply(
        lambda mid: pos_by_mlbam.get(int(mid), fallback) if pd.notna(mid) else fallback
    )
    df["Age"] = df["MLBAMID"].apply(
        lambda mid: age_by_mlbam.get(int(mid)) if pd.notna(mid) else None
    )

_apply_pos(hit_df)
_apply_pos(hit_full)

# Build name→MLBAMID lookup from all rostered players (used for headshots in Excel sheets)
for _src in (hit_full, pit_full):
    for _, _r in _src[["Name", "MLBAMID"]].iterrows():
        if pd.notna(_r["MLBAMID"]):
            _name_to_mlbam[_r["Name"]] = int(_r["MLBAMID"])

# Drop pitchers that leaked into the hitter batting-leaderboard CSV.
# The MLB API returns Pos="P" for pure pitchers; they will be captured by the
# pitcher roster CSV and pitcher loop — counting them here would double-count
# salary and create duplicate roster rows.
hit_df   = hit_df[hit_df["Pos"] != "P"].copy()
hit_full = hit_full[hit_full["Pos"] != "P"].copy()
print(f"  -> After removing pitchers from hitter frames: {len(hit_df):,} matched, {len(hit_full):,} total.")

# Pitchers: age only (no position lookup needed for pitching logic)
for _pf in (pit_df, pit_full):
    _pf["MLBAMID"] = pd.to_numeric(_pf["MLBAMID"], errors="coerce")
    _pf["Age"] = _pf["MLBAMID"].apply(
        lambda mid: age_by_mlbam.get(int(mid)) if pd.notna(mid) else None
    )

# Mirror the is_rp logic from constrained_pitcher_spts for the roster sheet
# For pit_full: prefer projection stats; fall back to ROSTER CSV stats for
# unmatched players (IP=0 from fillna would wrongly tag every stashed SP as RP)
for _pf in (pit_df, pit_full):
    if "Ros_IP" in _pf.columns:
        # Use projection IP where available, otherwise use roster-file IP
        eff_ip  = _pf["IP"].where(_pf["IP"] > 0, _pf["Ros_IP"])
        eff_sv  = _pf["SV"].where(_pf["SV"] > 0, _pf["Ros_SV"])
        eff_hld = _pf["HLD"].where(_pf["HLD"] > 0, _pf["Ros_HLD"])
    else:
        eff_ip, eff_sv, eff_hld = _pf["IP"], _pf["SV"], _pf["HLD"]
    _is_rp = (eff_sv > 2) | (eff_hld > 5) | (eff_ip < 70)
    _pf["Role"] = _is_rp.map({True: "RP", False: "SP"})

# ─────────────────────────────────────────────────────────────────────────────
# Step 3 – Per-team constrained SPTS
# ─────────────────────────────────────────────────────────────────────────────
teams = sorted(hit_df["Team"].unique())
results, team_lineups, team_pitching = [], {}, {}
roster_records: list = []

print(f"\nOptimising lineups for {len(teams)} teams…")

for team in teams:
    roster  = hit_df[hit_df["Team"] == team][["Name", "Pos", "SPTS", "SPTS/G", "AB", "H", "2B", "BB", "HR", "SB", "wOBA", "OPS"]].copy()
    hit_spts, hit_ab, hit_h, hit_2b, hit_bb, hit_hr, hit_sb, hit_woba, hit_ops, lineup = optimal_lineup_spts(roster)
    team_lineups[team] = lineup

    pit_roster = pit_df[pit_df["Team"] == team].copy()
    sp_spts, rp_spts, sv_constrained, hld_constrained, pit_k, pit_bb, pit_hr, pit_fip, sp_det, rp_det = constrained_pitcher_spts(pit_roster)
    team_pitching[team] = (sp_det, rp_det)

    # Used SPTS lookup keyed by player name — used for roster sheet
    # Hitters: sum across all slots a player appears in
    used_spts: dict = {}
    for lrow in lineup:
        used_spts[lrow["Name"]] = used_spts.get(lrow["Name"], 0.0) + lrow["SPTS"]
    for prow in sp_det:
        used_spts[prow["Name"]] = used_spts.get(prow["Name"], 0.0) + prow["SPTS_used"]
    for prow in rp_det:
        used_spts[prow["Name"]] = used_spts.get(prow["Name"], 0.0) + prow["SPTS_used"]

    team_hit  = hit_df[hit_df["Team"] == team]       # projection-matched (SPTS calc)
    team_pit  = pit_df[pit_df["Team"] == team]
    team_hit_full = hit_full[hit_full["Team"] == team]  # all contracted players
    team_pit_full = pit_full[pit_full["Team"] == team]

    # Identify true two-way players: must have a meaningful projection in BOTH
    # the hitter AND pitcher projection file. Using projection-file PlayerIds
    # prevents pitchers with 1 PA from being flagged as two-way via the
    # roster-file overlap.
    _hit_proj_pids = set(hit_proj["PlayerId"].astype(str))
    _pit_proj_pids  = set(pit_proj["PlayerId"].astype(str))
    _twoway_proj    = _hit_proj_pids & _pit_proj_pids
    hit_pids        = set(team_hit_full["PlayerId"].astype(str))
    pit_pids        = set(team_pit_full["PlayerId"].astype(str))
    twoway_pids     = (hit_pids & pit_pids) & _twoway_proj
    pit_sal_df      = team_pit_full[~team_pit_full["PlayerId"].astype(str).isin(twoway_pids)]

    # Salary & age aggregations (all rostered players, deduplicated)
    # Compute SP/RP role inline from best available stats (projection > roster CSV)
    def _pit_role(p):
        _ip  = p.get("IP",  0) or p.get("Ros_IP",  0)
        _sv  = p.get("SV",  0) or p.get("Ros_SV",  0)
        _hld = p.get("HLD", 0) or p.get("Ros_HLD", 0)
        return "RP" if (_sv > 2 or _hld > 5 or _ip < 70) else "SP"

    sal_hit  = float(team_hit_full["Salary"].sum())
    sal_sp   = sum(float(p.get("Salary", 0)) for _, p in pit_sal_df.iterrows()
                   if _pit_role(p) == "SP")
    sal_rp   = sum(float(p.get("Salary", 0)) for _, p in pit_sal_df.iterrows()
                   if _pit_role(p) == "RP")
    age_hit  = team_hit_full[["PlayerId", "Age"]].drop_duplicates("PlayerId")
    age_pit  = pit_sal_df[["PlayerId", "Age"]].drop_duplicates("PlayerId")
    all_ages = pd.concat([age_hit["Age"], age_pit["Age"]]).dropna()
    avg_age  = round(float(all_ages.mean()), 1) if len(all_ages) > 0 else 0.0

    # Collect per-player rows for the Roster sheet (all contracted players)
    # Pos: DH → Util (DH-only hitters fill the Util slot in Ottoneu)
    # Role: H / TWP for hitters; SP / RP computed inline for pitchers
    # SPTS: constrained used SPTS (0 if not in lineup / no projection)
    for _, p in team_hit_full.iterrows():
        pid  = str(p.get("PlayerId", ""))
        role = "TWP" if pid in twoway_pids else "H"
        raw_pos = p.get("Pos", "?")
        display_pos = "Util" if raw_pos == "DH" else raw_pos
        _mid = p.get("MLBAMID")
        _mid_int = int(_mid) if pd.notna(_mid) else None
        roster_records.append({
            "Team":    team,  "Name": p["Name"],
            "Role":    role,  "Pos":  display_pos,
            "Salary":  p.get("Salary", 0),
            "Age":     p.get("Age"),
            "MLBAMID": _mid_int,
            "SPTS":    round(used_spts.get(p["Name"], 0.0), 1),
        })
    for _, p in team_pit_full.iterrows():
        if str(p.get("PlayerId", "")) in twoway_pids:
            continue  # salary already recorded on hitting side
        # Compute SP/RP inline using best available stats (proj > roster CSV)
        _ip  = p.get("IP",  0) or p.get("Ros_IP",  0)
        _sv  = p.get("SV",  0) or p.get("Ros_SV",  0)
        _hld = p.get("HLD", 0) or p.get("Ros_HLD", 0)
        role = "RP" if (_sv > 2 or _hld > 5 or _ip < 70) else "SP"
        _mid = p.get("MLBAMID")
        _mid_int = int(_mid) if pd.notna(_mid) else None
        roster_records.append({
            "Team":    team,  "Name": p["Name"],
            "Role":    role,  "Pos":  role,
            "Salary":  p.get("Salary", 0),
            "Age":     p.get("Age"),
            "MLBAMID": _mid_int,
            "SPTS":    round(used_spts.get(p["Name"], 0.0), 1),
        })

    raw_hit = team_hit["SPTS"].sum()
    raw_pit = team_pit["SPTS"].sum()

    sp_ip_used = sum(p["IP_used"] for p in sp_det)
    rp_ip_used = sum(p["IP_used"] for p in rp_det)

    results.append({
        "Team":       team,
        "Hit_SPTS":   round(hit_spts, 1),
        "SP_SPTS":    round(sp_spts, 1),
        "RP_SPTS":    round(rp_spts, 1),
        "Pit_SPTS":   round(sp_spts + rp_spts, 1),
        "Total_SPTS": round(hit_spts + sp_spts + rp_spts, 1),
        "Hit_AB":     round(hit_ab, 1),
        "Hit_H":      round(hit_h, 1),
        "Hit_2B":     round(hit_2b, 1),
        "Hit_BB":     round(hit_bb, 1),
        "Hit_HR":     round(hit_hr, 1),
        "Hit_SB":     round(hit_sb, 1),
        "Hit_wOBA":   round(hit_woba, 3),
        "Hit_OPS":    round(hit_ops, 3),
        "Pit_IP":     round(sp_ip_used + rp_ip_used, 1),
        "SV":         sv_constrained,
        "HLD":        hld_constrained,
        "Pit_K":      pit_k,
        "Pit_BB":     pit_bb,
        "Pit_HR":     pit_hr,
        "Pit_FIP":    pit_fip,
        "SP_IP":      round(sp_ip_used, 1),
        "RP_IP":      round(rp_ip_used, 1),
        "Total_IP":   round(sp_ip_used + rp_ip_used, 1),
        "Raw_Hit":    round(raw_hit, 1),
        "Raw_Pit":    round(raw_pit, 1),
        "Raw_Total":  round(raw_hit + raw_pit, 1),
        "N_Bat":      len(team_hit_full),
        "N_Pit":      len(team_pit_full),
        "Total_Salary": round(sal_hit + sal_sp + sal_rp, 0),
        "Sal_Hit":      round(sal_hit, 0),
        "Sal_SP":       round(sal_sp, 0),
        "Sal_RP":       round(sal_rp, 0),
        "Avg_Age":      avg_age,
    })

roster_df = pd.DataFrame(roster_records)

rankings = (pd.DataFrame(results)
              .sort_values("Total_SPTS", ascending=False)
              .reset_index(drop=True))
rankings.index += 1

# ─────────────────────────────────────────────────────────────────────────────
# Step 4 – Print power rankings table
# ─────────────────────────────────────────────────────────────────────────────
W = 140
print("\n" + "=" * W)
print(f"{'OTTONEU 2026 POWER RANKINGS  —  Lineup-Constrained  (ZIPS)':^{W}}")
print("=" * W)
print(f"{'Rank':<5} {'Team':<14} {'Hit SPTS':>9} {'SP SPTS':>8} {'RP SPTS':>8} "
      f"{'TOTAL':>9}  {'Hit AB':>8} {'Hit H':>8} {'Pit IP':>8} {'SV':>6} {'HLD':>6} "
      f"{'SP IP':>7} {'RP IP':>7} {'Tot IP':>7}")
print("-" * W)
for rank, row in rankings.iterrows():
    print(
        f"{rank:<5} {row['Team']:<14} {row['Hit_SPTS']:>9,.1f} "
        f"{row['SP_SPTS']:>8,.1f} {row['RP_SPTS']:>8,.1f} "
        f"{row['Total_SPTS']:>9,.1f}  "
        f"{row['Hit_AB']:>8,.1f} {row['Hit_H']:>8,.1f} {row['Pit_IP']:>8,.1f} "
        f"{row['SV']:>6,.1f} {row['HLD']:>6,.1f} "
        f"{row['SP_IP']:>7,.1f} {row['RP_IP']:>7,.1f} {row['Total_IP']:>7,.1f}"
    )
print("=" * W)

# ─────────────────────────────────────────────────────────────────────────────
# Step 5 – Lineup + pitching detail per team
# ─────────────────────────────────────────────────────────────────────────────
print("=" * W)
print("OPTIMAL LINEUP & PITCHING DETAIL BY TEAM")
print("=" * W)

for rank, row in rankings.iterrows():
    team = row["Team"]
    print(f"\n{'─'*4} #{rank}  {team}  "
          f"(Hit: {row['Hit_SPTS']:,.1f} | SP: {row['SP_SPTS']:,.1f} | "
          f"RP: {row['RP_SPTS']:,.1f} | Total: {row['Total_SPTS']:,.1f})")

    lineup = team_lineups[team]
    if lineup:
        SLOT_PRINT_ORDER = ["C", "1B", "2B", "SS", "MIF", "3B", "OF", "Util"]
        slot_order_idx   = {s: i for i, s in enumerate(SLOT_PRINT_ORDER)}
        lineup_sorted    = sorted(lineup, key=lambda x: (slot_order_idx.get(x["Slot"], 99), -x["SPTS/G"]))
        print(f"  {'SLOT':<6} {'NAME':<28} {'POS':<5} {'G_proj':>6} {'G_used':>6} {'SPTS/G':>7} {'SPTS':>8}")
        print(f"  {'─'*6} {'─'*28} {'─'*5} {'─'*6} {'─'*6} {'─'*7} {'─'*8}")
        cur_slot = None
        for p in lineup_sorted:
            slot_label = p["Slot"] if p["Slot"] != cur_slot else "  ↳"
            cur_slot   = p["Slot"]
            print(f"  {slot_label:<6} {p['Name']:<28} {p['Pos']:<5} "
                  f"{p['G_proj']:>6} {p['G_used']:>6.1f} "
                  f"{p['SPTS/G']:>7.2f} {p['SPTS']:>8.1f}")

    sp_det, rp_det = team_pitching[team]
    if sp_det:
        print(f"\n  Starting Pitchers  (cap: {SP_STARTS_CAP} starts)")
        print(f"  {'NAME':<28} {'GS_proj':>8} {'GS_used':>8} {'IP/GS':>7} {'IP_used':>8} {'SPTS_proj':>10} {'SPTS_used':>10}")
        print(f"  {'─'*28} {'─'*8} {'─'*8} {'─'*7} {'─'*8} {'─'*10} {'─'*10}")
        for p in sp_det:
            print(f"  {p['Name']:<28} {p['Proj_Starts']:>8.1f} {p['Used_Starts']:>8.1f} "
                  f"{p['IP_per_GS']:>7.2f} {p['IP_used']:>8.1f} {p['SPTS_proj']:>10.1f} {p['SPTS_used']:>10.1f}")

    if rp_det:
        print(f"\n  Relief Pitchers  (cap: {RP_APPS_CAP} IP)")
        print(f"  {'NAME':<28} {'App_proj':>8} {'App_used':>8} {'IP_used':>8} {'SPTS_proj':>10} {'SPTS_used':>10} {'SV_proj':>8} {'SV_used':>8} {'HLD_proj':>9} {'HLD_used':>9}")
        print(f"  {'─'*28} {'─'*8} {'─'*8} {'─'*8} {'─'*10} {'─'*10} {'─'*8} {'─'*8} {'─'*9} {'─'*9}")
        for p in rp_det:
            print(f"  {p['Name']:<28} {p['Proj_Apps']:>8.1f} {p['Used_Apps']:>8.1f} "
                  f"{p['IP_used']:>8.1f} {p['SPTS_proj']:>10.1f} {p['SPTS_used']:>10.1f} "
                  f"{p['SV_proj']:>8.1f} {p['SV_used']:>8.1f} {p['HLD_proj']:>9.1f} {p['HLD_used']:>9.1f}")

# ─────────────────────────────────────────────────────────────────────────────
# Step 6 – Save CSV
# ─────────────────────────────────────────────────────────────────────────────
# CSV summary
rankings.to_csv(OUT_PATH, index_label="Rank")

# Excel workbook (summary + detail)
rankings_with_rank = rankings.reset_index().rename(columns={"index": "Rank"})

team_rank = rankings["Team"].to_dict()  # {rank_index: team_name}
team_rank = {v: k for k, v in team_rank.items()}  # invert → {team_name: rank}

lineup_records = []
for team, lineup in team_lineups.items():
    for row in lineup:
        lineup_records.append({
            "Rank":    team_rank.get(team),
            "Team":    team,
            "MLBAMID": _name_to_mlbam.get(row["Name"]),
            **row,
        })

sp_records, rp_records = [], []
for team, (sp_det, rp_det) in team_pitching.items():
    for row in sp_det:
        sp_records.append({"Rank": team_rank.get(team), "Team": team,
                           "MLBAMID": _name_to_mlbam.get(row["Name"]), **row})
    for row in rp_det:
        rp_records.append({"Rank": team_rank.get(team), "Team": team,
                           "MLBAMID": _name_to_mlbam.get(row["Name"]), **row})

lineup_df = pd.DataFrame(lineup_records)
sp_df_out = pd.DataFrame(sp_records)
rp_df_out = pd.DataFrame(rp_records)

with pd.ExcelWriter(OUT_XLSX) as writer:
    rankings_with_rank.to_excel(writer, sheet_name="Summary", index=False)
    if not lineup_df.empty:
        lineup_df.sort_values(["Rank", "Slot", "SPTS/G"], ascending=[True, True, False]).to_excel(
            writer, sheet_name="Hitters", index=False
        )
    if not sp_df_out.empty:
        sp_df_out.sort_values(["Rank", "SPTS_used"], ascending=[True, False]).to_excel(
            writer, sheet_name="SP_Detail", index=False
        )
    if not rp_df_out.empty:
        rp_df_out.sort_values(["Rank", "SPTS_used"], ascending=[True, False]).to_excel(
            writer, sheet_name="RP_Detail", index=False
        )
    if not roster_df.empty:
        roster_df["SPTS_per_$"] = roster_df.apply(
            lambda r: round(float(r["SPTS"]) / float(r["Salary"]), 2)
            if r.get("Salary", 0) > 0 else 0.0, axis=1
        )
        roster_out = roster_df.sort_values(
            ["Team", "Salary"], ascending=[True, False]
        )
        # Match team name display mapping already applied to rankings
        roster_out.to_excel(writer, sheet_name="Roster", index=False)

print(f"\n{'='*W}")
print(f"Rankings saved -> {OUT_PATH}")
print(f"Workbook saved  -> {OUT_XLSX}")
