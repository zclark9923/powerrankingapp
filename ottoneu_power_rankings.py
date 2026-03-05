"""
Ottoneu Power Rankings – Lineup-Constrained
============================================
Applies actual Ottoneu lineup rules before summing projected SPTS:

    HITTERS  (per day):  C · 1B · 2B · SS · MIF · 3B · OF×5 · Util  (12 slots), 140 games
    SP       (per week): 10 starts/week  →  210 total starts/season (21 weeks)
    RP       (per day):  capped at 350 appearances

Position data is pulled from the FanGraphs fielding leaderboard and MLB Stats API.

Run directly to regenerate the XLSX:
    python ottoneu_power_rankings.py

Import freely (no side effects) to use the optimizer functions:
    from ottoneu_power_rankings import optimal_lineup_spts, constrained_pitcher_spts
"""

import re
import warnings
import requests
import numpy as np
import pandas as pd
from pathlib import Path

warnings.filterwarnings("ignore")

# ── Season constants ─────────────────────────────────────────────────────────
SEASON_WEEKS   = 21
SEASON_DAYS    = 140
SP_STARTS_CAP  = 10 * SEASON_WEEKS   # 210 starts / season
RP_APPS_CAP    = 350
IP_PER_START   = 5.25
SCHEDULE_SCALE = 140 / 162
MAX_PLAYER_G   = 140
POS_FALLBACK   = "OF"

# ── File paths ────────────────────────────────────────────────────────────────
HITTER_ROSTER_FILE  = r"C:\Users\Rachel\Downloads\fangraphs-leaderboards (30).csv"
PITCHER_ROSTER_FILE = r"C:\Users\Rachel\Downloads\fangraphs-leaderboards (31).csv"
FIELDING_FILE       = r"C:\Users\Rachel\Downloads\fangraphs-leaderboards (29).csv"

PROJ_SYSTEMS = {
    "ZiPS DC": (
        r"C:\Users\Rachel\Downloads\fangraphs-leaderboard-projections (8).csv",
        r"C:\Users\Rachel\Downloads\fangraphs-leaderboard-projections (9).csv",
    ),
    "ZiPS": (
        r"C:\Users\Rachel\Downloads\RegularZipsHitters.csv",
        r"C:\Users\Rachel\Downloads\RegularZipsPitcher.csv",
    ),
    "Steamer": (
        r"C:\Users\Rachel\Downloads\SteamerHitter.csv",
        r"C:\Users\Rachel\Downloads\SteamerPitcher.csv",
    ),
    "ATC": (
        r"C:\Users\Rachel\Downloads\ATCHitter.csv",
        r"C:\Users\Rachel\Downloads\ATCPitcher.csv",
    ),
}

OUT_PATH = Path(r"C:\Users\Rachel\Desktop\Scripts\ottoneu_power_rankings.csv")
OUT_XLSX = OUT_PATH.with_suffix(".xlsx")

# ─────────────────────────────────────────────────────────────────────────────
# Pure helper functions (no side effects — safe to import)
# ─────────────────────────────────────────────────────────────────────────────

def extract_team(html: str) -> str:
    m = re.search(r'">(.+?)</a>', str(html))
    return m.group(1).strip() if m else "Free Agent"


def player_eligible(pos_str: str, slot: str) -> bool:
    """True if a player whose position string can fill the given lineup slot."""
    parts = {p.strip().upper() for p in str(pos_str).split("/")}
    if slot == "C":    return "C" in parts
    if slot == "1B":   return "1B" in parts
    if slot == "2B":   return "2B" in parts
    if slot == "SS":   return "SS" in parts
    if slot == "MIF":  return bool(parts & {"2B", "SS"})
    if slot == "3B":   return "3B" in parts
    if slot == "OF":   return "OF" in parts
    if slot == "Util": return not bool(parts & {"P", "SP", "RP"})
    return False


