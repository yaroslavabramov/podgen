# Phase 6 Quality Script Execution Plan

## Objective

Turn the current `planning -> writing -> audio` flow into a system that reliably produces a podcast script that is:

- factually defensible
- structurally coherent across scene boundaries
- conversational rather than lecture-shaped
- auditable when quality degrades

This plan is intentionally focused on the current bottleneck:

- `showrunner` is in good shape
- `act_planner` is good enough for the next iteration
- the remaining quality gap is in `phase6_writing`

The goal is not to keep adding prompt text blindly.
The goal is to make the writing quality stack actionable:

- detect the right failures
- log them
- repair them at the right granularity
- block audio when unresolved factual problems remain

## Current State

### What is already working

- `showrunner` no longer smears the same major event across multiple acts
- `act_planner` now produces much cleaner act progression
- `screenwriter` continuity is materially better than before
- local speaker normalization to ASCII `A/B` is fixed

### What is still failing

1. `writer_revolt` is too weak to be a real quality control stage.
   - It evaluates one scene at a time.
   - It uses a single scalar threshold (`score < 0.6`).
   - It does not really see adjacent-scene failures.
   - In the latest run it flagged nothing, even though surface repetition is still obvious.

2. `citation_auditor` is currently passive.
   - It spends Sonnet tokens.
   - It stores `PASSED/FAILED` and aggregate metrics.
   - It does not persist useful issue logs.
   - It does not trigger repair.
   - It does not block audio.

3. The pipeline has no real stitch-quality layer.
   - There is no dedicated check for:
     - scene opening echo
     - repeated punchline carryover
     - act-level rhythm collapse
     - repetitive agreement/challenge shapes across neighboring scenes

4. The system can only rewrite at scene granularity.
   - That is better than rewriting the whole episode.
   - But many current failures are smaller than a full scene rewrite.
   - We need targeted repair, not full regeneration by default.

## Root Causes

### A. Wrong unit of evaluation

Some failures are scene-local.
Some failures only exist at the boundary between scenes.
Some failures only appear across an entire act.

Today:

- `writer_revolt` only sees a single scene
- `citation_auditor` only sees a single scene

That means the stack is blind to exactly the kind of transition and rhythm defects we still hear.

### B. Wrong unit of repair

The system currently has two extremes:

- do nothing
- rewrite the whole scene

That is too coarse.

Many likely failures are actually:

- one bad opening line
- one repeated callback sentence
- one overstated factual claim
- one attribution miss

Longer-term, some of these should be repairable as patches.

But for the first implementation pass, the stable unit should be:

- `scene_polish` for dialogue/style problems
- `scene_rewrite` only for structurally broken output
- targeted citation repair for factual problems

In other words:

- do not jump to whole-episode rewrite
- do not start with brittle line-patch surgery either

### C. Audits are not first-class artifacts

We currently keep:

- `engagement_score`
- `audit_status`
- `quality_metrics`

We do not keep:

- concrete weak points
- concrete citation issues
- severity
- recommended repair scope

So the pipeline cannot make informed decisions later.

## Target Operating Model

The desired writing stack is:

1. `write_screenplay`
2. `audit_and_polish_dialogue`
3. `audit_and_repair_citations`
4. only then `generate_audio`

This is the key design shift:

- style/flow issues get a dedicated polish pass
- factual issues get a dedicated citation-repair pass
- unresolved factual failures can block audio

These are graph-level stages, not necessarily one LLM call each.

Inside them:

- `audit_and_polish_dialogue` can do:
  - one cheap dialogue audit
  - conditional scene polish only for flagged scenes
- `audit_and_repair_citations` can do:
  - citation audit
  - conditional citation repair
  - re-audit only repaired scenes

## Design Principles

- No word blacklists
- No corpus-specific style dictionaries
- No deterministic semantic-dedupe layer as the first response
- Prefer structured audit outputs over free-form text
- Prefer targeted repair over whole-scene rewrite
- Use cheaper models for detection and more capable models for repair
- Do not rewrite content that is already good

## Phase 1: Make Audits Observable And Actionable

### Goal

Turn audits from opaque verdicts into reusable structured artifacts.

### Files

- [/Users/yaroslavabramov/side/podgen/backend/app/models/script_segment.py](/Users/yaroslavabramov/side/podgen/backend/app/models/script_segment.py)
- new Alembic migration under [/Users/yaroslavabramov/side/podgen/backend/alembic/versions](/Users/yaroslavabramov/side/podgen/backend/alembic/versions)
- [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/writer_revolt.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/writer_revolt.py)
- [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/citation_auditor.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/citation_auditor.py)

### Changes

Add structured findings storage for each `ScriptSegment`.

Recommended shape:

```json
{
  "dialogue_local": {
    "passed": true,
    "issues": []
  },
  "dialogue_transition": {
    "passed": true,
    "issues": []
  },
  "citation": {
    "passed": false,
    "issues": []
  }
}
```

