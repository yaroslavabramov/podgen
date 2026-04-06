## Objective

Reduce act-to-act structural sameness in `phase5_planning` without introducing domain-specific or language-specific heuristics, and without changing `showrunner` as the first step.


## Constraints

- Do not infer act form from keywords in `act_title`, `purpose`, `internal_arc`, or other language-specific text patterns.
- Do not add domain-specific routing such as "historical acts must do X" or "mechanism acts must do Y".
- Do not change `showrunner` first.
- Use only generic structural signals already available inside `act_planner`.


## Phase 1: Fix This In `act_planner`

### 1. Add generic prior-act structure context to the prompt

Add a new helper that derives a compact structural signature from already planned acts in `acts[:i-1]`.

Suggested helper:

- `_act_structure_signature(beat_sheet: list[dict[str, Any]]) -> dict[str, Any]`

Suggested fields:

- `scene_type_sequence`
- `scene_job_sequence`
- `scene_claim_mode_sequence` (`primary`, `mixed`, `callback`)
- `opening_type`
- `opening_job`
- `ending_type`
- `ending_job`
- `scene_count`
- `explanation_ratio_bucket`
- `case_study_ratio_bucket`
- `callback_density_bucket`
- `dominant_middle_type`
- `dominant_middle_job`

Add these prior signatures to the act-planner prompt payload so the model can see what shapes were already used.

Touchpoints:

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py:224`
- new helper near the existing prior-act collection helpers


### 2. Add a generic anti-repetition prompt constraint

Extend the act-planner prompt contract so that each new act must differ from recent prior acts along structural dimensions, not topic keywords.

Prompt change should say, in generic terms:

- do not reuse the same opening move as the immediately previous act
- do not reuse the same dominant middle pattern if the previous act already used it
- do not reuse the same ending function if a different ending move is available
- vary the act by structure, not by paraphrasing the same educational cadence

This must be phrased using the structural signatures from step 1, not using manual act labels.

Touchpoint:

- `ACT_PLANNER_SYSTEM` in `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`


### 3. Add deterministic post-generation diversity validation

After beat-sheet normalization and scene-job assignment, compute the candidate act signature and compare it against prior-act signatures.

Add a helper such as:

- `_collect_structural_diversity_issues(candidate_beat_sheet, prior_act_signatures) -> list[dict[str, Any]]`

Use only generic comparisons. Suggested dimensions:

- same opening type/job
- same ending type/job
- same dominant middle type/job
- same explanation-heavy vs not explanation-heavy bucket
- same scene-type cadence family

Suggested structural fail rule:

- hard fail when the candidate matches the immediately previous act on 3 or more major dimensions
- hard fail when the candidate has an effectively identical type/job cadence to the previous act
- advisory only when it is somewhat similar but still differs on at least 2 major dimensions

This should return structural quality issues so the existing retry loop can handle them.

Touchpoints:

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py:322`
- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py:1143`


### 4. Add generic retry feedback for structural sameness

When the new diversity gate fails, retry feedback must tell the model to change structure generically:

- change the opening move
- change the dominant middle move
- change the ending function
- reduce similarity to the prior act's cadence

Do not mention domain labels such as historical/mechanism/final act.

Touchpoint:

- extend `_format_structural_retry_detail(...)` in `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`


### 5. Add debug observability for structure similarity

Persist the following into act-planner debug dumps:

- candidate act signature
- compared prior-act signatures
- similarity diagnostics
- diversity rejection reasons

This is needed so future reviews can say whether retries improved structure or only consumed tokens.

Touchpoint:

- `_dump_act_planner_debug(...)` in `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py`


## Adjacent Cleanup From The Same Run

### 6. Fix the prior-act short-ID leak in the prompt

Current behavior is inconsistent:

- current-act claims are shortened to `cN`
- `prior_acts_primary_claims` remain full UUIDs because `claim_sid` is built only from current-act claim IDs

That creates misleading warnings like:

- `dropped unknown claim_id '1407d544-aaee-4c15-b9b5-8b1fc00993fb'`

The model did not invent that ID out of nowhere; it was present in `prior_acts_primary_claims` inside the prompt payload.

Required change:

- either shorten prior-act IDs with a separate prompt-local map
- or remove prior-act IDs from the prompt entirely and keep only their statements plus act number

The second option is safer if prior-act claims are meant to provide context rather than directly reusable IDs.

Touchpoints:

- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py:221`
- `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase5_planning/act_planner.py:247`


## Tests To Add

### 1. Structural signature extraction

Add tests that verify stable signature extraction from normalized beat sheets.

Suggested coverage:

- opening/ending extraction
- dominant middle extraction
- explanation-ratio bucketing
- callback-density bucketing


### 2. Diversity gate rejection

Add tests that reject:

- two adjacent acts with the same opening type/job, same ending type/job, and same explanation-heavy middle
- near-identical type/job sequences


### 3. Diversity gate acceptance

Add tests that allow:

- similar topics with different structural signatures
- same opening type but different middle and ending behavior
- callback-heavy endings when the rest of the act shape is sufficiently different


### 4. Prior-act prompt hygiene

Add a test proving that `prior_acts_primary_claims` no longer leak full UUIDs into the LLM-facing prompt when current-act claims are shortened.


## Exit Criteria

Treat this phase as successful only if all of the following become true across reruns:

- adjacent acts do not repeatedly collapse into the same `hook -> explanation-heavy middle -> takeaway` cadence
- retries for structural sameness are explainable from debug dumps
- `unknown claim_id` warnings are no longer caused by prior-act UUID leakage
- the planner still passes existing structural and grounding checks


## Only Then Consider `showrunner`

Only escalate to `showrunner` if, after the generic `act_planner` diversity gate is in place, multiple reruns still converge to near-identical act signatures.

If that happens, the upstream change should also stay generic:

- pass structural intent or structural avoidance metadata
- do not add domain-specific or language-specific act routing

