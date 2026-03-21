# Screenwriter Scene Job And Prior Primary Claims Plan

## Goal

Remove the remaining high-signal semantic repetition without introducing corpus-specific rules, keyword-policing, or extra LLM passes.

The current state is:

- callback continuity is better
- `previous_scene.last_line` continuity works
- literal within-act duplicate primary claims are already handled

But two important problems remain:

1. `screenwriter` still sometimes turns callback-heavy ending scenes into mini-explanations because it knows what not to do, but not what positive move to make instead.
2. `act_planner` still has no explicit cross-act memory of prior primary explanations, so different claim IDs can still restate the same proposition core in later acts.

This plan is for the general pipeline, not for one episode or one corpus.

## Current Diagnosis

### What is already working

- `primary_claim_ids` / `callback_claim_ids` split inside acts
- `scene_claim_mode` reaches `screenwriter`
- `previous_scene` now comes from actual written dialogue when available
- `previous_scene.last_line` can bridge scene openings
- callback-only endings can exist without losing evidence attachment

### What still fails

1. Callback scenes still rely on a negative-only rule:
   - do not reteach
   - but no explicit positive scene job is provided

2. Later acts can still get new primary claims that overlap with prior-act primary material:
   - different claim IDs
   - same proposition core

3. Form is still too templated:
   - strict `ABAB`
   - repeated scene openers
   - repeated answer markers like `Именно`

These form issues matter, but they should be fixed with cheap deterministic continuity inputs, not with a large style subsystem.

## Design Principles

- No corpus-specific dictionaries
- No keyword validators for “bad explanation prose”
- No extra LLM call for summaries
- Prefer deterministic continuity extraction from written dialogue
- Prefer explicit positive scene contracts over broad anti-pattern wording
- Keep new fields compact and auditable in debug dumps

## Phase 1: Add `scene_job` For Callback And Mixed Scenes

### Files

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`
- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py`
- `/Users/yaroslavabramov/side/podgen/backend/tests/test_act_planner_prompt.py`
- `/Users/yaroslavabramov/side/podgen/backend/tests/test_screenwriter_prompt.py`

### New field

Add a compact scene-level field:

- `scene_job`

For now, require it at least for:

- `callback` scenes
- `mixed` scenes

Possible enum values:

- `reframe`
- `cost`
- `contrast`
- `bridge`
- `synthesize`
- `apply`
- `stress_test`

Do not add a free-text paragraph here. It must stay enumerable and compact.

If `scene_job` is later expanded to cover `primary` scenes as well, add:

- `introduce`

### Why

`scene_claim_mode` only says whether the material is new or old.

It does **not** say what kind of move the scene should make.

That is the missing contract in cases like callback-only takeaways that still drift into explanation.

### Writer prompt behavior

`screenwriter` should be told:

- `scene_claim_mode=callback` means prior mechanism is already established
- `scene_job` tells you what to do instead of reteaching

Examples:

- `scene_job=reframe`
  - reinterpret what the listener already knows
- `scene_job=cost`
  - show what price the system pays
- `scene_job=bridge`
  - pivot to the next scene or act
- `scene_job=synthesize`
  - compress the changed understanding without rebuilding the mechanism

### Important scope boundary

Do **not** add a second high-level field like `information_role` yet.

`scene_job` is enough for the next step.

## Phase 2: Cheap Style Continuity Fixes In Screenwriter

### Files

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py`
- `/Users/yaroslavabramov/side/podgen/backend/tests/test_screenwriter_prompt.py`

### Changes

Add a small deterministic continuity payload:

- `recent_scene_starts`
  - last few scene-opening phrases in the current act
- optionally `recent_reply_markers`
  - last few repeated short reply-openers if needed later

### Prompt rule

Add a narrow rule:

- do not start the scene with an opener already used recently in this act if a natural alternative is available

This is meant to reduce:

- `Подожди`
- repeated `Именно`
- repetitive opener rhythm

### Why this is separate

This is not the main semantic fix.

It is a cheap, deterministic quality improvement that should run in parallel with `scene_job`, not instead of it.

## Phase 3: Strengthen Callback-Only Ending Rules In Act Planner

### Files

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`
- `/Users/yaroslavabramov/side/podgen/backend/tests/test_act_planner_prompt.py`