This can be implemented either as:

- one new `audit_findings` JSONB field

or:

- dedicated JSONB fields per audit type

Preferred choice:

- one compact `audit_findings` JSONB field
- keep numeric rollups in existing `quality_metrics`

### Minimum issue schema

Each issue should include:

- `type`
- `severity`
- `scope`
- `description`
- `suggestion`

Where `scope` is one of:

- `scene_polish`
- `scene_rewrite`
- `act_warning`

Do not start with `line_patch` in v1.
It can be added later if scene-level polish proves too expensive.

### Why this matters

Without this, every later repair step is blind.

## Phase 2: Replace Scene-Only Revolt With One Real Dialogue Audit

### Goal

Replace the current single-scene score gate with one structured dialogue audit that can still see the scene boundary.

### Files

- [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/writer_revolt.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/writer_revolt.py)
- optionally renamed later to `dialogue_auditor.py`

### Input window

The auditor should receive:

- current scene
- previous scene last 1-2 lines or compact boundary context
- current scene opener
- current scene metadata like `scene_job`

This keeps it to one pass, not separate local and transition calls.

### What it should detect

Keep a per-scene audit, but stop treating it as a single score gate.

It should detect:

- `teacherly_exposition`
- `rigid_turn_taking`
- `repetitive_agreement_shape`
- `repetitive_challenge_shape`
- `persona_mismatch`
- `dry_facts`
- opener echo
- repeated punchline carryover
- recap openings when the prior scene already landed the point
- awkward reset between scenes

It may still output a score, but repair decisions should be based on structured issues, not only on the score threshold.

### Model choice

Use a cheaper model for both auditors first.

These are pattern-detection tasks, not heavy generative tasks.

### Repair trigger policy

Trigger downstream polish when:

- dialogue audit contains a medium-or-higher style issue
- dialogue audit contains a medium-or-higher boundary issue

Do not require `score < 0.6`.

### `writer_revolt` deprecation path

Do not keep the current revolt semantics as-is.

Replace:

- `score < 0.6` -> `REVISED` -> loop back to full scene regeneration

with:

- structured findings
- severity
- repair scope
- conditional `scene_polish`

Keep full scene regeneration only as a rare fallback for `scene_rewrite` findings.

## Phase 3: Add A Dedicated `dialogue_polish` Stage

### Goal

Fix surface naturalness and boundary quality after the full script exists.

### Files

- new: [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/dialogue_polish.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/dialogue_polish.py)
- [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/graph.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/graph.py)
- tests in [/Users/yaroslavabramov/side/podgen/backend/tests](/Users/yaroslavabramov/side/podgen/backend/tests)

### Inputs

For each flagged scene:

- `dialogue_json`
- `scene_job`
- scene claims already used in prompt payload
- previous scene boundary context
- recent scene starts
- dialogue audit findings

### Hard invariants

- no new facts
- no loss of required claim support
- no removal of attribution
- no chronology changes
- no scene-job drift

### Allowed rewrites

- change opener shape
- reduce explicit recap at the scene boundary
- vary agreement and challenge forms
- break mechanical turn rhythm
- merge or split lines for natural flow

### Repair granularity

Start with:

- `scene_polish`

Reserve:

- `scene_rewrite`

for cases where the scene is structurally wrong.

Do not implement `line_patch` in the first version.

### Why this stage exists

This is the clean place to solve:

- repeated confirmation style
- repeated interruption style
- formulaic scene openings
- local echo between adjacent scenes

This should not be forced onto `act_planner`.

### Rollback rule

Every accepted polish must pass a post-check against the original scene.

Rollback to the original if the polished version:

- loses required claim ids
- materially lowers factual coverage
- drops attribution
- drifts from the original scene job

Persist both the attempted rewrite and the rollback decision in debug artifacts.

## Phase 4: Turn Citation Audit Into A Real Quality Gate

### Goal

Stop paying for a factual audit that does not influence the output.

### Files

- [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/citation_auditor.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/citation_auditor.py)
- new: [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/citation_repair.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/citation_repair.py)
- [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/graph.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/graph.py)

### New citation issue severities

Use at least three levels:

- `low`
- `medium`
- `high`

Suggested mapping:

- `missing_attribution` -> medium
- `audience_mismatch` -> low or medium
- `unverified_stated_as_fact` -> high
- `missing_claim_link` on factual line -> high
- `forbidden_phrase` -> medium

### Repair policy

- `low` issues -> optionally record only
- `medium` issues -> `citation_repair`
- `high` issues -> `citation_repair`, then mandatory re-audit
- unresolved `high` issues after repair -> stop before audio

### Important rule

Do not rewrite a whole scene by default.

For v1, use targeted repair prompts against the scene, but accept or reject them at the scene level.

Only escalate to full scene rewrite if:

- too many factual lines are unsupported
- the scene drifts so much that patching would be unstable

### Citation repair rollback

