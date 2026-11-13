# Saved hosted research → writing

`research-materials-prepare` admits an already completed hosted research run and explicitly selected saved acquisition results. It does not perform research, fetch URLs, construct knowledge cards, call a model, or derive the writing assignment. Supply an existing `Assignment` explicitly; the full hosted commission remains backend provenance.

The admission retains the complete report, original reported occurrences, exact source revisions/representations, acquisition receipts, blobs and hashes. A deterministic, gapless H1/H2 partition supplies M sections. S numbers follow the report's final Markdown Sources list, independently of acquisition order; native annotation offsets remain unvalidated provenance. A section's citation links are reported associations, never verified source support.

Readable original acquisitions take precedence; otherwise the first readable retained fallback in acquisition order is selected with its explicit role. Catalogs distinguish available, partial, unavailable and not acquired. Explicit source assessments can restrict acquired material, never create readable evidence. Unavailable overrides remove the readable document. Catalog titles come from a leading retained Markdown heading, available source metadata, or the reported label; the title basis and heading range are retained.

## Model views and tools

The initial planning request contains the writing assignment, `research_contents` and `source_catalog`. Planning, independent plan review and plan revision use the ordinary production workflow, models and round limits. They can open report sections with `read_material` and underlying retained documents with the existing `search_sources`/`read_source` tools.

`read_material` takes `{material_id, start, end, context}`. Offsets count section-local Unicode characters; `end=null` reads the next page. Continue at `next_start`; neighboring M identifiers provide navigation. Optional before/after context has separate actual report ranges and shares the character budget. R receipts record report reading and never enter the E source evidence catalog.

The writer receives the **complete selected sections**, the accepted plan and compact catalogs. Source documents are opened separately; a cited 200k-character source is not automatically attached to the writer. Fact review receives report navigation and honest source bibliography, with no report passages represented as original evidence. Its actual E reads survive required revisions as P passages and subsequent audit memory. The cold reader receives its existing script-only view.

`research_material_contract` pins admission digest/reference, sectioning, projection, instruction-adapter and evidence-description versions. `frozen-corpus-tools/5` adds report reading and planning tool access. Existing inputs keep their previous tool definitions, instructions and frozen manifests. Admission and reading reauthorize the repository dependency closure; stale in-memory text cannot bypass revoked access.

## Commands

Use the ordinary `podgen.writing_cli` with project, actor, object store and `PODGEN_DATABASE_URL` configured. In Bash, define `pgn=(python -m podgen.writing_cli --project PROJECT_UUID --actor ACTOR_UUID --objects OBJECTS_DIRECTORY)`. `prepare.json` follows `PrepareResearchMaterials` in `podgen.research.materials.contracts`: operation ID, hosted run ID, saved selection IDs, explicit assignment and optional access assessments.

```sh
"${pgn[@]}" research-materials-prepare \
  --input prepare.json --out shared-ref.json
"${pgn[@]}" preview \
  --shared-ref @shared-ref.json --case CASE --stage plan \
  --profile @profile-planner.json --run-id RUN_UUID --out plan-preview.json
"${pgn[@]}" run \
  --run-id RUN_UUID --case CASE --author-ref @shared-ref.json \
  --writer-profile @profile-author.json --reviewer-profile @profile-editor.json \
  --planner-profile @profile-planner.json --plan-reviewer-profile @profile-editor.json \
  --export-dir export
```

Evaluator export writes the complete admitted report, readable source texts, admission, source/catalog ranges, original preparation and retained result/acquisition provenance under `research/`; fixed-plan clearances retain their original journal provenance.

Prepare and preview are offline. `run` and `resume` execute model calls. The defaults retain one allowed plan revision and two allowed script revisions. Review the full actual request and instruction changes before a paid run; inspect the resulting plan and its separate review before evaluating downstream quality.
