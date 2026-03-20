# Screenwriter Semantic Repetition Fix List

## Goal

Reduce meaning-level repetition in the writing stack without doing a full redesign of the pipeline.

This document focuses on the current bottleneck in `phase6_writing/screenwriter.py`: the writer now runs correctly on rerun, but still turns overlapping planning material into repeated exposition.

The main problem is not just scene form. It is repeated proposition cores:

- Act 1 Scene 1 and Scene 2 both restate the "no unified currency / free port / monetary chaos" setup.
- Act 2 Scene 4, Act 2 Scene 6, and Act 3 Scene 3 all re-explain the certificate / USD reserve / `7.80` mechanism.
- Act 4 Scene 2 and Act 4 Scene 6 partially repeat the same constitutional trust argument.

## Current Diagnosis

### 1. Cross-act memory is missing

In `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py`, `covered_claim_ids_in_act` resets at the start of each act.

Effect:

- the writer can avoid repeating exact claims inside one act
- but the writer does not know what previous acts already fully explained
- repeated mechanisms are reintroduced as if they were new

This is currently the highest-leverage fix.

### 2. The writer tracks covered claim IDs, not covered meanings

`already_covered_claims` helps only when the same claim id returns later.

It does **not** help when:

- scene A explains claim `c1`
- scene B explains claim `c2`
- `c1` and `c2` are different claims with the same proposition core

That is why semantic repetition survives even after the local-memory fix.

### 3. The host dynamic is structurally rigid

The current pattern is almost always:

- A opens
- B asks/reacts
- A explains
- B asks again
- A lands

Measured on the current run:

- A opens 21/24 scenes
- A closes 23/24 scenes
- all 24 scenes use strict alternation (`ABABA` / `ABABABA`)

This makes repetition more audible because every scene delivers repeated meaning through the same delivery pattern.

### 4. Writer prompt still treats evidence too passively

The writer receives `claims` and `evidence`, but the prompt mostly says:

- use evidence organically
- do not dump raw quotes

That is not enough. The model usually:

- extracts facts from evidence
- but does not sound attributed
- and rarely uses source-aware phrasing

The result is acceptable factual grounding, but weak "why should I believe this" texture.

### 5. Some repetition is upstream and cannot be fully fixed in writer

There are beat-sheet overlaps that are already too close before writing:

- Act 1 Scene 1 vs Scene 2
- Act 2 Scene 4 vs Scene 6
- Act 3 Scene 2 vs Scene 3 vs Scene 6

The writer can reduce some of that with callbacks, but cannot fully solve it if two scene briefs ask for the same idea with slightly different wording.

## Priority Fixes

## P0: Add Cross-Act Covered Memory

### Change

Track covered claims across the whole screenplay pass, not only inside one act.

### Implementation

In `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py`:

- keep current per-act memory for local scene flow
- add screenplay-level memory:
  - `covered_claim_ids_global`
  - `covered_claim_ids_global_set`
- pass a second payload into the prompt:
  - `already_covered_claims_global`

### Prompt behavior

The system prompt should say:

- if a current scene overlaps with globally covered material, do not teach it from scratch again
- refer back briefly and move to the new implication, new angle, or new consequence

### Expected effect

- Act 3 stops re-teaching what Act 2 already fully explained
- repeated mechanism scenes become callbacks + extension instead of full resets

## P0.5: Add "What Is New Here" to the Scene Prompt

### Change

Do not ask the writer to infer novelty only from `description`.

Explicitly include a short field in the prompt payload:

- `new_information_goal`

This should be a single sentence generated from the scene brief and covered-memory comparison.

### Purpose

The writer currently sees:

- scene description
- prior covered claims

but still has to infer the delta itself.

This field should make the task explicit:

- "what is new in this scene relative to what the listener already heard?"

### Expected effect

- less semantic repetition
- more additive progression scene to scene

## P1: Relax Host Role Rigidity

### Change

We should stop enforcing a near-static `A explains / B challenges` pattern.

### Prompt update

In `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py`, change the host instructions from fixed role descriptions into looser tendencies.

Instead of:

- `Host A = Guide`
- `Host B = Challenger`

prefer something like:

- both hosts may question, infer, summarize, or reframe
- do not force every scene into explainer/questioner rhythm
- allow one host to carry a mini-story while the other only lands one reaction
- allow occasional `AA` or `BB` adjacency when it improves flow

### Also add explicit anti-pattern

Add a rule such as:

- avoid the default dialogue skeleton of `A setup -> B question -> A explanation -> B prompt -> A landing`

### Expected effect

- repeated content becomes less audible
- scenes stop feeling generated from one dialogue mold

## P1: Add Lexical Anti-Repetition Rules

