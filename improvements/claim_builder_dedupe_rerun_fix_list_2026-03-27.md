# Claim Builder + Dedupe Follow-up Fix List (2026-03-27)

## Why this file exists

Latest targeted rerun of `claim_builder -> dedupe` exposed a deeper contract problem:

- `claim_builder` is not behaving like a fresh rebuild of the current claim pool
- `dedupe` is compensating for builder overlap and sometimes overreaches into editorial decisions
- debug counters are currently confusing enough that they obscure the real failure mode

This file isolates the fixes needed for **claims pipeline correctness**, separate from unrelated Phase 5/6 work.

---

## Main Problems

### P0. Targeted rebuild is preserving stale/orphan claims from older runs

Current behavior in `build_claims`:

- targeted rerun deletes only `impacted_claim_ids`
- everything else in incoming `state["claim_ids"]` is carried forward as `preserved_claim_ids`
- this includes claims from branches that were not rebuilt in the current run
- this can also include claims that no longer belong to the current active question tree / current evidence scope

Observed symptom:

- latest `dedupe` node started with `221` input IDs in state
- but only `90` live claim rows were actually present in debug summary
- `b6` claims appeared in dedupe even though the current claim-builder rerun rebuilt only `b1-b5`

This is the biggest correctness problem in the current slice.

### P0. Claim-pool state hygiene is broken

`dedupe` currently reports `input_count = len(state["claim_ids"])`, which can be much larger than the number of real claim rows that still exist.

This means:

- stale IDs survive across iterations
- debug output is misleading
- downstream nodes are forced to reason over historical state artifacts rather than the current live pool

### P1. Builder input is not branch-local enough

`_summarize_branch_context(...)` currently unions all `lineage.question_contexts` found on evidence in the batch.

In practice this means:

- `b1` builder batches can see question contexts from `b4` and `b6`
- `b4` can see `b1` and `b2`
- `b5` can see `b3`

This is useful provenance information, but it also encourages the model to restate the same mechanism family under slightly different branch framings.

### P1. Builder contract over-rewards compression

Current prompt strongly biases toward:

- fewer, stronger claims
- merge whenever spans support the same proposition core
- treat dates/numbers/actors/consequences as supporting detail unless they materially change meaning

This is useful up to a point, but it currently pushes the model toward **mini-syntheses**:

- setup + institutional response collapsed into one claim
- regime transition collapsed into one claim
- precondition + consequence collapsed into one claim

These are too large as planning primitives.

### P1. Semantic dedupe is collapsing some distinct narrative moves

Some current semantic merges are good.

Some are not duplicates and should survive as separate claims:

- precondition -> remedy
- floating period -> 1983 peg
- chaos -> standardization

The dedupe layer should remove duplicate propositions, not compress historical arcs.

### P2. Some true duplicate families are still surviving across branches

Current kept pool still contains recurring families like:

- `1935 legal tender / three authorized banks`
- `three banks + HKMA authorization / supervision`

So dedupe currently does both of these at once:

- merges some things that are actually distinct
- fails to merge some things that are still true repeats

### P2. Debug output is not explicit enough about pool shape

Current dedupe debug summary does not clearly separate:

- IDs passed in state
- IDs successfully loaded from DB
- claims actually considered after stale-ID filtering
- claims dropped lexically vs semantically

This makes postmortems harder than they should be.

---

## Fixes

## P0. Fix rerun semantics for claim builder

### Goal

A claim-builder rerun should not silently reattach stale claims from older state unless they are still valid for the current scope.

### Required changes

1. Add an explicit rerun mode distinction:
   - `full_rebuild`
   - `targeted_rebuild_with_revalidation`

2. In targeted rebuild, do **not** preserve old claims by default just because their `builder_branch_id` is outside `targeted_branch_ids`.

3. Before carrying any claim into `preserved_claim_ids`, revalidate it against current scope:
   - still exists in DB
   - still belongs to an active branch in the current tree
   - still has live evidence links
   - still belongs to the current run's effective episode question map / branch set

4. If current active branch set differs materially from the prior pool, fall back to `full_rebuild`.

### Desired outcome

- no orphan claims from old branch layouts
- no stale branch material silently flowing into dedupe
- rerun semantics become understandable again

---

## P0. Sanitize claim pool before dedupe

### Goal

`dedupe` should operate on a clean live pool, not on historical `state["claim_ids"]` sludge.

### Required changes

1. Before dedupe logic starts, resolve incoming IDs against DB and produce:
   - `requested_claim_id_count`
   - `loaded_claim_row_count`
   - `missing_claim_id_count`

