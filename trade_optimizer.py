"""
trade_optimizer.py
==================
Finds the most synergistic trades between two Ottoneu teams.

Synergy is defined as:
    ΔA + ΔB  =  (new_spts_a − old_spts_a) + (new_spts_b − old_spts_b)

Net gain can be positive because positional / cap constraints mean the same
player may be worth more to one team than another:
  • A slugger rotting behind a crowded 1B/Util depth gains full OF slot value elsewhere
  • An 11th SP past the 210-start cap contributes 0 to the sender, full value at receiver

Usage (standalone):
    python trade_optimizer.py

Usage (imported by dashboard):
    from trade_optimizer import find_optimal_trades
"""

from __future__ import annotations

import time
import warnings
from itertools import combinations
from typing import Optional

import numpy as np
import pandas as pd

from ottoneu_power_rankings import optimal_lineup_spts, constrained_pitcher_spts

warnings.filterwarnings("ignore")

# ── Constants ─────────────────────────────────────────────────────────────────
SALARY_TOLERANCE  = 3      # max $ imbalance allowed per side
APPROX_PREFILTER  = 75     # max candidates to validate with full optimizer
MAX_TRADE_SIZE    = 3      # hard cap: players per side
POOL_TOP_N        = 25     # only consider each team's top-N players by SPTS
GEN_CAP           = 5_000  # stop generating combos after this many salary-valid hits
_SLOT_G_CAP       = 140    # copy of pipeline constant (avoid circular import)
_HIT_KEEP = ["Name", "Pos", "SPTS", "SPTS/G", "AB", "H", "2B",
             "BB", "HR", "SB", "wOBA", "OPS"]
_PIT_KEEP_NUM = ["SV", "HLD", "IP", "GS", "SPTS", "SO", "BB", "HR", "FIP"]


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _coerce_num(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)
        else:
            df[c] = 0.0
    return df


def _team_spts_exact(team: str, hf: pd.DataFrame, pf: pd.DataFrame) -> float:
    """Run full lineup optimiser for one team, return total SPTS."""
    rh = hf[hf["Team"] == team][[c for c in _HIT_KEEP if c in hf.columns]].copy()
    rh = _coerce_num(rh, ["SPTS", "SPTS/G"])
    # Fix missing SPTS/G for FA additions
    _mask = (rh["SPTS/G"] == 0) & (rh["SPTS"] > 0)
    if _mask.any():
        rh.loc[_mask, "SPTS/G"] = rh.loc[_mask, "SPTS"] / _SLOT_G_CAP

    rp = pf[pf["Team"] == team].copy()
    rp = _coerce_num(rp, _PIT_KEEP_NUM + ["Ros_IP", "Ros_SV", "Ros_HLD"])

    hit, *_ = optimal_lineup_spts(rh)
    sp, rp_s, *_ = constrained_pitcher_spts(rp)
    return round(hit + sp + rp_s, 2)


def _build_player_pool(team: str, hf: pd.DataFrame, pf: pd.DataFrame) -> pd.DataFrame:
    """
    Return a unified player table for one team with columns:
    Name, Type (H/SP/RP), SPTS, Salary, _used_spts.
    _used_spts is filled from the optimizer detail if available, else SPTS.
    """
    rows = []
    # Hitters
    th = hf[hf["Team"] == team].copy()
    th = _coerce_num(th, ["SPTS", "SPTS/G", "Salary"])
    for _, r in th.iterrows():
        rows.append({
            "Name":   r["Name"],
            "Type":   "H",
            "Pos":    r.get("Pos", "?"),
            "SPTS":   float(r["SPTS"]),
            "Salary": float(r.get("Salary", 0)),
        })
    # Pitchers
    tp = pf[pf["Team"] == team].copy()
    tp = _coerce_num(tp, ["SPTS", "IP", "SV", "HLD", "Salary",
                           "Ros_IP", "Ros_SV", "Ros_HLD"])
    for _, r in tp.iterrows():
        _ip  = r["IP"]  or r.get("Ros_IP",  0)
        _sv  = r["SV"]  or r.get("Ros_SV",  0)
        _hld = r["HLD"] or r.get("Ros_HLD", 0)
        typ  = "RP" if (_sv > 2 or _hld > 5 or _ip < 70) else "SP"
        rows.append({
            "Name":   r["Name"],
            "Type":   typ,
            "Pos":    typ,
            "SPTS":   float(r["SPTS"]),
            "Salary": float(r.get("Salary", 0)),
        })
    return pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=["Name", "Type", "Pos", "SPTS", "Salary"])


