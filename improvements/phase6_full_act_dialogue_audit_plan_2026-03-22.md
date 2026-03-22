# Phase 6 Full-Act Dialogue Audit Plan

Date: 2026-03-22
Scope: `screenwriter` -> `writer_revolt` (`dialogue_audit`, `act_dialogue_audit`, `dialogue_polish`)
Primary run that exposed the problem: `fccc9c33-fd78-4da7-90ac-bf40c0177936`

## Goal

Keep `act_audit`, but replace the current surrogate with a real act-level dialogue audit that can:

- see repetition across the actual text of an act
- distinguish good continuity from bad replay
- prioritize the right scenes for polish
- catch cross-act seam failures like `A1S6 -> A2S1`
- reduce repeated opener habits and repeated conversational mechanisms without wordlists

This plan exists because the current `act_dialogue_audit` is too weak to justify the trust currently placed in it.

## Problem Statement

The current `act_dialogue_audit` in `writer_revolt` is not a real act audit.

It currently sees only:

- first 2 lines of each scene
- last 2 lines of each scene
- opening speaker / closing speaker
- local issue types
- heuristic counts

This is enough for a cheap prioritizer.
It is not enough for a real act-level judgment.

As a result:

- it overflags some acceptable carry-over as `scene_opening_echo`
- it misses some bad replay that depends on penultimate lines or fuller scene context
- it cannot reliably judge repeated explanatory mechanisms across multiple scenes
- it cannot meaningfully judge repeated short markers or repeated discourse shapes inside an act
- it cannot see cross-act seam failures at all

## Non-Negotiable Constraints

This redesign must remain:

- universal
- multilingual
- portable across very different corpora

Therefore:

- no system-level phrase dictionaries
- no hardcoded Russian or English marker lists
- no case-specific fixes for one episode
- no banned-word solution for `именно`, `то есть`, `подожди`, etc.

The layer must work from:

- actual act text
- structured scene metadata
- language passed explicitly
- deterministic repetition metrics
- structured LLM output

## What The Layer Must Actually Do

The correct responsibilities of a real act-level dialogue audit are:

1. Detect repeated conversational mechanisms across scenes in the same act.
2. Detect scene-openers that replay previous scene conclusions instead of advancing the act.
3. Detect speaker-distribution imbalance at the act level.
4. Distinguish:
   - good continuity
   - normal thesis-to-example carry-over
   - bad replay / re-asking / re-framing
5. Prioritize the smallest number of scenes whose polishing would improve the act the most.
6. Provide act-level context to `dialogue_polish`.

It must not:

1. Replace local scene audit.
2. Replace citation audit.
3. Become a phrase blacklist.
4. Become a pairwise scene-by-scene expensive audit loop.

## Core Design

### 1. Real Full-Act Input

Replace the current edge-only scene summaries with actual act text.

For each scene in the act, the prompt payload should include:

- `scene_number`
- `scene_job`
- full `dialogue`
- local `issue_types`
- opener speaker
- closer speaker
- optional deterministic per-scene features

This does not require the whole episode.
It requires the whole act.

That is feasible:

- one act is usually small enough
- there are only 4 acts
- this is still much cheaper than pairwise auditing every scene boundary

### 2. Separate Cross-Act Seam Audit

Act audit alone cannot catch `A1S6 -> A2S1`-style replay, because that is an act boundary, not an intra-act problem.

Add one cheap extra seam layer:

- input: only the 3 act seams in a 4-act episode
- each seam gets:
  - last 2 lines of previous act final scene
  - first 2 lines of next act opening scene
  - scene jobs for both scenes
- output:
  - `bad_seams`
  - `priority_scene_numbers`
  - `type = replay_question | replay_punchline | weak_reset | acceptable_handoff`

This should be one cheap call per episode, not one call per seam.

### 3. Deterministic Repetition Report From Real Text

Before any LLM act audit call, compute a cheap full-act repetition report from actual dialogue.

This report should be language-agnostic and based on structure and recurrence, not hand-built phrase lists.

Suggested signals:

- opener speaker distribution by act
- closer speaker distribution by act
- per-scene turn pattern (`ABAB`, `BABABA`, etc.)
- repeated scene-opener prefix clusters based on normalized first-line token spans
- repeated confirmation-first answer shapes
- repeated question-first opener shapes
- repeated explanatory reset shapes
- repeated short line shapes by position:
  - first line
  - second line
  - scene landing

Implementation principle:

- use normalized token spans and positional recurrence
- not fixed lexical dictionaries
- not language-specific markers

This report becomes structured input to the LLM act audit.

### 4. Full-Act LLM Audit

Use one LLM pass per act, but make it real.

The LLM should receive:

- full act scene cards with actual dialogue
- deterministic repetition report
- local issue types
- language

It should return:

- `priority_scene_numbers`
- `distribution_findings`
- `bad_boundary_pairs`
- `dominant_repetition_families`
- optional `speaker_distribution_warning`

Key requirement:

The LLM must explicitly distinguish:

- `acceptable_handoff`
- `thesis_to_example_progression`
- `replay_of_same_question`
- `replay_of_same_punchline`
- `repeated_explanatory_reset`
- `repeated_confirmation_first_opening`

This is the core semantic improvement missing today.

### 5. Prioritization Logic

Current prioritization is too dependent on local scene findings and too weakly grounded in act reality.

New priority score should combine:

- local scene issues
- act-level repetition findings
- whether the scene participates in a bad boundary pair
- whether the scene is one of the smallest edits that breaks a repeated family
- whether the act is already overly skewed to one opener speaker

Priority should not mean:

- polish every flagged scene

Priority should mean:

- polish the scenes that break the repeated act pattern most efficiently

### 6. Dialogue Polish Must Receive Real Act Context

`dialogue_polish` should receive a richer act-level context block:

- act repetition families
- whether the scene participates in a bad boundary pair
- whether this opener currently repeats a previous mechanism
- whether the act is overly dominated by one opener speaker or one opener family

This context should describe the problem abstractly, not via banned words.

Example of correct input shape:

- `this act is overusing confirmation-first scene openings`
- `this scene currently replays the previous scene's question instead of moving to consequence`
- `this act is overusing listener-question openings from speaker B`

Not:

- `avoid saying "подожди"`
- `avoid saying "именно"`

## Acceptance Rules

### 1. Local Scene Acceptance

For local polish acceptance, keep the existing local guards, but add better act-aware acceptance:

- if the scene was prioritized because of opener replay, the opener must materially change
- if the scene was prioritized because of repeated opener family, the opener must leave that family
- if the scene was prioritized because of speaker-distribution imbalance, the opener should not preserve the same skew when a natural alternative exists

Do not try to deterministically solve all discourse quality.
Do only narrow acceptance where the intervention goal is explicit.

### 2. Seam Acceptance

If a scene was polished due to seam replay:

- compare:
  - previous scene closing lines
  - current scene opening lines
- reject polish if the current opening still asks or states materially the same thing

This acceptance must focus on semantic replay, not exact string similarity.

### 3. Repetition Acceptance

If a scene was polished due to act-level repetition:

- verify that the repeated family is actually weakened
- not necessarily eliminated globally in one shot, but reduced locally

The acceptance check can be narrow and structured:

- opener family changed
- same repeated short opener shape no longer dominates this scene
- speaker role at opening is more varied when appropriate

## Implementation Plan

### P0. Stop Calling The Current Layer A Real Act Audit

Rename or re-scope the current layer mentally and in code comments:

- current version = cheap edge-summary prioritizer

This prevents false confidence during further debugging.

### P1. Build Deterministic Full-Act Report

In `writer_revolt.py`:

- compute full-act repetition features from actual `dialogue_json`
- base them on structure and recurrence, not dictionaries
- store them in `quality_signals["dialogue_act_reports"]`

Deliverables:

- opener speaker distribution
- closer speaker distribution
- repeated opener-shape clusters
- repeated turn-pattern clusters
- repeated short line families by scene position
- bad local boundary candidates

### P2. Replace Edge-Only LLM Act Audit With Full-Act Input

Change `_build_act_dialogue_audit_prompt_payload(...)` so it sends:

- full scene dialogue for the act
- scene job
- deterministic repetition report
- local issue types