def optimal_lineup_spts(roster: pd.DataFrame) -> tuple:
    """
    Greedy lineup optimizer — returns:
    (hit_spts, ab, h, 2b, bb, hr, sb, woba_wavg, ops_wavg, lineup_detail_list)
    """
    if roster.empty:
        return 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, []

    roster = roster.copy().reset_index(drop=True)
    roster["proj_G"] = (
        roster["SPTS"] / roster["SPTS/G"].replace(0, np.nan)
    ).clip(lower=1, upper=MAX_PLAYER_G).fillna(1).round()
    roster["rem_G"] = roster["proj_G"].copy()

    SLOT_ORDER = [
        ("C",    140), ("3B",   140), ("SS",   140), ("2B",   140),
        ("1B",   140), ("MIF",  140), ("OF",   700), ("Util", 140),
    ]

    total_spts = total_ab = total_h = total_2b = total_bb = 0.0
    total_hr   = total_sb = woba_sum = ops_sum = weight_sum = 0.0
    lineup_rows = []

    for slot, cap in SLOT_ORDER:
        eligible_idx = roster.index[
            roster["Pos"].apply(lambda p: player_eligible(p, slot)) &
            (roster["rem_G"] > 0)
        ]
        eligible = roster.loc[eligible_idx].sort_values("SPTS/G", ascending=False)
        remaining = float(cap)
        for idx in eligible.index:
            if remaining <= 0: break
            row    = roster.loc[idx]
            used_G = min(row["rem_G"], remaining)
            pts    = used_G * row["SPTS/G"]
            frac   = used_G / row["proj_G"] if row["proj_G"] > 0 else 0
            total_spts  += pts
            total_ab    += row.get("AB",   0) * frac
            total_h     += row.get("H",    0) * frac
            total_2b    += row.get("2B",   0) * frac
            total_bb    += row.get("BB",   0) * frac
            total_hr    += row.get("HR",   0) * frac
            total_sb    += row.get("SB",   0) * frac
            woba_sum    += row.get("wOBA", 0) * used_G
            ops_sum     += row.get("OPS",  0) * used_G
            weight_sum  += used_G
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
            round(total_2b, 1),  round(total_bb, 1), round(total_hr, 1),
            round(total_sb, 1),  round(woba_wavg, 3), round(ops_wavg, 3), lineup_rows)


