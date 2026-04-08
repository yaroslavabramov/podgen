# Phase 6 Writer / Audit Routing And Prompt Refactor Plan

Date: 2026-04-07
Scope: `screenwriter` -> `writer_revolt` (`dialogue_audit`, `act_dialogue_audit`, `dialogue_polish`, `episode_seam_audit`)
Primary run used as evidence: `3870242f-5f13-4dcf-a74f-46dc76faf4d7`

## Goal

Improve Phase 6 as a product layer, not as a one-off rescue pass for a single episode.

The main problem exposed by the latest run is not factual weakness. It is that:

- `screenwriter` still falls into repeatable conversational habits
- `dialogue_audit` detects those habits, but in a noisy and overly local way
- `dialogue_polish` is asked to solve too many problems at scene level
- seam handling catches real boundary failures, but the loop is not closed after polishing

This memo focuses on where the fix should live:

- what should be fixed in `screenwriter`
- what should be fixed in `dialogue_audit`
- what should be fixed in routing / escalation logic
- where prompt refactor is preferable to yet another layer of additive instructions

## Non-Negotiable Constraints

Phase 6 changes must remain:

- universal
- multilingual
- portable across different domains and corpora

Therefore:

- no language-specific phrase blacklists
- no domain-specific heuristics
- no episode-specific hacks
- no "if topic is X, use dialogue style Y" logic

The layer should operate through:

- structured scene metadata
- prompt contracts
- issue taxonomy
- routing / escalation decisions
- compact, machine-facing audit outputs

## What The Latest Run Actually Shows

From the latest `writer + engagement audit` run:

- `26` scenes were audited
- total issues: `65`
- severity split: `59 low`, `6 medium`
- top issue types:
  - `repetitive_agreement_shape`: `20`
  - `scene_opening_echo`: `18`
  - `rigid_turn_taking`: `13`
  - `repetitive_challenge_shape`: `4`
  - `no_examples`: `4`
- `dialogue_polish` attempts: `25`
- accepted: `19`
- `target_not_resolved`: `5`
- `llm_or_parse_failed`: `1`

Key observations:

1. The dominant failure mode is repeated conversational shape, not factual failure.
2. A large share of the issue volume is low-severity stylistic / rhythmic noise.
3. `dialogue_polish` can improve individual scene openings and closings, but it is weak when an entire act is locked into one explanatory rhythm.
4. The strongest high-value signal came from `episode_seam_audit`, especially `Act 4 -> Act 5`.

## What Is A Writer Problem Vs What Is A Routing Problem

### Mostly writer problems

These usually point to how the scene is written, not to bad planning:

- `repetitive_agreement_shape`
- `rigid_turn_taking`
- `teacherly_exposition`

These should be reduced first in `screenwriter`, not just cleaned up later by `dialogue_polish`.

### Mixed writer / boundary problems

These often reflect both local scene writing and weak cross-scene / cross-act handoff:

- `scene_opening_echo`
- `boundary_restatement`
- seam statuses like `replay_question`

These need:

- better first-scene / first-lines writer behavior
- better seam-aware repair logic

### Not automatically scene-level problems

When the same low/medium style flags recur across several adjacent scenes in one act, that is no longer "three or four independent local bugs." It is an act-level monotony signal.

This is exactly what happened in `Act 2`: repeated explanatory resets and repeated confirmation rhythm kept reappearing, and scene-level polishing had poor ROI.

## Main Product Diagnosis

## P0. `dialogue_audit` is too verbose and too eager to produce local findings

### Evidence

- `65` issues for `26` scenes is too much for a machine-facing routing layer
- almost all findings are `low`
- the one truncated Haiku response still shows a long-form essay-style diagnostic instead of a compact machine report

### Why this matters

This makes the system:

- expensive
- noisy
- hard to route correctly
- fragile to output truncation

### Change

Refactor the scene audit contract into a compact machine-facing format:

- maximum `2` or `3` issues per scene
- short descriptions
- short suggestions
- prefer `act_warning` when the issue is clearly part of a repeated act-level pattern
- use a single stable output language for internal audits; prefer English for consistency

### Specific prompt refactor

Do not keep growing `EVAL_SYSTEM` by adding more guidance bullets.

Refactor it into:

1. role
2. compact schema
3. issue-type definitions
4. severity / scope policy
5. anti-overflag rules
6. output brevity contract

Also add explicit constraints:

- do not return more than `3` issues
- do not repeat the same root cause under multiple issue types
- prefer one dominant issue over several paraphrases
- if a problem is mostly act-level, emit `act_warning` instead of several scene-polish findings

## P0. `dialogue_polish` is being used for act-level monotony that it cannot solve

### Evidence

`Act 2` accumulated repeated low/medium stylistic findings, but:

- `Act 2 Scene 3`: `target_not_resolved`
- `Act 2 Scene 4`: `target_not_resolved`
- `Act 2 Scene 5`: `target_not_resolved`
- `Act 2 Scene 6`: `target_not_resolved`

That is not "bad luck on four scenes." It is a routing failure.

### Why this matters

