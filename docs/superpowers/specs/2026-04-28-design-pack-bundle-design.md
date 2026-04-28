# Design Pack Bundle for Claude Design Ingest

**Date:** 2026-04-28
**Status:** Spec — pending implementation plan
**Owner:** zak@sunstone.ie

## Problem

The report-builder pipeline produces sanitized, DeSlop'd report content (`report.md`, `report.docx`, `report.pdf`, `charts.yaml`) intended for Claude Design (Anthropic's research-preview design tool) to typeset into a polished deliverable. Today, content is handed off via `report publish` → GitHub raw URLs → paste into Claude.ai. In this mode Claude Design treats the input as a prose prompt and frequently generates additional AI-flavored text alongside the supplied body, defeating the DeSlop pipeline's quality guarantees.

Claude Design has no documented "verbatim mode." It treats structured uploads (DOCX/YAML) more literally than prose prompts, and is least likely to generate when every text slot in the design is already filled. The fix is therefore on our side: pre-generate every text slot Claude Design would otherwise invent, run them through a quality gate, and bundle them into a single self-contained input file.

## Goal

Replace the prose-prompt handoff with a **bundled YAML design pack** containing every text element of the final design (headlines, decks, TLDRs, pull quotes, captions, exec summary, key findings) plus the body, charts, and facts. Claude Design's role becomes layout/typesetting only.

Within the pipeline, generate this design pack as a normal artifact of the report-building process, gated by the same human-approval and quality-pass discipline that today protects the body.

## Non-goals

- Replacing `report publish` for repo source-of-truth — `publish` continues to push to GitHub for versioning. Only the *Claude Design ingest path* changes.
- Programmatic Claude Design integration via MCP. Upload remains manual (drag-and-drop). Programmatic ingest is a possible follow-up but out of scope.
- Cross-section visual theming, brand kits, color palettes, or layout templates. The design pack carries text and structured chart specs only; visual treatment is Claude Design's job.
- Replacing the existing prose `deslop.py`. The new short-form pass is a sibling, not a replacement.

## Design

### Pipeline placement

Two new stages slot into the existing per-section lifecycle, plus one final report-level pass.

**Existing per-section lifecycle:**
```
draft → DeSlop (body) → human approve body → next section
```

**New per-section lifecycle:**
```
draft
  → DeSlop (body)
  → human approve body
  → generate design pack (per-section slots: kicker, headline, dek, tldr,
    pull_quotes, chart_captions for charts in this section)
  → design-deslop (short-form pass over paraphrased slots only)
  → human approve design pack
  → next section
```

**End-of-pipeline (new):**
```
all sections approved
  → final polish pass (report-level slots: subtitle, cover_kicker,
    exec_summary, key_findings, toc_headlines, cover_pull_quote)
  → design-deslop (short-form pass)
  → human approve polish
  → compose-design → output/report.design.yaml
  → manual upload to Claude Design
```

### Bundled YAML schema

One self-contained file, `output/report.design.yaml`. Section bodies, design slots, full chart specs, and facts all live in this file. References across sections (pull-quote sources, chart_refs) are by ID.

```yaml
report:
  title:              # already known from metadata
  subtitle:           # paraphrased; ≤20 words
  cover_kicker:       # paraphrased; ≤4 words
  exec_summary:       # paraphrased; ~120 words; written with full-report context
  key_findings:       # paraphrased; 3-5 bullet headlines, each ≤12 words
  toc_headlines:      # deduped section-headline list
  cover_pull_quote:   # verbatim-extracted from any section's body

sections:
  - id: market-overview
    kicker:           # paraphrased; ≤4 words
    headline:         # paraphrased; ≤8 words
    dek:              # paraphrased; ≤20 words
    tldr:             # paraphrased; ≤60 words
    body: |           # DeSlop'd prose, verbatim
    pull_quotes:      # 1-3 per section; verbatim-extracted
      - text:
        source:       # paragraph anchor (`<section_id>::p<index>`) or `fact:A.3`
    chart_refs: [B.1, B.3]

charts:               # full specs from existing charts.yaml
  - id: B.1
    type:
    data:
    caption:          # paraphrased; ≤25 words; lives next to its chart

facts:                # appendix entries
  - id: A.1
    text:
    source:
```

### Extraction vs paraphrase

To minimize slop surface, slots are split between two generation modes:

- **Verbatim-extracted** (zero slop risk by construction): `pull_quotes`, `cover_pull_quote`. Agent emits a substring; system verifies the substring appears in the body or fact text exactly. If verification fails, agent is re-prompted up to a configurable retry limit; persistent failure errors out with a clear message.
- **Paraphrased** (subject to design-deslop): everything else — `kicker`, `headline`, `dek`, `tldr`, chart `caption`, `subtitle`, `cover_kicker`, `exec_summary`, `key_findings`, `toc_headlines`. The agent writes new copy to length, with the body as input.

This split exists because pull quotes are the highest-cliché-risk slot type and the body is already DeSlop'd, so extraction gives a free quality guarantee. Length-bounded slots (TLDRs, exec_summary) cannot be cleanly assembled from existing sentences and need honest paraphrase.

