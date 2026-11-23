# GPS clock case

The [shared input](shared.json) uses the version 2 editorial contract. It keeps the labels, titles, claims, qualifications and exact excerpts from [the version 1 input](../../v1/gps-clock/shared.json). Each card has a persistent ID. Titles and labels can change, and cards can move, without changing that ID.

The [migration record](card-id-migration.json) maps legacy labels to IDs. Its original `target_digest` identifies the first version 2 snapshot. The later `story_facts_revision` lists the cards that acquired the `story_facts` field. Removing those additions reproduces the earlier digest. Reversing the ID mapping reproduces the version 1 input, whose saved bytes are identified by `source_sha256`.

The [fixed plan](fixed-plan.json) binds to the digest of this shared input and still references the original facts and plan document. Import it together with the matching shared revision. Mixing it with the old digest fails validation. For a planner-generated candidate use `own-plan`. See the [input schema](../../../../backend/schemas/editorial-input.v2.schema.json) and [writing workflow](../../../../backend/WRITING-EVALUATION.md).
