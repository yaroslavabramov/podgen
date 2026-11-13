# Initial fact review with retained sources only

Status: prepared and tested; no live provider call made. The coordinator must inspect the code commit before live submission. The initial request hash is approved:

`400d5c617fd04a13c41edfef0ce8692ea8ac6e891f4c35da36981ae0c93dee1d`

## Controlled inputs

Source run: `f7e9dbac-c64c-5698-807a-fa8b25e69a2f`.
New independent run: `a07cc6d1-c399-5979-a698-084ece565c47`.

The runner loads the original immutable reviewer ModelProfile, script reference and pinned contracts. `preflight/model-profile.json` contains the complete profile. The compiler must reproduce the saved native request exactly after the explicit report-access adaptation and new run/invocation metadata, or preparation fails.

The 51 readable retained documents keep their exact S IDs, titles, content hashes and Unicode lengths. This includes contemporary records and later accounts. All 62 catalog entries retain their original status, coverage and access limitations. No later runtime admission or supplemental investigation from the source run is imported. The initial evidence is empty and the audit context is unchanged. No old fact output or findings are read by preparation or supplied to the model.

`preflight/source-inventory.json` records every source's identity, length, hash, coverage and limitations. `comparison.json` records equality and request hashes. `before/after.request.json`, instruction and payload files and `full-request.diff` contain the complete reviewable inputs.

## Changed model-facing fields and paragraphs

- Remove `research_contents` and `research_results` from the initial payload. Script, factual remit, cautions, bibliography, output language, evidence and audit context stay identical.
- Remove `read_material`, `read_research_result`, `list_research_results`, `list_research_sources`, `select_research_result` and `request_research`.
- Remove M-scoped search and report references in source-reader descriptions. Unknown M scopes reject explicitly, including empty queries and streaming retrieval.
- Keep `support_labels`' existing meaning; instruct `[]` because no report material is supplied. Remove its M enum, with no `maxItems` restriction or replacement evidence meaning. Keep source support in E receipts and audit source references.
- Replace the report-reading section with retained-document navigation and coverage/access guidance. Remove supplemental report discovery, reading and research-request paragraphs. Change only three report-related phrases in the factual instructions. Contextual factual judgment and audit requirements stay intact.
- Preserve source requests and the ordinary unlimited durable tool loop. Preserve the original raw JSON history contract: `history_view` remains absent. No checkpoint or semantic-history redesign is included.

The intended behavioral difference is document-based factual judgment without consulting research synthesis. Model, reasoning effort, output limit, cache configuration, transport settings and factual judgment criteria stay fixed.

## Execution and persistence

The variant is explicit: `fact_review_input_mode=retained-sources-only/1`, pinned in both manifest and source-tool contract, restricted to the initial fact-review stage. Normal production defaults and archived contracts are unchanged. Both the corpus and operational executor reject hidden tools. Later X report admissions remain inaccessible. S document acquisition remains available through the existing traced resolver.

The dedicated runner calls `execute_reading_stage` and the existing durable role/tool loop. It never starts a plan, writer, reader review, rewrite or episode acceptance. Completion is `completed_unaccepted`. A regular full-workflow resume rejects this experiment. The reviewed request hash is checked before admission and again on the actual compiled root request before provider construction. Replay reuses completed work.

`admit` pins an already prepared object and never reloads the corpus. Its idempotency and complete runner replay are tested. Every native request, response, passage, history record and usage receipt uses the ordinary journal and export. A new context history is created for the independent run.

## Validation

56 tests passed in 19.89 seconds against an isolated PostgreSQL copy migrated from 0022 to 0025. The original saved database was not migrated or modified. Checks covered the new variant and runner, original research navigation and native replay, the existing full research workflow, history views, operation guards and source-tool recovery. Ruff and whitespace checks passed.

The runner integration uses a fake provider: an actual source read followed by a validated complete contextual audit, then replay with provider construction forbidden. Admission is also repeated with corpus loading forbidden. These tests establish execution contracts, not the editorial quality of a future live verdict.

The actual 51-document free preview completed in about one second. Exact SQL/object counters are in `preflight/preview-measurements.json`. It performed no provider request. The initial preview includes source loading and exact-wire comparison, not index startup or a whole review.

For live execution, measurements separately capture startup, fact review, export and whole-review time/SQL/object operations, plus search call count/time and process RSS high-water mark. Object counters measure repository reads and logical staging/publication calls/bytes; they do not claim physical disk writes or count cached Unicode projection reads. Native trace preserves provider usage, including cache receipts. USD may be estimated separately from `evals/reports/2026-11-08-author-openings/pricing.json`, explicitly as an estimate using that saved table rather than a billing receipt.

## Entry point

Run with the source actor/project, object store and an isolated migrated database in `PODGEN_DATABASE_URL`:

```text
PYTHONPATH=backend/src python -m podgen.fact_review_experiment_cli \
  --project PROJECT --actor ACTOR --objects OBJECTS \
  --source-run-id f7e9dbac-c64c-5698-807a-fa8b25e69a2f \
  --run-id a07cc6d1-c399-5979-a698-084ece565c47 \
  --out OUTPUT preview
```

After coordinator review, replace `preview` with `run --approved-request-hash 400d5c617fd04a13c41edfef0ce8692ea8ac6e891f4c35da36981ae0c93dee1d`. The provider is configured from the pinned profile, including its RPC timeout. Do not change the request or infer a new trial from a failed attempt. Incomplete or uncertain paid work remains governed by the ordinary journal/recovery contracts.