def constrained_pitcher_spts(team_pit: pd.DataFrame) -> tuple:
    """
    Apply SP (210-start) and RP (350-app) caps.
    Returns (sp_spts, rp_spts, sv, hld, pit_k, pit_bb, pit_hr, pit_fip, sp_detail, rp_detail)
    """
    is_rp = (
        (team_pit["SV"]  > 2) |
        (team_pit["HLD"] > 5) |
        (team_pit["IP"]  < 70)
    )
    if "Pos" in team_pit.columns:
        dual_sp = (team_pit["Pos"].astype(str).str.upper().str.contains("SP") &
                   team_pit["Pos"].astype(str).str.upper().str.contains("RP"))
        is_rp = is_rp & ~dual_sp

    sp_df = team_pit[~is_rp].copy()
    rp_df = team_pit[is_rp].copy()

    # SP bucket
    sp_df["IP_per_GS"]   = sp_df.apply(
        lambda r: r["IP"] / r["GS"] if r.get("GS", 0) > 0 else IP_PER_START, axis=1)
    sp_df["Proj_Starts"] = sp_df.apply(
        lambda r: r["GS"] if r.get("GS", 0) > 0 else (r["IP"] / IP_PER_START), axis=1
    ).clip(lower=0.01)
    sp_df["SPTS_per_IP"] = (sp_df["SPTS"] / sp_df["IP"].replace(0, np.nan)).fillna(0)
    sp_df = sp_df.sort_values("SPTS_per_IP", ascending=False).reset_index(drop=True)

    bucket = float(SP_STARTS_CAP)
    sp_spts = sp_k = sp_bb = sp_hr = sp_fip_ip_sum = sp_fip_ip_total = 0.0
    sp_detail = []
    for _, row in sp_df.iterrows():
        if bucket <= 0: break
        usable  = min(row["Proj_Starts"], bucket)
        frac    = usable / row["Proj_Starts"]
        ip_used = row["IP"] * frac
        pts     = row["SPTS"] * frac
        sp_spts += pts
        sp_k    += row.get("SO", row.get("K", 0)) * frac
        sp_bb   += row.get("BB", 0) * frac
        sp_hr   += row.get("HR", 0) * frac
        if row.get("FIP", 0) > 0:
            sp_fip_ip_sum   += row["FIP"] * ip_used
            sp_fip_ip_total += ip_used
        bucket -= usable
        sp_detail.append({
            "Name": row["Name"], "Role": "SP",
            "Proj_Starts": round(row["Proj_Starts"], 1),
            "Used_Starts": round(usable, 1),
            "IP_per_GS":   round(row["IP_per_GS"], 2),
            "IP_used":     round(ip_used, 1),
            "SPTS_proj":   row["SPTS"],
            "SPTS_used":   round(pts, 1),
        })

    # RP bucket
    rp_df["Proj_Apps"]    = rp_df["IP"].clip(lower=1)
    rp_df["SPTS_per_App"] = rp_df["SPTS"] / rp_df["Proj_Apps"]
    rp_df = rp_df.sort_values("SPTS_per_App", ascending=False).reset_index(drop=True)

    bucket_rp = float(RP_APPS_CAP)
    rp_spts = sv_constrained = hld_constrained = 0.0
    rp_k = rp_bb = rp_hr = rp_fip_ip_sum = rp_fip_ip_total = 0.0
    rp_detail = []
    for _, row in rp_df.iterrows():
        if bucket_rp <= 0: break
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
        bucket_rp -= usable
        rp_detail.append({
            "Name": row["Name"], "Role": "RP",
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
            pit_fip, sp_detail, rp_detail)


def _pit_role_from_row(row) -> str:
    """SP / RP classification from a single pitcher row (handles Ros_* fallbacks)."""
    _ip  = row.get("IP",  0) or row.get("Ros_IP",  0)
    _sv  = row.get("SV",  0) or row.get("Ros_SV",  0)
    _hld = row.get("HLD", 0) or row.get("Ros_HLD", 0)
    return "RP" if (_sv > 2 or _hld > 5 or _ip < 70) else "SP"


def _apply_pos(df, pos_by_playerid: dict, pos_by_mlbam: dict,
               age_by_mlbam: dict, fallback: str = POS_FALLBACK):
    """Assign Pos and Age columns using fielding-file eligibility + MLB API fallback."""
    df["MLBAMID"]  = pd.to_numeric(df["MLBAMID"], errors="coerce")
    df["PlayerId"] = df["PlayerId"].astype(str).str.strip()
    def _get_pos(row):
        fp = pos_by_playerid.get(row["PlayerId"])
        if fp: return fp
        mid = row["MLBAMID"]
        if pd.notna(mid): return pos_by_mlbam.get(int(mid), fallback)
        return fallback
    df["Pos"] = df.apply(_get_pos, axis=1)
    df["Age"] = df["MLBAMID"].apply(
        lambda mid: age_by_mlbam.get(int(mid)) if pd.notna(mid) else None)


# ─────────────────────────────────────────────────────────────────────────────
# load_roster_data  – one-time setup (CSV reads + MLB API call)
# ─────────────────────────────────────────────────────────────────────────────

def load_roster_data() -> dict:
    """
    Load roster CSVs, fetch position + age data from the MLB Stats API, and
    build multi-position eligibility from the FanGraphs fielding leaderboard.

    Returns a data-dict consumed by run_projection_system().
    """
    hit_roster = pd.read_csv(HITTER_ROSTER_FILE)
    pit_roster  = pd.read_csv(PITCHER_ROSTER_FILE)
    for df in (hit_roster, pit_roster):
        df["Team"] = df["Fantasy"].apply(extract_team)

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

    hit_roster_ids = hit_roster_full[["PlayerId", "Team", "Salary"]].copy()
    pit_roster_ids  = pit_roster_full[["PlayerId", "Team", "Salary"]].copy()

    # MLB Stats API – positions + ages
    MLB_POS_MAP = {
        "C": "C", "1B": "1B", "2B": "2B", "SS": "SS", "3B": "3B",
        "LF": "OF", "CF": "OF", "RF": "OF", "OF": "OF",
        "DH": "DH", "TWP": "DH", "P": "P",
    }
    hit_mlbam = hit_roster_full["MLBAMID"].dropna().astype(int).unique().tolist()
    pit_mlbam = pit_roster_full["MLBAMID"].dropna().astype(int).unique().tolist()
    all_ids   = list(set(hit_mlbam + pit_mlbam))
    pos_by_mlbam:  dict = {}
    age_by_mlbam:  dict = {}
    BATCH = 300
    for i in range(0, len(all_ids), BATCH):
        batch = all_ids[i: i + BATCH]
        try:
            resp = requests.get(
                "https://statsapi.mlb.com/api/v1/people",
                params={"personIds": ",".join(map(str, batch)),
                        "fields": "people,id,primaryPosition,abbreviation,currentAge"},
                timeout=20,
            )
            resp.raise_for_status()
            for person in resp.json().get("people", []):
                abbr = person.get("primaryPosition", {}).get("abbreviation", "")
                pos_by_mlbam[person["id"]] = MLB_POS_MAP.get(abbr, POS_FALLBACK)
                age_by_mlbam[person["id"]] = person.get("currentAge")
        except Exception:
            pass

    # FanGraphs fielding leaderboard – multi-position eligibility
    _FIELD_POS_ORDER = ["C", "1B", "2B", "SS", "3B", "OF", "DH"]
    _OF_SLOTS        = {"LF", "CF", "RF", "OF"}
    pos_by_playerid: dict = {}
    try:
        _field = pd.read_csv(FIELDING_FILE)
        _field["PlayerId"] = _field["PlayerId"].astype(str).str.strip()
        _field["PosGroup"] = _field["Pos"].str.upper().apply(
            lambda p: "OF" if p in _OF_SLOTS else p)
        _field = _field[~_field["PosGroup"].isin(["P", "SP", "RP"])]
        _agg   = _field.groupby(["PlayerId", "PosGroup"])["G"].sum().reset_index()
        _elig  = _agg[_agg["G"] >= 5]
        for _pid, _grp in _elig.groupby("PlayerId"):
            _positions = [p for p in _FIELD_POS_ORDER if p in _grp["PosGroup"].values]
            if _positions:
                pos_by_playerid[str(_pid)] = "/".join(_positions)
    except Exception:
        pass

    name_to_mlbam: dict = {}
    for _src in (hit_roster_full, pit_roster_full):
        for _, _r in _src[["Name", "MLBAMID"]].iterrows():
            if pd.notna(_r["MLBAMID"]):
                name_to_mlbam[_r["Name"]] = int(_r["MLBAMID"])

    return {
        "hit_roster_full":  hit_roster_full,
        "pit_roster_full":  pit_roster_full,
        "hit_roster_ids":   hit_roster_ids,
        "pit_roster_ids":   pit_roster_ids,
        "pos_by_mlbam":     pos_by_mlbam,
        "age_by_mlbam":     age_by_mlbam,
        "pos_by_playerid":  pos_by_playerid,
        "name_to_mlbam":    name_to_mlbam,
    }


# ─────────────────────────────────────────────────────────────────────────────
# run_projection_system
# ─────────────────────────────────────────────────────────────────────────────

def run_projection_system(sys_name: str, hit_proj_file: str,
                          pit_proj_file: str, roster_data: dict) -> dict:
    """
    Load & merge projections for one system, optimise lineups, return result dict.
    roster_data must come from load_roster_data().
    """
    hit_roster_full = roster_data["hit_roster_full"]
    pit_roster_full = roster_data["pit_roster_full"]
    hit_roster_ids  = roster_data["hit_roster_ids"]
    pit_roster_ids  = roster_data["pit_roster_ids"]
    pos_by_playerid = roster_data["pos_by_playerid"]
    pos_by_mlbam    = roster_data["pos_by_mlbam"]
    age_by_mlbam    = roster_data["age_by_mlbam"]
    _name_to_mlbam  = roster_data["name_to_mlbam"]

    hit_proj = pd.read_csv(hit_proj_file)
    pit_proj  = pd.read_csv(pit_proj_file)

    hit_proj["PlayerId"] = hit_proj["PlayerId"].astype(str).str.strip()
    pit_proj["PlayerId"]  = pit_proj["PlayerId"].astype(str).str.strip()
    hit_proj = hit_proj.drop(columns=["Team"], errors="ignore")
    pit_proj  = pit_proj.drop(columns=["Team"], errors="ignore")

    hit_df = hit_proj.merge(hit_roster_ids, on="PlayerId", how="inner")
    pit_df  = pit_proj.merge(pit_roster_ids,  on="PlayerId", how="inner")

    _HIT_PROJ_COLS = [c for c in ("PlayerId", "SPTS", "SPTS/G", "AB", "H", "2B",
                                   "BB", "HR", "SB", "wOBA", "OPS")
                      if c in hit_proj.columns]
    _PIT_PROJ_COLS = [c for c in ("PlayerId", "SPTS", "IP", "SV", "HLD", "GS",
                                   "SO", "BB", "HR", "FIP")
                      if c in pit_proj.columns]
    hit_full = hit_roster_full.merge(hit_proj[_HIT_PROJ_COLS], on="PlayerId", how="left")
    pit_full = pit_roster_full.merge(pit_proj[_PIT_PROJ_COLS], on="PlayerId", how="left")

    for col in ("SPTS", "SPTS/G", "AB", "H", "2B", "BB", "HR", "SB", "wOBA", "OPS"):
        hit_df[col]   = pd.to_numeric(hit_df.get(col,   0), errors="coerce").fillna(0)
        hit_full[col] = pd.to_numeric(hit_full.get(col, 0), errors="coerce").fillna(0)
    for col in ("SPTS", "IP", "SV", "HLD", "GS", "SO", "BB", "HR", "FIP"):
        pit_df[col]   = pd.to_numeric(pit_df.get(col,   0), errors="coerce").fillna(0)
        pit_full[col] = pd.to_numeric(pit_full.get(col, 0), errors="coerce").fillna(0)

    _HIT_SCALE = ["SPTS", "AB", "H", "2B", "BB", "HR", "SB"]
    _PIT_SCALE = ["SPTS", "IP", "GS", "SV", "HLD", "SO", "BB", "HR"]
    for _col in _HIT_SCALE:
        for _df in (hit_df, hit_full): _df[_col] = _df[_col] * SCHEDULE_SCALE
    for _col in _PIT_SCALE:
        for _df in (pit_df, pit_full): _df[_col] = _df[_col] * SCHEDULE_SCALE

    _apply_pos(hit_df,   pos_by_playerid, pos_by_mlbam, age_by_mlbam)
    _apply_pos(hit_full, pos_by_playerid, pos_by_mlbam, age_by_mlbam)
    hit_df   = hit_df[hit_df["Pos"] != "P"].copy()
    hit_full = hit_full[hit_full["Pos"] != "P"].copy()

    # ── Free-agent frames (projected but not rostered) ────────────────────────
    _rostered_hit_pids = set(hit_roster_ids["PlayerId"].astype(str))
    _rostered_pit_pids = set(pit_roster_ids["PlayerId"].astype(str))
    fa_hit_raw = hit_proj[~hit_proj["PlayerId"].astype(str).isin(_rostered_hit_pids)].copy()
    fa_pit_raw = pit_proj[~pit_proj["PlayerId"].astype(str).isin(_rostered_pit_pids)].copy()
    for col in ("SPTS", "SPTS/G", "AB", "H", "2B", "BB", "HR", "SB", "wOBA", "OPS"):
        fa_hit_raw[col] = pd.to_numeric(fa_hit_raw.get(col, 0), errors="coerce").fillna(0)
    for col in ("SPTS", "IP", "SV", "HLD", "GS", "SO", "BB", "HR", "FIP"):
        fa_pit_raw[col] = pd.to_numeric(fa_pit_raw.get(col, 0), errors="coerce").fillna(0)
    for col in _HIT_SCALE:
        if col in fa_hit_raw.columns: fa_hit_raw[col] = fa_hit_raw[col] * SCHEDULE_SCALE
    for col in _PIT_SCALE:
        if col in fa_pit_raw.columns: fa_pit_raw[col] = fa_pit_raw[col] * SCHEDULE_SCALE
    # Ensure FA hitters have SPTS/G — derive from projected games if the
    # projection file didn't include the column (coercion left it at all-zeros).
    if (fa_hit_raw["SPTS/G"] == 0).all() and (fa_hit_raw["SPTS"] > 0).any():
        _fa_g = pd.to_numeric(
            fa_hit_raw["G"] if "G" in fa_hit_raw.columns else MAX_PLAYER_G,
            errors="coerce",
        ).clip(lower=1).fillna(MAX_PLAYER_G)
        fa_hit_raw["SPTS/G"] = (fa_hit_raw["SPTS"] / _fa_g).replace([np.inf, -np.inf], 0)
    # Assign positions using fielding file (PlayerId-keyed); default OF for unknowns
    fa_hit_raw["Pos"] = fa_hit_raw["PlayerId"].astype(str).apply(
        lambda pid: pos_by_playerid.get(pid, "OF"))
    fa_hit_raw = fa_hit_raw[fa_hit_raw["Pos"] != "P"].copy()
    _fa_rp = (fa_pit_raw["SV"] > 2) | (fa_pit_raw["HLD"] > 5) | (fa_pit_raw["IP"] < 70)
    fa_pit_raw["Role"] = _fa_rp.map({True: "RP", False: "SP"})
    _FA_HIT_KEEP = [c for c in ["Name", "PlayerId", "Pos", "SPTS", "SPTS/G",
                                 "AB", "H", "2B", "BB", "HR", "SB", "wOBA", "OPS"]
                    if c in fa_hit_raw.columns]
    _FA_PIT_KEEP = [c for c in ["Name", "PlayerId", "Role", "SPTS", "IP", "GS",
                                 "SV", "HLD", "SO", "BB", "HR", "FIP"]
                    if c in fa_pit_raw.columns]
    fa_hit = fa_hit_raw[_FA_HIT_KEEP].sort_values("SPTS", ascending=False).reset_index(drop=True)
    fa_pit = fa_pit_raw[_FA_PIT_KEEP].sort_values("SPTS",  ascending=False).reset_index(drop=True)

    for _pf in (pit_df, pit_full):
        _pf["MLBAMID"] = pd.to_numeric(_pf["MLBAMID"], errors="coerce")
        _pf["Age"] = _pf["MLBAMID"].apply(
            lambda mid: age_by_mlbam.get(int(mid)) if pd.notna(mid) else None)

    for _pf in (pit_df, pit_full):
        if "Ros_IP" in _pf.columns:
            _eff_ip  = _pf["IP"].where(_pf["IP"]  > 0, _pf["Ros_IP"])
            _eff_sv  = _pf["SV"].where(_pf["SV"]  > 0, _pf["Ros_SV"])
            _eff_hld = _pf["HLD"].where(_pf["HLD"] > 0, _pf["Ros_HLD"])
        else:
            _eff_ip, _eff_sv, _eff_hld = _pf["IP"], _pf["SV"], _pf["HLD"]
        _pf["Role"] = (((_eff_sv > 2) | (_eff_hld > 5) | (_eff_ip < 70))
                       .map({True: "RP", False: "SP"}))

    teams = sorted(hit_df["Team"].unique())
    results, team_lineups, team_pitching = [], {}, {}
    roster_records: list = []

    _hit_proj_pids = set(hit_proj["PlayerId"].astype(str))
    _pit_proj_pids = set(pit_proj["PlayerId"].astype(str))
    _twoway_proj   = _hit_proj_pids & _pit_proj_pids

    for team in teams:
        roster   = hit_df[hit_df["Team"] == team][
            ["Name", "Pos", "SPTS", "SPTS/G", "AB", "H", "2B", "BB", "HR", "SB", "wOBA", "OPS"]
        ].copy()
        hit_spts, hit_ab, hit_h, hit_2b, hit_bb, hit_hr, hit_sb, hit_woba, hit_ops, lineup = \
            optimal_lineup_spts(roster)
        team_lineups[team] = lineup

        pit_roster_t = pit_df[pit_df["Team"] == team].copy()
        sp_spts, rp_spts, sv_constrained, hld_constrained, pit_k, pit_bb, pit_hr, pit_fip, \
            sp_det, rp_det = constrained_pitcher_spts(pit_roster_t)
        team_pitching[team] = (sp_det, rp_det)

        used_spts: dict = {}
        for lrow in lineup:
            used_spts[lrow["Name"]] = used_spts.get(lrow["Name"], 0.0) + lrow["SPTS"]
        for prow in sp_det:
            used_spts[prow["Name"]] = used_spts.get(prow["Name"], 0.0) + prow["SPTS_used"]
        for prow in rp_det:
            used_spts[prow["Name"]] = used_spts.get(prow["Name"], 0.0) + prow["SPTS_used"]

        team_hit_full = hit_full[hit_full["Team"] == team]
        team_pit_full = pit_full[pit_full["Team"] == team]

        hit_pids    = set(team_hit_full["PlayerId"].astype(str))
        pit_pids    = set(team_pit_full["PlayerId"].astype(str))
        twoway_pids = (hit_pids & pit_pids) & _twoway_proj

        pit_sal_df = team_pit_full[~team_pit_full["PlayerId"].astype(str).isin(twoway_pids)]

        sal_hit = float(team_hit_full["Salary"].sum())
        # Role column was already vectorised on pit_full before this loop
        sal_sp  = float(pit_sal_df[pit_sal_df["Role"] == "SP"]["Salary"].sum())
        sal_rp  = float(pit_sal_df[pit_sal_df["Role"] == "RP"]["Salary"].sum())
        age_hit = team_hit_full[["PlayerId", "Age"]].drop_duplicates("PlayerId")
        age_pit = pit_sal_df[["PlayerId", "Age"]].drop_duplicates("PlayerId")
        all_ages = pd.concat([age_hit["Age"], age_pit["Age"]]).dropna()
        avg_age  = round(float(all_ages.mean()), 1) if len(all_ages) > 0 else 0.0

        for _, p in team_hit_full.iterrows():
            pid  = str(p.get("PlayerId", ""))
            role = "TWP" if pid in twoway_pids else "H"
            raw_pos     = p.get("Pos", "?")
            display_pos = "Util" if raw_pos == "DH" else raw_pos
            _mid        = p.get("MLBAMID")
            roster_records.append({
                "Team":    team,   "Name":    p["Name"],
                "Role":    role,   "Pos":     display_pos,
                "Salary":  p.get("Salary", 0),
                "Age":     p.get("Age"),
                "MLBAMID": int(_mid) if pd.notna(_mid) else None,
                "SPTS":    round(used_spts.get(p["Name"], 0.0), 1),
            })
        for _, p in team_pit_full.iterrows():
            if str(p.get("PlayerId", "")) in twoway_pids:
                continue
            role = p.get("Role") or _pit_role_from_row(p)
            _mid = p.get("MLBAMID")
            roster_records.append({
                "Team":    team,   "Name":    p["Name"],
                "Role":    role,   "Pos":     role,
                "Salary":  p.get("Salary", 0),
                "Age":     p.get("Age"),
                "MLBAMID": int(_mid) if pd.notna(_mid) else None,
                "SPTS":    round(used_spts.get(p["Name"], 0.0), 1),
            })

        raw_hit = hit_df[hit_df["Team"] == team]["SPTS"].sum()
        raw_pit = pit_df[pit_df["Team"] == team]["SPTS"].sum()
        sp_ip_used = sum(p["IP_used"] for p in sp_det)
        rp_ip_used = sum(p["IP_used"] for p in rp_det)

        results.append({
            "Team":         team,
            "Hit_SPTS":     round(hit_spts, 1),
            "SP_SPTS":      round(sp_spts, 1),
            "RP_SPTS":      round(rp_spts, 1),
            "Pit_SPTS":     round(sp_spts + rp_spts, 1),
            "Total_SPTS":   round(hit_spts + sp_spts + rp_spts, 1),
            "Hit_AB":       round(hit_ab, 1),  "Hit_H":    round(hit_h, 1),
            "Hit_2B":       round(hit_2b, 1),  "Hit_BB":   round(hit_bb, 1),
            "Hit_HR":       round(hit_hr, 1),  "Hit_SB":   round(hit_sb, 1),
            "Hit_wOBA":     round(hit_woba, 3),"Hit_OPS":  round(hit_ops, 3),
            "Pit_IP":       round(sp_ip_used + rp_ip_used, 1),
            "SV":           sv_constrained,    "HLD":       hld_constrained,
            "Pit_K":        pit_k,             "Pit_BB":    pit_bb,
            "Pit_HR":       pit_hr,            "Pit_FIP":   pit_fip,
            "SP_IP":        round(sp_ip_used, 1),
            "RP_IP":        round(rp_ip_used, 1),
            "Total_IP":     round(sp_ip_used + rp_ip_used, 1),
            "Raw_Hit":      round(raw_hit, 1), "Raw_Pit":   round(raw_pit, 1),
            "Raw_Total":    round(raw_hit + raw_pit, 1),
            "N_Bat":        len(team_hit_full),"N_Pit":     len(team_pit_full),
            "Total_Salary": round(sal_hit + sal_sp + sal_rp, 0),
            "Sal_Hit":      round(sal_hit, 0), "Sal_SP":    round(sal_sp, 0),
            "Sal_RP":       round(sal_rp, 0),  "Avg_Age":   avg_age,
        })

    roster_df = pd.DataFrame(roster_records)
    rankings  = (pd.DataFrame(results)
                 .sort_values("Total_SPTS", ascending=False)
                 .reset_index(drop=True))
    rankings.index += 1
    rankings_with_rank = rankings.reset_index().rename(columns={"index": "Rank"})
    team_rank = {v: k for k, v in rankings["Team"].to_dict().items()}

    lineup_records = []
    for _team, _lineup in team_lineups.items():
        for _row in _lineup:
            lineup_records.append({
                "Rank": team_rank.get(_team), "Team": _team,
                "MLBAMID": _name_to_mlbam.get(_row["Name"]), **_row,
            })
    sp_records, rp_records = [], []
    for _team, (_sp_det, _rp_det) in team_pitching.items():
        for _row in _sp_det:
            sp_records.append({"Rank": team_rank.get(_team), "Team": _team,
                                "MLBAMID": _name_to_mlbam.get(_row["Name"]), **_row})
        for _row in _rp_det:
            rp_records.append({"Rank": team_rank.get(_team), "Team": _team,
                                "MLBAMID": _name_to_mlbam.get(_row["Name"]), **_row})

    hitters_df = pd.DataFrame(lineup_records)
    sp_df_out  = pd.DataFrame(sp_records)
    rp_df_out  = pd.DataFrame(rp_records)

    return {
        "sys_name":  sys_name,
        "rankings":  rankings_with_rank,
        "hitters":   hitters_df,
        "sp":        sp_df_out,
        "rp":        rp_df_out,
        "roster":    roster_df,
        "hit_full":  hit_full,
        "pit_full":  pit_full,
        "fa_hit":    fa_hit,
        "fa_pit":    fa_pit,
        "lineups":   team_lineups,
        "pitching":  team_pitching,
    }


# ─────────────────────────────────────────────────────────────────────────────
# main – only runs when executed directly
# ─────────────────────────────────────────────────────────────────────────────

def main():
    roster_data = load_roster_data()

    all_results: dict = {}
    for _sys_name, (_hit_file, _pit_file) in PROJ_SYSTEMS.items():
        all_results[_sys_name] = run_projection_system(
            _sys_name, _hit_file, _pit_file, roster_data)

    # Save CSV (first system only for quick spreadsheet access)
    _first_data = next(iter(all_results.values()))
    _first_data["rankings"].to_csv(OUT_PATH, index=False)

    with pd.ExcelWriter(OUT_XLSX) as writer:
        for _sys_name, _data in all_results.items():
            _pfx = _sys_name.replace(" ", "_")

            _data["rankings"].to_excel(writer, sheet_name=f"{_pfx}_Summary", index=False)

            if not _data["hitters"].empty:
                _data["hitters"].sort_values(
                    ["Rank", "Slot", "SPTS/G"], ascending=[True, True, False]
                ).to_excel(writer, sheet_name=f"{_pfx}_Hitters", index=False)

            if not _data["sp"].empty:
                _data["sp"].sort_values(
                    ["Rank", "SPTS_used"], ascending=[True, False]
                ).to_excel(writer, sheet_name=f"{_pfx}_SP_Detail", index=False)

            if not _data["rp"].empty:
                _data["rp"].sort_values(
                    ["Rank", "SPTS_used"], ascending=[True, False]
                ).to_excel(writer, sheet_name=f"{_pfx}_RP_Detail", index=False)

            if not _data["roster"].empty:
                _ros = _data["roster"].copy()
                _ros["SPTS_per_$"] = _ros.apply(
                    lambda r: round(float(r["SPTS"]) / float(r["Salary"]), 2)
                    if r.get("Salary", 0) > 0 else 0.0, axis=1)
                _ros.sort_values(["Team", "Salary"], ascending=[True, False]).to_excel(
                    writer, sheet_name=f"{_pfx}_Roster", index=False)

            # Trade-machine source sheets
            if not _data["hit_full"].empty:
                _data["hit_full"].to_excel(
                    writer, sheet_name=f"{_pfx}_Hit_Roster", index=False)
            if not _data["pit_full"].empty:
                _data["pit_full"].to_excel(
                    writer, sheet_name=f"{_pfx}_Pit_Roster", index=False)
            if not _data["fa_hit"].empty:
                _data["fa_hit"].to_excel(
                    writer, sheet_name=f"{_pfx}_FA_Hit", index=False)
            if not _data["fa_pit"].empty:
                _data["fa_pit"].to_excel(
                    writer, sheet_name=f"{_pfx}_FA_Pit", index=False)


if __name__ == "__main__":
    main()
