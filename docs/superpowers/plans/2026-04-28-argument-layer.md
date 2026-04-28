# Argument Layer Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Insert an argument-graph generation phase between research and drafting that produces a per-section claim tree, persists it on the section yaml, soft-pauses the pipeline for user review, then feeds the graph to the drafter as structural input.

**Architecture:** New module `report_builder/argument.py` with the `propose_argument` agent (mirrors `design_pack.py` shape — same agent_call indirection, validation-with-retry, JSON output coercion). `run_pipeline` becomes resumable, branching on `research_notes` / `argument` / `drafts` state to advance from the next unblocked phase. Drafter prompt gains a rendered argument-graph block as structural backbone. Soft gate, no new statuses.

**Tech Stack:** Python 3.10+, Pydantic v2, Typer CLI, Anthropic SDK, pytest, PyYAML.

**Spec:** [`docs/superpowers/specs/2026-04-28-argument-layer-design.md`](../specs/2026-04-28-argument-layer-design.md)

---

## Working Directory

> **All commands assume cwd is `C:/Users/Rachel/Desktop/Scripts/report-builder/`.** `cd` there once at the start. File paths are project-relative.

The plan and spec live inside the Ottoneu repo's worktree (`Scripts/.git`), but the code being modified is in the report-builder project at `Scripts/report-builder/`. This is the same repo as the design-pack feature was implemented against; it has its own local git history.

**Most recent report-builder commit (baseline for this plan):** `bb37b58` (design-pack feature complete, 41 tests passing).

---

## File Structure

**New files:**
- `report_builder/argument.py` — agent module with `propose_argument` and validation
- `report_builder/prompts/argument_system.md` — agent system prompt
- `tests/test_schemas_argument.py` — schema round-trip + invariant tests
- `tests/test_argument.py` — agent module tests (happy path + validation/retry)
- `tests/test_argument_pipeline.py` — pipeline resume logic tests
- `tests/test_argument_drafter.py` — drafter prompt integration tests
- `tests/test_argument_cli.py` — CLI smoke tests
- `tests/test_argument_integration.py` — end-to-end fixture run

**Modified files:**
- `report_builder/schemas.py` — add `Claim`, `ArgumentGraph`; extend `Section` with `research_notes` and `argument`
- `report_builder/memory.py` — add `render_argument_graph(section) -> str`
- `report_builder/pipeline.py` — resume logic + new propose-argument phase + persist research_notes
- `report_builder/drafters.py` — inject rendered argument graph into the user prompt
- `report_builder/cli.py` — add `--argument` flag to `show`; add `report argue <section-id>` command
- `tests/conftest.py` — add `tmp_project_post_research` fixture

---

## Tasks

### Task 1: Schema additions

**Files:**
- Modify: `report_builder/schemas.py`
- Modify: `tests/conftest.py` (add new fixture)
- Create: `tests/test_schemas_argument.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_schemas_argument.py`:

```python
"""Tests for argument-layer additions to schemas.py."""

from __future__ import annotations

import pytest

from report_builder.schemas import (
    ArgumentGraph,
    Claim,
    Section,
)


def test_claim_round_trip():
    c = Claim(
        id="c1",
        text="Five nodes hold 73% of locations",
        depends_on=[],
        supports_via=["fact:node_distribution"],
        is_caveat=False,
    )
    payload = c.model_dump()
    restored = Claim.model_validate(payload)
    assert restored.id == "c1"
    assert restored.supports_via == ["fact:node_distribution"]
    assert restored.is_caveat is False


def test_claim_defaults():
    c = Claim(id="c1", text="hello")
    assert c.depends_on == []
    assert c.supports_via == []
    assert c.is_caveat is False


def test_argument_graph_round_trip():
    g = ArgumentGraph(
        conclusion_id="c2",
        claims=[
            Claim(id="c1", text="premise", supports_via=["fact:x"]),
            Claim(id="c2", text="conclusion", depends_on=["c1"]),
        ],
    )
    restored = ArgumentGraph.model_validate(g.model_dump())
    assert restored.conclusion_id == "c2"
    assert len(restored.claims) == 2
    assert restored.claims[1].depends_on == ["c1"]


def test_section_carries_argument_fields():
    sec = Section(id="x")
    assert sec.research_notes is None
    assert sec.argument is None


def test_section_argument_round_trip():
    sec = Section(
        id="x",
        research_notes="some research narrative",
        argument=ArgumentGraph(
            conclusion_id="c1",
            claims=[Claim(id="c1", text="conclusion")],
        ),
    )
    payload = sec.model_dump()
    restored = Section.model_validate(payload)
    assert restored.research_notes == "some research narrative"
    assert restored.argument is not None
    assert restored.argument.conclusion_id == "c1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_schemas_argument.py -v`
Expected: ImportError on `Claim` / `ArgumentGraph`.

- [ ] **Step 3: Implement schema additions**

Edit `report_builder/schemas.py`. Locate the existing `DesignFinal` class (added in the design-pack feature). Add the following AFTER `DesignFinal` and BEFORE the `Section` class:

```python
class Claim(_Base):
    """A single node in a section's argument graph.

    Leaf claims have empty depends_on and reference grounding facts via
    supports_via. Internal claims have non-empty depends_on; they may also
    reference facts directly. Caveats are leaf claims that the prose must
    acknowledge as constraints rather than establish as findings.
    """
    id: str
    text: str
    depends_on: list[str] = Field(default_factory=list)
    supports_via: list[str] = Field(default_factory=list)
    is_caveat: bool = False


class ArgumentGraph(_Base):
    """The argument structure for a single section. conclusion_id points at
    the claim that is THE conclusion — the realized version of the section's
    load_bearing_finding."""
    conclusion_id: str
    claims: list[Claim]
```

Then modify the existing `Section` class to add two fields. Find the design-pack additions block and add the new fields immediately after:

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
    # --- argument layer fields (additive) ---
    research_notes: Optional[str] = None
    argument: Optional[ArgumentGraph] = None
```

- [ ] **Step 4: Add the post-research fixture to conftest.py**

Edit `tests/conftest.py`. Append a new fixture after the existing `deterministic_rewriter` fixture:

```python
@pytest.fixture
def tmp_project_post_research(tmp_project) -> ReportProject:
    """A tmp_project where the section has research_notes populated and
    is in DRAFTING state — the pipeline state just before propose-argument
    would run.

    Strips section.final (the existing fixture leaves it as ACCEPTED) so
    the section is genuinely pre-draft.
    """
    from report_builder.schemas import SectionBrief, SectionStatus

    section = tmp_project.load_section("01-overview")
    section.status = SectionStatus.DRAFTING
    section.final = None  # strip the ACCEPTED-state final from the base fixture
    section.brief = SectionBrief(
        load_bearing_finding="Markets are markets.",
        target_words=200,
    )
    section.research_notes = (
        "Research surfaced 8,788 records in the dataset, of which 6,312 (72%) "
        "match to a company registry. The top three sectors by record count are "
        "Healthcare (1,179), Retail (908), and Education (730). Five named "
        "geographic nodes account for 73% of all locations."
    )
    tmp_project.save_section(section)
    return tmp_project
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_schemas_argument.py -v`
Expected: 5 tests pass.

Run: `pytest tests/ -v`
Expected: all prior tests still pass (41 + 5 = 46 total).

- [ ] **Step 6: Commit**

```bash
git add report_builder/schemas.py tests/conftest.py tests/test_schemas_argument.py
git commit -m "Argument layer: add Claim, ArgumentGraph schemas + post-research fixture"
```

---

### Task 2: render_argument_graph helper

**Files:**
- Modify: `report_builder/memory.py`
- Create: `tests/test_render_argument.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_render_argument.py`:

```python
"""Tests for the argument-graph tree-view renderer."""

from __future__ import annotations

from report_builder.memory import render_argument_graph
from report_builder.schemas import ArgumentGraph, Claim, Section


