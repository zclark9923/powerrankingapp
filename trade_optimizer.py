"""
trade_optimizer.py  (v2)
========================
Three-stage pipeline for finding synergistic Ottoneu trades.

  Stage 1  Precompute per-team marginal player values  (fast, sub-second)
  Stage 2  Beam search: generate & rank candidates with marginals  (< half budget)
  Stage 3  Exact scoring: re-run full lineup solver on top EXACT_CAP  (rest of budget)

Synergy is ΔA + ΔB  =  (new_spts_a − old_spts_a) + (new_spts_b − old_spts_b).
Net gain can be positive because positional / cap constraints mean the same
player is worth more to one team than another.

Usage (standalone):
    python trade_optimizer.py

Usage (imported):
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
SALARY_TOLERANCE = 3       # default max $ imbalance between the two trade sides
MAX_TRADE_SIZE   = 5       # hard cap: players per side
POOL_TOP_N       = 30      # top-N players per team to consider (by SPTS)
BEAM_WIDTH       = 80      # beam search width: keep this many seeds at each level
EXACT_CAP        = 120     # max candidates forwarded to the exact scorer
RAW_SPTS_GATE    = 20      # reject combos where either side loses > X raw SPTS
_SLOT_G_CAP      = 140     # hitter games cap (mirrors pipeline constant)

# Estimated optimal lineup capacities used for marginal approximation.
_HIT_LINEUP_N = 12         # hitters who typically crack the optimal lineup
_SP_LINEUP_N  = 6          # SP starters typically scoring
_RP_LINEUP_N  = 6          # RP reliever slots

_HIT_KEEP = ["Name", "Pos", "SPTS", "SPTS/G", "AB", "H", "2B",
             "BB", "HR", "SB", "wOBA", "OPS"]
_PIT_KEEP_NUM = ["SV", "HLD", "IP", "GS", "SPTS", "SO", "BB", "HR", "FIP"]


# ─────────────────────────────────────────────────────────────────────────────
# Low-level helpers
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
    _mask = (rh["SPTS/G"] == 0) & (rh["SPTS"] > 0)
    if _mask.any():
        rh.loc[_mask, "SPTS/G"] = rh.loc[_mask, "SPTS"] / _SLOT_G_CAP

    rp = pf[pf["Team"] == team].copy()
    rp = _coerce_num(rp, _PIT_KEEP_NUM + ["Ros_IP", "Ros_SV", "Ros_HLD"])

    hit, *_ = optimal_lineup_spts(rh)
    sp, rp_s, *_ = constrained_pitcher_spts(rp)
    return round(hit + sp + rp_s, 2)


def _build_player_pool(team: str, hf: pd.DataFrame, pf: pd.DataFrame) -> pd.DataFrame:
    """Build a unified player table (Name, Type, Pos, SPTS, Salary) for one team."""
    rows = []
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
                team_b: str, give_b: list[str],
                ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Swap give_a → team_b and give_b → team_a on copies of hf/pf."""
    hf = hf.copy(); pf = pf.copy()
    for _names, _frm, _to in [(give_a, team_a, team_b), (give_b, team_b, team_a)]:
        hf.loc[(hf["Name"].isin(_names)) & (hf["Team"] == _frm), "Team"] = _to
        pf.loc[(pf["Name"].isin(_names)) & (pf["Team"] == _frm), "Team"] = _to
    return hf, pf


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Marginal value approximation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_marginals(pool_df: pd.DataFrame) -> dict[str, float]:
    """
    Estimate each player's marginal lineup contribution for their current team.

    Players ranked above the estimated lineup cutoff contribute close to their
    full projected SPTS (slight depth discount since starter > backup).
    Players below the cutoff are surplus/bench-warmers and contribute little.

    This is O(N) and replaces N+1 exact solver calls.
    """
    marginals: dict[str, float] = {}
    for ptype, cutoff in [("H", _HIT_LINEUP_N), ("SP", _SP_LINEUP_N),
                          ("RP", _RP_LINEUP_N)]:
        sub = (pool_df[pool_df["Type"] == ptype]
               .sort_values("SPTS", ascending=False)
               .reset_index(drop=True))
        for rank, row in sub.iterrows():
            spts = float(row["SPTS"])
            if rank < cutoff:
                # In/near optimal lineup — contribution tapers slightly with depth.
                # Rank 0 → 100 %, rank cutoff-1 → 70 %.
                frac = 1.0 - 0.30 * (rank / max(cutoff - 1, 1))
            else:
                # Below the lineup cutoff — mostly replaceable.
                frac = max(0.05, 0.25 * (cutoff / (rank + 1)))
            marginals[str(row["Name"])] = spts * frac
    return marginals


