# Addressed script revision

Independent readings pin `addressed-script-revision/1`, the two reading schemas
and the private revision response schemas in the run manifest. The public result
is a complete `WriteOutput` script. Older full-script runs keep their original
contract. See [independent script readings](independent-readings.md) for the
current reading versions.

Both editors explicitly choose `revision_scope.scale`. Runtime never infers scope
from text length or finding count. `local` allows several distant independent
repairs. `dependent_passages` names additional passages whose relationship needs
repair. `sequence` requires explicit `sequence_ranges`, each with first and last
anchors delimiting inclusive utterances. Any `whole_episode` selects structural
revision. Historical sequence assessments without boundaries select structural
revision rather than granting an inferred range.

## Model contract

`revision_operation` contains a mode, ordinal `script_version`, compact targets,
and explicit sequence ranges. Target IDs are request-local. Models never copy
source hashes, record references, offsets, or authorization metadata.

- `targeted`: `{edits, composition_update}`. Edits replace/delete offered text
  targets or insert utterances at offered boundaries. `composition_update` is
  nullable.
- `sequence`: `{sequences, edits, composition}`. Each sequence replaces an
  offered range; independent addressed edits remain available outside it.
- `structural`: `{script, composition}`. The model returns the complete chosen
  script and its current narrative choices.

Composition contains `main_question`, `listener_promise`, `route_summary`, and
`decisions`. The server carries unresolved research questions alongside it.
New production runs give the next writer the current question, listener promise,
route and unresolved questions. Targeted and sequence edits use that frame with
the current script. Structural edits also receive the original plan. The full
composition, including the writer’s decisions, stays in the archive alongside the
plans, scripts, reviews and requests. Earlier runs use their pinned request layout.

## Addressing and application

Only actual findings and explicitly declared dependent passages grant text
targets; scope reasons and praise anchors grant no additional edit permission.
Each quoted anchor must occur exactly once within its utterance. Targets offer
the exact quotation and bounded sentence context where available. Full private
catalogs retain their source offsets, evidence of authorization and deletion
buffers; the model sees compact IDs and text.

The server binds the source record/hash, both review records and invocation.
Edits apply simultaneously against that source. Replacement preserves external
source separators; deletion uses its precomputed exact separator buffer. No
arbitrary trimming repairs model output. Unknown IDs, stale sources, overlaps,
duplicate operations, no-ops, empty scripts and invalid speakers fail validation.
Unchanged text outside authorized effects is preserved exactly. The application
record stores the wire output, exact effects and source/result hashes for replay.

For production runs pinned to `script-edit-assignment/3`, the writer receives
required assignments from the two reviewers and the evidence needed for factual
repairs. Audit rationales and previous verdicts stay in the archived reviews.
Earlier manifests use their original handoff.

## Lifecycle

Run, preview and resume use the same source selection, compact catalog and bound
schema. `preview --stage revise-N --composition state.json` can carry current
composition with explicit scoped reviews; saved-run previews load pinned state.
Exports include private revision context, application proof and composition.
Every materialized revision receives fresh independent reader and factual
readings before a final outcome. Resume verifies completed applications and
reuses saved requests without repeating completed invocations.