### New components

**`report_builder/design_pack.py`** — per-section agent. Inputs: approved section body, section metadata, voice constraints, the section's chart entries from `charts.yaml`. Output: the per-section block of the schema above, written to `sections/<id>.design.yaml`. Implements pull-quote verification (substring-match against body) with retry + hard error on persistent failure.

**`report_builder/design_polish.py`** — end-of-pipeline agent. Inputs: full composed report (all approved sections + their design packs), voice constraints. Output: the `report:` block of the schema above, written to `report.polish.yaml`. Has visibility into all section headlines and TLDRs and is responsible for cross-section deduplication of `toc_headlines`. Uses verbatim extraction for `cover_pull_quote`.

**`report_builder/design_deslop.py`** — short-form quality pass, sibling to existing `deslop.py`. Same iterative-rewrite shape, different scorer set:
- Length bounds per slot type (e.g. headline ≤ 8 words, dek ≤ 20, tldr ≤ 60, caption ≤ 25, key_finding ≤ 12).
- AI-tell phrase list (configurable; starts with terms like "delve," "navigate the landscape," "in today's [X] world," "it's worth noting," "in conclusion").
- Cliché regex bank (rhetorical questions, tricolons, weak intensifiers).
- Em-dash budget per slot.
- No-go opener patterns.

Runs over every paraphrased slot. Skips verbatim-extracted slots (already trusted by construction).

**`report_builder/compose_design.py`** — bundler. Inputs: all per-section `*.design.yaml` files, `report.polish.yaml`, existing `charts.yaml`, facts list. Output: single `output/report.design.yaml`. Generates paragraph anchors (e.g., `<section_id>::p3`) at compose time so pull-quote `source` references resolve. Refuses to bundle if any section's design-pack `body_hash` does not match the current body (see Edge cases).

### CLI surface

Additions to the existing `report` CLI, mirroring existing command shape:

- `report design <section-id>` — generate per-section design pack. The per-section approval flow runs this automatically after body approval; the explicit command is for re-runs after a body re-draft.
- `report polish` — generate report-level polish pack.
- `report compose-design` — bundle all design artifacts into `output/report.design.yaml`.
- `report show <section-id> --design` — review the design pack alongside the body during the per-section approval loop.

`report publish` is unchanged; it continues to push to GitHub for repo source-of-truth. The Claude Design ingest path is now: run `report compose-design`, then manually upload `output/report.design.yaml` to Claude Design.

### Voice constraints carry-through

Existing `voice_constraints` (from `schemas.py`) are passed into both `design_pack.py` and `design_polish.py` as part of the system prompt. The cliché regex bank and AI-tell list in `design_deslop.py` are an additive enforcement layer on top of the voice prompt — the prompt steers, the deslop pass catches.

### Edge cases

**Body re-draft after design pack exists.** If the user rejects an approved body and re-drafts, the per-section design pack is invalidated. Pull-quote `source` substrings may no longer exist in the new body. Each design pack records a `body_hash` field (hash of the body it was generated from); `compose-design` refuses to bundle if a section's stored `body_hash` doesn't match the current body. Re-running `report design <section-id>` regenerates the pack against the new body and updates the hash.

**Verification failure on extraction.** If the agent cannot produce a verbatim pull quote after the retry budget, the section's design pack errors out and the user is prompted to either approve a relaxed extraction (substring with normalized whitespace) or skip pull quotes for this section.

**Cross-section duplication.** Two sections may independently produce near-identical headlines or pull quotes. The polish pass receives all section headlines/TLDRs as input and is explicitly tasked with flagging duplicates and rewriting `toc_headlines` to deduplicate. Body-level pull quotes are not auto-deduplicated; flagged in polish-pass output for human resolution.

**Length-bound violation persists after deslop iterations.** If iterative rewrite cannot bring a slot under its length bound, the deslop pass surfaces the violation in the human-approval view rather than silently truncating.

### Testing

Unit tests:
- Design-deslop scorers: length bounds, AI-tell catches, cliché bank, em-dash budget. Each scorer has positive and negative cases.
- Pull-quote verification: exact match, whitespace-normalized match, no match (retry triggered), persistent no match (error).
- `compose_design.py`: bundle schema validation, paragraph-anchor generation, stale-pack refusal.

Integration test:
- Run a fixture report (small, ~3 sections) end-to-end. Assert: `report.design.yaml` validates against schema; every pull quote's source substring is present verbatim in the referenced body or fact; no paraphrased slot exceeds its length bound; `toc_headlines` has no exact duplicates.

Out of scope for tests: visual quality of the resulting Claude Design output. That is verified manually.

## Open questions

None at spec time. Implementation plan will surface remaining detail (specific length bound numbers, initial AI-tell phrase list, retry budget for pull-quote verification, exact paragraph-anchor format).

## Follow-ups (out of scope)

- Programmatic Claude Design ingest via the `mcp__*__generate-design-structured` tool, bypassing manual upload.
- Round-trip diff: export from Claude Design, diff against `report.design.yaml`, flag any drift in body text.
- Brand kit / theme hints in the bundle.
