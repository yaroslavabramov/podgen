# Act Planner Overlap And Novelty Fix List

## Problem

The latest `plan_acts` run still bakes semantic repetition into the beat sheet before the
screenwriter sees it.

This is not only a writer problem. The beat sheet itself already repeats:

- the same `claim_id` across multiple scenes in the same act
- the same proposition core across adjacent scenes
- takeaway/summary beats that reteach mechanism instead of landing consequence

As a result, `screenwriter` can only soften the repetition, not remove it.

## Evidence From Latest Run

Files:

- `/Users/yaroslavabramov/side/podgen/tmp/debug/planning_acts/20260320T150420265372Z_run-fccc9c33-fd78-4da7-90ac-bf40c0177936_act-01_ok_a4eb351670.json`
- `/Users/yaroslavabramov/side/podgen/tmp/debug/planning_acts/20260320T150510139975Z_run-fccc9c33-fd78-4da7-90ac-bf40c0177936_act-02_ok_eaa39dad0e.json`
- `/Users/yaroslavabramov/side/podgen/tmp/debug/planning_acts/20260320T150554781329Z_run-fccc9c33-fd78-4da7-90ac-bf40c0177936_act-03_ok_39b858dcfe.json`
- `/Users/yaroslavabramov/side/podgen/tmp/debug/planning_acts/20260320T150647066715Z_run-fccc9c33-fd78-4da7-90ac-bf40c0177936_act-04_ok_5c2d08b539.json`

Literal repeated `claim_id`s inside one act:

- Act 1:
  - `71aca657-3c51-466f-93c2-9a61ea41ff71` in scenes `1` and `2`
  - `3992bbc6-7f9b-427c-a6d7-b0b57fedc888` in scenes `4` and `6`
- Act 2:
  - `ac26677b-e372-4451-a261-cd1b558e4a35` in scenes `1` and `6`
  - `8c127c80-ba1d-4283-a2c3-2baf8bf30b9f` in scenes `4` and `6`
- Act 3:
  - `a901938e-9209-4291-b6d6-70d6dce85310` in scenes `3` and `6`
  - `5bf3f7a7-1bdc-4564-bd13-fa6516413893` in scenes `5` and `6`
- Act 4:
  - `b8e4f218-a163-4528-84cb-1cc40ce649d0` in scenes `1` and `6`
  - `1d705345-9323-4173-a3f3-b5ae615763f7` in scenes `2` and `6`

Visible repeated proposition cores:

- Act 1 scenes `1` and `2`: same “currency chaos / mixed currencies / no single money” opener
- Act 2 scenes `4` and `6`: same `7.80 + certificate + full backing` mechanism
- Act 3 scenes `2` and `3`: `7.80` introduction followed immediately by reteaching the same mechanism core
- Act 3 scene `6`: takeaway repeats mechanism instead of keeping only the policy-cost landing
- Act 4 scene `6`: summary repeats legal/system setup instead of widening the lens cleanly

## Root Causes

### 1. No novelty validation after LLM output

`_normalize_beat_sheet(...)` in
`/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`
only validates:

- scene structure
- scene numbering
- allowed `claim_ids`
- allowed `evidence_ids`

It does **not** validate:

- repeated `claim_ids` across scenes
- repeated scene jobs
- takeaway/summary beats that reteach mechanism-heavy claims

### 2. Beat sheet has only one claim bucket

Each scene only has `claim_ids`.

This collapses two different functions into one list:

- claims that the scene introduces as new information
- claims that the scene only references or echoes

Because of this, takeaway scenes often inherit already-covered mechanism claims and then writer
treats them as material that must be explained again.

### 3. Prompt guidance is still too soft for endings

The prompt says:

- avoid repeating proposition cores
- takeaway should land consequence, not recap

But it does not make this operational enough to stop LLM from:

- reusing mechanism claims in `takeaway`
- reusing setup claims in `summary`

### 4. Writer is being asked to solve upstream overlap

`screenwriter` can soften overlap with callbacks, but if the beat sheet itself already repeats the
same mechanism or opener material, writer still receives duplicated scene jobs.

## Fixes

## P0: Deterministic primary/callback split after normalization

Do **not** use retry as the first-line fix for repeated `claim_id`s.

Instead, add a deterministic post-processing pass in
`/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`:

1. First appearance of a `claim_id` in an act becomes `primary_claim_ids`
2. Later appearances of the same `claim_id` become `callback_claim_ids`
3. The original flat `claim_ids` should no longer be the only scene-level claim field

Important detail:

- “first appearance” means first appearance in normalized scene order after scene numbering is fixed
- the split must run after scene normalization, not on raw LLM order

Why this is better than immediate retry:

- repeated `claim_id` is a predictable normalization problem
- retry is expensive and may return the same shape again
- the planner currently has no explicit callback bucket, so LLM is forced to reuse the same list for
  both “introduce” and “refer back”

This should be purely deterministic and should not require a second LLM call.

## P0.5: Add a real novelty/overlap quality gate only for structural failures

Keep a post-LLM quality gate, but make it narrower and more targeted.

Use retry only when post-processing still leaves a beat sheet structurally weak, for example:

- a scene ends up with no `primary_claim_ids` and no meaningful scene job
- a `takeaway` or `summary` scene still contains only repeated mechanism material
- adjacent scenes still perform the same explanatory job after primary/callback split

This keeps retry for genuine structural failures, not for every repeated `claim_id`.

Retry budget:

- keep this low
- maximum `2` act-planner retries is enough
- if the beat sheet still fails after that, surface the failure instead of looping longer

## P1: Split new claims from callback claims

Extend act planner output contract so scenes can distinguish:

- `primary_claim_ids`
- `callback_claim_ids`

Rules:

- `primary_claim_ids` must be unique within the act
- `callback_claim_ids` are optional references only
- takeaway/summary should usually use `callback_claim_ids` if they refer back, not repeat primary claims

This creates a usable contract for writer:

- primary = explain / reveal / establish
- callback = refer back briefly, do not reteach

## P1: Strengthen ending-scene constraints in prompt

In `ACT_PLANNER_SYSTEM`, make endings more explicit:

- `takeaway` must land consequence, implication, or reframing
- `takeaway` must not reteach the act’s main mechanism if that mechanism already had its own scene
- `summary` must compress understanding shift, not replay opener/setup

Add direct anti-patterns:

- do not reuse mechanism claims from the act’s core explanation scene in the final scene
- do not repeat the opening image/problem in the final scene unless the act is explicitly circular and adds a new consequence

## P2: Add debug reporting for overlap failures

When novelty validation fails, write debug details:

- repeated claim ids
- overlapping scene pairs
- invalid final-scene reuse

This should go into the act planner debug dump so prompt iteration becomes faster.

## Coupled Follow-Up: Cross-Act Semantic Overlap

This plan fixes within-act duplication first. It does **not** fully solve cross-act semantic overlap,
for example:

- Act 2 scene 6 and Act 3 scene 3 both reteach `7.80 + Certificates of Indebtedness + full backing`

Primary/callback split inside one act will not catch this, because the overlapping material sits in
different acts and often uses different `claim_id`s.

Near-term coupled fix:

- improve `screenwriter` continuity payload so global covered memory carries compact anchor terms, not
  only truncated claim statements
- prefer a dedicated field such as `mechanism_anchors` over merely increasing statement length
- preserve key mechanism markers such as:
  - numbers like `7.80`
  - institution names like `HKMA`, `Exchange Fund`
  - mechanism terms like `Certificates of Indebtedness`

This should help writer recognize that a mechanism was already established in an earlier act, even
when later scenes use different claim ids around the same proposition core.

This is a coupled downstream fix, not a replacement for upstream act-planning cleanup.

## Immediate Manual Rewrites Suggested By Latest Run

### Act 1

- Scene 1 should keep the image of currency chaos
- Scene 2 should move directly to `1842 legal tender surrender`
- Scene 2 should not redraw the same mixed-currency tableau

### Act 2

- Scene 4 should remain the only full explanation of `7.80 + certificate + backing`
- Scene 6 should only land the implication:
  - not trust in bank brand
  - but collateralized privilege
  - then bridge to `1983`

### Act 3

- Scene 2 should be only:
  - panic
  - Greenwood
  - why `7.80`
- Scene 3 should be the only full mechanism scene
- Scene 6 should keep only the cost of stability:
  - no repeated currency-board explanation

### Act 4

- Scene 6 should not repeat:
  - `1997`
  - legal guarantees
  - system mechanics
- It should widen the lens and land the general principle cleanly

## Implementation Order

1. In parallel:
   - add deterministic `primary_claim_ids` / `callback_claim_ids` post-processing
   - tighten prompt rules for takeaway/summary
2. Add narrow structural quality gate for severe overlap failures
3. Add debug reporting for overlap failures
4. Re-run `plan_acts`
5. Then re-evaluate cross-act overlap in `screenwriter` continuity payload

## Success Criteria

The next `plan_acts` run should satisfy all of:

- no repeated `primary_claim_id` within any act
- no opener pair like current Act 1 scenes `1` and `2`
- no duplicate mechanism pair like current Act 2 scenes `4` and `6`
- no final-scene reuse of core explanation claims unless they are callback-only
- writer should then have fewer opportunities to reteach the same proposition core
