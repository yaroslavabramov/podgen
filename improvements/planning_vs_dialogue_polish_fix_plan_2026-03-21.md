# Planning vs Dialogue Polish Fix Plan

## Why this document exists

The current run quality issues are no longer one big undifferentiated failure.
They split into two different problem classes:

1. `planning/content` problems
2. `surface dialogue/style` problems

These should not be fixed in the same place.

In particular:

- semantic overlap between adjacent scenes is a `planner/screenwriter handoff` problem
- repetitive markers like repeated explicit confirmations, interruption shapes, and rigid turn rhythm are a `late dialogue polish` problem

This document records the recommended fixes without adding new deterministic dedupe gates or case-specific word blacklists.

## Current diagnosis

### A. Planning/content problems

These belong in `showrunner`, `act_planner`, or `screenwriter`.

Examples from the latest run:

- `Act 2` still keeps too many adjacent mechanism-heavy beats in one family
- one scene establishes a procedural chain, and the next scene partly walks the same chain again under a different label
- some scene-to-scene transitions still restate the previous punchline instead of advancing it

This is not primarily a surface-style issue.

### B. Surface dialogue/style problems

These should not be fixed by banning specific words.

Examples:

- too many explicit confirmation moves
- too many interruption-shaped questions
- overly rigid turn alternation
- repeated scene opening shapes

These are visible only after full dialogue is written.
They are best handled by a dedicated late rewrite pass.

## What to fix in planning

### 1. Keep `showrunner` mostly where it is

`showrunner` has improved substantially.
Recent prompt work already fixed major cross-act problems like:

- one event split across multiple acts
- weak dramatic home for 1983
- branch coherence overriding semantic novelty

So `showrunner` is no longer the main bottleneck.

Do not add new deterministic semantic dedupe here for now.

### 1.5 Add soft density guidance to `showrunner`

File:
- [backend/app/pipeline/phase5_planning/showrunner.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/showrunner.py)

The latest issue is no longer major cross-act event duplication.
It is that one act can receive too many near-adjacent claims from one mechanism family.

Prompt guidance to add:

- If one act receives multiple claims from different branches that all describe the same mechanism family, prefer a lean explanatory backbone rather than assigning every near-paraphrase claim equal narrative weight.
- One act may carry the mechanism family, but it should not become overcrowded with multiple near-duplicate explanatory claims unless each one clearly enables a different scene move.
- Listener novelty is more important than exhaustive inclusion of every claim in the act-level backbone.

This is still prompt guidance, not deterministic dedupe.

### 2. Tighten adjacent-scene differentiation in `act_planner`

File:
- [backend/app/pipeline/phase5_planning/act_planner.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py)

Goal:
- prevent adjacent scenes from re-walking the same procedural chain

Prompt changes to add:

- If scene `N` already established a procedural or causal chain, scene `N+1` must not walk through the same chain again under a different label.
- Adjacent scenes must do different listener work.
- If one scene explains the mechanism, the next scene should do one of:
  - scale it
  - stress-test it
  - show consequence
  - show tradeoff
  - shift abstraction level
  - contrast it with another system
- `synthesize` scenes may callback to prior mechanism in at most one compact sentence before moving to implication.
- `apply` and `stress_test` scenes should start from the new condition or pressure, not from restating the mechanism itself.

This is the main fix for cases like:

- `A2S3 -> A2S4`
- `A2S4 -> A2S6`

### 3. Make scene-job semantics more operational

Still in:
- [backend/app/pipeline/phase5_planning/act_planner.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py)

The problem is not just overlap.
It is also that some scene jobs are too abstract for the model to honor consistently.

Prompt clarifications:

- `apply`: use the established mechanism in a concrete operating context
- `stress_test`: place the mechanism under pressure
- `cost`: foreground tradeoff, sacrifice, contradiction, or price paid
- `synthesize`: answer the act question or unify prior material without reteaching it
- `reframe`: change the interpretive lens, not the factual base
- `contrast`: compare with one dominant outside comparator, not a bag of examples

This should reduce planner outputs where the scene job is technically correct but still produces recap-shaped descriptions.

## What to fix in `screenwriter`

### 4. Add stronger anti-echo scene-transition rules

File:
- [backend/app/pipeline/phase6_writing/screenwriter.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py)

Goal:
- stop the next scene from opening by repeating the proposition or punchline that just closed the previous scene

Prompt changes to add:

- The first 1-2 lines of a new scene should advance, challenge, narrow, or reframe what was just established.
- Do not open a scene by paraphrasing the previous scene's closing punchline unless the beat explicitly requires a callback.
- If `previous_scene.last_line` already states the key conclusion, the new scene must move to consequence, pressure, exception, counterexample, or scale.
- `cost` scenes should open with the price or contradiction, not with a restatement of the event.
- `callback` and `mixed` scenes may use at most one compact callback line before the new move.

This targets issues like:

- `A3S3 -> A3S4`
- `A4S5 -> A4S6`

### 5. Add abstract style rules to `screenwriter`, but not word blacklists

Still in:
- [backend/app/pipeline/phase6_writing/screenwriter.py](/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py)

Useful prompt rules:

- vary agreement and confirmation shapes
- not every challenge needs an interruption-style opener
- do not default every scene to perfect `ABAB` alternation
- allow one speaker to hold the floor briefly when explanation or reaction naturally continues
- vary scene opening function across adjacent scenes

Do not add:

- banned-word lists
- hardcoded discouragement of specific discourse markers
- corpus-specific style heuristics

These style rules should be tried before introducing a new pipeline stage.

## Late dialogue-polish stage as fallback

### 6. Introduce a new post-screenwriter stage only if prompt-only fixes are insufficient

Recommended fallback placement in pipeline:

1. `write_screenplay`
2. `dialogue_polish`
3. `audit_citations`
4. audio / shownotes

This stage should operate on already written dialogue, not on beat cards.

Why:

- surface repetition is only fully visible after dialogue exists
- style cleanup should not force `act_planner` to solve conversational rhythm
- this keeps responsibilities clean

### 7. What `dialogue_polish` should do if introduced

Goal:
- improve conversational naturalness without changing factual content or scene intent

Inputs per scene:

- current `dialogue_json`
- `scene_job`
- `scene_claim_ids`
- prior polished scene in same act
- recent scene starts
- previous scene last line

Required invariants:

- no new facts
- no removal of required claims
- no loss of attribution
- no change to scene purpose
- no collapse of chronology

Allowed rewrites:

- vary agreement forms
- vary question/challenge forms
- merge or split lines for better rhythm
- break rigid `ABAB` alternation when natural
- reduce opener reuse
- reduce local proposition echo

### 8. How to instruct the polisher

Prompt focus should be functional, not lexical.

Good instruction types:

- vary discourse moves
- avoid repeating the same agreement shape
- avoid repeating the same interruption shape
- avoid opening a scene with the same move used very recently
- prefer natural continuity over explicit recap
- preserve meaning, evidence, and scene job

Bad instruction types:

- do not say word X
- do not use phrase Y
- blacklist specific markers

### 9. How to decide which scenes to polish

Two acceptable options:

#### Option A: polish everything once

Pros:
- simpler pipeline
- no separate style evaluator needed

Cons:
- more cost and latency

#### Option B: add a lightweight style-eval step first

Possible evaluator labels:

- `rigid_turn_taking`
- `repetitive_agreement_shape`
- `repetitive_challenge_shape`
- `scene_opening_echo`
- `teacherly_exposition`

Pros:
- cheaper than polishing all scenes

Cons:
- one more model step

This should remain a fallback, not the first response.

## Recommended rollout order

### P0

- strengthen adjacent-scene differentiation in `act_planner`
- strengthen anti-echo transition rules in `screenwriter`
- add abstract style-variation rules to `screenwriter`

These are the cheapest and most direct fixes.

### P1

- add soft density guidance to `showrunner` for mechanism-family claim overload inside one act

Use this if `Act 2`-style overpacking persists after `act_planner` prompt tightening.

### P2

- introduce `dialogue_polish` after `write_screenplay`
- optionally add a style-eval gate before it

Only if prompt-only fixes do not reduce the style artifacts enough.

## What not to do

- do not add deterministic semantic dedupe gates right now
- do not add case-specific vocab lists
- do not add banned-word blacklists for style
- do not ask `act_planner` to solve final dialogue rhythm
- do not expect `writer_revolt` in its current form to act as a real polish stage
- do not add a new late polish stage before trying prompt-only fixes in `screenwriter`

## Decision summary

Use:

- `act_planner` for adjacent-scene meaning separation
- `screenwriter` for anti-echo scene continuation and abstract style variation
- `dialogue_polish` only as a fallback if prompt-only fixes plateau

Do not use:

- word blacklists
- corpus-specific style dictionaries
- new deterministic dedupe machinery as the first response