def _section_with_graph(graph: ArgumentGraph) -> Section:
    return Section(id="01-overview", argument=graph)


def test_render_returns_placeholder_when_no_argument():
    sec = Section(id="x")
    out = render_argument_graph(sec)
    assert "no argument graph" in out.lower() or out.strip() == ""


def test_render_simple_two_claim_graph():
    g = ArgumentGraph(
        conclusion_id="c2",
        claims=[
            Claim(id="c1", text="Five nodes hold most locations",
                  supports_via=["fact:node_distribution"]),
            Claim(id="c2", text="DLR is a portfolio of nodes",
                  depends_on=["c1"]),
        ],
    )
    out = render_argument_graph(_section_with_graph(g))
    # Conclusion should appear at the top
    assert "DLR is a portfolio of nodes" in out
    # The premise should be referenced
    assert "Five nodes hold most locations" in out
    # The fact reference should appear
    assert "fact:node_distribution" in out
    # Conclusion appears before its premise in the rendered text (top-down)
    assert out.index("DLR is a portfolio of nodes") < out.index("Five nodes hold most locations")


def test_render_groups_caveats_separately():
    g = ArgumentGraph(
        conclusion_id="c1",
        claims=[
            Claim(id="c1", text="finding text"),
            Claim(id="cv1", text="caveat text", is_caveat=True,
                  supports_via=["fact:coverage_limit"]),
        ],
    )
    out = render_argument_graph(_section_with_graph(g))
    assert "caveat text" in out
    # A "caveat" header or section should appear, separating caveats from findings
    assert "caveat" in out.lower()


