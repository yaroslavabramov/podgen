# Planning and ending selection in V9

`editorial-role-projections/9` separates the supported answer established across the episode from its selected stopping point. The planner selects one central story, combines or removes duplicating branches, prepares premises a newcomer needs and preserves chronology without prescribing a fixed order of telling or an expressive device.

## Interface

The V9 planner and plan-revise wire uses `PlanningOutputV9`:

- `plan.working_answer`: supported episode-wide meaning, including uncertainty that changes it. This need not be spoken as a final explanation.
- `plan.ending.last_moment`: the last action, observation, discovery or thought the listener encounters; not prescribed spoken lines.
- `plan.ending.why_end_here`: why stopping there completes this particular promise without another recap.
- `plan.ending.promises_to_close`: selected promises; an empty list is valid.

The remaining plan fields, supplied references, local act IDs, speaker binding, research state and duration checks retain their contracts. New text fields use lossless `FullText`; no word-count gate or silent shortening is added. The plan editor separately checks the prepared opening, promise and resolution, novice premises, duplicate branches, action/knowledge chronology and the selected last moment. It addresses the exact saved candidate through the existing pointer catalog and bounded pointer repair.

V9 author handoff exposes `story.episode_answer` and `story.ending`, with `authority=revisable_authorial_decisions` and `suggested_priority`. The initial revision frame carries the same distinction. The writer retains control over wording and expression. For a legacy plan opened in a NEW V9 preview, `episode_answer` comes from its existing `working_answer` and `ending=null`: no final moment is invented from `ending.answer_to_deliver`. Replaying a pinned V8 request still produces its original `conclusion.meaning` and field order.

Once a rewrite has selected a current composition, it replaces the initial editorial frame. Initial `last_moment` is not copied back over a newer ending. The latest script and saved composition decisions remain the working state; the original plan remains archived. The two-revision regression covers this replacement explicitly without modifying the historical private revision schema.

## Version and snapshot boundaries

`planning_versions.planning_model()` selects the strict model for the pinned projection; legacy author inputs retain their original planning wire. `planning_snapshot()` selects the saved plan shape for preview, pointer recovery and planning continuation. It preserves complete values and rejects an incomplete or mixed ending shape rather than migrating it silently. Fixed-plan input accepts both shapes without downcasting; only current exported schemas are updated.

V1–V8 model-facing wires and existing plans are unchanged. Existing run manifests keep their prompt artifacts, schema and projection. Resume does not load current prompts into an old request. Current planning prompts are edited in place; no extra historical prompt copies or new role are introduced.

V9 source-excerpt selection and author material requests are separate integrations owned by their respective components. They share V9 capability selection; this planning change does not edit the dossier, reader or writer instructions.

## Validation

`test_planning_v9.py` covers strict schemas, long complete fields, historical/current snapshot roundtrips, fixed-plan import, final-moment addressing, selector repair, handoff and two successive compositions. `test_planning_v9_runtime.py` exercises ordinary CLI plan/review, pinned preview and resume for V8 and V9, stopping before writer generation. Historical wire tests use frozen V1–V8 schema expectations. Operator-pair tests distinguish a new current preview of an old plan from replay of a pinned historical request.

## Material-selection handoff (separate input pin)

New V9 runs pin `plan_selection_contract=plan-material-selection/2`. The planner expresses the chosen level of detail in the acts themselves, as the account the audience will follow. `duration_strategy.assumptions` holds timing calculations. Concrete extensions or cuts belong in `optional_material` and name the material itself, with catalog locators kept in reference fields.

Both `/1` and `/2` pass the existing `optional_material` values to the author and the initial revision frame as `material_options.choices`. The accompanying guidance explains when a change in duration warrants an extension or a cut. The field is omitted when there are no options.

Under `/2`, the draft writer can follow `story.opening.support_refs` and each act’s `support_refs` to a single shared list of evidence cards. Each card has a short `ref` such as `M3` and keeps its complete selected source material. The reviser receives `editorial_frame.accepted_plan_scenes` with the original opening and each act’s title, intended change in understanding and source references. This supplies the emphasis chosen for each scene even when the draft has moved it. Utterance ranges for edits still come from the reviewers and refer to the actual script. The cold reader and fact checker keep their independent inputs.

The input pin is independent of V9 output schemas. A saved manifest without it retains the previous payload, including in preview and continuation; resume never adds the pin to a saved run. Fresh previews use the current handoff. An unsupported pin or a pin attached to another projection is rejected. Existing reader, factual-review and revision output schemas do not change.

After a rewrite supplies its working composition, its current route and decisions govern the next revision. In `/2` the reviser can still consult the original acts in `accepted_plan_scenes` as source context. A run already pinned to `/1` reproduces its original draft and revision requests.

New addressed revisions also pin `revision_context_contract=script-edit-assignment/1`. They deliver the actual script, revision targets, editors’ instructions and positive observations, source support and composition. Draft-writing exercises and the cold reader’s general retelling are omitted from this request. The initial writer still gets the exercises. Replaying an earlier manifest restores its original context. [Provider token counts on two archived edits](../../evals/reports/2026-11-08-editorial-revision/handoff-check/compact-sequence-counts.json)

### Offline certificates and limits

