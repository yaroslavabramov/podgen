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

New V9 runs pin `plan_selection_contract=plan-material-selection/1`. The planner expresses the chosen level of detail in the acts themselves, as the actual account the audience will follow. `duration_strategy.assumptions` remains a working timing calculation; it is not the sole carrier of editorial selection. Concrete extensions or cuts belong in `optional_material` and name the material itself, with catalog locators kept in reference fields.

Under the new pin, the existing `optional_material` values reach the author and the initial revision frame as `material_options.choices`. Its model-facing `guidance` explicitly describes conditional extensions or cuts and says that listing an item does not mean it should be spoken. They remain revisable choices, not a coverage requirement. Empty options do not add a field. The full `assumptions` list, backend IDs and hashes are not forwarded. No prose is rewritten or shortened during persistence or handoff.

The input pin is independent of V9 output schemas. A saved manifest without it retains the previous payload, including in preview and continuation; resume never adds the pin to a saved run. Fresh previews use the current handoff. An unsupported pin or a pin attached to another projection is rejected. Existing reader, factual-review and revision output schemas do not change.

After a rewrite supplies its working composition, that composition replaces the initial editorial frame. Its current route and decisions govern the next revision; the initial material options are not reinserted over a new selection. The old plan remains archived.

### Offline certificates and limits

`test_plan_selection.py` freezes the actual accepted Snow plan and author input from `tmp/snow-narrative-nov8/calls`. The old handoff reproduces that input's story exactly; the new one adds only the three optional editorial choices. Thus the 61/6/6 expansion is delivered as optional instead of being visible only in supporting material. The original act is unchanged, including its existing detailed breakdown: reducing that choice still requires an editorial revision of the plan.

The archived first option contains the free-prose locator `M4`. It is preserved verbatim; no regex or guessed replacement migrates source prose. New planning instructions require naming the actual material in such decisions. This historical nuance is not evidence that a newly generated plan has followed the instruction.

A separate clock-comparison example verifies the ordinary draft/revise preview, separation of positive selection and optional extension from timing assumptions, and replacement by current composition. Tests also cover two successive materialized compositions, byte-identical cold readings, unchanged V9 preview when the pin is absent, and native V8/V9 manifest/preview/resume.

No model inference is part of these certificates. The minimum live follow-up is one ordinary plan-revise call on the same saved reporting and candidate with the new planning prompt. Check that its chosen detail is consistent across the acts and optional material, then inspect the resulting author input offline. A new full-episode generation is not needed to verify delivery; whether a model makes and follows a better editorial choice remains a separate live check.