def _apply_swap(hf: pd.DataFrame, pf: pd.DataFrame,
                team_a: str, give_a: list[str],
                team_b: str, give_b: list[str]
                ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Swap give_a → team_b and give_b → team_a on copies of hf/pf."""
    hf = hf.copy(); pf = pf.copy()
    for _names, _frm, _to in [(give_a, team_a, team_b), (give_b, team_b, team_a)]:
        hf.loc[(hf["Name"].isin(_names)) & (hf["Team"] == _frm), "Team"] = _to
        pf.loc[(pf["Name"].isin(_names)) & (pf["Team"] == _frm), "Team"] = _to
    return hf, pf


def _approx_score(give_a: list[dict], give_b: list[dict]) -> float:
    """
    Fast approximation of total synergy:  what each side gains in raw SPTS.
    Useful only for pre-filtering — does not account for positional constraints.
    """
    # Each side's approximate gain = sum SPTS received − sum SPTS given
    # Total net ≈ 0 in raw terms, so we score by |individual gains| to surface
    # imbalanced swaps of surplus players.  Better metric: the minimum of the
    # two unilateral deltas (both teams must benefit or at worst be neutral).
    raw_a = sum(p["SPTS"] for p in give_b) - sum(p["SPTS"] for p in give_a)
    raw_b = sum(p["SPTS"] for p in give_a) - sum(p["SPTS"] for p in give_b)
    # Prioritise trades where the worse-off team loses as little as possible
    return min(raw_a, raw_b)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def find_optimal_trades(
    team_a: str,
    team_b: str,
    hit_full: pd.DataFrame,
    pit_full: pd.DataFrame,
    summary: pd.DataFrame,
    max_players: int = 2,
    top_n: int = 10,
    salary_tol: float = SALARY_TOLERANCE,
    progress_cb=None,          # optional callable(pct_float, message_str)
) -> list[dict]:
    """
    Search all salary-balanced trade combos between team_a and team_b,
    score by total SPTS synergy (Δa + Δb), return top_n ranked results.

    Each result dict:
      give_a      – list of player names team_a sends
      give_b      – list of player names team_b sends
      spts_a_before / spts_a_after / delta_a
      spts_b_before / spts_b_after / delta_b
      total_delta – Δa + Δb  (the primary sort key)
      sal_a       – salary of players team_a sends
      sal_b       – salary of players team_b sends
      sal_diff    – sal_a − sal_b
    """
    max_players = min(max_players, MAX_TRADE_SIZE)
    t0 = time.time()

    # ── Baseline ──────────────────────────────────────────────────────────────
    baseline_a = _team_spts_exact(team_a, hit_full, pit_full)
    baseline_b = _team_spts_exact(team_b, hit_full, pit_full)

    if progress_cb:
        progress_cb(0.05, "Baselines computed")

    # ── Player pools ──────────────────────────────────────────────────────────
    pool_a = _build_player_pool(team_a, hit_full, pit_full)
    pool_b = _build_player_pool(team_b, hit_full, pit_full)

    if pool_a.empty or pool_b.empty:
        return []

    pool_a_list = pool_a.sort_values("SPTS", ascending=False).head(POOL_TOP_N).to_dict("records")
    pool_b_list = pool_b.sort_values("SPTS", ascending=False).head(POOL_TOP_N).to_dict("records")

    # ── Generate salary-valid combinations ────────────────────────────────────
    all_candidates: list[dict] = []
    _gen_count = 0
    _gen_limit_hit = False

    for k_a in range(1, max_players + 1):
        for k_b in range(1, max_players + 1):
            if _gen_limit_hit:
                break
            for combo_a in combinations(pool_a_list, k_a):
                if _gen_limit_hit:
                    break
                sal_a = sum(p["Salary"] for p in combo_a)
                for combo_b in combinations(pool_b_list, k_b):
                    sal_b = sum(p["Salary"] for p in combo_b)
                    if abs(sal_a - sal_b) > salary_tol:
                        continue
                    all_candidates.append({
                        "give_a":   list(combo_a),
                        "give_b":   list(combo_b),
                        "sal_a":    round(sal_a, 0),
                        "sal_b":    round(sal_b, 0),
                        "sal_diff": round(sal_a - sal_b, 0),
                        "_approx":  _approx_score(list(combo_a), list(combo_b)),
                    })
                    _gen_count += 1
                    if _gen_count >= GEN_CAP:
                        _gen_limit_hit = True
                        break

    if not all_candidates:
        return []

    if progress_cb:
        progress_cb(0.15, f"{len(all_candidates):,} salary-valid combos found")

    # ── Pre-filter to top APPROX_PREFILTER by approximate score ───────────────
    all_candidates.sort(key=lambda c: c["_approx"], reverse=True)
    to_validate = all_candidates[:APPROX_PREFILTER]

    # ── Full optimizer validation ─────────────────────────────────────────────
    scored: list[dict] = []
    n = len(to_validate)

    for i, cand in enumerate(to_validate):
        names_a = [p["Name"] for p in cand["give_a"]]
        names_b = [p["Name"] for p in cand["give_b"]]

        hf2, pf2 = _apply_swap(hit_full, pit_full,
                                team_a, names_a,
                                team_b, names_b)

        new_a = _team_spts_exact(team_a, hf2, pf2)
        new_b = _team_spts_exact(team_b, hf2, pf2)

        delta_a = round(new_a - baseline_a, 1)
        delta_b = round(new_b - baseline_b, 1)
        total   = round(delta_a + delta_b, 1)

        # Both teams must not lose more than their own total SPTS (sanity)
        scored.append({
            "give_a":        names_a,
            "give_b":        names_b,
            "spts_a_before": baseline_a,
            "spts_a_after":  new_a,
            "delta_a":       delta_a,
            "spts_b_before": baseline_b,
            "spts_b_after":  new_b,
            "delta_b":       delta_b,
            "total_delta":   total,
            "sal_a":         cand["sal_a"],
            "sal_b":         cand["sal_b"],
            "sal_diff":      cand["sal_diff"],
        })

        if progress_cb and i % max(1, n // 20) == 0:
            pct = 0.15 + 0.82 * (i / n)
            elapsed = time.time() - t0
            progress_cb(pct, f"Evaluated {i+1}/{n} combos ({elapsed:.1f}s)")

    # ── Rank and return top_n ─────────────────────────────────────────────────
    scored.sort(key=lambda r: r["total_delta"], reverse=True)

    if progress_cb:
        progress_cb(1.0, f"Done — {len(scored)} trades evaluated in {time.time()-t0:.1f}s")

    return scored[:top_n]


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (dev / debug)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from pathlib import Path
    import pandas as pd

    XLSX = Path(r"C:\Users\Rachel\Desktop\Scripts\ottoneu_power_rankings.xlsx")
    SYS  = "ZiPS_DC"       # sheet prefix

    def _rd(sheet):
        try:
            return pd.read_excel(XLSX, sheet_name=sheet)
        except Exception:
            return pd.DataFrame()

    hit_full = _rd(f"{SYS}_Hit_Roster")
    pit_full = _rd(f"{SYS}_Pit_Roster")
    summary  = _rd(f"{SYS}_Summary")

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
    for df in (hit_full, pit_full, summary):
        if "Team" in df.columns:
            df["Team"] = df["Team"].replace(TEAM_NAMES)

    TEAM_A = "Even Baked Alaska"
    TEAM_B = "The Big Luzinski"

    def progress(pct, msg):
        bar = "█" * int(pct * 30) + "░" * (30 - int(pct * 30))
        print(f"  [{bar}] {pct*100:.0f}%  {msg}")

    print(f"\nOptimising trades: {TEAM_A}  ⇄  {TEAM_B}\n")
    results = find_optimal_trades(
        TEAM_A, TEAM_B, hit_full, pit_full, summary,
        max_players=2, top_n=10, progress_cb=progress,
    )

    print(f"\n{'='*72}")
    print(f"  TOP TRADES — {TEAM_A} ⇄ {TEAM_B}")
    print(f"{'='*72}")
    for i, r in enumerate(results, 1):
        a_gives = ", ".join(r["give_a"])
        b_gives = ", ".join(r["give_b"])
        sign_a  = "+" if r["delta_a"] >= 0 else ""
        sign_b  = "+" if r["delta_b"] >= 0 else ""
        print(
            f"#{i:>2}  {a_gives:30s}  ⇄  {b_gives:30s}\n"
            f"      ΔA={sign_a}{r['delta_a']:.1f}  ΔB={sign_b}{r['delta_b']:.1f}  "
            f"Total={'+' if r['total_delta']>=0 else ''}{r['total_delta']:.1f}  "
            f"Salary: ${r['sal_a']:.0f} ⇄ ${r['sal_b']:.0f} (diff ${r['sal_diff']:+.0f})\n"
        )