def _approx_trade_score(
    give: list[dict],
    receive: list[dict],
    marginals_giver: dict[str, float],
) -> float:
    """
    Approximate SPTS delta for the team that sends `give` and receives `receive`.

    Cost = sum of marginal contributions the team loses.
    Gain = sum of raw SPTS of players received (we don't yet know how they fit
           the receiving lineup — the exact scorer resolves that).
    """
    cost = sum(marginals_giver.get(p["Name"], p["SPTS"] * 0.5) for p in give)
    gain = sum(p["SPTS"] for p in receive)
    return gain - cost


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Beam-search candidate generation
# ─────────────────────────────────────────────────────────────────────────────

def _generate_candidates(
    pool_a: list[dict],
    pool_b: list[dict],
    marginals_a: dict[str, float],
    marginals_b: dict[str, float],
    max_players: int,
    salary_tol: float,
    untouchables_a: set[str],
    untouchables_b: set[str],
) -> list[dict]:
    """
    Generate trade candidates via beam search.

    Level 1: all salary-valid 1-for-1 pairs.
    Asymmetric: extend the best Level-1 seeds on one side to produce 1-for-2
                and 2-for-1 packages.
    Level k: extend the top-BEAM_WIDTH seeds from level k-1 by adding one
             player to each side simultaneously (→ k-for-k coverage).

    All candidates scored by min(approx_δa, approx_δb); beam retains the
    trades most likely to benefit both sides.

    Returns candidates sorted by approx_min descending.
    """
    seen: set[tuple] = set()
    candidates: list[dict] = []

    # Remove untouchable players from the tradeable pools
    pool_a = [p for p in pool_a if p["Name"] not in untouchables_a]
    pool_b = [p for p in pool_b if p["Name"] not in untouchables_b]

    def _cand_key(ga: list[dict], gb: list[dict]) -> tuple:
        return (frozenset(p["Name"] for p in ga),
                frozenset(p["Name"] for p in gb))

    def _try_add(ga: list[dict], gb: list[dict]) -> Optional[dict]:
        key = _cand_key(ga, gb)
        if key in seen:
            return None
        seen.add(key)

        sal_a = sum(p["Salary"] for p in ga)
        sal_b = sum(p["Salary"] for p in gb)
        if abs(sal_a - sal_b) > salary_tol:
            return None

        spts_ga = sum(p["SPTS"] for p in ga)
        spts_gb = sum(p["SPTS"] for p in gb)
        if (spts_gb - spts_ga) < -RAW_SPTS_GATE:   # A loses too much raw SPTS
            return None
        if (spts_ga - spts_gb) < -RAW_SPTS_GATE:   # B loses too much raw SPTS
            return None

        da = _approx_trade_score(ga, gb, marginals_a)
        db = _approx_trade_score(gb, ga, marginals_b)
        # Pre-filter: skip only if BOTH sides look clearly negative
        if da < 0 and db < 0:
            return None

        cand = {
            "give_a":     ga,
            "give_b":     gb,
            "sal_a":      round(sal_a, 0),
            "sal_b":      round(sal_b, 0),
            "sal_diff":   round(sal_a - sal_b, 0),
            "approx_da":  da,
            "approx_db":  db,
            "approx_min": min(da, db),
        }
        candidates.append(cand)
        return cand

    # ── Level 1: all 1-for-1 pairs ────────────────────────────────────────────
    level1: list[dict] = []
    for pa in pool_a:
        for pb in pool_b:
            c = _try_add([pa], [pb])
            if c:
                level1.append(c)

    if not level1:
        return []

    level1.sort(key=lambda c: c["approx_min"], reverse=True)
    beam = level1[:BEAM_WIDTH]

    # ── Asymmetric extensions from best Level-1 seeds ─────────────────────────
    # 1-for-2 (team_a gives 1, receives 2) and 2-for-1 vice-versa.
    if max_players >= 2:
        seed_limit = min(30, len(beam))
        for seed in beam[:seed_limit]:
            names_a = {p["Name"] for p in seed["give_a"]}
            names_b = {p["Name"] for p in seed["give_b"]}
            for pb2 in pool_b:
                if pb2["Name"] not in names_b:
                    _try_add(seed["give_a"], seed["give_b"] + [pb2])
            for pa2 in pool_a:
                if pa2["Name"] not in names_a:
                    _try_add(seed["give_a"] + [pa2], seed["give_b"])

    # ── Level k: extend beam to k-for-k ───────────────────────────────────────
    for _level in range(2, max_players + 1):
        next_level: list[dict] = []
        for seed in beam:
            # Only extend seeds that are still at level k-1 on both sides
            if len(seed["give_a"]) != _level - 1 or len(seed["give_b"]) != _level - 1:
                continue
            names_a = {p["Name"] for p in seed["give_a"]}
            names_b = {p["Name"] for p in seed["give_b"]}
            for pa in pool_a:
                if pa["Name"] in names_a:
                    continue
                for pb in pool_b:
                    if pb["Name"] in names_b:
                        continue
                    c = _try_add(seed["give_a"] + [pa], seed["give_b"] + [pb])
                    if c:
                        next_level.append(c)

        if not next_level:
            break
        # Merge new level into beam and keep best BEAM_WIDTH as seeds for next level
        next_level.sort(key=lambda c: c["approx_min"], reverse=True)
        beam = (beam + next_level)
        beam.sort(key=lambda c: c["approx_min"], reverse=True)
        beam = beam[:BEAM_WIDTH]

    candidates.sort(key=lambda c: c["approx_min"], reverse=True)
    return candidates


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
    time_budget: float = 20.0,
    progress_cb=None,
    untouchables_a: list[str] | None = None,
    untouchables_b: list[str] | None = None,
) -> list[dict]:
    """
    Find the most synergistic trades between team_a and team_b.

    Three-stage pipeline:
      Stage 1  Compute marginal player values per team  (fast approximation)
      Stage 2  Beam search to generate & rank candidate packages  (marginal scoring)
      Stage 3  Exact lineup re-solve on top EXACT_CAP candidates  (ground truth)

    Always returns best-so-far if the deadline arrives mid-validation.

    Parameters
    ----------
    untouchables_a : player names on team_a that must not be included in any trade
    untouchables_b : player names on team_b that must not be included in any trade

    Each result dict:
      give_a      – list of player names team_a sends
      give_b      – list of player names team_b sends
      spts_a_before / spts_a_after / delta_a
      spts_b_before / spts_b_after / delta_b
      total_delta – delta_a + delta_b  (primary sort key)
      sal_a / sal_b / sal_diff
    """
    max_players = min(max_players, MAX_TRADE_SIZE)
    t0       = time.time()
    deadline = t0 + time_budget

    def _elapsed() -> float:
        return time.time() - t0

    # ── Baselines ─────────────────────────────────────────────────────────────
    baseline_a = _team_spts_exact(team_a, hit_full, pit_full)
    baseline_b = _team_spts_exact(team_b, hit_full, pit_full)
    if progress_cb:
        progress_cb(0.05, f"Baselines computed ({_elapsed():.1f}s)")

    # ── Stage 1: build pools and marginals ────────────────────────────────────
    pool_a_df = _build_player_pool(team_a, hit_full, pit_full)
    pool_b_df = _build_player_pool(team_b, hit_full, pit_full)

    if pool_a_df.empty or pool_b_df.empty:
        return []

    pool_a_list = (pool_a_df.sort_values("SPTS", ascending=False)
                   .head(POOL_TOP_N).to_dict("records"))
    pool_b_list = (pool_b_df.sort_values("SPTS", ascending=False)
                   .head(POOL_TOP_N).to_dict("records"))

    marginals_a = _compute_marginals(pool_a_df)
    marginals_b = _compute_marginals(pool_b_df)

    if progress_cb:
        progress_cb(0.10, f"Marginals computed ({_elapsed():.1f}s)")

    # ── Stage 2: beam-search candidate generation ─────────────────────────────
    all_candidates = _generate_candidates(
        pool_a_list, pool_b_list,
        marginals_a, marginals_b,
        max_players, salary_tol,
        untouchables_a=set(untouchables_a or []),
        untouchables_b=set(untouchables_b or []),
    )

    if not all_candidates:
        return []

    if progress_cb:
        progress_cb(0.20, f"{len(all_candidates):,} candidates found ({_elapsed():.1f}s)")

    # ── Stage 3: exact scoring on the top shortlist ───────────────────────────
    # Shortlist is pre-sorted by approx_min so we try the most promising first
    # and stop at the deadline, returning best-so-far.
    shortlist = all_candidates[:EXACT_CAP]
    scored: list[dict] = []
    n = len(shortlist)

    for i, cand in enumerate(shortlist):
        if time.time() >= deadline:
            break

        names_a = [p["Name"] for p in cand["give_a"]]
        names_b = [p["Name"] for p in cand["give_b"]]

        hf2, pf2 = _apply_swap(hit_full, pit_full, team_a, names_a, team_b, names_b)
        new_a = _team_spts_exact(team_a, hf2, pf2)
        new_b = _team_spts_exact(team_b, hf2, pf2)

        delta_a = round(new_a - baseline_a, 1)
        delta_b = round(new_b - baseline_b, 1)
        total   = round(delta_a + delta_b, 1)

        # Only keep trades where both sides gain (or break even)
        if delta_a < 0 or delta_b < 0:
            continue

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

        if progress_cb and (i % max(1, n // 20) == 0):
            pct = 0.20 + 0.78 * (i / n)
            progress_cb(pct, f"Exact scored {i+1}/{n}  ({_elapsed():.1f}s)")

    # ── Rank and return ───────────────────────────────────────────────────────
    scored.sort(key=lambda r: r["total_delta"], reverse=True)

    if progress_cb:
        progress_cb(1.0, f"Done — {len(scored)} winning trades in {_elapsed():.1f}s")

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