The system spends tokens repeatedly on local rewrites even when the act is stuck in one dialogue mode.

### Change

Add act-level escalation routing:

- if several scenes in one act receive the same repetition / opener / explanatory-shape findings
- and polishing fails or is rejected repeatedly
- stop treating them as independent local scene-polish problems

Escalate to an act-level signal such as:

- `act_monotony_explanatory_reset`
- `act_monotony_confirmation_rhythm`
- `act_monotony_opener_echo`

This does not require immediate full act rewrite.
It at least prevents wasteful repeated scene polish attempts.

## P0. Add `multi_scene_polish` as the default intervention between scene polish and act rewrite

### Why this matters

There is a missing middle layer between:

- polishing one scene at a time
- rewriting a whole act

That gap is exactly where many current failures live.

In cases like the latest `Act 2`, the problem is not that one scene is locally broken. The problem is that several adjacent scenes are written in the same explanatory cadence:

- similar opener shape
- similar B-restates / A-confirms rhythm
- similar landing function

Trying to repair them one by one is low ROI.
Rewriting the whole act is usually too expensive and too risky.

### Change

Add a new intervention mode:

- `multi_scene_polish`

This mode should operate on a short contiguous block inside one act, usually:

- `2` scenes
- or `3` scenes

It should be triggered when:

- several nearby scenes share the same repetition family
- scene-level polish is repeatedly rejected or accepted with weak effect
- the act-level problem is real, but still local enough that a full act rewrite is unnecessary

### What it should do

`multi_scene_polish` should preserve:

- scene order
- scene jobs
- claim coverage
- evidence rails

But it should rewrite the selected block jointly so the scenes no longer sound like copies of the same conversational move.

The model should be asked to improve not just each scene in isolation, but the local distribution across the block:

- vary opener shape
- vary challenge shape
- vary agreement / confirmation rhythm
- avoid making all scenes explanation-first
- ensure the block has forward motion, not repetition

### Why this is better than repeated scene polish

Scene-by-scene polish cannot reliably solve a pattern that only becomes visible across several adjacent scenes.

`multi_scene_polish` gives the model the actual local neighborhood and lets it redistribute movement across scenes instead of making shallow local edits that still preserve the same overall cadence.

### Why this is safer than full act rewrite

It does not ask the system to:

- redesign the act
- reorder scenes
- change the macro arc

It only asks for a coordinated local rewrite inside a bounded region.

That makes it a better first escalation path for act-level monotony than jumping straight to a full act rewrite.

### Routing policy

Preferred escalation order should become:

1. `scene_polish` for a genuinely local issue
2. `multi_scene_polish` for a repeated family across adjacent scenes
3. `act rewrite` only when the problem is broader than one local scene cluster

### Prompt refactor implication

This should not reuse the current single-scene `POLISH_SYSTEM` unchanged.

`multi_scene_polish` needs its own prompt contract:

- the input is a block of neighboring scenes
- the goal is to improve local variety and progression across the block
- the model must preserve each scene's content function while differentiating the scenes from one another

Without that contract, the system will simply produce three cleaner scenes that still share the same act-level rhythm.

## P0. `episode_seam_audit` finds real problems, but the repair loop is incomplete

### Evidence

The seam audit correctly identified:

- `Act 4 -> Act 5`: `replay_question`, `high`

Then `dialogue_polish` improved `Act 5 Scene 1`, but there was no post-polish seam re-check.

### Why this matters

The system has no deterministic way to know whether seam repair actually worked.

### Change

When a first scene of a non-first act is polished and the intervention targets include a seam issue:

- rerun seam audit for that seam only
- use the post-polish seam result as the final seam status

This should be a cheap targeted re-check, not a second full episode pass.

## P1. `screenwriter` should fix common rhythm failures directly instead of outsourcing them to audit

### Evidence

The most frequent scene issues are writer-surface issues:

- `repetitive_agreement_shape`
- `rigid_turn_taking`
- `teacherly_exposition`

These are usually visible in the raw written scene before any audit.

### Why this matters

If `screenwriter` keeps defaulting to:

- `B asks / reformulates`
- `A confirms`
- `A explains`

then downstream repair is forced to undo a recurring base pattern scene by scene.

### Change

Strengthen `SCREENWRITER_SYSTEM` around positive conversational variety:

- B should not mainly paraphrase A's point back for confirmation
- not every challenge should be question-shaped
- not every clarification should have an explicit confirmation marker
- scenes should use more direct continuations, objections, partial answers, consequences, and concrete observations

But do this by prompt refactor, not more additive bullets.

### Specific prompt refactor

The current `SCREENWRITER_SYSTEM` is directionally good but too additive and overloaded.

Symptoms:

- too many overlapping rules
- too many repeated "do not default to..." forms
- several continuity / opener instructions overlap semantically
- a lot of negative guidance, not enough compact positive operating modes

Refactor it into a smaller hierarchy:

1. output format
2. continuity contract
3. scene-role contract
4. dialogue-move diversity contract
5. evidence / attribution contract
6. anti-pattern shortlist

The prompt should state a smaller set of stronger principles, for example:

