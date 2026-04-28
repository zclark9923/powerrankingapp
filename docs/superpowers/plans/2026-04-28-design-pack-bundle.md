# Design Pack Bundle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the prose-prompt handoff to Claude Design with a self-contained YAML bundle whose every text slot is pre-generated, quality-gated, and ready for typesetting.

**Architecture:** Two new agent stages run after a section is body-accepted (`design_pack`) and after the whole report is complete (`design_polish`). Their outputs go through a sibling deslop pass tuned for short-form copy (`design_deslop`), get human-approved, and are bundled into a single `output/report.design.yaml` (`compose_design`) that the user manually uploads to Claude Design. Pull quotes are extracted-verbatim from the body; everything else is paraphrased.

**Tech Stack:** Python 3.10+, Pydantic v2 schemas, Typer CLI, Anthropic + OpenAI SDKs, pytest, PyYAML.

**Spec:** [`docs/superpowers/specs/2026-04-28-design-pack-bundle-design.md`](../specs/2026-04-28-design-pack-bundle-design.md)

---

## Working Directory

> **All commands in this plan assume cwd is `C:/Users/Rachel/Desktop/Scripts/report-builder/`.** `cd` there once at the start of the implementation session. File paths in tasks are project-relative (e.g., `report_builder/design_pack.py` means `Scripts/report-builder/report_builder/design_pack.py`).

The plan and spec live inside the Ottoneu repo's worktree (`Scripts/.git`), but the code being modified is in the *untracked* `Scripts/report-builder/` project. Implementation commits happen against the report-builder code via plain file writes; no git operations on the report-builder code unless/until the user `git init`s it.

---

## File Structure

**New files:**
- `report_builder/design_pack.py` — per-section design pack agent
- `report_builder/design_polish.py` — end-of-pipeline polish agent
- `report_builder/design_deslop.py` — short-form deslop pass
- `report_builder/compose_design.py` — bundle assembler
- `report_builder/prompts/design_pack_system.md` — design-pack agent system prompt
- `report_builder/prompts/design_polish_system.md` — polish agent system prompt
- `tests/conftest.py` — shared pytest fixtures
- `tests/test_design_deslop.py` — scoring + iterative pass tests
- `tests/test_design_pack.py` — agent + verification tests
- `tests/test_design_polish.py` — polish agent tests
- `tests/test_compose_design.py` — bundle assembly tests
- `tests/test_design_cli.py` — CLI smoke tests
- `tests/test_design_integration.py` — end-to-end fixture run

**Modified files:**
- `report_builder/schemas.py` — add `DesignRecord`, `DesignDeslopRecord`, `DesignFinal`, status enums, `Section` fields
- `report_builder/cli.py` — add `design`, `polish`, `accept-design`, `compose-design` commands; modify `accept` to auto-trigger design generation
- `pyproject.toml` — add pytest as dev dependency

---

## Tasks

### Task 1: Test infrastructure

**Files:**
- Modify: `pyproject.toml`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest as a dev dependency**

Edit `pyproject.toml`. Add an `[project.optional-dependencies]` `dev` group (the existing `pdf` group stays). After the existing `pdf = [...]` line, add:

```toml
dev = ["pytest>=8.0", "pytest-mock>=3.12"]
```

- [ ] **Step 2: Install dev deps**

Run: `pip install -e ".[dev]"`
Expected: pytest and pytest-mock installed; `pytest --version` prints a version.

- [ ] **Step 3: Create the tests package**

Create `tests/__init__.py` as an empty file.

- [ ] **Step 4: Write the conftest fixture**

Create `tests/conftest.py`:

```python
"""Shared pytest fixtures for report-builder tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from report_builder.schemas import (
    OutlineSection,
    Report,
    ReportMetadata,
    Section,
    SectionFinal,
    SectionStatus,
)
from report_builder.state import ReportProject


@pytest.fixture
def tmp_project(tmp_path: Path) -> ReportProject:
    """A minimal report project on disk with one accepted section.

    Layout:
        <tmp_path>/
            report.yaml
            facts.yaml
            charts.yaml
            sections/01-overview.yaml
    """
    project_root = tmp_path / "proj"
    project_root.mkdir()

    report = Report(
        report=ReportMetadata(
            id="test",
            title="Test Report",
            audience="Test audience",
            purpose="Testing",
        ),
        outline=[
            OutlineSection(
                id="01-overview",
                title="Overview",
                load_bearing_finding="Markets are markets.",
                target_words=200,
            ),
        ],
        voice_constraints=["no first-person plural"],
    )
    (project_root / "report.yaml").write_text(
        yaml.safe_dump(report.model_dump(), sort_keys=False), encoding="utf-8",
    )
    (project_root / "facts.yaml").write_text("facts: []\n", encoding="utf-8")
    (project_root / "charts.yaml").write_text("charts: []\n", encoding="utf-8")

    sections_dir = project_root / "sections"
    sections_dir.mkdir()
    section = Section(
        id="01-overview",
        status=SectionStatus.ACCEPTED,
        final=SectionFinal(
            text=(
                "Markets exhibit cyclical patterns driven by sentiment. "
                "When investors crowd into a single thesis, returns compress. "
                "Patient capital that holds through compression captures the rebound."
            ),
            flesch=65.0,
            slop_risk="low",
            struct_hits=0,
            word_count=33,
            accepted_at="2026-04-28T00:00:00+00:00",
        ),
    )
    (sections_dir / "01-overview.yaml").write_text(
        yaml.safe_dump(section.model_dump(), sort_keys=False), encoding="utf-8",
    )
    return ReportProject(project_root)


@pytest.fixture
def deterministic_rewriter():
    """A stub rewriter for design_deslop tests — returns canned outputs.

    Usage:
        rewriter = deterministic_rewriter({"headline": "Markets cycle"})
        ...
    """
    def make(canned: dict[str, str]):
        def rewriter(slot_name: str, current: str, violations: list[str], budget: int) -> str:
            return canned.get(slot_name, current)
        return rewriter
    return make
```

- [ ] **Step 5: Verify pytest discovers the empty test suite**

Run: `pytest tests/ -v`
Expected: `no tests ran` (or zero collected) — but no errors. Confirms imports work.

- [ ] **Step 6: Commit**

```bash
# In Scripts/report-builder/ — note this is NOT a git repo yet, so this commit
# only happens if the user has run `git init` here. If not, skip the commit
# step for every task and let the user batch-commit when they `git init`.
# The plan still works without commits; commits are an optional checkpoint.
```

(If `Scripts/report-builder/` is not a git repo, treat every "Commit" step in this plan as a no-op. The user can `git init` and bulk-commit at any point.)

---

### Task 2: Schema extensions

**Files:**
- Modify: `report_builder/schemas.py`
- Create: `tests/test_schemas_design.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_schemas_design.py`:

```python
"""Tests for design-pack additions to schemas.py."""

from __future__ import annotations

from report_builder.schemas import (
    DesignDeslopRecord,
    DesignFinal,
    DesignRecord,
    PullQuote,
    Section,
    SectionStatus,
    body_hash,
)


def test_section_status_has_design_states():
    assert SectionStatus.AWAITING_DESIGN_ACCEPT.value == "awaiting-design-accept"
    assert SectionStatus.DESIGN_ACCEPTED.value == "design-accepted"


def test_design_record_round_trip():
    rec = DesignRecord(
        body_hash="abc123",
        kicker="Eyebrow",
        headline="Markets cycle",
        dek="A short one-liner",
        tldr="Short summary.",
        pull_quotes=[PullQuote(text="Markets exhibit", source="01-overview::p0")],
        chart_captions={"B.1": "Caption text"},
    )
    payload = rec.model_dump()
    restored = DesignRecord.model_validate(payload)
    assert restored.headline == "Markets cycle"
    assert restored.pull_quotes[0].source == "01-overview::p0"
    assert restored.chart_captions == {"B.1": "Caption text"}


def test_design_deslop_record_defaults():
    rec = DesignDeslopRecord()
    assert rec.iterations == 0
    assert rec.history == []


def test_design_final_accepted_at_optional():
    f = DesignFinal(
        body_hash="abc",
        kicker="x", headline="y", dek="z", tldr="w",
        pull_quotes=[],
        chart_captions={},
    )
    assert f.accepted_at is None


def test_section_carries_design_fields():
    sec = Section(id="x")
    assert sec.design is None
    assert sec.design_deslop is None
    assert sec.design_final is None
    assert sec.design_versions == []


def test_body_hash_stable():
    assert body_hash("hello world") == body_hash("hello world")
    assert body_hash("hello") != body_hash("hello world")
    # Sanity: hex string of expected length (sha256 -> 64)
    assert len(body_hash("x")) == 64
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schemas_design.py -v`
Expected: ImportError or AttributeError on `DesignDeslopRecord`, `DesignFinal`, `DesignRecord`, `PullQuote`, `body_hash`, or `SectionStatus.AWAITING_DESIGN_ACCEPT`.

- [ ] **Step 3: Implement schema additions**

Edit `report_builder/schemas.py`. Add after the existing `SectionStatus` enum definition:

```python
# Replace the existing SectionStatus class with:
class SectionStatus(str, Enum):
    PENDING = "pending"
    DRAFTING = "drafting"
    AWAITING_ACCEPT = "awaiting-accept"
    ACCEPTED = "accepted"
    AWAITING_DESIGN_ACCEPT = "awaiting-design-accept"
    DESIGN_ACCEPTED = "design-accepted"
```

Add a `body_hash` helper near the top (after `_now_iso`):

```python
import hashlib


def body_hash(text: str) -> str:
    """Stable sha256 hex digest of a body text. Used to detect when a
    section's body has changed under a previously-generated design pack."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
```

Add new design-related models after the existing `SectionFinal` class:

```python
class PullQuote(_Base):
    """A verbatim extraction from the body or a fact."""
    text: str
    source: str  # paragraph anchor like "01-overview::p2" or "fact:A.3"


class DesignRecord(_Base):
    """The per-section design pack as initially generated (pre-deslop, pre-accept).

    body_hash captures the body text the pack was generated against, so a
    later body re-draft invalidates this pack via mismatched hash.
    """
    body_hash: str
    kicker: str = ""
    headline: str = ""
    dek: str = ""
    tldr: str = ""
    pull_quotes: list[PullQuote] = Field(default_factory=list)
    chart_captions: dict[str, str] = Field(default_factory=dict)


class DesignDeslopRecord(_Base):
    """Iteration history of the short-form deslop pass over a design pack."""
    iterations: int = 0
    best_iteration: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)


class DesignFinal(_Base):
    """The locked, human-accepted design pack for a section. Mirrors
    SectionFinal in spirit — the design counterpart of section.final."""
    body_hash: str
    kicker: str = ""
    headline: str = ""
    dek: str = ""
    tldr: str = ""
    pull_quotes: list[PullQuote] = Field(default_factory=list)
    chart_captions: dict[str, str] = Field(default_factory=dict)
    accepted_at: Optional[str] = None
    replaced_at: Optional[str] = None
    replaced_by: Optional[str] = None
```