2. Run dedupe only on live loaded claim rows.

3. Return sanitized current claim IDs downstream instead of preserving stale IDs from state.

### Desired outcome

- state no longer accumulates dead claim IDs
- debug counts become interpretable
- `input_count` no longer overstates the real pool

---

## P1. Make builder context branch-aware without hiding provenance

### Goal

Preserve provenance signal without encouraging cross-branch restatement.

### Required changes

1. In builder payload, separate:
   - `primary_branch_context`
   - `secondary_question_contexts`

2. The prompt should treat secondary contexts as provenance only, not as permission to restate those branches' explanatory backbone.

3. Prefer branch-local direct questions in the main branch summary.

4. Optionally cap foreign-context exposure:
   - if a batch belongs to `b1`, do not dump a full union of `b4/b6` question contexts into the same main context block

### Desired outcome

- less branch bleed
- less repeated mechanism phrasing across branches
- provenance remains available without becoming the dominant framing

---

## P1. Retune builder contract from "maximum compression" to "one downstream move per claim"

### Goal

Avoid both extremes:

- micro-claim sludge
- mini-scene claims

### Required changes

1. Keep the anti-sludge rules:
   - do not create one claim per evidence span
   - merge true paraphrases
   - keep writer-usable propositions

2. Add explicit split rules:
   - do not merge setup and response
   - do not merge regime A and regime B transition into one claim unless the transition itself is the proposition
   - do not merge precondition and later institutional consequence when they would create different scene moves downstream
   - do not merge chronology into one claim if planner would naturally place those beats in separate scenes

3. Rephrase prompt target from:
   - `smallest high-value set`
   to:
   - `smallest set that still preserves distinct downstream narrative moves`

### Desired outcome

- claims remain compact
- but become cleaner planning atoms

---

## P1. Tighten semantic dedupe so it removes duplicates, not historical structure

### Goal

Stop dedupe from erasing distinct beats while still catching true repeats.

### Required changes

1. Add stricter non-duplicate guidance for:
   - precondition vs institutional fix
   - floating regime vs later peg regime
   - legal rule vs later application/result
   - problem statement vs remedy

2. Prefer "keep both" when two claims belong to different chronological stages, even if topic overlap is high.

3. Add regressions using current bad merge families from this run.

### Desired outcome

- dedupe becomes less destructive
- builder no longer needs to be protected by later planning hacks

---

## P2. Strengthen dedupe on persistent cross-branch repeat families

### Goal

Still remove true duplicate mechanism families that currently survive.

### Required changes

1. Add focused regression coverage for recurring survivors:
   - `1935 legal tender / three authorized banks`
   - `three commercial banks + HKMA authorization / oversight`

2. Improve semantic duplicate prompt/examples so these are treated as one proposition family when they are genuine restatements.

3. If needed, add a deterministic pre-clustering hint by normalized proposition signature, but avoid language-specific phrase lists.

### Desired outcome

- less repeated backbone entering planning
- no dependence on planner to clean obvious claim-family repeats

---

## P2. Improve debug clarity

### Goal

Make postmortems understandable without reverse-engineering counters.

### Required changes

Add explicit fields to dedupe debug summary:

- `requested_claim_id_count`
- `loaded_claim_row_count`
- `missing_claim_id_count`
- `preserved_claim_id_count`
- `rebuilt_claim_id_count`
- `live_branch_ids`

Also add explicit builder rerun summary fields:

- `targeted_branch_ids`
- `impacted_claim_count`
- `preserved_claim_count`
- `dropped_stale_preserved_claim_count`

### Desired outcome

- future analysis stops confusing state size with live pool size
- rerun behavior becomes auditable

---

## Recommended order

1. `P0` rerun semantics: revalidate or drop preserved claims
2. `P0` sanitize claim pool before dedupe
3. `P1` builder context separation: primary vs secondary branch context
4. `P1` builder prompt retune: preserve distinct downstream moves
5. `P1` semantic dedupe non-duplicate guardrails
6. `P2` strengthen remaining true duplicate family removal
7. `P2` debug clarity improvements

---

## Success criteria

- rerunning `claim_builder` no longer drags stale claims from irrelevant old branches
- `dedupe` input counts reflect live claims, not dead historical IDs
- builder claims are still compact, but fewer of them already contain multi-beat scene structure
- semantic dedupe stops merging setup -> response / regime A -> regime B pairs
- persistent mechanism repeats across branches materially decrease before planning