- every scene must make one dominant conversational move
- the first 1-2 lines must advance from the boundary, not paraphrase it
- B must add substance by hypothesis / objection / implication / example, not by restatement alone
- avoid repeating one confirmation or challenge pattern within the scene

This is preferable to continuing to append more low-level anti-pattern bullets.

## P1. `scene_opening_echo` should be split more cleanly between local and boundary cases

### Why this matters

Right now `scene_opening_echo` is doing too much:

- some cases are harmless continuity
- some cases are scene-local weak starts
- some cases are true cross-act seam failures

Those are not the same intervention type.

### Change

Keep one local scene issue for weak carry-over, but rely on seam audit for real cross-act replay:

- local weak opener -> scene polish candidate
- cross-act replay -> seam problem first
- repeated opener family across act -> act-level repetition problem

This reduces scope confusion and helps routing.

## P1. `no_examples` is useful but should not dominate short scenes

### Why this matters

In some scenes, `no_examples` is a good diagnosis.
In others, it becomes a weak preference for "add more texture" even when the scene already has a valid single example or is intentionally brief.

### Change

Tighten the audit contract:

- do not emit `no_examples` for a short scene that already has one strong concrete anchor
- prefer `no_examples` only when the scene's declared job clearly implies concreteness (`case_study`, `stress_test`, some `apply` scenes)

This reduces noise without needing domain-specific rules.

## P1. `dialogue_polish` prompt should be refactored into targeted intervention mode

### Why this matters

The current `POLISH_SYSTEM` tries to do too many things at once:

- preserve facts
- preserve metadata
- preserve scene job
- handle repetition
- handle seam problems
- improve naturalness

The result is a rewrite prompt that is broad and often "softly aware" instead of sharply targeted.

### Change

Refactor `POLISH_SYSTEM` around intervention mode:

- opener repair
- rhythm repair
- agreement-shape repair
- seam repair

The prompt can still be one prompt, but it should explicitly prioritize the provided `intervention_targets` over generic polish.

### Specific prompt refactor

Move generic style guidance lower.
Make the opening section of the prompt:

- what must stay fixed
- which target must be repaired
- what counts as a successful repair

This should make accepted polish more likely to change the actual failing move rather than performing a broad but shallow rewrite.

## P1. Add act-level monotony output as a real routing signal, not just observability

### Why this matters

`act_dialogue_audit` already exists, but it is still used mostly as a prioritizer and context attachment.

The system needs a stronger use of act-level output:

- not to rewrite the whole act automatically
- but to decide when local polishing is the wrong tool

### Change

If the act audit identifies a dominant repeated family and local scene findings match it across several scenes:

- reduce scene-polish spend on late duplicate cases
- attach an act-level quality signal to the run
- prefer one or two scenes whose change would break the repeated family most efficiently

## P2. Tighten output contracts to reduce truncation / parse noise

### Evidence

The failed Haiku response was not nonsense. It was a partially valid audit JSON that got cut off mid-response.

### Change

Fix the contract before increasing token budgets:

- fewer issues
- shorter descriptions
- shorter suggestions
- one language
- no essay-style explanations

Only after this, reconsider whether `max_tokens=1024` should move upward.

## What Should Be Fixed First

1. Refactor `EVAL_SYSTEM` into a compact, low-noise, machine-facing contract.
2. Add routing that escalates repeated scene-level stylistic failures into an act-level monotony signal.
3. Add targeted post-polish seam re-audit for repaired act openings.
4. Refactor `POLISH_SYSTEM` so it behaves like a targeted intervention prompt rather than a broad rewrite prompt.
5. Refactor `SCREENWRITER_SYSTEM` so it produces fewer confirmation / restatement loops up front.
6. Only if the above is still insufficient, add `multi_scene_polish` as the preferred middle intervention between single-scene repair and full act rewrite.

## Implementation Order Adopted

The first implementation pass should stay conservative:

1. compact `dialogue_audit`
2. act-level monotony as a routing / quality signal
3. targeted `dialogue_polish` contract
4. post-polish seam re-audit

`multi_scene_polish` is intentionally deferred.

Reason:

- it adds a new intervention mode, new prompt surface, and new routing complexity
- the current system still has cheaper wins available in audit compaction, routing, and seam loop closure
- Phase 6 should first prove that existing `scene_polish` + better routing cannot solve the remaining pattern before introducing a new repair layer

## What Should Not Be Done First

- Do not start by pushing this whole problem upstream into `showrunner`.
- Do not add language-specific banned markers.
- Do not patch this with per-episode examples or phrase rules.
- Do not simply increase the number of scene polish attempts.
- Do not treat every `scene_opening_echo` as a severe bug.

## Success Criteria

This plan is working when:

- issue volume per scene drops materially without losing the real seam / monotony detections
- `repetitive_agreement_shape` and `rigid_turn_taking` decrease at the writer-output level
- repeated scene-polish failures inside one act become rare
- first-scene seam repairs are re-checked after polish
- audit outputs become shorter, more consistent, and easier to route
- Phase 6 spends fewer tokens on cosmetic scene rewrites and more on the places where intervention really changes the episode