Modify the existing `Section` class to add fields:

```python
class Section(_Base):
    id: str
    status: SectionStatus = SectionStatus.PENDING
    brief: Optional[SectionBrief] = None
    drafts: list[DraftAttempt] = Field(default_factory=list)
    synthesis: Optional[SynthesisOutput] = None
    deslop: Optional[DeslopRecord] = None
    final: Optional[SectionFinal] = None
    versions: list[SectionFinal] = Field(default_factory=list)
    rejection_history: list[str] = Field(default_factory=list)
    # --- design pack fields (additive) ---
    design: Optional[DesignRecord] = None
    design_deslop: Optional[DesignDeslopRecord] = None
    design_final: Optional[DesignFinal] = None
    design_versions: list[DesignFinal] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_schemas_design.py -v`
Expected: 6 tests pass.

- [ ] **Step 5: Commit (or skip if not git-init'd)**

---

### Task 3: design_deslop scoring primitives

**Files:**
- Create: `report_builder/design_deslop.py` (partial — scorers only)
- Create: `tests/test_design_deslop.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_design_deslop.py`:

```python
"""Tests for short-form design deslop scorers and iterative pass."""

from __future__ import annotations

from report_builder.design_deslop import (
    LENGTH_BOUNDS,
    ai_tell_violations,
    cliche_violations,
    em_dash_violations,
    length_violations,
    score_slot,
)


def test_length_bounds_table_keys():
    # Spec-specified slot types
    for slot in ("kicker", "headline", "dek", "tldr", "caption", "key_finding"):
        assert slot in LENGTH_BOUNDS, f"missing length bound for {slot}"


def test_length_violations_under_bound():
    assert length_violations("kicker", "Three short words") == []


def test_length_violations_over_bound():
    long_headline = "this headline goes on for far too many words indeed"
    msgs = length_violations("headline", long_headline)
    assert msgs and "headline" in msgs[0].lower()


def test_ai_tell_catches_known_phrases():
    assert ai_tell_violations("In today's rapidly evolving market") != []
    assert ai_tell_violations("Let's delve into the data") != []
    assert ai_tell_violations("It's worth noting the trend") != []


def test_ai_tell_clean_text():
    assert ai_tell_violations("Markets cycle") == []


def test_cliche_violations_rhetorical_question():
    assert cliche_violations("What does this mean for investors?") != []


def test_cliche_violations_clean():
    assert cliche_violations("Investors face compressed returns.") == []


def test_em_dash_budget_zero_for_headline():
    assert em_dash_violations("headline", "Markets cycle—patient capital wins") != []
    assert em_dash_violations("headline", "Markets cycle, patient capital wins") == []


def test_em_dash_budget_one_for_tldr():
    one_dash = "Markets cycle — patient capital wins."
    two_dash = "Markets cycle — patient capital wins — every time."
    assert em_dash_violations("tldr", one_dash) == []
    assert em_dash_violations("tldr", two_dash) != []


def test_score_slot_aggregates_violations():
    score = score_slot("headline", "In today's market — what's next?")
    # Length OK (under 8 words), but: AI-tell ("in today's"), em-dash, rhetorical
    assert score.violations
    assert score.is_clean is False


def test_score_slot_clean():
    score = score_slot("headline", "Markets cycle")
    assert score.violations == []
    assert score.is_clean is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_design_deslop.py -v`
Expected: ImportError on `design_deslop`.

- [ ] **Step 3: Implement scoring primitives**

Create `report_builder/design_deslop.py`:

```python
"""Short-form deslop — sibling to deslop.py but tuned for design-pack copy.

Where deslop.py runs Flesch + slop_risk + struct_hits over paragraphs of
prose, this module runs slot-typed checks over headlines, decks, TLDRs,
captions, and other short-form items where Flesch is meaningless.

Public API:
    score_slot(slot_name, text) -> SlotScore
    run_design_deslop(slots, *, rewriter=...) -> DesignDeslopResult
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Length bounds (words). Spec § "design_deslop scorer set".
# ---------------------------------------------------------------------------


LENGTH_BOUNDS: dict[str, tuple[int, int]] = {
    # slot_name: (min_words, max_words)
    "kicker":       (1, 4),
    "headline":     (2, 8),
    "dek":          (4, 20),
    "tldr":         (15, 60),
    "caption":      (4, 25),
    "key_finding":  (3, 12),
    "subtitle":     (4, 20),
    "exec_summary": (80, 160),
}


# ---------------------------------------------------------------------------
# AI-tell phrase list — quoted strings, case-insensitive. Conservative starter
# set; extend in implementation as patterns emerge from real output.
# ---------------------------------------------------------------------------


AI_TELL_PHRASES: tuple[str, ...] = (
    "in today's",
    "in the modern",
    "rapidly evolving",
    "ever-changing landscape",
    "navigate the landscape",
    "delve into",
    "delving into",
    "it's worth noting",
    "it is worth noting",
    "in conclusion",
    "to summarize",
    "in summary",
    "at the end of the day",
    "the world of",
    "world of finance",
    "in the realm of",
    "harness the power",
    "leverage the power",
    "unlock the potential",
    "game-changer",
    "game changer",
)


# ---------------------------------------------------------------------------
# Cliché regex bank — grammatical patterns that read AI-flavored.
# ---------------------------------------------------------------------------


# Rhetorical question ending in `?` — short-form copy almost never wants this.
_RHETORICAL_Q = re.compile(r"\?\s*$")
# Tricolon ("X, Y, and Z" / "X, Y, Z" framings in slot copy)
_TRICOLON = re.compile(r"\b\w+,\s+\w+,?\s+(?:and\s+)?\w+\b", re.IGNORECASE)
# Weak-intensifier opener
_WEAK_OPENER = re.compile(r"^(very|really|quite|truly|simply)\b", re.IGNORECASE)


CLICHE_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (_RHETORICAL_Q, "rhetorical question"),
    (_WEAK_OPENER, "weak-intensifier opener"),
)


# ---------------------------------------------------------------------------
# Em-dash budget — character count of `—` (U+2014) plus ASCII `--` if present.
# ---------------------------------------------------------------------------


EM_DASH_BUDGETS: dict[str, int] = {
    "kicker":       0,
    "headline":     0,
    "dek":          0,
    "subtitle":     0,
    "key_finding":  0,
    "tldr":         1,
    "caption":      1,
    "exec_summary": 2,
}


# ---------------------------------------------------------------------------
# Public scoring helpers
# ---------------------------------------------------------------------------


def length_violations(slot_name: str, text: str) -> list[str]:
    """Return [] if word count is within bounds for the slot, else a one-element
    list with a human-readable violation message. Unknown slot names return []."""
    bounds = LENGTH_BOUNDS.get(slot_name)
    if bounds is None:
        return []
    n = len(text.split())
    lo, hi = bounds
    if n < lo:
        return [f"{slot_name} too short: {n} words, min {lo}"]
    if n > hi:
        return [f"{slot_name} too long: {n} words, max {hi}"]
    return []


def ai_tell_violations(text: str) -> list[str]:
    """Return one violation per matched AI-tell phrase. Case-insensitive."""
    lower = text.lower()
    return [f"AI-tell phrase: {p!r}" for p in AI_TELL_PHRASES if p in lower]


def cliche_violations(text: str) -> list[str]:
    """Return one violation per matched cliché pattern."""
    out: list[str] = []
    for pattern, label in CLICHE_PATTERNS:
        if pattern.search(text):
            out.append(f"cliché pattern: {label}")
    return out


def em_dash_violations(slot_name: str, text: str) -> list[str]:
    """Return a violation if em-dash count exceeds the slot's budget."""
    budget = EM_DASH_BUDGETS.get(slot_name)
    if budget is None:
        return []
    count = text.count("—") + text.count("--")
    if count > budget:
        return [f"em-dash budget for {slot_name}: {count} present, max {budget}"]
    return []


@dataclass
class SlotScore:
    slot_name: str
    text: str
    violations: list[str]

    @property
    def is_clean(self) -> bool:
        return not self.violations


def score_slot(slot_name: str, text: str) -> SlotScore:
    """Run every scorer for a single slot and aggregate violations."""
    v: list[str] = []
    v.extend(length_violations(slot_name, text))
    v.extend(ai_tell_violations(text))
    v.extend(cliche_violations(text))
    v.extend(em_dash_violations(slot_name, text))
    return SlotScore(slot_name=slot_name, text=text, violations=v)


# Public API for the iterative pass — implemented in Task 4.
@dataclass
class DesignDeslopResult:
    """What run_design_deslop returns to the caller (mirrors DeslopResult)."""
    slots: dict[str, str]  # final, possibly-rewritten slot text
    iterations: int
    best_iteration: int
    history: list[dict[str, Any]]
    error: Optional[str] = None


def run_design_deslop(
    slots: dict[str, str],
    *,
    rewriter: Optional[Callable[[str, str, list[str], int], str]] = None,
    max_iterations: int = 3,
) -> DesignDeslopResult:
    """Stub — implemented in Task 4."""
    raise NotImplementedError("Task 4 implements this.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_design_deslop.py -v`
Expected: 12 tests pass (the run_design_deslop test is in Task 4, not here).

- [ ] **Step 5: Commit (or skip)**

---

### Task 4: design_deslop iterative pass

**Files:**
- Modify: `report_builder/design_deslop.py` (replace `run_design_deslop` stub)
- Modify: `tests/test_design_deslop.py` (append iterative-pass tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_design_deslop.py`:

```python
def test_run_design_deslop_clean_input_no_iterations():
    from report_builder.design_deslop import run_design_deslop
    slots = {"headline": "Markets cycle", "dek": "Patient capital captures the rebound"}
    result = run_design_deslop(slots, rewriter=lambda *a, **kw: "should-not-be-called")
    assert result.iterations == 0
    assert result.slots == slots
    assert result.error is None


def test_run_design_deslop_rewrites_dirty_slot():
    from report_builder.design_deslop import run_design_deslop
    dirty = {"headline": "In today's market, what's next?"}
    canned = {"headline": "Markets cycle"}

    def rewriter(slot_name, current, violations, budget):
        return canned[slot_name]

    result = run_design_deslop(dirty, rewriter=rewriter, max_iterations=2)
    assert result.iterations >= 1
    assert result.slots["headline"] == "Markets cycle"
    assert result.error is None


def test_run_design_deslop_gives_up_after_max_iterations():
    from report_builder.design_deslop import run_design_deslop
    dirty = {"headline": "In today's market, what's next?"}

    def stubborn_rewriter(slot_name, current, violations, budget):
        return current  # never improves

    result = run_design_deslop(dirty, rewriter=stubborn_rewriter, max_iterations=2)
    assert result.iterations == 2
    assert "headline" in result.slots
    # Surface the unresolved violations to the caller via history
    last = result.history[-1]
    assert last["slots"]["headline"]["violations"]


def test_run_design_deslop_records_history_per_iteration():
    from report_builder.design_deslop import run_design_deslop
    dirty = {"headline": "In today's market, what's next?"}
    canned_iters = [
        {"headline": "What's next in markets?"},   # still rhetorical
        {"headline": "Markets cycle"},              # clean
    ]
    state = {"i": 0}

    def rewriter(slot_name, current, violations, budget):
        text = canned_iters[state["i"]][slot_name]
        state["i"] += 1
        return text

    result = run_design_deslop(dirty, rewriter=rewriter, max_iterations=3)
    assert result.iterations == 2
    assert result.slots["headline"] == "Markets cycle"
    assert len(result.history) == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_design_deslop.py -v`
Expected: 4 failures with `NotImplementedError`.

- [ ] **Step 3: Implement run_design_deslop**

Replace the `run_design_deslop` stub at the bottom of `report_builder/design_deslop.py` with:

```python
def run_design_deslop(
    slots: dict[str, str],
    *,
    rewriter: Optional[Callable[[str, str, list[str], int], str]] = None,
    max_iterations: int = 3,
) -> DesignDeslopResult:
    """Iteratively rewrite any slot with violations until clean or budget exhausted.

    Args:
        slots: {slot_name: text} — must use slot names from LENGTH_BOUNDS to be scored.
        rewriter: callable invoked once per dirty slot per iteration.
            Signature: (slot_name, current_text, violations, remaining_budget) -> new_text
            Default rewriter is a thin Anthropic wrapper (see _default_rewriter).
        max_iterations: cap on rewrite passes; remaining violations are surfaced
            in the result rather than retried indefinitely.
    """
    if rewriter is None:
        rewriter = _default_rewriter

    current = dict(slots)  # mutate a copy
    history: list[dict[str, Any]] = []
    iteration = 0
    best_iteration = 0
    best_violation_count: Optional[int] = None
    error: Optional[str] = None

    for iteration in range(1, max_iterations + 1):
        scored = {name: score_slot(name, text) for name, text in current.items()}
        dirty = {name: s for name, s in scored.items() if s.violations}
        total_v = sum(len(s.violations) for s in scored.values())

        history.append({
            "iteration": iteration,
            "slots": {
                name: {"text": s.text, "violations": s.violations}
                for name, s in scored.items()
            },
            "total_violations": total_v,
        })

        if best_violation_count is None or total_v < best_violation_count:
            best_violation_count = total_v
            best_iteration = iteration

        if not dirty:
            # Clean — but record-keeping wants the count to reflect the rewrite count,
            # not the post-clean inspection. iteration-1 = number of rewrites done.
            return DesignDeslopResult(
                slots=current,
                iterations=iteration - 1,
                best_iteration=best_iteration,
                history=history,
                error=None,
            )

        # Rewrite each dirty slot once per iteration.
        budget = max_iterations - iteration
        for name, score in dirty.items():
            try:
                new_text = rewriter(name, score.text, score.violations, budget)
            except Exception as exc:
                logger.exception("rewriter raised for slot %s", name)
                error = f"{type(exc).__name__}: {exc}"
                # Continue with current text; surface error in result.
                continue
            current[name] = new_text

    # Budget exhausted with violations remaining — return what we have.
    return DesignDeslopResult(
        slots=current,
        iterations=max_iterations,
        best_iteration=best_iteration,
        history=history,
        error=error,
    )


def _default_rewriter(slot_name: str, current: str, violations: list[str], budget: int) -> str:
    """Default rewriter — calls Anthropic with a tight, slot-aware prompt.

    Lazy-imports anthropic so the module can be used in unit tests without
    the SDK installed. Raises on API failure (caller wraps and degrades).
    """
    import anthropic  # lazy
    bounds = LENGTH_BOUNDS.get(slot_name, (1, 100))
    em_budget = EM_DASH_BUDGETS.get(slot_name, 99)
    system = (
        "You rewrite short-form design copy to remove specific violations "
        "without adding new content or AI-flavored prose. Preserve meaning. "
        "Output ONLY the rewritten text — no preamble, no quotes, no explanation."
    )
    user = (
        f"Slot type: {slot_name}\n"
        f"Length bounds (words): {bounds[0]}-{bounds[1]}\n"
        f"Em-dash budget: {em_budget}\n\n"
        f"Current text:\n{current}\n\n"
        f"Violations to fix:\n"
        + "\n".join(f"- {v}" for v in violations)
        + "\n\nRewrite. Output only the new text."
    )
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=200,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    blocks = [getattr(b, "text", "") for b in (response.content or []) if getattr(b, "type", None) == "text"]
    text = "\n".join(b for b in blocks if b).strip()
    if not text:
        raise RuntimeError("Anthropic returned no text")
    return text
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_design_deslop.py -v`
Expected: all 16 tests in the file pass (12 from Task 3 + 4 new).

- [ ] **Step 5: Commit (or skip)**

---

### Task 5: design_pack agent — initial generation

**Files:**
- Create: `report_builder/prompts/design_pack_system.md`
- Create: `report_builder/design_pack.py`
- Create: `tests/test_design_pack.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_design_pack.py`:

```python
"""Tests for the per-section design-pack agent."""

from __future__ import annotations

from report_builder.design_pack import (
    DesignPackResult,
    generate_design_pack,
    paragraph_anchor,
    paragraphs_of,
    verify_extractions,
)
from report_builder.schemas import DesignRecord, PullQuote


def test_paragraphs_of_splits_on_blank_lines():
    body = "First paragraph.\n\nSecond paragraph.\n\nThird."
    paras = paragraphs_of(body)
    assert paras == ["First paragraph.", "Second paragraph.", "Third."]


def test_paragraph_anchor_format():
    assert paragraph_anchor("01-overview", 0) == "01-overview::p0"
    assert paragraph_anchor("01-overview", 3) == "01-overview::p3"


def test_verify_extractions_substring_match():
    body = "Markets exhibit cyclical patterns. Patient capital wins."
    quotes = [PullQuote(text="cyclical patterns", source="x::p0")]
    ok, bad = verify_extractions(quotes, body)
    assert ok == quotes
    assert bad == []


def test_verify_extractions_finds_unmatched():
    body = "Markets exhibit cyclical patterns."
    quotes = [
        PullQuote(text="cyclical patterns", source="x::p0"),
        PullQuote(text="not in the body", source="x::p0"),
    ]
    ok, bad = verify_extractions(quotes, body)
    assert len(ok) == 1
    assert len(bad) == 1
    assert bad[0].text == "not in the body"


def test_generate_design_pack_uses_stub_agent(tmp_project):
    section = tmp_project.load_section("01-overview")

    canned = {
        "kicker": "Markets",
        "headline": "Markets cycle",
        "dek": "Patient capital captures the rebound",
        "tldr": (
            "Markets cycle through sentiment-driven peaks and troughs. "
            "Compressed returns reward capital that holds through compression."
        ),
        "pull_quotes": [
            {"text": "cyclical patterns driven by sentiment", "source_para": 0}
        ],
        "chart_captions": {},
    }

    def stub_agent(prompt: str) -> dict:
        return canned

    result = generate_design_pack(
        tmp_project, section, agent_call=stub_agent,
    )
    assert isinstance(result, DesignPackResult)
    assert result.record is not None
    rec: DesignRecord = result.record
    assert rec.headline == "Markets cycle"
    assert rec.body_hash  # populated
    assert rec.pull_quotes[0].text == "cyclical patterns driven by sentiment"
    assert rec.pull_quotes[0].source.startswith("01-overview::p")
    assert result.error is None
```

- [ ] **Step 2: Create the system prompt**

Create `report_builder/prompts/design_pack_system.md`:

```markdown
# Design Pack Agent — System

You generate per-section design copy for a research report that will be
typeset in Claude Design. Your output is structured: kicker, headline,
dek, TLDR, pull quotes (extracted verbatim), and chart captions.

## Hard rules

1. **Pull quotes are EXTRACTIONS.** Each pull quote MUST be a substring
   that appears verbatim in the section body. Pick distinctive sentences
   or sentence fragments. Do NOT paraphrase, edit, or compose.
   Indicate the paragraph index (0-based) the quote was extracted from.

2. **Headlines, decks, kickers, TLDRs, captions are PARAPHRASED** — write
   new copy. Stay strictly within the source material. Do not invent
   numbers, claims, or themes that the body does not establish.

3. **Length bounds (words):**
   - kicker: 1–4
   - headline: 2–8
   - dek: 4–20
   - tldr: 15–60
   - chart caption: 4–25

4. **Voice rules** (apply to all paraphrased slots): no rhetorical questions,
   no AI-flavored phrasings (delve, navigate the landscape, in today's …,
   in conclusion, it's worth noting), no em-dashes in headlines/decks/kickers.

5. **Output JSON only.** Schema below.

## Output schema

```json
{
  "kicker": "string",
  "headline": "string",
  "dek": "string",
  "tldr": "string",
  "pull_quotes": [
    {"text": "verbatim substring of body", "source_para": 0}
  ],
  "chart_captions": {"chart_id": "caption text"}
}
```

If a chart_refs list is empty, return `chart_captions: {}`.
```

- [ ] **Step 3: Implement the agent module**

Create `report_builder/design_pack.py`:

```python
"""Per-section design-pack agent.

Reads an accepted section's body + the project's voice constraints +
stylesheet + the section's chart references, and produces a DesignRecord
with all per-section design slots filled. Pull quotes are verbatim-
extracted from the body and verified by substring match.

Public API:
    generate_design_pack(project, section, *, agent_call=...) -> DesignPackResult
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable, Optional

from .charts import ChartManager
from .facts import FactsManager
from .memory import (
    render_charts_for_drafter,
    render_facts_for_drafter,
    render_stylesheet_for_drafter,
    render_voice_constraints,
)
from .schemas import (
    DesignRecord,
    PullQuote,
    Section,
    body_hash,
)
from .state import ReportProject

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Paragraph utilities — paragraph anchors are <section_id>::p<index>
# ---------------------------------------------------------------------------


_BLANK_LINE_RE = re.compile(r"\n\s*\n")


def paragraphs_of(body: str) -> list[str]:
    """Split a body into paragraphs on blank lines, stripping each."""
    return [p.strip() for p in _BLANK_LINE_RE.split(body.strip()) if p.strip()]


def paragraph_anchor(section_id: str, index: int) -> str:
    return f"{section_id}::p{index}"


# ---------------------------------------------------------------------------
# Pull-quote verification
# ---------------------------------------------------------------------------


def verify_extractions(
    quotes: list[PullQuote], body: str,
) -> tuple[list[PullQuote], list[PullQuote]]:
    """Return (verified, failed). A quote is verified iff its text appears
    verbatim in the body (case-sensitive substring match)."""
    ok: list[PullQuote] = []
    bad: list[PullQuote] = []
    for q in quotes:
        if q.text and q.text in body:
            ok.append(q)
        else:
            bad.append(q)
    return ok, bad


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class DesignPackResult:
    record: Optional[DesignRecord]
    failed_quotes: list[PullQuote]
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompt + agent call
# ---------------------------------------------------------------------------


def _read_system_prompt() -> str:
    return (
        resources.files("report_builder.prompts")
        .joinpath("design_pack_system.md")
        .read_text(encoding="utf-8")
    )


def _build_user_prompt(
    project: ReportProject, section: Section,
) -> str:
    report = project.load_report()
    facts_mgr = FactsManager(project)
    charts_mgr = ChartManager(project)
    outline_entry = next(o for o in report.outline if o.id == section.id)
    body = section.final.text if section.final else ""
    paras = paragraphs_of(body)
    para_block = "\n".join(f"[p{i}] {p}" for i, p in enumerate(paras))

    chart_ids = [c.id for c in charts_mgr.load().charts
                 if any(u.section == section.id for u in c.used_in)]
    chart_block = (
        "\n".join(f"- {cid}" for cid in chart_ids) if chart_ids else "(none)"
    )

    return f"""# Section context

- id: {section.id}
- title: {outline_entry.title}
- load-bearing finding: {outline_entry.load_bearing_finding}

# Body (paragraph-indexed)

{para_block}

# Charts referenced in this section (caption needed for each)

{chart_block}

# Voice constraints

{render_voice_constraints(report)}

# Stylesheet

{render_stylesheet_for_drafter(project)}

# Available facts (for context only — pull-quote sources are paragraphs in the body above)

{render_facts_for_drafter(facts_mgr, prefer_section=section.id)}

Generate the design pack. Output JSON only, matching the schema in the system prompt.
"""


def _default_agent_call(prompt_pair: tuple[str, str]) -> dict:
    """Default agent — Anthropic with JSON-mode-style coercion."""
    import anthropic  # lazy
    system, user = prompt_pair
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    blocks = [getattr(b, "text", "") for b in (response.content or [])
              if getattr(b, "type", None) == "text"]
    raw = "\n".join(b for b in blocks if b).strip()
    # Strip markdown code fences if present.
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_design_pack(
    project: ReportProject,
    section: Section,
    *,
    agent_call: Optional[Callable[..., dict]] = None,
) -> DesignPackResult:
    """Generate the per-section design pack.

    `agent_call` is a function called once with either (system, user) prompts
    as a tuple OR a single prompt (for tests using a stub). The default
    invokes Anthropic. The function must return a dict matching the schema
    documented in design_pack_system.md.
    """
    if section.final is None or not section.final.text:
        return DesignPackResult(
            record=None, failed_quotes=[],
            error="section has no final body text — accept the body first",
        )

    body = section.final.text
    h = body_hash(body)

    system = _read_system_prompt()
    user = _build_user_prompt(project, section)

    # The stub form takes a single string; the default form takes a tuple.
    call = agent_call or _default_agent_call
    try:
        if agent_call is not None:
            try:
                payload = agent_call(user)
            except TypeError:
                payload = agent_call((system, user))
        else:
            payload = call((system, user))
    except Exception as exc:
        logger.exception("design-pack agent failed")
        return DesignPackResult(
            record=None, failed_quotes=[],
            error=f"{type(exc).__name__}: {exc}",
        )

    quotes_in = [
        PullQuote(
            text=q.get("text", ""),
            source=paragraph_anchor(section.id, int(q.get("source_para", 0))),
        )
        for q in payload.get("pull_quotes", [])
    ]
    verified, failed = verify_extractions(quotes_in, body)

    record = DesignRecord(
        body_hash=h,
        kicker=str(payload.get("kicker", "")),
        headline=str(payload.get("headline", "")),
        dek=str(payload.get("dek", "")),
        tldr=str(payload.get("tldr", "")),
        pull_quotes=verified,
        chart_captions=dict(payload.get("chart_captions", {})),
    )
    return DesignPackResult(record=record, failed_quotes=failed, error=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_design_pack.py -v`
Expected: 5 tests pass.

- [ ] **Step 5: Commit (or skip)**

---

### Task 6: design_pack — pull-quote retry loop

**Files:**
- Modify: `report_builder/design_pack.py` (add retry logic around the agent call)
- Modify: `tests/test_design_pack.py` (append retry tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_design_pack.py`:

```python
def test_generate_design_pack_retries_on_failed_quote(tmp_project):
    """If a pull quote isn't verbatim, the agent is re-prompted up to N times."""
    section = tmp_project.load_section("01-overview")
    body = section.final.text
    assert "cyclical patterns" in body  # baseline

    attempts = []

    def flaky_agent(prompt: str) -> dict:
        attempts.append(prompt)
        if len(attempts) == 1:
            # First attempt: bad quote
            return {
                "kicker": "Markets", "headline": "Markets cycle",
                "dek": "Patient capital", "tldr": "Markets cycle through sentiment-driven peaks and troughs each cycle reliably enough.",
                "pull_quotes": [{"text": "this string is not in the body", "source_para": 0}],
                "chart_captions": {},
            }
        # Retry: good quote
        return {
            "kicker": "Markets", "headline": "Markets cycle",
            "dek": "Patient capital", "tldr": "Markets cycle through sentiment-driven peaks and troughs each cycle reliably enough.",
            "pull_quotes": [{"text": "cyclical patterns", "source_para": 0}],
            "chart_captions": {},
        }

    result = generate_design_pack(
        tmp_project, section, agent_call=flaky_agent, max_quote_retries=2,
    )
    assert result.error is None
    assert result.record is not None
    assert len(result.record.pull_quotes) == 1
    assert result.record.pull_quotes[0].text == "cyclical patterns"
    assert len(attempts) == 2  # one retry happened


def test_generate_design_pack_gives_up_after_retry_budget(tmp_project):
    section = tmp_project.load_section("01-overview")

    def stubborn(prompt: str) -> dict:
        return {
            "kicker": "X", "headline": "Y", "dek": "Z W",
            "tldr": "Markets cycle through sentiment-driven peaks and troughs each cycle reliably enough.",
            "pull_quotes": [{"text": "never in the body", "source_para": 0}],
            "chart_captions": {},
        }

    result = generate_design_pack(
        tmp_project, section, agent_call=stubborn, max_quote_retries=1,
    )
    # Even on persistent failure: we surface the failed quotes but DO produce
    # a record (without the bad quotes), so the user can manually fix in review.
    assert result.error is None
    assert result.record is not None
    assert result.record.pull_quotes == []
    assert len(result.failed_quotes) >= 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_design_pack.py -v`
Expected: 2 failures — `max_quote_retries` is an unexpected keyword argument.

- [ ] **Step 3: Add retry loop to generate_design_pack**

Modify `generate_design_pack` in `report_builder/design_pack.py`. Replace the function body's agent-call section (the part starting with `call = agent_call or _default_agent_call` through `record = DesignRecord(...)`) with:

```python
    # Replace `def generate_design_pack(...)` signature to add max_quote_retries:
    # def generate_design_pack(
    #     project: ReportProject,
    #     section: Section,
    #     *,
    #     agent_call: Optional[Callable[..., dict]] = None,
    #     max_quote_retries: int = 3,
    # ) -> DesignPackResult:

    payload: Optional[dict] = None
    failed: list[PullQuote] = []
    verified: list[PullQuote] = []
    last_error: Optional[str] = None

    for attempt in range(max_quote_retries + 1):  # initial + retries
        try:
            if agent_call is not None:
                try:
                    payload = agent_call(user)
                except TypeError:
                    payload = agent_call((system, user))
            else:
                payload = _default_agent_call((system, user))
        except Exception as exc:
            logger.exception("design-pack agent failed (attempt %d)", attempt + 1)
            last_error = f"{type(exc).__name__}: {exc}"
            break

        quotes_in = [
            PullQuote(
                text=q.get("text", ""),
                source=paragraph_anchor(section.id, int(q.get("source_para", 0))),
            )
            for q in payload.get("pull_quotes", [])
        ]
        verified, failed = verify_extractions(quotes_in, body)

        if not failed:
            break  # all clean

        # Retry budget remaining? Append a corrective hint to the user prompt.
        if attempt < max_quote_retries:
            bad_block = "\n".join(
                f"- you wrote: {q.text!r} — that string is NOT in the body. "
                f"Pick a different verbatim substring."
                for q in failed
            )
            user = user + (
                f"\n\n# Retry — pull-quote verification failed\n\n"
                f"On your previous attempt, these pull quotes did not appear "
                f"verbatim in the body:\n{bad_block}\n\n"
                f"Re-emit the entire JSON payload with corrected quotes. "
                f"Pick distinctive substrings that you can find in the body above."
            )
            continue
        # Out of retries — keep the verified quotes, drop the failed ones,
        # surface failures to the caller via failed_quotes.
        break

    if payload is None:
        return DesignPackResult(record=None, failed_quotes=[], error=last_error)

    record = DesignRecord(
        body_hash=h,
        kicker=str(payload.get("kicker", "")),
        headline=str(payload.get("headline", "")),
        dek=str(payload.get("dek", "")),
        tldr=str(payload.get("tldr", "")),
        pull_quotes=verified,
        chart_captions=dict(payload.get("chart_captions", {})),
    )
    return DesignPackResult(record=record, failed_quotes=failed, error=None)
```

(Replace from `# The stub form takes a single string` through the final `return DesignPackResult(...)`. Update the function signature to include `max_quote_retries: int = 3`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_design_pack.py -v`
Expected: all 7 tests pass.

- [ ] **Step 5: Commit (or skip)**

---

### Task 7: design_polish agent

**Files:**
- Create: `report_builder/prompts/design_polish_system.md`
- Create: `report_builder/design_polish.py`
- Create: `tests/test_design_polish.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_design_polish.py`:

```python
"""Tests for the end-of-pipeline polish agent."""

from __future__ import annotations

import yaml

from report_builder.design_polish import PolishResult, generate_polish
from report_builder.schemas import (
    DesignFinal,
    PullQuote,
    SectionStatus,
)


def test_generate_polish_uses_stub(tmp_project):
    # Promote the section's body to design-accepted with a finalized design pack
    section = tmp_project.load_section("01-overview")
    section.status = SectionStatus.DESIGN_ACCEPTED
    section.design_final = DesignFinal(
        body_hash="fake",
        kicker="Markets",
        headline="Markets cycle",
        dek="Patient capital captures the rebound",
        tldr="Cycles compress returns; patience is rewarded.",
        pull_quotes=[PullQuote(text="cyclical patterns", source="01-overview::p0")],
        chart_captions={},
        accepted_at="2026-04-28T00:00:00+00:00",
    )
    tmp_project.save_section(section)

    canned = {
        "subtitle": "How patient capital wins through compression",
        "cover_kicker": "Markets",
        "exec_summary": (
            "Markets cycle. Returns compress when investors crowd into a single thesis. "
            "Patient capital that holds through compression captures the rebound. "
            "Three cycles in the last decade illustrate the pattern. "
            "The prescription is positional discipline, not prediction."
        ),
        "key_findings": [
            "Cycles compress returns",
            "Patient capital captures rebounds",
            "Discipline beats prediction",
        ],
        "toc_headlines": ["Markets cycle"],
        "cover_pull_quote": {"text": "cyclical patterns", "source_section": "01-overview"},
    }

    def stub(prompt: str) -> dict:
        return canned

    result = generate_polish(tmp_project, agent_call=stub)
    assert isinstance(result, PolishResult)
    assert result.error is None
    assert result.report_block["subtitle"].startswith("How patient capital")
    assert result.report_block["cover_pull_quote"]["text"] == "cyclical patterns"
    assert "Markets cycle" in result.report_block["toc_headlines"]


def test_generate_polish_errors_when_no_design_accepted(tmp_project):
    # Section is body-accepted but not design-accepted
    result = generate_polish(tmp_project, agent_call=lambda p: {})
    assert result.error is not None
    assert "no design-accepted sections" in result.error.lower()
```

- [ ] **Step 2: Create the polish system prompt**

Create `report_builder/prompts/design_polish_system.md`:

```markdown
# Design Polish Agent — System

You are the final report-level pass before a design pack is bundled and
handed to Claude Design. You see ALL design-accepted sections at once
(their headlines, TLDRs, pull quotes, body) and produce report-level
copy: subtitle, cover kicker, executive summary, key findings,
TOC headlines, and a cover pull quote.

## Hard rules

1. **Cover pull quote is EXTRACTED.** Pick a distinctive verbatim substring
   from any section's body. Indicate which section.
2. **Everything else is paraphrased.** Stay strictly within the source.
3. **Length bounds (words):**
   - subtitle: 4–20
   - cover_kicker: 1–4
   - exec_summary: 80–160
   - key_findings: each 3–12
   - toc_headlines: each 2–8
4. **Deduplicate.** If two section headlines are near-identical, the
   toc_headlines list must rewrite one to differentiate.
5. **Voice rules.** No rhetorical questions. No AI-flavored phrasings.
6. **Output JSON only.**

## Output schema

```json
{
  "subtitle": "string",
  "cover_kicker": "string",
  "exec_summary": "string",
  "key_findings": ["string", ...],
  "toc_headlines": ["string", ...],
  "cover_pull_quote": {"text": "verbatim from any section body", "source_section": "section-id"}
}
```
```

- [ ] **Step 3: Implement the polish agent**

Create `report_builder/design_polish.py`:

```python
"""End-of-pipeline polish agent — produces report-level design copy.

Reads every design-accepted section and emits the `report:` block of the
bundled design pack: subtitle, cover_kicker, exec_summary, key_findings,
toc_headlines, cover_pull_quote.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from importlib import resources
from typing import Any, Callable, Optional

from .memory import render_voice_constraints
from .schemas import SectionStatus
from .state import ReportProject

logger = logging.getLogger(__name__)


@dataclass
class PolishResult:
    report_block: dict[str, Any] = field(default_factory=dict)
    failed_cover_quote: bool = False
    error: Optional[str] = None


def _read_system_prompt() -> str:
    return (
        resources.files("report_builder.prompts")
        .joinpath("design_polish_system.md")
        .read_text(encoding="utf-8")
    )


def _collect_design_accepted(project: ReportProject) -> list[dict[str, Any]]:
    """Gather a structured summary of every design-accepted section."""
    report = project.load_report()
    out: list[dict[str, Any]] = []
    for entry in report.outline:
        sec = project.load_section(entry.id)
        st = sec.status if isinstance(sec.status, str) else sec.status.value
        if st != SectionStatus.DESIGN_ACCEPTED.value or sec.design_final is None:
            continue
        body = sec.final.text if sec.final else ""
        out.append({
            "id": entry.id,
            "title": entry.title,
            "headline": sec.design_final.headline,
            "dek": sec.design_final.dek,
            "tldr": sec.design_final.tldr,
            "body": body,
        })
    return out


def _build_user_prompt(
    project: ReportProject, sections: list[dict[str, Any]]
) -> str:
    report = project.load_report()
    section_blocks = []
    for s in sections:
        section_blocks.append(
            f"## Section `{s['id']}` — {s['title']}\n\n"
            f"- headline: {s['headline']}\n"
            f"- dek: {s['dek']}\n"
            f"- TLDR: {s['tldr']}\n\n"
            f"### Body\n\n{s['body']}\n"
        )
    sections_block = "\n\n".join(section_blocks)
    return f"""# Report

- title: {report.report.title}
- audience: {report.report.audience}
- purpose: {report.report.purpose}

# Voice constraints

{render_voice_constraints(report)}

# Design-accepted sections

{sections_block}

Generate the report-level polish pack. Output JSON only, matching the schema in the system prompt.
"""


def _default_agent_call(prompt_pair: tuple[str, str]) -> dict:
    import anthropic
    system, user = prompt_pair
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=3000,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    blocks = [getattr(b, "text", "") for b in (response.content or [])
              if getattr(b, "type", None) == "text"]
    raw = "\n".join(b for b in blocks if b).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    return json.loads(raw)


def generate_polish(
    project: ReportProject,
    *,
    agent_call: Optional[Callable[..., dict]] = None,
) -> PolishResult:
    sections = _collect_design_accepted(project)
    if not sections:
        return PolishResult(error="no design-accepted sections found")

    system = _read_system_prompt()
    user = _build_user_prompt(project, sections)

    try:
        if agent_call is not None:
            try:
                payload = agent_call(user)
            except TypeError:
                payload = agent_call((system, user))
        else:
            payload = _default_agent_call((system, user))
    except Exception as exc:
        logger.exception("polish agent failed")
        return PolishResult(error=f"{type(exc).__name__}: {exc}")

    # Verify cover pull quote against the cited section's body.
    cover = payload.get("cover_pull_quote") or {}
    cover_text = str(cover.get("text", ""))
    cover_section_id = str(cover.get("source_section", ""))
    failed = True
    for s in sections:
        if s["id"] == cover_section_id and cover_text and cover_text in s["body"]:
            failed = False
            break

    return PolishResult(
        report_block=payload,
        failed_cover_quote=failed,
        error=None,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_design_polish.py -v`
Expected: 2 tests pass.

- [ ] **Step 5: Commit (or skip)**

---

### Task 8: compose_design — bundle assembler

**Files:**
- Create: `report_builder/compose_design.py`
- Create: `tests/test_compose_design.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_compose_design.py`:

```python
"""Tests for the bundled design YAML composer."""

from __future__ import annotations

import yaml

from report_builder.compose_design import (
    ComposeDesignResult,
    compose_design,
    is_stale,
)
from report_builder.schemas import (
    DesignFinal,
    PullQuote,
    SectionStatus,
    body_hash,
)


def test_is_stale_detects_body_change(tmp_project):
    section = tmp_project.load_section("01-overview")
    h = body_hash(section.final.text)
    fresh = DesignFinal(
        body_hash=h, kicker="x", headline="y", dek="z", tldr="w",
        pull_quotes=[], chart_captions={},
    )
    assert is_stale(fresh, section) is False
    stale = DesignFinal(
        body_hash="different-hash", kicker="x", headline="y", dek="z", tldr="w",
        pull_quotes=[], chart_captions={},
    )
    assert is_stale(stale, section) is True


def test_compose_design_writes_bundle(tmp_project):
    section = tmp_project.load_section("01-overview")
    h = body_hash(section.final.text)
    section.status = SectionStatus.DESIGN_ACCEPTED
    section.design_final = DesignFinal(
        body_hash=h,
        kicker="Markets",
        headline="Markets cycle",
        dek="Patient capital captures the rebound",
        tldr="Compressed returns reward patient capital.",
        pull_quotes=[PullQuote(text="cyclical patterns", source="01-overview::p0")],
        chart_captions={},
        accepted_at="2026-04-28T00:00:00+00:00",
    )
    tmp_project.save_section(section)

    polish_block = {
        "subtitle": "How patient capital wins",
        "cover_kicker": "Markets",
        "exec_summary": "Cycles compress returns. Patience is rewarded.",
        "key_findings": ["Cycles compress returns", "Patience wins"],
        "toc_headlines": ["Markets cycle"],
        "cover_pull_quote": {"text": "cyclical patterns", "source_section": "01-overview"},
    }

    result = compose_design(tmp_project, polish_block=polish_block)
    assert isinstance(result, ComposeDesignResult)
    assert result.bundle_path is not None
    assert result.bundle_path.exists()
    assert result.sections_included == 1
    assert result.error is None

    bundle = yaml.safe_load(result.bundle_path.read_text(encoding="utf-8"))
    assert bundle["report"]["title"] == "Test Report"
    assert bundle["report"]["subtitle"] == "How patient capital wins"
    assert bundle["sections"][0]["id"] == "01-overview"
    assert bundle["sections"][0]["headline"] == "Markets cycle"
    assert bundle["sections"][0]["body"].startswith("Markets exhibit")
    assert bundle["sections"][0]["pull_quotes"][0]["text"] == "cyclical patterns"


def test_compose_design_refuses_stale_section(tmp_project):
    section = tmp_project.load_section("01-overview")
    section.status = SectionStatus.DESIGN_ACCEPTED
    section.design_final = DesignFinal(
        body_hash="stale-hash-does-not-match",
        kicker="x", headline="y", dek="z", tldr="w",
        pull_quotes=[], chart_captions={},
        accepted_at="2026-04-28T00:00:00+00:00",
    )
    tmp_project.save_section(section)

    result = compose_design(tmp_project, polish_block={})
    assert result.bundle_path is None
    assert result.error is not None
    assert "stale" in result.error.lower()


def test_compose_design_warns_when_no_polish(tmp_project):
    section = tmp_project.load_section("01-overview")
    h = body_hash(section.final.text)
    section.status = SectionStatus.DESIGN_ACCEPTED
    section.design_final = DesignFinal(
        body_hash=h, kicker="x", headline="y", dek="z", tldr="w",
        pull_quotes=[], chart_captions={},
        accepted_at="2026-04-28T00:00:00+00:00",
    )
    tmp_project.save_section(section)

    result = compose_design(tmp_project, polish_block=None)
    assert result.bundle_path is not None  # bundle still produced
    assert any("polish" in w.lower() for w in result.warnings)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_compose_design.py -v`
Expected: ImportError on `compose_design`.

- [ ] **Step 3: Implement compose_design**

Create `report_builder/compose_design.py`:

```python
"""Bundle every per-section design pack + the polish pack + charts + facts
into a single self-contained `output/report.design.yaml` for upload to
Claude Design.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .charts import ChartManager
from .facts import FactsManager
from .schemas import (
    DesignFinal,
    Section,
    SectionStatus,
    body_hash,
)
from .state import ProjectError, ReportProject

logger = logging.getLogger(__name__)

OUTPUT_BUNDLE_FILENAME = "report.design.yaml"


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ComposeDesignResult:
    bundle_path: Optional[Path] = None
    sections_included: int = 0
    sections_skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


def is_stale(design_final: DesignFinal, section: Section) -> bool:
    """A design pack is stale if the section's body hash has changed since
    the pack was generated."""
    if section.final is None or not section.final.text:
        return True
    return design_final.body_hash != body_hash(section.final.text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compose_design(
    project: ReportProject,
    *,
    polish_block: Optional[dict[str, Any]] = None,
) -> ComposeDesignResult:
    """Bundle the full report into output/report.design.yaml.

    Args:
        polish_block: the report-level pack from generate_polish(); if None,
            the bundle's `report:` block falls back to title/audience only
            and a warning is recorded.
    """
    report = project.load_report()
    facts_mgr = FactsManager(project)
    charts_mgr = ChartManager(project)
    warnings: list[str] = []
    skipped: list[str] = []

    # Build sections list — must be design-accepted and not stale.
    sections_block: list[dict[str, Any]] = []
    for entry in report.outline:
        sec = project.load_section(entry.id)
        st = sec.status if isinstance(sec.status, str) else sec.status.value
        if st != SectionStatus.DESIGN_ACCEPTED.value or sec.design_final is None:
            skipped.append(entry.id)
            continue
        if is_stale(sec.design_final, sec):
            return ComposeDesignResult(
                error=(
                    f"section `{entry.id}` has a stale design pack "
                    f"(body changed since pack was generated). "
                    f"Re-run `report design {entry.id}` then `report accept-design`."
                ),
            )
        df = sec.design_final
        body = sec.final.text  # is_stale guarantees this exists

        # chart_refs: charts whose used_in cites this section
        chart_refs = sorted({
            c.id for c in charts_mgr.load().charts
            if any(u.section == entry.id for u in c.used_in)
        })

        sections_block.append({
            "id": entry.id,
            "kicker": df.kicker,
            "headline": df.headline,
            "dek": df.dek,
            "tldr": df.tldr,
            "body": body,
            "pull_quotes": [
                {"text": q.text, "source": q.source} for q in df.pull_quotes
            ],
            "chart_refs": chart_refs,
        })

    # Build report block
    if polish_block is None:
        warnings.append(
            "no polish block supplied — `report:` block has metadata only "
            "(run `report polish` first for full report-level copy)"
        )
        report_block = {
            "title": report.report.title,
            "subtitle": "",
            "cover_kicker": "",
            "exec_summary": "",
            "key_findings": [],
            "toc_headlines": [s["headline"] for s in sections_block],
            "cover_pull_quote": None,
        }
    else:
        report_block = {
            "title": report.report.title,
            "subtitle": polish_block.get("subtitle", ""),
            "cover_kicker": polish_block.get("cover_kicker", ""),
            "exec_summary": polish_block.get("exec_summary", ""),
            "key_findings": list(polish_block.get("key_findings", [])),
            "toc_headlines": list(polish_block.get("toc_headlines", [])),
            "cover_pull_quote": polish_block.get("cover_pull_quote"),
        }

    # Build charts block — full specs from charts.yaml + per-section captions.
    # If two sections caption the same chart, first-cited wins (with warning).
    charts_block: list[dict[str, Any]] = []
    referenced_chart_ids = sorted({cid for s in sections_block for cid in s["chart_refs"]})
    caption_owner: dict[str, str] = {}  # chart_id -> first section that captioned it
    for s in sections_block:
        sec = project.load_section(s["id"])
        for cid, cap in (sec.design_final.chart_captions or {}).items():
            if cid in caption_owner and caption_owner[cid] != s["id"]:
                warnings.append(
                    f"chart `{cid}` captioned in multiple sections — using "
                    f"caption from `{caption_owner[cid]}`, ignoring `{s['id']}`"
                )

    for cid in referenced_chart_ids:
        chart = charts_mgr.get(cid)
        if chart is None:
            warnings.append(f"chart `{cid}` referenced by a section but not in charts.yaml")
            continue
        # Find first section that captioned this chart.
        caption = ""
        for s in sections_block:
            sec = project.load_section(s["id"])
            cap = (sec.design_final.chart_captions or {}).get(cid)
            if cap:
                caption = cap
                caption_owner[cid] = s["id"]
                break
        chart_dict = chart.model_dump()
        chart_dict["caption"] = caption
        charts_block.append(chart_dict)

    # Facts block — every fact referenced by an included section.
    facts_block = [f.model_dump() for f in facts_mgr.load().facts]

    # Assemble and write bundle.
    bundle = {
        "report": report_block,
        "sections": sections_block,
        "charts": charts_block,
        "facts": facts_block,
    }
    output_dir = project.root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / OUTPUT_BUNDLE_FILENAME
    bundle_path.write_text(
        yaml.safe_dump(bundle, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    return ComposeDesignResult(
        bundle_path=bundle_path,
        sections_included=len(sections_block),
        sections_skipped=skipped,
        warnings=warnings,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_compose_design.py -v`
Expected: 4 tests pass.

- [ ] **Step 5: Commit (or skip)**

---

### Task 9: CLI — `design` and `accept-design` commands

**Files:**
- Modify: `report_builder/cli.py`
- Create: `tests/test_design_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_design_cli.py`:

```python
"""CLI smoke tests for the new design-pack commands."""

from __future__ import annotations

import shutil
from pathlib import Path

import yaml
from typer.testing import CliRunner

from report_builder.cli import app
from report_builder.schemas import (
    DesignDeslopRecord,
    DesignFinal,
    DesignRecord,
    Section,
    SectionStatus,
    body_hash,
)


runner = CliRunner()


def _stub_design_pack_call(monkeypatch):
    """Force generate_design_pack to use a deterministic agent."""
    from report_builder import design_pack as dp

    def stub(prompt: str) -> dict:
        return {
            "kicker": "Markets",
            "headline": "Markets cycle",
            "dek": "Patient capital captures the rebound through compression",
            "tldr": (
                "Markets cycle through sentiment-driven peaks and troughs. "
                "Compressed returns reward patient capital that holds."
            ),
            "pull_quotes": [{"text": "cyclical patterns", "source_para": 0}],
            "chart_captions": {},
        }

    real_generate = dp.generate_design_pack

    def patched(project, section, **kwargs):
        kwargs.setdefault("agent_call", stub)
        return real_generate(project, section, **kwargs)

    monkeypatch.setattr(dp, "generate_design_pack", patched)


def test_design_command_runs_pack_and_marks_awaiting(tmp_project, monkeypatch):
    _stub_design_pack_call(monkeypatch)
    # Also stub design_deslop's default rewriter so it doesn't try Anthropic.
    from report_builder import design_deslop
    monkeypatch.setattr(
        design_deslop, "_default_rewriter",
        lambda slot, current, violations, budget: current,
    )

    result = runner.invoke(
        app, ["design", "01-overview", "--project", str(tmp_project.root)],
    )
    assert result.exit_code == 0, result.output
    section = tmp_project.load_section("01-overview")
    assert section.status == SectionStatus.AWAITING_DESIGN_ACCEPT.value
    assert section.design is not None
    assert section.design.headline == "Markets cycle"


def test_accept_design_promotes_to_design_accepted(tmp_project):
    section = tmp_project.load_section("01-overview")
    h = body_hash(section.final.text)
    section.status = SectionStatus.AWAITING_DESIGN_ACCEPT
    section.design = DesignRecord(
        body_hash=h, kicker="x", headline="y", dek="z w", tldr="aa bb cc dd ee ff gg hh ii jj kk ll mm nn oo pp",
        pull_quotes=[], chart_captions={},
    )
    section.design_deslop = DesignDeslopRecord()
    tmp_project.save_section(section)

    result = runner.invoke(
        app, ["accept-design", "01-overview", "--project", str(tmp_project.root)],
    )
    assert result.exit_code == 0, result.output
    section = tmp_project.load_section("01-overview")
    assert section.status == SectionStatus.DESIGN_ACCEPTED.value
    assert section.design_final is not None
    assert section.design_final.accepted_at is not None
    assert section.design_final.headline == "y"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_design_cli.py -v`
Expected: failures — `Usage: ... No such command 'design'` / `'accept-design'`.

- [ ] **Step 3: Add the CLI commands**

Edit `report_builder/cli.py`. After the existing `accept` command (around line 415), add:

```python
# ---------------------------------------------------------------------------
# design — generate per-section design pack
# ---------------------------------------------------------------------------


@app.command()
def design(
    section_id: str = typer.Argument(..., help="Section id to generate the design pack for."),
    project_path: Path = typer.Option(
        Path.cwd(), "--project", "-p",
        help="Path to the project directory.",
        exists=True, file_okay=False, readable=True,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Generate the per-section design pack and run it through design-deslop."""
    _configure_logging(verbose=verbose)
    project = _load_or_die(project_path)

    from .schemas import DesignDeslopRecord, SectionStatus
    from .design_pack import generate_design_pack
    from .design_deslop import run_design_deslop

    section = project.load_section(section_id)
    st = section.status if isinstance(section.status, str) else section.status.value
    if st not in (SectionStatus.ACCEPTED.value,
                  SectionStatus.AWAITING_DESIGN_ACCEPT.value,
                  SectionStatus.DESIGN_ACCEPTED.value):
        typer.secho(
            f"Section {section_id} status is `{section.status}` — must be `accepted` "
            f"(or have an existing design pack to regenerate).",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    pack_result = generate_design_pack(project, section)
    if pack_result.error or pack_result.record is None:
        typer.secho(f"Error: {pack_result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    rec = pack_result.record
    paraphrased = {
        "kicker": rec.kicker,
        "headline": rec.headline,
        "dek": rec.dek,
        "tldr": rec.tldr,
    }
    paraphrased.update({f"caption:{cid}": cap for cid, cap in rec.chart_captions.items()})
    deslop_result = run_design_deslop(paraphrased)

    # Apply deslop rewrites back onto the record.
    rec.kicker = deslop_result.slots.get("kicker", rec.kicker)
    rec.headline = deslop_result.slots.get("headline", rec.headline)
    rec.dek = deslop_result.slots.get("dek", rec.dek)
    rec.tldr = deslop_result.slots.get("tldr", rec.tldr)
    rec.chart_captions = {
        k.split(":", 1)[1]: v for k, v in deslop_result.slots.items()
        if k.startswith("caption:")
    }

    section.design = rec
    section.design_deslop = DesignDeslopRecord(
        iterations=deslop_result.iterations,
        best_iteration=deslop_result.best_iteration,
        history=deslop_result.history,
    )
    section.status = SectionStatus.AWAITING_DESIGN_ACCEPT
    project.save_section(section)

    typer.secho(f"Design pack ready for {section_id}.", fg=typer.colors.GREEN)
    typer.echo(f"  headline: {rec.headline}")
    typer.echo(f"  dek: {rec.dek}")
    typer.echo(f"  pull quotes: {len(rec.pull_quotes)} verified")
    if pack_result.failed_quotes:
        typer.secho(
            f"  WARNING: {len(pack_result.failed_quotes)} pull quote(s) failed verification — "
            f"agent could not produce verbatim extractions after retries",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"  deslop iterations: {deslop_result.iterations}")
    typer.echo(f"\nReview with `report show {section_id}` then run `report accept-design {section_id}`.")


# ---------------------------------------------------------------------------
# accept-design — lock the design pack as design_final
# ---------------------------------------------------------------------------


@app.command(name="accept-design")
def accept_design(
    section_id: str = typer.Argument(..., help="Section id to accept the design pack for."),
    project_path: Path = typer.Option(
        Path.cwd(), "--project", "-p",
        help="Path to the project directory.",
        exists=True, file_okay=False, readable=True,
    ),
) -> None:
    """Lock a design pack as design-accepted; mirrors `accept` but for design slots."""
    from datetime import datetime, timezone
    from .schemas import DesignFinal, SectionStatus

    project = _load_or_die(project_path)
    section = project.load_section(section_id)
    st = section.status if isinstance(section.status, str) else section.status.value
    if st != SectionStatus.AWAITING_DESIGN_ACCEPT.value:
        typer.secho(
            f"Section {section_id} status is `{section.status}`, not awaiting-design-accept. "
            f"Run `report design {section_id}` first.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)
    if section.design is None:
        typer.secho(
            f"Section {section_id} has no design pack to accept.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(code=1)

    rec = section.design
    section.design_final = DesignFinal(
        body_hash=rec.body_hash,
        kicker=rec.kicker,
        headline=rec.headline,
        dek=rec.dek,
        tldr=rec.tldr,
        pull_quotes=rec.pull_quotes,
        chart_captions=rec.chart_captions,
        accepted_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    section.status = SectionStatus.DESIGN_ACCEPTED
    project.save_section(section)
    typer.secho(f"Design accepted: {section_id}", fg=typer.colors.GREEN)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_design_cli.py -v`
Expected: 2 tests pass.

- [ ] **Step 5: Commit (or skip)**

---

### Task 10: CLI — `polish` and `compose-design` commands

**Files:**
- Modify: `report_builder/cli.py` (append two more commands)
- Modify: `tests/test_design_cli.py` (append tests)

- [ ] **Step 1: Append failing tests**

Append to `tests/test_design_cli.py`:

```python
def test_polish_command_writes_polish_yaml(tmp_project, monkeypatch):
    section = tmp_project.load_section("01-overview")
    h = body_hash(section.final.text)
    section.status = SectionStatus.DESIGN_ACCEPTED
    section.design_final = DesignFinal(
        body_hash=h, kicker="Markets", headline="Markets cycle", dek="Patient capital",
        tldr="Markets cycle through sentiment-driven peaks and troughs steady enough for capital to track.",
        pull_quotes=[], chart_captions={},
        accepted_at="2026-04-28T00:00:00+00:00",
    )
    tmp_project.save_section(section)

    from report_builder import design_polish as dp_mod
    from report_builder import design_deslop as deslop_mod

    # Long exec_summary (80+ words) so design-deslop has nothing to fix.
    long_exec = (
        "Markets exhibit cyclical patterns driven by collective sentiment rather than underlying value. "
        "When investors crowd into a single thesis, returns compress and the asymmetry between conviction "
        "and price disappears almost entirely. Patient capital that holds through compression captures "
        "the rebound when sentiment eventually normalizes. Three cycles in the past decade illustrate "
        "the pattern: energy in 2022, rate-shock repricing in 2023, AI-thesis crowding in 2025. The "
        "prescription is positional discipline rather than prediction. Predict less and position more "
        "carefully, accepting that timing is harder than direction."
    )

    def stub(prompt):
        return {
            "subtitle": "How patient capital wins through compression",
            "cover_kicker": "Markets",
            "exec_summary": long_exec,
            "key_findings": ["Cycles compress returns", "Patience captures rebounds", "Discipline beats prediction"],
            "toc_headlines": ["Markets cycle"],
            "cover_pull_quote": {"text": "cyclical patterns", "source_section": "01-overview"},
        }

    real = dp_mod.generate_polish

    def patched(project, **kwargs):
        kwargs.setdefault("agent_call", stub)
        return real(project, **kwargs)

    monkeypatch.setattr(dp_mod, "generate_polish", patched)
    # No-op rewriter so design-deslop doesn't reach for the Anthropic SDK.
    monkeypatch.setattr(
        deslop_mod, "_default_rewriter",
        lambda slot, current, violations, budget: current,
    )

    result = runner.invoke(app, ["polish", "--project", str(tmp_project.root)])
    assert result.exit_code == 0, result.output
    polish_path = tmp_project.root / "output" / "report.polish.yaml"
    assert polish_path.exists()
    payload = yaml.safe_load(polish_path.read_text(encoding="utf-8"))
    assert payload["subtitle"].startswith("How patient capital")
    assert "Markets exhibit cyclical patterns" in payload["exec_summary"]


def test_compose_design_command_writes_bundle(tmp_project):
    section = tmp_project.load_section("01-overview")
    h = body_hash(section.final.text)
    section.status = SectionStatus.DESIGN_ACCEPTED
    section.design_final = DesignFinal(
        body_hash=h, kicker="Markets", headline="Markets cycle", dek="Patient capital",
        tldr="Markets cycle through sentiment-driven peaks and troughs steady enough for capital to track.",
        pull_quotes=[], chart_captions={},
        accepted_at="2026-04-28T00:00:00+00:00",
    )
    tmp_project.save_section(section)

    # Pre-write a polish file so compose-design picks it up.
    output_dir = tmp_project.root / "output"
    output_dir.mkdir(exist_ok=True)
    (output_dir / "report.polish.yaml").write_text(
        yaml.safe_dump({
            "subtitle": "x", "cover_kicker": "M", "exec_summary": "y" * 80,
            "key_findings": ["a", "b", "c"], "toc_headlines": ["Markets cycle"],
            "cover_pull_quote": None,
        }), encoding="utf-8",
    )

    result = runner.invoke(app, ["compose-design", "--project", str(tmp_project.root)])
    assert result.exit_code == 0, result.output
    bundle_path = output_dir / "report.design.yaml"
    assert bundle_path.exists()
    bundle = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))
    assert bundle["report"]["subtitle"] == "x"
    assert bundle["sections"][0]["headline"] == "Markets cycle"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_design_cli.py -v`
Expected: failures — no `polish` / `compose-design` commands.

- [ ] **Step 3: Implement the commands**

Append to `report_builder/cli.py` (after `accept_design`):

```python
# ---------------------------------------------------------------------------
# polish — end-of-pipeline report-level pack
# ---------------------------------------------------------------------------


@app.command()
def polish(
    project_path: Path = typer.Option(
        Path.cwd(), "--project", "-p",
        help="Path to the project directory.",
        exists=True, file_okay=False, readable=True,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Generate the report-level polish pack, run it through design-deslop,
    and write to output/report.polish.yaml. Mirrors the design command's
    generate-then-deslop flow but at the whole-report level."""
    import yaml
    _configure_logging(verbose=verbose)
    project = _load_or_die(project_path)

    from .design_polish import generate_polish
    from .design_deslop import run_design_deslop

    result = generate_polish(project)
    if result.error:
        typer.secho(f"Error: {result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    rb = result.report_block
    # Run design-deslop on every paraphrased slot. cover_pull_quote is verbatim;
    # toc_headlines and key_findings are lists — flatten with index suffixes so
    # each gets scored individually, then unflatten when applying back.
    flat: dict[str, str] = {
        "subtitle": str(rb.get("subtitle", "")),
        "cover_kicker": str(rb.get("cover_kicker", "")),
        "exec_summary": str(rb.get("exec_summary", "")),
    }
    for i, kf in enumerate(rb.get("key_findings", [])):
        flat[f"key_finding:{i}"] = str(kf)
    for i, th in enumerate(rb.get("toc_headlines", [])):
        flat[f"toc_headline:{i}"] = str(th)

    # Map list-element keys to a base slot name LENGTH_BOUNDS knows about.
    # run_design_deslop scores by exact slot name, so we need to alias.
    # Easiest path: rename the keys to their base slot type before scoring,
    # then map back. We do this via a translation pass.
    SLOT_FOR_KEY = {
        "subtitle": "subtitle",
        "cover_kicker": "cover_kicker",
        "exec_summary": "exec_summary",
    }
    for k in list(flat):
        if k.startswith("key_finding:"):
            SLOT_FOR_KEY[k] = "key_finding"
        elif k.startswith("toc_headline:"):
            SLOT_FOR_KEY[k] = "headline"  # toc headlines share headline bounds

    # Score each slot one at a time under its base slot name (so list items
    # like key_finding:0 use the base "key_finding" length bound).
    from .design_deslop import _default_rewriter

    deslopped = dict(flat)
    deslop_iters = 0
    for key, text in flat.items():
        base = SLOT_FOR_KEY[key]
        single = run_design_deslop(
            {base: text}, rewriter=_default_rewriter, max_iterations=3,
        )
        deslopped[key] = single.slots[base]
        deslop_iters += single.iterations

    # Reassemble into the report_block shape.
    polished_rb = dict(rb)
    polished_rb["subtitle"] = deslopped["subtitle"]
    polished_rb["cover_kicker"] = deslopped["cover_kicker"]
    polished_rb["exec_summary"] = deslopped["exec_summary"]
    polished_rb["key_findings"] = [
        deslopped[f"key_finding:{i}"] for i in range(len(rb.get("key_findings", [])))
    ]
    polished_rb["toc_headlines"] = [
        deslopped[f"toc_headline:{i}"] for i in range(len(rb.get("toc_headlines", [])))
    ]

    output_dir = project.root / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    polish_path = output_dir / "report.polish.yaml"
    polish_path.write_text(
        yaml.safe_dump(polished_rb, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    typer.secho(f"Polish pack written: {polish_path}", fg=typer.colors.GREEN)
    typer.echo(f"  deslop iterations (total across slots): {deslop_iters}")
    if result.failed_cover_quote:
        typer.secho(
            "  WARNING: cover pull quote failed verbatim verification — review before bundling",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"\nReview {polish_path}, then run `report compose-design`.")


# ---------------------------------------------------------------------------
# compose-design — bundle into one report.design.yaml for Claude Design upload
# ---------------------------------------------------------------------------


@app.command(name="compose-design")
def compose_design_cmd(
    project_path: Path = typer.Option(
        Path.cwd(), "--project", "-p",
        help="Path to the project directory.",
        exists=True, file_okay=False, readable=True,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Bundle every design-accepted section + the polish pack into output/report.design.yaml."""
    import yaml
    _configure_logging(verbose=verbose)
    project = _load_or_die(project_path)

    from .compose_design import compose_design

    polish_path = project.root / "output" / "report.polish.yaml"
    polish_block: Optional[dict] = None
    if polish_path.exists():
        polish_block = yaml.safe_load(polish_path.read_text(encoding="utf-8"))

    result = compose_design(project, polish_block=polish_block)
    if result.error:
        typer.secho(f"Error: {result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    typer.secho(
        f"Bundled {result.sections_included} section(s) -> {result.bundle_path}",
        fg=typer.colors.GREEN,
    )
    if result.sections_skipped:
        typer.echo(f"  Skipped (not design-accepted): {', '.join(result.sections_skipped)}")
    for w in result.warnings:
        typer.secho(f"  warning: {w}", fg=typer.colors.YELLOW)
    typer.echo(f"\nUpload {result.bundle_path} to Claude Design.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_design_cli.py -v`
Expected: all 4 tests in the file pass.

- [ ] **Step 5: Commit (or skip)**

---

### Task 11: End-to-end integration test

**Files:**
- Create: `tests/test_design_integration.py`

- [ ] **Step 1: Write the integration test**

Create `tests/test_design_integration.py`:

```python
"""End-to-end: run the full design-pack flow on a fixture project, assert
the bundled YAML validates and pull-quote substrings are real."""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from report_builder.cli import app
from report_builder.schemas import (
    DesignDeslopRecord,
    DesignFinal,
    DesignRecord,
    SectionStatus,
    body_hash,
)


runner = CliRunner()


def test_full_design_flow_end_to_end(tmp_project, monkeypatch):
    """Fully exercise: design -> accept-design -> polish -> compose-design."""
    # ---- Stub the agent calls ----
    from report_builder import design_pack as dp_mod
    from report_builder import design_polish as polish_mod
    from report_builder import design_deslop as deslop_mod

    def pack_stub(prompt):
        return {
            "kicker": "Markets",
            "headline": "Markets cycle",
            "dek": "Patient capital captures the rebound through compression",
            "tldr": (
                "Markets cycle through sentiment-driven peaks and troughs. "
                "Compressed returns reward capital that holds patiently."
            ),
            "pull_quotes": [{"text": "cyclical patterns", "source_para": 0}],
            "chart_captions": {},
        }

    def polish_stub(prompt):
        return {
            "subtitle": "How patient capital wins through compression",
            "cover_kicker": "Markets",
            "exec_summary": (
                "Markets exhibit cyclical patterns driven by collective sentiment rather than "
                "underlying value. When investors crowd into a single thesis, returns compress "
                "and the asymmetry between conviction and price disappears almost entirely. "
                "Patient capital that holds through compression captures the rebound when "
                "sentiment eventually normalizes. Three cycles in the past decade illustrate "
                "the pattern: energy in 2022, rate-shock repricing in 2023, AI-thesis crowding "
                "in 2025. The prescription is positional discipline rather than prediction. "
                "Predict less and position more carefully, accepting that timing is harder "
                "than direction."
            ),
            "key_findings": [
                "Cycles compress returns",
                "Patience captures rebounds",
                "Discipline beats prediction",
            ],
            "toc_headlines": ["Markets cycle"],
            "cover_pull_quote": {
                "text": "cyclical patterns", "source_section": "01-overview",
            },
        }

    real_pack = dp_mod.generate_design_pack
    real_polish = polish_mod.generate_polish

    monkeypatch.setattr(
        dp_mod, "generate_design_pack",
        lambda project, section, **kw: real_pack(project, section, agent_call=pack_stub, **{k: v for k, v in kw.items() if k != "agent_call"}),
    )
    monkeypatch.setattr(
        polish_mod, "generate_polish",
        lambda project, **kw: real_polish(project, agent_call=polish_stub, **{k: v for k, v in kw.items() if k != "agent_call"}),
    )
    monkeypatch.setattr(
        deslop_mod, "_default_rewriter",
        lambda slot, current, violations, budget: current,
    )

    proj_arg = ["--project", str(tmp_project.root)]

    # 1. design
    r = runner.invoke(app, ["design", "01-overview", *proj_arg])
    assert r.exit_code == 0, r.output

    # 2. accept-design
    r = runner.invoke(app, ["accept-design", "01-overview", *proj_arg])
    assert r.exit_code == 0, r.output

    # 3. polish
    r = runner.invoke(app, ["polish", *proj_arg])
    assert r.exit_code == 0, r.output

    # 4. compose-design
    r = runner.invoke(app, ["compose-design", *proj_arg])
    assert r.exit_code == 0, r.output

    # ---- Assertions on the produced bundle ----
    bundle_path = tmp_project.root / "output" / "report.design.yaml"
    assert bundle_path.exists()
    bundle = yaml.safe_load(bundle_path.read_text(encoding="utf-8"))

    # 1. Schema shape
    assert "report" in bundle
    assert "sections" in bundle
    assert "charts" in bundle
    assert "facts" in bundle

    # 2. Pull-quote verbatim invariant
    section = bundle["sections"][0]
    body = section["body"]
    for q in section["pull_quotes"]:
        assert q["text"] in body, f"pull quote {q['text']!r} not in body"

    # 3. No paraphrased slot exceeds its length bound
    from report_builder.design_deslop import LENGTH_BOUNDS
    for slot in ("kicker", "headline", "dek", "tldr"):
        wc = len((section.get(slot) or "").split())
        lo, hi = LENGTH_BOUNDS[slot]
        assert lo <= wc <= hi, f"{slot} word count {wc} out of bounds {lo}-{hi}"

    rep = bundle["report"]
    for slot in ("subtitle", "cover_kicker", "exec_summary"):
        wc = len((rep.get(slot) or "").split())
        lo, hi = LENGTH_BOUNDS[slot]
        assert lo <= wc <= hi, f"{slot} word count {wc} out of bounds {lo}-{hi}"

    # 4. toc_headlines deduplicated (no exact duplicates)
    assert len(rep["toc_headlines"]) == len(set(rep["toc_headlines"]))

    # 5. cover_pull_quote text is verbatim in the cited section
    cover = rep["cover_pull_quote"]
    cited_section = next(s for s in bundle["sections"] if s["id"] == cover["source_section"])
    assert cover["text"] in cited_section["body"]
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_design_integration.py -v`
Expected: passes (or fails with a specific assertion that surfaces an integration bug — debug the bug, not the test).

- [ ] **Step 3: Run the full test suite**

Run: `pytest tests/ -v`
Expected: all tests across all files pass.

- [ ] **Step 4: Manual smoke check (optional, requires real API keys)**

Run on a real project (e.g., `Scripts/report-builder/<some-real-project>/`):

```bash
report design <section-id> --project <real-project>
report show <section-id> --project <real-project>     # eyeball the design copy
report accept-design <section-id> --project <real-project>
# repeat for all sections, then:
report polish --project <real-project>
report compose-design --project <real-project>
# Open output/report.design.yaml, eyeball it, drag into Claude Design.
```

- [ ] **Step 5: Commit (or skip)**

---

## Out of scope

The following items are deferred per the spec's "Follow-ups" section. Do not implement in this plan:

- Programmatic Claude Design ingest via `mcp__*__generate-design-structured`. Upload remains manual.
- Round-trip diff (export from Claude Design, diff against `report.design.yaml`). No drift detection.
- Brand kit / theme hints in the bundle.
- Modifying `report accept` to auto-trigger design generation. The spec stub'd this as "auto-trigger after body approval"; this plan keeps `accept` and `design` as separate commands. Reasoning: auto-trigger adds an LLM call to a previously-quick command, which is surprising. Running `report next` → `report accept` → `report design <id>` is one extra command, which is acceptable.
- A formal `accept-polish` command. The spec mentioned "human approve polish → compose-design"; this plan has the user manually review `output/report.polish.yaml` after running `report polish`, then run `report compose-design` directly. Polish runs once per report, not N times like per-section design, so a formal accept gate adds friction without commensurate value.
- Modifying `report show <id>` to display the design pack. The existing `show` will display the section YAML which already includes `design`/`design_final` thanks to schema additions; specialized rendering is out of scope.
- OpenAI fallback for the default agents. Both `_default_agent_call` and `_default_rewriter` use Anthropic. If the user runs with only `OPENAI_API_KEY`, the agents fail at runtime — they'll need to set `ANTHROPIC_API_KEY`.

## Spec divergences

Two deliberate simplifications relative to the spec:

- **Design-pack persistence.** The spec said per-section files like `sections/<id>.design.yaml`. This plan instead stores the design pack inline on the existing `Section` schema (new fields: `design`, `design_deslop`, `design_final`, `design_versions`). Reasoning: reuses the existing `save_section`/`load_section` machinery; the user's existing `report show <id>` flow works for design too; one fewer file class to maintain. Trade-off: the section YAML grows.
- **No formal accept-polish gate** (see Out of scope above).