### Prompt change

For callback-only ending scenes:

- `takeaway`
- `summary`
- `transition`

require:

- `scene_job` must be one of:
  - `reframe`
  - `cost`
  - `contrast`
  - `bridge`
  - `synthesize`

and forbid:

- `introduce`
- `explain`

### Structural constraints

Use language-agnostic constraints only:

- callback-only ending scenes should have at most `1-2` callback claims
- callback-only ending descriptions should stay compact
- callback-only ending scenes should not be oversized recap blocks

Do **not** implement:

- keyword detectors
- phrase matching like “how it works”
- corpus-tuned lexicons

### Why

This addresses the planner side of the same problem:

- writer should not receive a callback scene whose brief is already shaped like a recap explanation

## Phase 4: Add `prior_acts_primary_claims` To Act Planner

### Files

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`
- `/Users/yaroslavabramov/side/podgen/backend/tests/test_act_planner_prompt.py`

### New input

Before planning act `N`, pass:

- `prior_acts_primary_claims`

Each item should contain only:

- `id`
- `statement`
- `act_number`

No anchor bag. No additional summary object.

### Prompt rule

Tell `act_planner`:

- these points were already established as primary material in earlier acts
- do not assign materially overlapping points as new primary explanations unless the act adds a genuinely new layer
- if prior material must recur, prefer callback framing

### Important limitation

Do **not** build an automated semantic overlap gate yet.

First give the planner the missing information.

If this is not enough, then revisit a gate later.

## Phase 5: Improve Previous-Scene Continuity Quality

### Files

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase6_writing/screenwriter.py`
- `/Users/yaroslavabramov/side/podgen/backend/tests/test_screenwriter_prompt.py`

### Changes

Small continuity cleanup:

- raise `statement_chars` for previous-scene continuity from the current compressed setting to `200`
- continue joining `what_listener_now_knows` with `; `
- keep `claim_ids` compact and limited to those actually represented in the continuity extract

### Why

This improves callback-scene context without introducing another prompt channel.

## What Not To Add

Do not add:

- corpus-specific term lists
- keyword validators for mechanism prose
- a separate `do_not_repeat` blob that duplicates existing continuity context
- an extra LLM summary step for previous scenes
- an automated semantic overlap gate before `prior_acts_primary_claims` is tried

## Implementation Order

### P0

1. Add `scene_job` for callback/mixed scenes in `act_planner`
2. Pass `scene_job` through to `screenwriter` and update prompt rules

### P1

3. Add `recent_scene_starts` to `screenwriter`
4. Strengthen callback-only ending constraints in `act_planner`

### P2

5. Add `prior_acts_primary_claims` to `act_planner`
6. Slightly widen previous-scene continuity extract

## Tests To Add

### Screenwriter

- callback scene payload includes:
  - `scene_claim_mode`
  - `callback_claim_ids`
  - `scene_job`
- callback-only scene prompt explicitly instructs reframing/bridge/synthesis rather than re-explanation
- `recent_scene_starts` payload is deterministic and capped
- repeated recent scene opener is available to the prompt as continuity context

### Act Planner

- callback-only ending scene gets valid `scene_job`
- callback-only ending scene with bad job or oversized description fails the quality gate
- later acts receive `prior_acts_primary_claims`
- planner payload uses prior primary claims, not a raw anchor bag, for cross-act memory

## Success Criteria

The next planning + writing run should show:

- callback-only endings landing via reframe/bridge/synthesis more consistently
- fewer re-explanations in ending scenes, especially where the scene is already marked callback
- fewer repeated scene openers inside the same act
- reduced cross-act reteaching when a later act revisits an already established mechanism