`test_plan_selection.py` freezes the actual accepted Snow plan and author input from `tmp/snow-narrative-nov8/calls`. It verifies the three optional choices introduced with `/1`. Those choices include the optional 61/6/6 expansion. The source relationships introduced with `/2` can be checked independently against the stored plan and the complete catalog.

The archived first option contains the free-prose locator `M4`. It is preserved verbatim; no regex or guessed replacement migrates source prose. New planning instructions require naming the actual material in such decisions. This historical nuance is not evidence that a newly generated plan has followed the instruction.

A separate clock-comparison example verifies the ordinary draft/revise preview, separation of positive selection and optional extension from timing assumptions, and replacement by current composition. Tests also cover two successive materialized compositions, byte-identical cold readings, unchanged V9 preview when the pin is absent, and native V8/V9 manifest/preview/resume.

No model inference is part of these certificates. The minimum live follow-up is one ordinary plan-revise call on the same saved reporting and candidate with the new planning prompt. Check that its chosen detail is consistent across the acts and optional material, then inspect the resulting author input offline. A new full-episode generation is not needed to verify delivery; whether a model makes and follows a better editorial choice remains a separate live check.


## Editorial instruction freeze: 2026-11-08

This follow-up changes only the current `plan.md`, `plan-revise.md` and `plan-review.md` instructions. `why_end_here` explains why the movement of the whole telling completes at the selected moment and what that ending gives the listener. The last testimony or a knowledge limit can serve when it completes that telling; source exhaustion alone does not establish an ending. Genre governs the choice, with no prescribed image, moral or emotional effect.

The acts choose the version to be heard and its level of detail; `optional_material` does not defer that selection to the writer. They supply new subject knowledge where needed while leaving ordinary inferences to listeners. The last Snow control established that making optional numbers visible alone did not solve this editorial problem.

The active V9 descriptions remain compatible: `why_end_here` already asks why the selected story stops here and what remains with the listener; act descriptions permit the chosen account and its new knowledge; prerequisites are what this audience needs, not every inference. `plan-revise` uses the same V9 output schema as `plan` and assembles the full planner instructions before its revision suffix. No schema descriptions, output fields, historical pins, saved plans or other role instructions change. New runs archive the new content IDs; existing snapshots retain their own instruction bytes.

Offline request tests cover all four planning stages, including `plan-review-revised`: fresh requests receive the edited bodies, while saved requests retain their archived instructions with identical inputs and schemas. Existing assembly and historical schema hash tests remain green. These checks establish delivery and compatibility, not improved model judgment; the new Snow generation remains the live editorial check.

## Choice of telling before act development

New V9 own-plan runs pin `planning_output_contract=planning-story-choice/1`, independently of the role projection and the material-options handoff. The existing planner call first returns `story_selection`: two brief concepts (`first`, `second`), their substantive `difference`, the `selected` variant and its short editorial advantage (`why_selected`). This is an editorial result, not a reasoning transcript. Only the selected telling is developed into the existing plan. The schema requests the comparison before that developed plan; no extra invocation or act/number quotas are introduced.

The plan editor receives the complete result and assesses the chosen telling as a whole, duplicate contributions of scenes and what its ending adds. `generated-plan.md` displays both concepts, their difference, the selection and its advantage in Russian or English; adjacent machine output preserves full values. Author and rewrite projections continue to whitelist the developed plan fields, so neither receives the comparison. No persistence field is shortened or heuristically rewritten, and no semantic validator judges whether two concepts are sufficiently different.

The pin is opt-in at new-run creation and fresh planning preview. An absent pin selects the exact former V9 model and schema; V1–V8 wires stay unchanged. Saved requests and planning descendants inherit their original pin, prompts and schemas, with no automatic upgrade. Shape-based snapshot parsing and fixed-plan import retain both historical plans and complete choice results, including blocked/null plans. New output validation requires a non-null selection whenever a plan exists. Unsupported pins are rejected before inference. Published current planning/fixed-input schemas include the new form; historical published files are unchanged.

Offline tests certify strict schemas, old V9 hashes, long-value roundtrips, import/export, reviewer visibility and identical full author/rewrite requests with or without the comparison. Synthetic providers answer the schema actually requested. Native tests exercise V8, pre-choice V9 and new V9 preview/resume plus ordinary planning revision, target recovery and continuation without real model calls. Editorial quality still requires evaluating the planned story and its resulting script in the independent live run.

## Shared duration estimate for planning

New V9 own-plan runs pin `planning_duration_contract=planning-duration-context/1`. Planning, planning revision and both plan-editor readings receive the same `duration_context(assignment, include_requested=False)` as a fresh author request. The assignment already carries the requested duration, so it is not duplicated. The existing Russian solo estimate gives 1650 words for a 15-minute target; no rate or word target is introduced for other languages or dialogue. The planner and editor use this as a rough guide to scope and room for development, not a word quota or measured audio.

The input pin is independent of `planning_output_contract=planning-story-choice/1` and projection V9. Output schemas and writer inputs are unchanged. Missing pins preserve the exact previous planning payload; saved previews, resume and planning descendants retain their manifest's original pin and archived instructions. New previews use the current input pin. Offline tests compare all four planning payloads with the real Snow/GPS author payloads, check the uncalibrated cases, and reproduce complete old V8/V9 requests without the new field. No live plan or frozen experiment is executed or changed by this fix.