### Change

Do not rely only on `forbidden_phrases` from the show bible.

Add prompt-level anti-repetition guidance for discourse markers.

### Why

The current run overuses:

- `именно`
- `подожди`
- `то есть`

These are not the core problem, but they amplify the perception of sameness.

### Recommended rule

Add to the system prompt:

- avoid reusing the same reaction opener or confirmation word across many scenes
- do not default to `подожди` for Host B or `именно` for Host A

Do **not** solve this only by hard-banning single words. That would just push the model into new canned fillers.

## P1: Teach Writer to Use Attributed Evidence Language

### Change

Add explicit source-aware phrasing guidance.

### Problem

The writer uses evidence as fact material, but rarely sounds attributed.

### Prompt update

Add guidance like:

- when a scene uses a precise institutional fact, date, legal provision, or figure, occasionally anchor it with light attribution
- examples:
  - `как формулирует HKMA`
  - `как прямо сказано в Основном законе`
  - `по данным Exchange Fund`

### Constraint

This should remain light. The goal is not academic citation in dialogue.

### Expected effect

- better epistemic texture
- less generic explainer tone

## P1: Carry Previous Act Summary Into First Scene of Each New Act

### Change

For `scene_number == 1`, the writer should receive:

- a short `previous_act_summary`
- optionally `previous_act_final_claims`

### Why

Right now first scenes of new acts start with empty covered memory.

That encourages:

- re-teaching the same mechanism
- re-opening already answered questions

### Expected effect

- Act 3 Scene 1/2 can build on Act 2
- Act 4 Scene 1/2 can build on Act 3 without re-establishing too much

## P2: Add Proposition-Core Memory, Not Only Claim-ID Memory

### Change

Introduce a compact semantic memory layer for the writer.

Possible forms:

- `covered_proposition_cores`
- `already_explained_points`
- `do_not_reteach_points`

### How to populate it

This does not need a new model call immediately.

A practical first version can be heuristic/manual:

- build short normalized summaries for previously written scenes
- or map grouped repeated claims from planning/integration into shared proposition labels

Examples:

- `hong_kong_had_no_single_currency_in_1840s`
- `private_note_issue_requires_usd_backing_at_7_80`
- `peg_trades_monetary_autonomy_for_stability`

### Why

Claim-ID memory only catches exact repeated claim usage.
Meaning-level repetition needs a higher-level memory object.

## P2: Use Integrator Findings Selectively for Writer-Relevant Scenes

### Change

Do not pass raw `issues_found` wholesale into the writer.

Instead, derive scene-level rewrite hints only for writer-actionable items:

- repeated explanation
- recap overload
- over-dense mechanism scene

### Example

Instead of:

- `duplicate_claim between Act 2 and Act 3`

pass:

- `Scene note: do not re-explain the CDI mechanism here; assume the listener already knows it and focus on reserve scale`

### Why

This is the only way integrator diagnostics become useful for writing without turning the writer into a replanner.

## Upstream Fixes Needed Later

These are not first writer fixes, but they should stay visible:

### 1. Beat-sheet overlap cleanup

Especially:

- Act 1 Scene 1 vs Scene 2
- Act 2 Scene 4 vs Scene 6
- Act 3 Scene 2 vs Scene 3 vs Scene 6

This is likely an `act_planner` follow-up, not a `screenwriter` fix.

### 2. Claim clustering by proposition core

The root cause is still that one mechanism can survive as multiple neighboring claims with different wording and branch provenance.

## Recommended Implementation Order

1. Add screenplay-level covered memory.
2. Add `previous_act_summary` for first scene of each act.
3. Add prompt rules for:
   - callback instead of re-teach
   - looser host dynamics
   - lexical anti-repetition
   - light evidence attribution
4. Re-run writer and inspect:
   - Act 1 Scene 1/2 overlap
   - Act 2 Scene 4/6 overlap
   - Act 3 Scene 2/3/6 overlap
5. Only then decide whether proposition-core memory is needed immediately or can wait.

## Non-Goals

These should **not** be the first fix:

- banning a long list of words one by one
- raising temperature and hoping variety appears
- adding tool use to writer
- redesigning the whole planning pipeline before validating simpler writer-memory fixes

## Success Criteria For The Next Writer Run

The next writer run should be considered meaningfully better only if:

- Act 1 Scene 2 no longer replays the same opening image from Scene 1
- Act 2 Scene 6 refers back to the mechanism from Scene 4 instead of teaching it again
- Act 3 Scene 3 adds only what is new relative to Act 2
- first scenes of later acts clearly build on prior acts instead of resetting
- `A/B` rhythm is no longer strict `ABABA` in every scene
- `именно` / `подожди` repetition drops materially