If a citation repair:

- does not reduce severity
- loses required claims
- lowers persona consistency significantly

reject it and preserve the pre-repair version.

### Why this is necessary

On the latest run:

- `12` scenes were `failed`
- yet the graph still proceeded

That is the clearest sign that the current audit is not acting as quality control.

## Phase 5: Rewire The Graph Around Repair Loops

### Goal

Put repair loops where they belong and keep them bounded.

### Files

- [/Users/yaroslavabramov/side/podgen/backend/app/pipeline/graph.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/graph.py)
- [/Users/yaroslavabramov/side/podgen/backend/app/config.py](/Users/yaroslavabramov/side/podgen/backend/app/config.py)

### Proposed graph-level flow

```text
write_screenplay
-> audit_and_polish_dialogue
-> audit_and_repair_citations
-> if severe failures remain: stop
-> generate_audio
```

### Internal step behavior

`audit_and_polish_dialogue`:

- run one cheap dialogue audit over each scene with boundary context
- polish only flagged scenes
- re-check polished scenes before accepting

`audit_and_repair_citations`:

- run citation audit
- repair only scenes with medium/high citation issues
- re-audit only repaired scenes
- block on unresolved high severity issues

### Governors

Add explicit budgets:

- max dialogue polish passes per scene
- max citation repair passes per scene
- max total phase6 repair loops per run
- max accepted scene_polish attempts per scene
- max accepted citation repair attempts per scene

### Reason

This prevents quality work from turning into infinite loop churn.

## Phase 6: Add Real Debug Artifacts

### Goal

Make audit and repair behavior inspectable without querying the DB blindly.

### Files

- `phase6_writing` modules
- new debug helpers under current `tmp/debug` convention

### Dumps to add

- `dialogue_audit`
- `dialogue_polish`
- `citation_repair`

Each dump should include:

- inputs used
- structured findings
- selected repair scope
- before/after snippets
- final decision

### Why

If a run gets worse, we should be able to answer:

- which audit triggered
- what it saw
- why it repaired
- what changed

without guesswork.

## Phase 7: Cost And Throughput Guardrails

### Goal

Keep the new quality stack financially and operationally sane.

### Rough incremental cost model

On a 24-scene episode:

- dialogue audit:
  - `24 x Haiku`
- dialogue polish:
  - only for flagged scenes, target `20-35%`
- citation audit:
  - already exists in the pipeline today
- citation repair:
  - only for medium/high failures, not every scene

This means the main new overhead should come from:

- one cheap audit pass per scene
- a limited number of Sonnet repairs

Not from doubling or tripling the number of unconditional passes.

### Guardrail

Do not ship a version where every scene always goes through:

- one style audit
- one polish
- one citation repair

The system must stay selective.

## Phase 8: Success Criteria

The pipeline should not be considered complete until all of these are true on a fresh end-to-end run.

### Content / rigor

- no unresolved high-severity citation failures before audio
- average `fact_coverage_ratio >= 0.9`
- average `rigor_policy_violations <= 0.5`

### Boundary quality

- no repeated punchline echo across adjacent scenes in the main act transitions
- no obvious recap-openers where the prior scene already landed the proposition

### Dialogue quality

- local style auditor flags only a small minority of scenes
- turn rhythm is no longer mechanically alternating in nearly every scene
- explicit confirmation shapes are clearly reduced

### Operational quality

- all audit findings are inspectable
- repair decisions are inspectable
- audio generation does not run after unresolved severe factual failures

## Implementation Order

### P0

Make the existing audits useful before adding more prompt text.

1. Add `audit_findings` storage
2. Persist structured outputs from current audits
3. Add severity and repair scope

### P1

Replace `writer_revolt` threshold logic with:

- one dialogue audit with boundary context

but keep rewrites disabled until outputs look sane.

### P2

Add `dialogue_polish` for flagged scenes only.

### P3

Add `citation_repair` and re-audit loop.

### P4

Make unresolved high-severity citation issues block audio.

### Parallelization note

`P0` storage work and `P1` audit logic can be developed in parallel, but they should be integrated in this order:

1. findings storage exists
2. new audit outputs are persisted into it

## What Not To Do

- Do not add banned-word lists for `Именно`, `Подожди`, or any other lexical marker.
- Do not route all quality problems back into `showrunner` or `act_planner`.
- Do not rewrite the whole episode when one line or one scene boundary is bad.
- Do not keep paying for passive citation verdicts that have no downstream effect.
- Do not add another broad deterministic semantic dedupe layer as the first move.

## Practical Outcome

If this plan is implemented correctly, the system should stop behaving like:

- one LLM writes
- one LLM judges
- nobody acts on the judgment

and start behaving like:

- the script is written
- weak spots are identified precisely
- only the weak spots are repaired
- unresolved factual defects stop the pipeline

That is the shortest credible path from the current state to a script that is actually good enough to publish as a podcast episode.