Do not keep the current edge-only summary as the primary payload.

### P3. Add Episode Seam Audit

Add one cheap seam pass across act boundaries:

- `A1S6 -> A2S1`
- `A2S6 -> A3S1`
- `A3S6 -> A4S1`

It should identify:

- bad replay
- weak reset
- acceptable handoff

This pass should feed prioritization, not rewrite directly.

### P4. Rework Priority Score

Use:

- local scene issues
- act-level repetition families
- seam findings
- current per-act intervention budget

This should decide which scenes get polish first.

### P5. Upgrade Dialogue Polish Payload

Pass into `dialogue_polish`:

- act-level repetition findings
- scene's role in those findings
- seam finding if present
- intended intervention target

The polish prompt should know:

- whether it is breaking an opener replay
- whether it is reducing repeated question-first openings
- whether it is fixing repeated explanatory resets

### P6. Add Narrow Act-Aware Acceptance

Only for explicit intervention targets:

- opener replay
- repeated opener family
- seam replay

Do not add broad vague acceptance rules for all dialogue quality.

### P7. Add Better Observability

Debug dumps must include:

- full act audit input summary
- deterministic repetition report
- seam audit result
- final priority ranking per act
- why a scene was chosen for polish
- whether polish was accepted or rejected because the target family persisted

## File-Level Change List

Likely files:

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/writer_revolt.py`
- maybe a new helper module such as:
  - `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/act_dialogue_features.py`
  - `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/act_seam_audit.py`
- tests:
  - `/Users/yaroslavabramov/side/podgen/backend/tests/test_writer_revolt_prompt.py`
  - new feature-level tests if the deterministic report becomes large

## Test Plan

### Unit Tests

1. Repetition clustering uses actual act text, not just edge lines.
2. Repeated opener families are detected without language-specific dictionaries.
3. Seam audit catches replayed question setup at act boundaries.
4. Thesis-to-example continuation is not automatically treated as bad replay.
5. Priority score promotes scenes that break repeated act patterns efficiently.
6. Act-aware acceptance rejects opener-polish that leaves the same replay intact.

### Fixture-Based Tests

Add focused synthetic acts:

1. Good carry-over:
   - scene N ends with thesis
   - scene N+1 opens with a concrete example
   - expected: acceptable handoff

2. Bad replay:
   - scene N ends with a question
   - scene N+1 opens by re-asking that same question
   - expected: bad seam / opener replay

3. Overused opener family:
   - 4 scenes in the act all open with the same challenge shape
   - expected: act audit prioritizes a subset of scenes for polish

4. Repeated short markers across act:
   - multiple scenes repeat the same short opener shape
   - expected: report clusters them without any hardcoded phrase list

## Success Criteria

The redesign is successful if:

1. `scene_opening_echo` findings align better with human judgment.
2. Good carry-over is no longer overflagged as replay.
3. Cross-act seam failures like `A1S6 -> A2S1` are caught.
4. Repetition reduction is visible at act level, not just scene-local level.
5. `dialogue_polish` no longer simply flips opener dominance from `A` to `B`.
6. Repeated opener / explanatory families decline across acts.
7. The system does this without:
   - language dictionaries
   - banned words
   - pairwise per-scene expensive passes

## What Not To Do

1. Do not keep the current edge-only LLM pass and pretend it is a full act audit.
2. Do not solve repeated words with a blacklist.
3. Do not add Russian or English phrase heuristics for opener families.
4. Do not add one expensive LLM pass per scene pair.
5. Do not let seam replay bugs hide just because act-local audit cannot see across acts.

## Recommended Shipping Order

1. `P1` deterministic full-act report
2. `P2` real full-act LLM audit payload
3. `P3` episode seam audit
4. `P4` priority-score rewrite
5. `P5` richer polish payload
6. `P6` narrow act-aware acceptance
7. rerun `write_screenplay -> evaluate_and_revolt`
8. only after that rerun `audit_citations`

## Bottom Line

`Act audit` should stay.

But it must become a real act-level layer:

- full act text
- real repetition features
- cross-act seam awareness
- narrow actionable outputs

The current version is a prioritizer with an act-shaped prompt.
The next version must actually understand the act.