def test_render_shows_dependencies():
    g = ArgumentGraph(
        conclusion_id="c3",
        claims=[
            Claim(id="c1", text="premise one", supports_via=["fact:a"]),
            Claim(id="c2", text="premise two", supports_via=["fact:b"]),
            Claim(id="c3", text="conclusion", depends_on=["c1", "c2"]),
        ],
    )
    out = render_argument_graph(_section_with_graph(g))
    # All three claims appear
    assert "premise one" in out
    assert "premise two" in out
    assert "conclusion" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_render_argument.py -v`
Expected: ImportError or AttributeError on `render_argument_graph`.

- [ ] **Step 3: Implement render_argument_graph**

Edit `report_builder/memory.py`. Append at the end of the file:

```python
def render_argument_graph(section: "Section") -> str:
    """Tree-view render of a section's argument graph for drafter consumption
    and `report show --argument`.

    Layout:
        Conclusion (the section must establish this):
          <conclusion text>

        Supporting claims (in dependency order):
          c1. <text>
              grounded in: fact:X, fact:Y
          c2. <text>
              builds on: c1
              grounded in: fact:Z

        Methodological caveats to acknowledge:
          - <caveat text>
            grounded in: fact:W

    If section.argument is None, returns a single-line placeholder.
    """
    from .schemas import ArgumentGraph

    graph: Optional[ArgumentGraph] = section.argument
    if graph is None or not graph.claims:
        return "(no argument graph proposed yet for this section)"

    by_id = {c.id: c for c in graph.claims}
    conclusion = by_id.get(graph.conclusion_id)

    lines: list[str] = []
    lines.append("Conclusion (the section must establish this):")
    lines.append(f"  {conclusion.text if conclusion is not None else '(missing conclusion)'}")
    lines.append("")

    # Supporting claims: every non-caveat, non-conclusion claim, in topo order.
    findings = [c for c in graph.claims
                if c.id != graph.conclusion_id and not c.is_caveat]
    if findings:
        lines.append("Supporting claims (in dependency order):")
        for c in _topological_order(findings, by_id):
            lines.append(f"  {c.id}. {c.text}")
            if c.depends_on:
                lines.append(f"      builds on: {', '.join(c.depends_on)}")
            if c.supports_via:
                lines.append(f"      grounded in: {', '.join(c.supports_via)}")
        lines.append("")

    # Caveats grouped at the bottom.
    caveats = [c for c in graph.claims if c.is_caveat]
    if caveats:
        lines.append("Methodological caveats to acknowledge:")
        for c in caveats:
            lines.append(f"  - {c.text}")
            if c.supports_via:
                lines.append(f"    grounded in: {', '.join(c.supports_via)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _topological_order(claims: list, by_id: dict) -> list:
    """Return claims in dependency order (claim X comes before any claim that
    lists X in depends_on). Uses Kahn's algorithm; falls back to input order
    if the graph contains cycles."""
    in_scope = {c.id for c in claims}
    indegree = {c.id: sum(1 for d in c.depends_on if d in in_scope) for c in claims}
    queue = [c for c in claims if indegree[c.id] == 0]
    visited: set[str] = set()
    out: list = []
    while queue:
        node = queue.pop(0)
        if node.id in visited:
            continue
        visited.add(node.id)
        out.append(node)
        for c in claims:
            if node.id in c.depends_on and c.id not in visited:
                indegree[c.id] -= 1
                if indegree[c.id] == 0:
                    queue.append(c)
    if len(out) < len(claims):
        # Cycle detected — append remaining in input order so we don't lose them.
        for c in claims:
            if c.id not in visited:
                out.append(c)
    return out
```

You'll need to add `Optional` to the imports at the top of `memory.py` if it's not already there. Check the existing imports — Python's `typing.Optional` is the standard import. Add it to the existing typing import line or as a new line.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_render_argument.py -v`
Expected: 4 tests pass.

Run: `pytest tests/ -v`
Expected: all 50 tests pass.

- [ ] **Step 5: Commit**

```bash
git add report_builder/memory.py tests/test_render_argument.py
git commit -m "Argument layer: add render_argument_graph helper"
```

---

### Task 3: propose_argument agent (initial)

**Files:**
- Create: `report_builder/argument.py`
- Create: `report_builder/prompts/argument_system.md`
- Create: `tests/test_argument.py`

This task implements the agent's happy path. Validation + retry comes in Task 4.

- [ ] **Step 1: Write the failing test**

Create `tests/test_argument.py`:

```python
"""Tests for the argument-graph proposal agent."""

from __future__ import annotations

from report_builder.argument import (
    ProposeArgumentResult,
    propose_argument,
)
from report_builder.facts import FactsManager
from report_builder.schemas import ArgumentGraph, Claim


def test_propose_argument_uses_stub_agent(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    facts_mgr = FactsManager(tmp_project_post_research)

    canned = {
        "conclusion_id": "c2",
        "claims": [
            {
                "id": "c1",
                "text": "Five nodes hold most locations",
                "depends_on": [],
                "supports_via": [],
                "is_caveat": False,
            },
            {
                "id": "c2",
                "text": "DLR is a portfolio of nodes",
                "depends_on": ["c1"],
                "supports_via": [],
                "is_caveat": False,
            },
        ],
    }

    def stub_agent(prompt) -> dict:
        return canned

    result = propose_argument(
        tmp_project_post_research,
        section,
        section.research_notes,
        facts_mgr,
        agent_call=stub_agent,
    )
    assert isinstance(result, ProposeArgumentResult)
    assert result.error is None
    assert result.graph is not None
    assert isinstance(result.graph, ArgumentGraph)
    assert result.graph.conclusion_id == "c2"
    assert len(result.graph.claims) == 2
    assert result.graph.claims[0].text == "Five nodes hold most locations"


def test_propose_argument_errors_when_research_notes_missing(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    facts_mgr = FactsManager(tmp_project_post_research)

    result = propose_argument(
        tmp_project_post_research,
        section,
        research_notes=None,
        facts_mgr=facts_mgr,
        agent_call=lambda p: {},
    )
    assert result.graph is None
    assert result.error is not None
    assert "research" in result.error.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_argument.py -v`
Expected: ImportError on `report_builder.argument`.

- [ ] **Step 3: Create the system prompt**

Create `report_builder/prompts/argument_system.md`:

```markdown
# Argument Proposal Agent — System

You produce an **argument graph** for a single research-report section.
The graph is the structural backbone the drafter will use to write prose.
Your output is JSON only.

## What an argument graph is

The graph is a tree of claims. The root is the section's conclusion (the
realized form of the section's load-bearing finding). Internal nodes are
sub-claims that build to the conclusion. Leaves are either premises
(grounded in specific facts from the project's facts catalog) or
methodological caveats (constraints the prose must acknowledge).

## Hard rules

1. **Every claim must be either grounded or derived.**
   - A leaf claim (empty `depends_on`) MUST cite at least one fact id in
     `supports_via`.
   - A non-leaf claim MUST have at least one id in `depends_on`.
   - A claim may have BOTH (a derived claim that also cites facts directly).

2. **Fact ids must exist.** Every entry in any claim's `supports_via` must
   match an `id` from the supplied facts catalog. If you cannot ground a
   claim, drop it. Do not invent fact ids.

3. **Conclusion realizes the load-bearing finding.** The claim referenced
   by `conclusion_id` is THE thing the section establishes. Its text may
   refine wording from the load-bearing finding (sharpen, qualify) but
   must not change scope.

4. **No cycles.** A claim's `depends_on` chain must not lead back to itself.

5. **Caveats are flagged.** Methodological constraints (limits of the data,
   things the section cannot establish) get `is_caveat: true`. They are
   leaf claims that the drafter will acknowledge in prose, not establish.

6. **Aim for 3–7 total claims.** Fewer than 3 is structurally trivial;
   more than 7 fragments the argument.

## Output schema

```json
{
  "conclusion_id": "string",
  "claims": [
    {
      "id": "c1",
      "text": "string",
      "depends_on": ["..."],
      "supports_via": ["fact_id", ...],
      "is_caveat": false
    }
  ]
}
```

Output JSON only. No preamble, no markdown fence required (but tolerated).
```

- [ ] **Step 4: Implement the agent module**

Create `report_builder/argument.py`:

```python
"""Per-section argument-graph proposal agent.

Reads a section's brief + research narrative + facts catalog and produces
an ArgumentGraph the drafter will use as structural input.

Public API:
    propose_argument(project, section, research_notes, facts_mgr, *, agent_call=...)
        -> ProposeArgumentResult
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any, Callable, Optional

from .facts import FactsManager
from .memory import render_facts_for_drafter, render_voice_constraints
from .schemas import ArgumentGraph, Claim, Section
from .state import ReportProject

logger = logging.getLogger(__name__)


@dataclass
class ProposeArgumentResult:
    graph: Optional[ArgumentGraph]
    error: Optional[str] = None


def _read_system_prompt() -> str:
    return (
        resources.files("report_builder.prompts")
        .joinpath("argument_system.md")
        .read_text(encoding="utf-8")
    )


def _build_user_prompt(
    project: ReportProject,
    section: Section,
    research_notes: str,
    facts_mgr: FactsManager,
) -> str:
    report = project.load_report()
    outline_entry = next(o for o in report.outline if o.id == section.id)
    return f"""# Section context

- id: {section.id}
- title: {outline_entry.title}
- load-bearing finding: {outline_entry.load_bearing_finding}

# Research narrative (from the research phase)

{research_notes}

# Facts available (your supports_via must reference ids from this list)

{render_facts_for_drafter(facts_mgr, prefer_section=section.id)}

# Voice constraints

{render_voice_constraints(report)}

Generate the argument graph. Output JSON only, matching the schema in the system prompt.
"""


def _default_agent_call(prompt_pair: tuple[str, str]) -> dict:
    """Default agent — Anthropic with JSON-mode-style coercion."""
    import anthropic
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
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    return json.loads(raw)


def propose_argument(
    project: ReportProject,
    section: Section,
    research_notes: Optional[str],
    facts_mgr: FactsManager,
    *,
    agent_call: Optional[Callable[..., dict]] = None,
) -> ProposeArgumentResult:
    """Generate the per-section argument graph."""
    if not research_notes:
        return ProposeArgumentResult(
            graph=None,
            error="cannot propose argument: section has no research_notes — run research first",
        )

    system = _read_system_prompt()
    user = _build_user_prompt(project, section, research_notes, facts_mgr)

    try:
        if agent_call is not None:
            try:
                payload = agent_call(user)
            except TypeError:
                payload = agent_call((system, user))
        else:
            payload = _default_agent_call((system, user))
    except Exception as exc:
        logger.exception("argument agent failed")
        return ProposeArgumentResult(
            graph=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    try:
        graph = _parse_graph(payload)
    except Exception as exc:
        return ProposeArgumentResult(
            graph=None,
            error=f"failed to parse graph from agent output: {exc}",
        )

    return ProposeArgumentResult(graph=graph, error=None)


def _parse_graph(payload: dict) -> ArgumentGraph:
    """Parse a payload dict into an ArgumentGraph. Raises on structural failure."""
    claims_in = payload.get("claims") or []
    claims = [
        Claim(
            id=str(c.get("id", "")),
            text=str(c.get("text", "")),
            depends_on=list(c.get("depends_on") or []),
            supports_via=list(c.get("supports_via") or []),
            is_caveat=bool(c.get("is_caveat", False)),
        )
        for c in claims_in
    ]
    return ArgumentGraph(
        conclusion_id=str(payload.get("conclusion_id", "")),
        claims=claims,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_argument.py -v`
Expected: 2 tests pass.

Run: `pytest tests/ -v`
Expected: all 52 tests pass.

- [ ] **Step 6: Commit**

```bash
git add report_builder/argument.py report_builder/prompts/argument_system.md tests/test_argument.py
git commit -m "Argument layer: propose_argument agent (happy path)"
```

---

### Task 4: propose_argument validation + retry

**Files:**
- Modify: `report_builder/argument.py`
- Modify: `tests/test_argument.py` (append)

This task adds the validation invariants from the spec (conclusion_id exists, depends_on references real ids, no cycles, supports_via references real fact ids) plus a retry loop with corrective hints when validation fails.

- [ ] **Step 1: Append failing tests**

Append to `tests/test_argument.py`:

```python
def test_propose_argument_retries_on_invalid_conclusion_id(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    facts_mgr = FactsManager(tmp_project_post_research)

    attempts = []

    def flaky_agent(prompt) -> dict:
        attempts.append(prompt)
        if len(attempts) == 1:
            # First attempt: conclusion_id doesn't exist
            return {
                "conclusion_id": "missing",
                "claims": [
                    {"id": "c1", "text": "premise", "supports_via": [], "depends_on": [], "is_caveat": False},
                ],
            }
        # Retry: conclusion_id exists
        return {
            "conclusion_id": "c1",
            "claims": [
                {"id": "c1", "text": "premise", "supports_via": [], "depends_on": [], "is_caveat": False},
            ],
        }

    result = propose_argument(
        tmp_project_post_research,
        section,
        section.research_notes,
        facts_mgr,
        agent_call=flaky_agent,
        max_validation_retries=2,
    )
    assert result.error is None
    assert result.graph is not None
    assert result.graph.conclusion_id == "c1"
    assert len(attempts) == 2  # one retry happened


def test_propose_argument_retries_on_dangling_depends_on(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    facts_mgr = FactsManager(tmp_project_post_research)

    attempts = []

    def flaky_agent(prompt) -> dict:
        attempts.append(prompt)
        if len(attempts) == 1:
            return {
                "conclusion_id": "c1",
                "claims": [
                    {"id": "c1", "text": "x", "depends_on": ["nonexistent"], "supports_via": [], "is_caveat": False},
                ],
            }
        return {
            "conclusion_id": "c1",
            "claims": [
                {"id": "c1", "text": "x", "depends_on": [], "supports_via": [], "is_caveat": False},
            ],
        }

    result = propose_argument(
        tmp_project_post_research, section, section.research_notes,
        facts_mgr, agent_call=flaky_agent, max_validation_retries=2,
    )
    assert result.error is None
    assert len(attempts) == 2


def test_propose_argument_retries_on_cycle(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    facts_mgr = FactsManager(tmp_project_post_research)

    attempts = []

    def cyclic_then_clean(prompt) -> dict:
        attempts.append(prompt)
        if len(attempts) == 1:
            # c1 -> c2 -> c1
            return {
                "conclusion_id": "c1",
                "claims": [
                    {"id": "c1", "text": "a", "depends_on": ["c2"], "supports_via": [], "is_caveat": False},
                    {"id": "c2", "text": "b", "depends_on": ["c1"], "supports_via": [], "is_caveat": False},
                ],
            }
        return {
            "conclusion_id": "c1",
            "claims": [
                {"id": "c1", "text": "a", "depends_on": [], "supports_via": [], "is_caveat": False},
            ],
        }

    result = propose_argument(
        tmp_project_post_research, section, section.research_notes,
        facts_mgr, agent_call=cyclic_then_clean, max_validation_retries=2,
    )
    assert result.error is None
    assert len(attempts) == 2


def test_propose_argument_gives_up_after_max_retries(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    facts_mgr = FactsManager(tmp_project_post_research)

    def stubborn_agent(prompt) -> dict:
        return {
            "conclusion_id": "missing",
            "claims": [
                {"id": "c1", "text": "x", "supports_via": [], "depends_on": [], "is_caveat": False},
            ],
        }

    result = propose_argument(
        tmp_project_post_research, section, section.research_notes,
        facts_mgr, agent_call=stubborn_agent, max_validation_retries=1,
    )
    assert result.graph is None
    assert result.error is not None
    assert "validation" in result.error.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_argument.py -v`
Expected: failures on `max_validation_retries` unexpected keyword argument and/or invalid graphs being accepted without retry.

- [ ] **Step 3: Add validation + retry**

Edit `report_builder/argument.py`. Modify `propose_argument` to add the `max_validation_retries` keyword argument and wrap the agent call in a retry loop. Add validation helpers near the top of the file (after the imports, before `_read_system_prompt`).

Add validation helpers:

```python
def _validate_graph(graph: ArgumentGraph, known_fact_ids: set[str]) -> list[str]:
    """Return list of validation violations. Empty list means valid."""
    violations: list[str] = []
    ids = {c.id for c in graph.claims}

    if graph.conclusion_id not in ids:
        violations.append(
            f"conclusion_id '{graph.conclusion_id}' does not match any claim id "
            f"(known ids: {sorted(ids)})"
        )

    for c in graph.claims:
        for d in c.depends_on:
            if d not in ids:
                violations.append(
                    f"claim {c.id!r} depends_on '{d}' which is not a claim id"
                )
        # Leaf claim must have grounding facts; non-leaf must have at least one dep.
        if not c.depends_on and not c.supports_via:
            violations.append(
                f"claim {c.id!r} is a leaf with no supports_via — every leaf must "
                f"cite at least one fact"
            )
        # Fact ids must exist (only enforce at generation time; at load we warn).
        if known_fact_ids:
            for fid in c.supports_via:
                # Strip optional "fact:" prefix if present
                bare = fid.split(":", 1)[-1] if fid.startswith("fact:") else fid
                if bare not in known_fact_ids:
                    violations.append(
                        f"claim {c.id!r} cites fact '{fid}' which is not in facts.yaml"
                    )

    if _has_cycle(graph):
        violations.append("argument graph contains a cycle in depends_on")

    return violations


def _has_cycle(graph: ArgumentGraph) -> bool:
    """Detect cycles in the depends_on DAG via DFS with three-color marking."""
    by_id = {c.id: c for c in graph.claims}
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {c.id: WHITE for c in graph.claims}

    def visit(cid: str) -> bool:
        if color.get(cid) == GRAY:
            return True  # back-edge = cycle
        if color.get(cid) == BLACK:
            return False
        color[cid] = GRAY
        node = by_id.get(cid)
        if node:
            for dep in node.depends_on:
                if dep in by_id and visit(dep):
                    return True
        color[cid] = BLACK
        return False

    return any(visit(c.id) for c in graph.claims)
```

Modify the public `propose_argument` function. Replace the existing function body with:

```python
def propose_argument(
    project: ReportProject,
    section: Section,
    research_notes: Optional[str],
    facts_mgr: FactsManager,
    *,
    agent_call: Optional[Callable[..., dict]] = None,
    max_validation_retries: int = 2,
) -> ProposeArgumentResult:
    """Generate the per-section argument graph."""
    if not research_notes:
        return ProposeArgumentResult(
            graph=None,
            error="cannot propose argument: section has no research_notes — run research first",
        )

    system = _read_system_prompt()
    user = _build_user_prompt(project, section, research_notes, facts_mgr)
    known_fact_ids = {f.id for f in facts_mgr.load().facts}

    last_violations: list[str] = []

    for attempt in range(max_validation_retries + 1):
        try:
            if agent_call is not None:
                try:
                    payload = agent_call(user)
                except TypeError:
                    payload = agent_call((system, user))
            else:
                payload = _default_agent_call((system, user))
        except Exception as exc:
            logger.exception("argument agent failed (attempt %d)", attempt + 1)
            return ProposeArgumentResult(
                graph=None,
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            graph = _parse_graph(payload)
        except Exception as exc:
            return ProposeArgumentResult(
                graph=None,
                error=f"failed to parse graph from agent output: {exc}",
            )

        violations = _validate_graph(graph, known_fact_ids)
        if not violations:
            return ProposeArgumentResult(graph=graph, error=None)

        last_violations = violations
        if attempt < max_validation_retries:
            user = user + (
                f"\n\n# Retry — validation failed on previous attempt\n\n"
                + "\n".join(f"- {v}" for v in violations)
                + "\n\nRe-emit the entire JSON payload with these issues fixed."
            )
            continue

    return ProposeArgumentResult(
        graph=None,
        error=f"argument graph failed validation after {max_validation_retries + 1} attempts: "
              + "; ".join(last_violations),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_argument.py -v`
Expected: all 6 tests in the file pass (2 from Task 3 + 4 new).

Run: `pytest tests/ -v`
Expected: all 56 tests pass.

- [ ] **Step 5: Commit**

```bash
git add report_builder/argument.py tests/test_argument.py
git commit -m "Argument layer: validation + retry loop on propose_argument"
```

---

### Task 5: Pipeline resume logic

**Files:**
- Modify: `report_builder/pipeline.py`
- Create: `tests/test_argument_pipeline.py`

The big one. `run_pipeline` becomes resumable based on `research_notes`, `argument`, `drafts` state.

- [ ] **Step 1: Write the failing test**

Create `tests/test_argument_pipeline.py`:

```python
"""Tests for the resume logic in run_pipeline (argument-layer integration)."""

from __future__ import annotations

import pytest

from report_builder.pipeline import run_pipeline
from report_builder.research import ResearchBundle
from report_builder.schemas import (
    ArgumentGraph,
    Claim,
    DraftAttempt,
    Section,
    SectionStatus,
    SynthesisOutput,
)


def _stub_research_bundle(notes: str = "stub research narrative") -> ResearchBundle:
    return ResearchBundle(
        notes=notes,
        facts_added=[],
        facts_referenced=[],
        user_qa=[],
    )


def _stub_argument_graph() -> ArgumentGraph:
    return ArgumentGraph(
        conclusion_id="c1",
        claims=[Claim(id="c1", text="conclusion", supports_via=["fact:x"])],
    )


def test_first_invocation_runs_research_and_argument_then_pauses(tmp_project, monkeypatch):
    """First call to run_pipeline runs research + propose-argument, then returns
    without invoking drafting/synthesis/deslop."""
    from report_builder import pipeline

    # Reset section: existing tmp_project has section in ACCEPTED state.
    section = tmp_project.load_section("01-overview")
    section.status = SectionStatus.PENDING
    section.final = None
    section.research_notes = None
    section.argument = None
    section.drafts = []
    tmp_project.save_section(section)

    research_called = []
    argument_called = []
    drafting_called = []

    def fake_research(*args, **kwargs):
        research_called.append(True)
        return _stub_research_bundle()

    def fake_propose_argument(*args, **kwargs):
        argument_called.append(True)
        from report_builder.argument import ProposeArgumentResult
        return ProposeArgumentResult(graph=_stub_argument_graph(), error=None)

    def fake_drafting(*args, **kwargs):
        drafting_called.append(True)
        return [DraftAttempt(model="stub", text="x")]

    monkeypatch.setattr(pipeline, "run_research", fake_research)
    monkeypatch.setattr(pipeline, "propose_argument", fake_propose_argument)
    monkeypatch.setattr(pipeline, "run_drafting", fake_drafting)

    result = run_pipeline(tmp_project, "01-overview")

    assert research_called == [True]
    assert argument_called == [True]
    assert drafting_called == []  # drafting NOT called on first invocation

    section = tmp_project.load_section("01-overview")
    assert section.research_notes == "stub research narrative"
    assert section.argument is not None
    assert section.argument.conclusion_id == "c1"
    assert section.drafts == []


def test_second_invocation_resumes_from_drafting(tmp_project, monkeypatch):
    """Second call (with research_notes + argument set, drafts empty) skips
    research and propose-argument, runs drafting + synthesis + deslop."""
    from report_builder import pipeline
    from report_builder.deslop import DeslopResult

    section = tmp_project.load_section("01-overview")
    section.status = SectionStatus.DRAFTING
    section.final = None
    section.research_notes = "prior research narrative"
    section.argument = _stub_argument_graph()
    section.drafts = []
    tmp_project.save_section(section)

    research_called = []
    argument_called = []
    drafting_called = []
    synthesis_called = []
    deslop_called = []

    monkeypatch.setattr(pipeline, "run_research",
                        lambda *a, **kw: research_called.append(True) or _stub_research_bundle())
    monkeypatch.setattr(pipeline, "propose_argument",
                        lambda *a, **kw: argument_called.append(True))
    monkeypatch.setattr(pipeline, "run_drafting",
                        lambda *a, **kw: (drafting_called.append(True),
                                          [DraftAttempt(model="stub", text="x" * 50)])[1])
    monkeypatch.setattr(pipeline, "run_synthesis",
                        lambda *a, **kw: (synthesis_called.append(True),
                                          SynthesisOutput(chosen_spine="stub", combined_draft="x" * 50))[1])
    monkeypatch.setattr(pipeline, "run_deslop",
                        lambda *a, **kw: (deslop_called.append(True),
                                          DeslopResult(text="x" * 50, flesch=65.0, slop_risk="low",
                                                       struct_hits=0, iterations=0, best_iteration=0,
                                                       history=[]))[1])

    result = run_pipeline(tmp_project, "01-overview")

    assert research_called == []
    assert argument_called == []
    assert drafting_called == [True]
    assert synthesis_called == [True]
    assert deslop_called == [True]

    section = tmp_project.load_section("01-overview")
    assert section.status in (SectionStatus.AWAITING_ACCEPT, SectionStatus.AWAITING_ACCEPT.value)
    assert section.final is not None


def test_argument_failure_does_not_re_run_research(tmp_project, monkeypatch):
    """If propose-argument fails on a prior invocation (research_notes set,
    argument None), subsequent invocations retry only argument, not research."""
    from report_builder import pipeline

    section = tmp_project.load_section("01-overview")
    section.status = SectionStatus.DRAFTING
    section.final = None
    section.research_notes = "prior research narrative"
    section.argument = None
    section.drafts = []
    tmp_project.save_section(section)

    research_called = []
    argument_called = []

    monkeypatch.setattr(pipeline, "run_research",
                        lambda *a, **kw: research_called.append(True) or _stub_research_bundle())

    def succeed_argument(*args, **kwargs):
        argument_called.append(True)
        from report_builder.argument import ProposeArgumentResult
        return ProposeArgumentResult(graph=_stub_argument_graph(), error=None)

    monkeypatch.setattr(pipeline, "propose_argument", succeed_argument)

    result = run_pipeline(tmp_project, "01-overview")

    assert research_called == []  # research NOT re-run
    assert argument_called == [True]  # argument retried

    section = tmp_project.load_section("01-overview")
    assert section.argument is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_argument_pipeline.py -v`
Expected: failures — `propose_argument` not imported in pipeline.py, resume logic not implemented.

- [ ] **Step 3: Modify pipeline.py with resume logic**

Edit `report_builder/pipeline.py`. Make these changes:

1. **Add import** at the top of the file, alongside the existing module-level imports:

```python
from .argument import ProposeArgumentResult, propose_argument
```

2. **Replace the body of `run_pipeline`** with the resumable version. The full new body of `run_pipeline` is:

```python
def run_pipeline(project: ReportProject, section_id: str) -> PipelineResult:
    """Run one section through the pipeline, advancing from the next unblocked phase.

    Phases (linear order):
        1. research               (skipped if section.research_notes is set)
        2. propose-argument       (skipped if section.argument is set)
        2a. SOFT PAUSE — return after argument is proposed; user reviews
        3. drafting (with argument graph injected)
        4. synthesis
        5. deslop                 (sets section.final, status -> AWAITING_ACCEPT)

    Re-invocation always advances to the next unblocked phase based on
    section.research_notes / section.argument / section.drafts state.
    """
    report = project.load_report()
    matching = [s for s in report.outline if s.id == section_id]
    if not matching:
        raise ProjectError(f"No outline entry for section id `{section_id}`.")
    outline_entry = matching[0]

    facts_mgr = FactsManager(project)
    charts_mgr = ChartManager(project)
    csv_workspace = CsvWorkspace(csvs_dir=project.csvs_dir)

    section = project.load_section(section_id)
    section.status = SectionStatus.DRAFTING
    section.brief = SectionBrief(
        load_bearing_finding=outline_entry.load_bearing_finding,
        target_words=outline_entry.target_words,
        scope=outline_entry.rough_scope,
    )
    project.save_section(section)

    result = PipelineResult(section_id=section_id)

    # ---------- PHASE 1: research (only if not already done) ----------
    if section.research_notes is None:
        logger.info("[pipeline] phase 1: research")
        try:
            bundle = run_research(project, outline_entry, csv_workspace, facts_mgr, charts_mgr)
        except Exception as exc:
            logger.exception("research crashed")
            result.error = f"research crashed: {type(exc).__name__}: {exc}"
            result.failed_phase = "research"
            return _persist_failure(project, section, result)

        if bundle.error:
            result.error = bundle.error
            result.failed_phase = "research"
            return _persist_failure(project, section, result)

        if bundle.user_qa:
            section.brief.open_questions = [
                f"Q: {qa['question']}  A: {qa['answer']}" for qa in bundle.user_qa
            ]
        section.research_notes = bundle.notes
        project.save_section(section)
        logger.info("[pipeline] research done: %d facts added, %d facts referenced",
                    len(bundle.facts_added), len(bundle.facts_referenced))

    # ---------- PHASE 2: propose-argument (only if not already done) ----------
    if section.argument is None:
        logger.info("[pipeline] phase 2: propose-argument")
        try:
            arg_result = propose_argument(
                project, section, section.research_notes, facts_mgr,
            )
        except Exception as exc:
            logger.exception("propose-argument crashed")
            result.error = f"propose-argument crashed: {type(exc).__name__}: {exc}"
            result.failed_phase = "propose-argument"
            return _persist_failure(project, section, result)

        if arg_result.error or arg_result.graph is None:
            result.error = arg_result.error or "propose-argument returned no graph"
            result.failed_phase = "propose-argument"
            return _persist_failure(project, section, result)

        section.argument = arg_result.graph
        project.save_section(section)
        logger.info(
            "[pipeline] argument graph proposed for %s — review with "
            "`report show %s --argument`, then run `report next %s` to resume drafting",
            section_id, section_id, section_id,
        )
        # SOFT PAUSE: return without running drafting.
        return result

    # ---------- PHASE 3: drafting ----------
    logger.info("[pipeline] phase 3: drafting (ensemble)")
    # Reconstruct a minimal ResearchBundle for the drafter from persisted state.
    from .research import ResearchBundle
    bundle = ResearchBundle(
        notes=section.research_notes or "",
        facts_added=[],
        facts_referenced=[],
        user_qa=[],
    )
    try:
        drafts = run_drafting(project, outline_entry, bundle, facts_mgr, charts_mgr)
    except Exception as exc:
        logger.exception("drafting crashed")
        result.error = f"drafting crashed: {type(exc).__name__}: {exc}"
        result.failed_phase = "drafting"
        return _persist_failure(project, section, result)

    section.drafts = drafts
    project.save_section(section)
    result.drafts = drafts
    non_empty = [d for d in drafts if d.text.strip()]
    logger.info("[pipeline] drafting done: %d/%d drafts non-empty",
                len(non_empty), len(drafts))
    if not non_empty:
        result.error = "every drafter produced empty output"
        result.failed_phase = "drafting"
        return _persist_failure(project, section, result)

    # ---------- PHASE 4: synthesis ----------
    logger.info("[pipeline] phase 4: synthesis")
    try:
        synthesis = run_synthesis(outline_entry, bundle, drafts, facts_mgr)
    except Exception as exc:
        logger.exception("synthesis crashed")
        result.error = f"synthesis crashed: {type(exc).__name__}: {exc}"
        result.failed_phase = "synthesis"
        return _persist_failure(project, section, result)

    section.synthesis = synthesis
    project.save_section(section)
    result.synthesis_chosen_spine = synthesis.chosen_spine
    logger.info("[pipeline] synthesis done: spine=%s, %d chars",
                synthesis.chosen_spine, len(synthesis.combined_draft))

    # ---------- PHASE 5: deslop ----------
    logger.info("[pipeline] phase 5: deslop")
    deslop = run_deslop(synthesis.combined_draft, section_title=outline_entry.title)
    section.deslop = DeslopRecord(
        iterations=deslop.iterations,
        best_iteration=deslop.best_iteration,
        history=deslop.history,
    )
    if deslop.error:
        logger.warning("[pipeline] deslop reported: %s", deslop.error)

    if section.final is not None and section.final.text:
        from .editor import push_to_versions
        cause = "post-reject-redraft" if section.rejection_history else "rerun"
        push_to_versions(section, replaced_by=cause)

    section.final = SectionFinal(
        text=deslop.text,
        flesch=deslop.flesch,
        slop_risk=deslop.slop_risk,
        struct_hits=deslop.struct_hits,
        word_count=len(deslop.text.split()),
        accepted_at=None,
    )
    section.status = SectionStatus.AWAITING_ACCEPT
    project.save_section(section)

    result.final_text = deslop.text
    result.flesch = deslop.flesch
    result.slop_risk = deslop.slop_risk
    result.struct_hits = deslop.struct_hits
    result.deslop_iterations = deslop.iterations
    logger.info("[pipeline] all phases done — section %s awaiting accept", section_id)
    return result
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_argument_pipeline.py -v`
Expected: 3 tests pass.

Run: `pytest tests/ -v`
Expected: all 59 tests pass. Pay attention to pre-existing pipeline tests — if any break, the resume logic is not perfectly backward-compatible. Investigate and fix without removing the resume logic.

- [ ] **Step 5: Commit**

```bash
git add report_builder/pipeline.py tests/test_argument_pipeline.py
git commit -m "Argument layer: pipeline resume logic and propose-argument phase"
```

---

### Task 6: Drafter prompt integration

**Files:**
- Modify: `report_builder/drafters.py`
- Create: `tests/test_argument_drafter.py`

The drafter consumes the argument graph as a structural directive in its user prompt.

- [ ] **Step 1: Write the failing test**

Create `tests/test_argument_drafter.py`:

```python
"""Test that the drafter user prompt includes the rendered argument graph."""

from __future__ import annotations

from report_builder.charts import ChartManager
from report_builder.drafters import _build_user_prompt
from report_builder.facts import FactsManager
from report_builder.research import ResearchBundle
from report_builder.schemas import ArgumentGraph, Claim


def test_drafter_prompt_includes_argument_graph_when_set(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    section.argument = ArgumentGraph(
        conclusion_id="c1",
        claims=[
            Claim(id="c1", text="DLR is a portfolio of nodes",
                  supports_via=["fact:node_distribution"]),
        ],
    )
    tmp_project_post_research.save_section(section)

    report = tmp_project_post_research.load_report()
    outline_entry = next(o for o in report.outline if o.id == section.id)
    facts_mgr = FactsManager(tmp_project_post_research)
    charts_mgr = ChartManager(tmp_project_post_research)
    bundle = ResearchBundle(
        notes="research narrative",
        facts_added=[], facts_referenced=[], user_qa=[],
    )

    prompt = _build_user_prompt(
        report, outline_entry, bundle, tmp_project_post_research,
        facts_mgr, charts_mgr,
    )

    # The argument graph block should appear
    assert "Argument structure" in prompt or "argument structure" in prompt.lower()
    # The conclusion text should be in the prompt
    assert "DLR is a portfolio of nodes" in prompt
    # The instruction sentence should be present
    assert "structural backbone" in prompt or "do not introduce" in prompt.lower()


def test_drafter_prompt_omits_argument_section_when_unset(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    section.argument = None  # explicitly unset
    tmp_project_post_research.save_section(section)

    report = tmp_project_post_research.load_report()
    outline_entry = next(o for o in report.outline if o.id == section.id)
    facts_mgr = FactsManager(tmp_project_post_research)
    charts_mgr = ChartManager(tmp_project_post_research)
    bundle = ResearchBundle(
        notes="research narrative",
        facts_added=[], facts_referenced=[], user_qa=[],
    )

    prompt = _build_user_prompt(
        report, outline_entry, bundle, tmp_project_post_research,
        facts_mgr, charts_mgr,
    )

    # When no argument graph exists, the drafter falls back to existing behavior.
    # The prompt should not contain the argument-structure header.
    assert "Argument structure" not in prompt
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_argument_drafter.py -v`
Expected: failures — argument block not in drafter prompt.

- [ ] **Step 3: Modify drafters.py**

Edit `report_builder/drafters.py`. The existing `_build_user_prompt` function builds a multi-section prompt string. We need to insert a new block.

Find the existing `_build_user_prompt` function. It currently has this structure (look for the f-string with markers like `# Stylesheet`, `# Sections already accepted`, etc.). Add the following AFTER the `# Charts available` block and BEFORE the `# Stylesheet` block.

In the function body, add a new line before the return statement that loads the section and renders its argument graph (if any):

```python
def _build_user_prompt(
    report: Report,
    section: OutlineSection,
    research: ResearchBundle,
    project: ReportProject,
    facts_mgr: FactsManager,
    charts_mgr: ChartManager,
) -> str:
    from .memory import render_argument_graph

    sec_type = section.section_type if isinstance(section.section_type, str) else section.section_type.value
    persisted = project.load_section(section.id)
    rejection_block = _render_rejection_history(persisted.rejection_history)
    voice_block = render_voice_constraints(report)
    aliases_block = render_field_aliases(report)

    # Argument graph block — only included if the section has one set.
    argument_block = ""
    if persisted.argument is not None:
        rendered = render_argument_graph(persisted)
        argument_block = (
            "\n# Argument structure (render this as prose; do not introduce claims outside it)\n\n"
            + rendered
            + "\nThe argument structure above is your structural backbone. Render it as prose. "
            + "Do not introduce quantitative or definitive claims that aren't in the argument graph.\n"
        )

    return f"""# Report

{render_outline_overview(report, section.id)}

# Section to draft

- id: {section.id}
- title: {section.title}
- type: {sec_type}
- target words: {section.target_words}
- load-bearing finding: {section.load_bearing_finding}

# Research bundle (from phase 1)

{research.notes}

Facts the research phase expects you to cite:
{', '.join(research.facts_referenced) if research.facts_referenced else '(none flagged)'}

# Facts dictionary (canonical numerical truth — cite by id)

{render_facts_for_drafter(facts_mgr, prefer_section=section.id)}

# Charts available (cite inline as `[chart:<id>]`; place each on its own line between paragraphs)

{render_charts_for_drafter(charts_mgr)}
{argument_block}
# Stylesheet

{render_stylesheet_for_drafter(project)}

# Sections already accepted (do not restate)

{render_rolling_summary(project)}
{aliases_block}{voice_block}{rejection_block}
Now write the section. Output prose only. No heading. No preamble. No bullets unless the section is genuinely a list. Word count target: {section.target_words} (within 10%). Cite every figure as `[fact:<id>]`. Do not invent numbers.
"""
```

This rewrites the function but keeps every existing block. The only structural change is the new `argument_block` variable computed up front and interpolated between `# Charts available` and `# Stylesheet`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_argument_drafter.py -v`
Expected: 2 tests pass.

Run: `pytest tests/ -v`
Expected: all 61 tests pass.

- [ ] **Step 5: Commit**

```bash
git add report_builder/drafters.py tests/test_argument_drafter.py
git commit -m "Argument layer: inject argument graph into drafter prompt"
```

---

### Task 7: `report show --argument` flag

**Files:**
- Modify: `report_builder/cli.py`
- Create: `tests/test_argument_cli.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_argument_cli.py`:

```python
"""CLI smoke tests for argument-layer commands."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from report_builder.cli import app
from report_builder.schemas import ArgumentGraph, Claim


runner = CliRunner()


def test_show_argument_flag_renders_graph(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    section.argument = ArgumentGraph(
        conclusion_id="c1",
        claims=[
            Claim(id="c1", text="DLR is a portfolio of nodes",
                  supports_via=["fact:node_distribution"]),
        ],
    )
    tmp_project_post_research.save_section(section)

    result = runner.invoke(
        app,
        ["show", "01-overview", "--argument", "--project", str(tmp_project_post_research.root)],
    )
    assert result.exit_code == 0, result.output
    assert "DLR is a portfolio of nodes" in result.output


def test_show_argument_when_no_graph_set(tmp_project_post_research):
    section = tmp_project_post_research.load_section("01-overview")
    section.argument = None
    tmp_project_post_research.save_section(section)

    result = runner.invoke(
        app,
        ["show", "01-overview", "--argument", "--project", str(tmp_project_post_research.root)],
    )
    assert result.exit_code == 0, result.output
    assert "no argument graph" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_argument_cli.py -v`
Expected: failures — `--argument` is not a recognized flag.

- [ ] **Step 3: Add the flag to `report show`**

Edit `report_builder/cli.py`. Find the existing `show` command (around line 234). It currently has signature roughly `def show(section_id, project_path, ...)`. Add a new boolean option:

Add this new option to the `show` function signature (alongside the existing options):

```python
    argument: bool = typer.Option(
        False, "--argument",
        help="Print the argument graph for this section instead of the section yaml.",
    ),
```

Then near the START of the `show` function body (before any existing rendering logic), add:

```python
    if argument:
        from .memory import render_argument_graph
        from .state import ProjectError

        try:
            project = _load_or_die(project_path)
            section = project.load_section(section_id)
        except ProjectError as e:
            typer.secho(f"Error: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=1)
        typer.echo(render_argument_graph(section))
        return
```

This branches early on the `--argument` flag and prints just the rendered argument graph, then returns before the existing section-yaml rendering runs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_argument_cli.py -v`
Expected: 2 tests pass.

Run: `pytest tests/ -v`
Expected: all 63 tests pass.

- [ ] **Step 5: Commit**

```bash
git add report_builder/cli.py tests/test_argument_cli.py
git commit -m "Argument layer: report show --argument flag"
```

---

### Task 8: `report argue <section-id>` command

**Files:**
- Modify: `report_builder/cli.py`
- Modify: `tests/test_argument_cli.py` (append)

Explicit regenerate command. Useful when the user reads the proposed graph, dislikes it, wants a fresh proposal without manually editing yaml.

- [ ] **Step 1: Append failing tests**

Append to `tests/test_argument_cli.py`:

```python
def test_argue_command_regenerates_graph(tmp_project_post_research, monkeypatch):
    """`report argue` calls propose_argument and overwrites section.argument."""
    from report_builder import argument as arg_mod

    canned_graph = ArgumentGraph(
        conclusion_id="c1",
        claims=[Claim(id="c1", text="brand new conclusion", supports_via=["fact:x"])],
    )

    def stub(prompt):
        return {
            "conclusion_id": "c1",
            "claims": [
                {"id": "c1", "text": "brand new conclusion",
                 "supports_via": ["fact:x"], "depends_on": [], "is_caveat": False},
            ],
        }

    real = arg_mod.propose_argument

    def patched(project, section, research_notes, facts_mgr, **kwargs):
        kwargs.setdefault("agent_call", stub)
        return real(project, section, research_notes, facts_mgr, **kwargs)

    monkeypatch.setattr(arg_mod, "propose_argument", patched)

    # Ensure the section has research_notes (the post-research fixture does).
    result = runner.invoke(
        app,
        ["argue", "01-overview", "--project", str(tmp_project_post_research.root)],
    )
    assert result.exit_code == 0, result.output
    section = tmp_project_post_research.load_section("01-overview")
    assert section.argument is not None
    assert section.argument.claims[0].text == "brand new conclusion"


def test_argue_errors_when_no_research_notes(tmp_project):
    """`report argue` errors out cleanly if research has not run yet."""
    section = tmp_project.load_section("01-overview")
    section.research_notes = None
    tmp_project.save_section(section)

    result = runner.invoke(
        app,
        ["argue", "01-overview", "--project", str(tmp_project.root)],
    )
    assert result.exit_code != 0
    assert "research" in result.output.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_argument_cli.py -v`
Expected: failures — `argue` is not a recognized command.

- [ ] **Step 3: Add the `argue` command**

Edit `report_builder/cli.py`. Find a sensible location to add the new command — between the `accept` and `reject` commands, OR near the existing `design` and `accept-design` commands (added in the design-pack feature). Add:

```python
# ---------------------------------------------------------------------------
# argue — explicit regenerate of the argument graph
# ---------------------------------------------------------------------------


@app.command()
def argue(
    section_id: str = typer.Argument(..., help="Section id to regenerate the argument graph for."),
    project_path: Path = typer.Option(
        Path.cwd(), "--project", "-p",
        help="Path to the project directory.",
        exists=True, file_okay=False, readable=True,
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Regenerate a section's argument graph. Requires research_notes to be set
    (i.e. research has run). Overwrites any existing section.argument."""
    _configure_logging(verbose=verbose)
    project = _load_or_die(project_path)

    from .argument import propose_argument
    from .facts import FactsManager

    section = project.load_section(section_id)
    if not section.research_notes:
        typer.secho(
            f"Section {section_id} has no research_notes — run `report next "
            f"{section_id}` first to do research, then come back.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(code=1)

    facts_mgr = FactsManager(project)
    result = propose_argument(project, section, section.research_notes, facts_mgr)
    if result.error or result.graph is None:
        typer.secho(f"Error: {result.error}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1)

    section.argument = result.graph
    project.save_section(section)
    typer.secho(f"Argument graph regenerated for {section_id}.", fg=typer.colors.GREEN)
    typer.echo(f"  conclusion: {next((c.text for c in result.graph.claims if c.id == result.graph.conclusion_id), '<missing>')}")
    typer.echo(f"  claims: {len(result.graph.claims)}")
    typer.echo(f"\nReview with `report show {section_id} --argument`. Run `report next {section_id}` to draft.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_argument_cli.py -v`
Expected: all 4 tests in the file pass.

Run: `pytest tests/ -v`
Expected: all 65 tests pass.

- [ ] **Step 5: Commit**

```bash
git add report_builder/cli.py tests/test_argument_cli.py
git commit -m "Argument layer: report argue command for explicit regenerate"
```

---

### Task 9: End-to-end integration test

**Files:**
- Create: `tests/test_argument_integration.py`

Exercise the full new flow with stubbed agents — first invocation runs through propose-argument and pauses; second invocation runs through drafting.

- [ ] **Step 1: Write the integration test**

Create `tests/test_argument_integration.py`:

```python
"""End-to-end: full pipeline through argument-layer pause-and-resume.

First invocation: research + propose-argument run, then pipeline returns
without drafting. Second invocation: argument is set; drafting + synthesis
+ deslop run; section ends in AWAITING_ACCEPT.
"""

from __future__ import annotations

import pytest

from report_builder.deslop import DeslopResult
from report_builder.pipeline import run_pipeline
from report_builder.research import ResearchBundle
from report_builder.schemas import (
    ArgumentGraph,
    Claim,
    DraftAttempt,
    SectionStatus,
    SynthesisOutput,
)


def test_argument_layer_full_round_trip(tmp_project, monkeypatch):
    """Run the pipeline twice on a fresh section. Verify pause-and-resume."""
    from report_builder import pipeline
    from report_builder import argument as arg_mod

    # Reset to a totally fresh state.
    section = tmp_project.load_section("01-overview")
    section.status = SectionStatus.PENDING
    section.final = None
    section.research_notes = None
    section.argument = None
    section.drafts = []
    section.synthesis = None
    section.deslop = None
    tmp_project.save_section(section)

    # Stub research.
    monkeypatch.setattr(
        pipeline, "run_research",
        lambda *a, **kw: ResearchBundle(
            notes="Stubbed research narrative — 8788 records, 5 nodes, 3 top sectors.",
            facts_added=[], facts_referenced=[], user_qa=[],
        ),
    )

    # Stub propose-argument to return a known graph.
    def stub_argument_agent(prompt):
        return {
            "conclusion_id": "c2",
            "claims": [
                {"id": "c1", "text": "Five nodes hold 73% of locations",
                 "supports_via": ["fact:x"], "depends_on": [], "is_caveat": False},
                {"id": "c2", "text": "DLR is a portfolio of distinct nodes",
                 "supports_via": [], "depends_on": ["c1"], "is_caveat": False},
            ],
        }

    real_propose = arg_mod.propose_argument

    def patched_propose(project, sec, notes, fmgr, **kwargs):
        # Disable validation against fact ids by passing through with the stub agent.
        # The fact 'x' in supports_via won't be in facts.yaml, so we set
        # max_validation_retries=0 and let the agent's output bypass the check
        # by NOT enforcing fact-id existence in the test (acceptable since the
        # validator's known_fact_ids parameter is computed from facts_mgr —
        # we test against the stub's known set).
        kwargs.setdefault("agent_call", stub_argument_agent)
        # Override the validator to skip fact-id checks for this integration test
        # by ensuring the supports_via fact actually exists in facts.yaml is
        # too brittle; instead, change the stub to use facts that DO exist.
        return real_propose(project, sec, notes, fmgr, **kwargs)

    # Better: use a stub agent whose supports_via doesn't exist; the validator
    # will reject. Instead, make the stub use NO fact references on the leaf claim
    # but also satisfy "leaf must have grounding" — that means we need an alternate
    # design. Workaround: pre-add a dummy fact to facts.yaml.
    import yaml
    facts_path = tmp_project.root / "facts.yaml"
    facts_path.write_text(
        yaml.safe_dump({
            "facts": [
                {
                    "id": "x",
                    "value": 1,
                    "derivation": "test fixture",
                    "query": "n/a",
                    "source": "n/a",
                    "confidence": "high",
                }
            ]
        }),
        encoding="utf-8",
    )

    monkeypatch.setattr(arg_mod, "propose_argument", patched_propose)
    # The pipeline imports propose_argument by name; patch it there too.
    monkeypatch.setattr(pipeline, "propose_argument", patched_propose)

    # Stub drafting / synthesis / deslop so we don't hit Anthropic.
    monkeypatch.setattr(
        pipeline, "run_drafting",
        lambda *a, **kw: [DraftAttempt(model="stub",
                                       text="Draft prose. " * 30)],
    )
    monkeypatch.setattr(
        pipeline, "run_synthesis",
        lambda *a, **kw: SynthesisOutput(
            chosen_spine="stub", combined_draft="Combined prose. " * 30,
        ),
    )
    monkeypatch.setattr(
        pipeline, "run_deslop",
        lambda *a, **kw: DeslopResult(
            text="Final prose. " * 30,
            flesch=65.0, slop_risk="low", struct_hits=0,
            iterations=0, best_iteration=0, history=[],
        ),
    )

    # ---- First invocation: should pause after argument ----
    result1 = run_pipeline(tmp_project, "01-overview")
    section = tmp_project.load_section("01-overview")
    assert section.research_notes is not None
    assert section.argument is not None
    assert section.argument.conclusion_id == "c2"
    assert section.drafts == []  # pipeline paused before drafting
    assert section.final is None  # not drafted yet

    # ---- Second invocation: should resume from drafting ----
    result2 = run_pipeline(tmp_project, "01-overview")
    section = tmp_project.load_section("01-overview")
    assert section.drafts != []  # drafting ran
    assert section.synthesis is not None  # synthesis ran
    assert section.final is not None  # deslop ran
    assert section.status in (
        SectionStatus.AWAITING_ACCEPT, SectionStatus.AWAITING_ACCEPT.value,
    )
```

- [ ] **Step 2: Run the test**

Run: `pytest tests/test_argument_integration.py -v`
Expected: passes (or fails on a real integration bug — debug the bug, not the test).

Run: `pytest tests/ -v`
Expected: all 66 tests pass.

- [ ] **Step 3: Manual smoke check (optional, requires Anthropic key)**

On a real project:

```bash
cd <some-project-with-pending-sections>
report next <section-id>           # runs research + propose-argument, pauses
report show <section-id> --argument  # eyeball the proposed graph
# Optionally edit sections/<id>.yaml to adjust the argument
report next <section-id>           # resumes from drafting
report show <section-id>           # see the final prose
```

If the argument graph looks bad on first generation, run `report argue <section-id>` to regenerate before resuming drafting.

- [ ] **Step 4: Commit**

```bash
git add tests/test_argument_integration.py
git commit -m "Argument layer: end-to-end integration test for pause-and-resume flow"
```

---

## Out of scope (deferred follow-ups)

- **Phase 1.5: prose-vs-graph auto-validation.** No new pipeline phase reads the final prose and flags sentences that don't map to a claim. Add this once we have real prose output to inform the validation design.
- **Phase 2: kickoff intent capture.** No `argument_intent` extraction during `report init`. The propose-argument agent generates cold from research output.
- **Phase 3: cross-section validation.** No `report check` walking inter-section dependencies between graphs.
- **Argument-graph-aware rejection.** No `report reject --claim c2 --reason "..."` targeting a specific claim. Rejection still operates on prose.
- **Multi-renderer output.** No deck/memo/appendix generators driven by the same argument graph.

## Spec divergences from the original spec doc

**One deferred-not-implemented item:** the spec describes load-time validation that surfaces warnings on dangling fact references in a graph (e.g., a fact got renamed in `facts.yaml` after the argument was generated). This plan implements hard validation at generation time (Task 4) but does not implement warning emission at load time. The natural home would be `report show --argument` walking each `claim.supports_via` against the current `facts.yaml` and printing yellow warnings for unknown ids. ~30 minutes of work; deferred to keep Task 7 focused on rendering. Add as a follow-up after Phase 1 ships.

If other divergences emerge during implementation (e.g. a different validation strategy turns out to fit the codebase better), document them inline in the relevant task and update the spec post-implementation.
