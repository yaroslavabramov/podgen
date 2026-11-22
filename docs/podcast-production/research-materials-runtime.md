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

## Fact-only native search and narrow research (`frozen-corpus-tools/8`)

New runs use `/8`; saved `/7` and older manifests retain their original model-facing requests and research commissions. Non-factual roles keep the `/7` tools and instructions unchanged. Factual review receives anchored `request_research`; OpenAI factual review also receives Responses `web_search` with low search context. Existing model profiles are reused. Hosted search requests enforce its 128,000-token window in checkpoint planning and dispatch.

Search is navigation, not primary evidence. Obtain a chosen document with `request_source`, then read its retained text with `read_source` to create E receipts. Search calls are physical provider work, accounted separately through `responses-web-search-receipt/1`, `web-search.json` and new-run `run.json.web_search`. Unknown network outcomes remain uncertain and are never automatically resubmitted. Existing count, token, cache and refusal accounting remains authoritative.

For both direct and structured factual requests, `targeted-script-context/1` validates exact anchors against the immutable caller script hash and attaches complete original turns and adjacent turns. Its exact script reference stays in the private caller context. The dedicated English research commission receives verified text; `output_language` controls generated prose.

`targeted-research-handoff/2` resumes the same assessment through the existing hosted-research lifecycle. Its bounded JSON answer is validated against retained document bytes and source paths. `targeted-research-answer/1` returns only the short answer, limitations, exact S excerpt ranges and relevance. These locations still require `read_source`; they are not E receipts. Only necessary excerpt documents are acquired, reusing the ordinary acquisition/admission mechanism.

Malformed/oversized answers, fabricated quotes and missing documents yield explicit unresolved/unavailable results without publishing a completed X answer or repeating paid research. Saved native results and acquisitions survive interruption; concurrent resolution uses the existing run lock and explicit busy rejection. See `evals/reports/2026-11-20-targeted-research/` for offline native requests, complete-operation measurements and verification limits.

Newly prepared `/8` runs also pin `source_tool_contract.search_argument_policy=recoverable-search-result-version/1`. Only factual model tool execution converts `source_search_result_version_mismatch` for a supplied continuation key into an explicit tool error. The response returns no old results and supplies retry arguments for the intended query/scope with `result_version=null`, `offset=0`, and no alias continuation. Authorization is checked again before returning it. Other conflicts, storage failures and access failures propagate. Saved manifests without this pin preserve the previous stopping behavior.

## Shared bound research orders

New writing manifests pin `research_request_contract=bound-research-orders/1`. Direct factual tools, structured requests and terminal episode `needs_material` use one binding to the immutable originating script and neighbors. The original material scope owns the reusable research order; a writing run owns its local interpretation and S/X names. Reordered/duplicate anchors and changed caller rationale do not create another paid order. Each call retains complete original arguments and a reference to its local shared result, with fresh authorization before reuse.

Terminal questions remain separate. Progress preserves processed questions and the remaining unresolved question; incomplete research prevents continuation. Restart reuses the prepared/completed child and saved interpretation. Local parser upgrades are append-only and do not resubmit paid work. Saved manifests without the pin keep their prior behavior. Complete research remains synthesis, not an editorial keep or an E source-reading receipt.

## Local unavailable-source recovery

Material results, including `unavailable`, admit their server-produced references
before publication through the existing input and private checkpoint authorization.
An unavailable document is an acquisition result; recovery does not reclassify it
from HTTP status or fetch it again.

`writing recover-source-result --run-id RUN --stage fact-review --invocation-id INVOCATION --call-id CALL --export-dir DIR`
is an explicit local action. It only reconciles a current unfinished `request_source`
whose exact paid response and immutable material operation result are retained after
`unbound_private_checkpoint_source`. Run/stage/attempt/call, full original arguments,
operation identity and current permissions are checked. Successful, pending,
unrelated and uncertain provider outcomes are not eligible.

One atomic checkpoint publishes the tool result and a versioned
`local-source-result-recovery/1` next-request candidate built by the normal turn
assembler. The action stops before input counting or generation, creates no new
physical turn and returns `stop_reason=before_external_count`. Saved requests and
paid receipts remain unchanged. The run projection becomes `prepared`; this is not
an editorial decision or permission to dispatch.

A repeat reads the exact existing checkpoint and reauthorizes its admitted closure
without reopening original request/response or acquisition bodies. Export is a
separate optional local read of the prepared application/native wire requests.
Ordinary `run`/`resume` still authorizes any subsequent external action separately.
