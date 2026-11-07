# Writing instructions

Edit the applicable UTF-8 Markdown file in `current/`. The planner receives only `plan.md`; planning revision receives `plan.md`, then `plan-revise.md`. These files form the complete planning task, with format, speakers and language supplied in the payload. Other ordinary roles receive `common.md`, the format component, then their stage component. Instruction text is English; `output_language` controls spoken text and other human-readable output. Preserve supplied labels, exact quotations, keys and enum values.

| Component | Responsibility |
| --- | --- |
| `common.md` | Language, assignment and evidence rules |
| `dialogue.md`, `solo.md` | Speaker functions and explanation in that format |
| `plan.md` | Choose a connected route from the assignment and semantic dossier |
| `plan-review.md` | Review the exact candidate with attributed evidence |
| `plan-revise.md` | Return a complete new proposal from actionable feedback |
| `draft.md` | Realize accepted story decisions as a complete spoken episode |
| `review.md` | Inspect the entire actual script, with exact anchors |
| `revise.md` | Return the complete improved script when revision is required |
| `anchor-repair.md` | Select passages for unresolved script anchors only |
| `plan-target-repair.md` | Select fields for unresolved plan review targets only |

Repair instructions are standalone. They do not inherit ordinary reviewer instructions or format components. They locate existing judgments without rewriting or approving anything.

## Inputs and decisions

New `editorial-role-packets/1` runs use role-specific semantic views. Full original sources, inputs and outputs remain persisted. No file paths, hashes or technical identities belong in model-visible content. Natural support labels and exact response locations are supplied only where the task needs them. The planner chooses the route and way of explaining factual research material. The author receives accepted creative decisions and selected claims with their context and limits; the positive plan review and source library are not writing inputs. No read/research tools are available in these calls.

Own-plan loads the shared assignment and dossier without the separate fixed outline. Only a complete ready proposal explicitly accepted by a separate plan editor reaches the writer. A required revision produces a full new proposal and a new review. A blocking evidence question stops writing; recording it does not obtain an answer. Fixed-plan uses its separately admitted structured outline and does not invoke planning roles.

The script editor distinguishes `keep`, `revise` and `needs_material`. An unambiguous `keep` reuses the exact saved draft without another author call. Missing material stops the chain; contradictory decisions fail explicitly. The prompts must not ask for a redundant rewrite of an adequate text.

See [the role-input contract](../../../../../docs/podcast-production/08-editorial-inputs.md) and [offline examples and commands](../../../../../evals/reports/2026-11-05-editorial-inputs/README.md).

## Editing and saved runs

A text edit is sufficient for a new assembly: no copied version directory, registration or unrelated hash updates. `load_instruction_set(format, include_planning=True)` reads components once and assembles all six ordinary roles. `role_instructions(stage, format)` can inspect any ordinary current role. Resources come from the installed package, independently of the working directory; deployed packages must receive changed assets.

Each assembly has a content ID derived from its stage and exact ordered component names/bytes, including format when a format component is used. Isolated planning bodies have the same ID for solo and dialogue. Editing `plan.md` affects planning and its revision; editing `plan-revise.md` affects only revision. Common and format edits affect the other ordinary roles. Their existing ID construction remains unchanged. The ID stays in runtime provenance, outside model instructions. Repair bodies have separate content IDs.

A new run pins complete prompt bodies in saved definitions. Resume uses these bodies, not mutable `current/` or Git. Planning's second review uses the pinned plan-review body but reviews the new exact candidate. Every candidate, draft and review retains its own binding. Successful structured output is not semantic acceptance.

The frozen `v1/`, `v2/`, `v3/` directories remain unchanged. The historical resolver handles numbered versions only. An old content hash is not a globally retrievable snapshot: inspect that run's saved definition or exported request. Existing legacy runs resume under their archived input contract; editing current assets does not migrate them.

To inspect actual instructions, open the exported stage's `request.json`, or `attempt-N/request.json` for a later attempt. To inspect a new request without any provider or token-endpoint call, use `podgen.writing_cli preview`. Its output includes the complete request and visible-byte sizes; rendering does not truncate source text. The report documents every role and both repair previews.

## Ownership and limits

The editor owns wording and editorial material. The developer owns schemas, projections, loading, persistence, isolation, validation and request assembly. Full semantic fields must never be silently shortened. A capacity error requires a deliberate meaningful representation or a different unit of work.

The script review uses exact zero-based utterance positions and verbatim nonempty quotes; plan review selects existing fields in the exact candidate. Locations are not independent evidence. A reviewer supplied with story intent and dossier reports `reading_mode=informed`; this is not a blind human listening trial.

Schema checks, source-range checks, compactness and completed execution establish their specific properties only. They do not establish independent factual approval, measured duration, human understanding or production readiness.
