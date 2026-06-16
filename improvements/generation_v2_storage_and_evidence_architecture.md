# Generation V2 Storage and Evidence Architecture

Date: 2026-06-14
Status: design amendment
Related document: `improvements/generation_v2_editorial_agent_architecture.md`

## Decision

Generation V2 should move to an OpenAI-first corpus reading model.

That does not mean "only store data in OpenAI." It means the model-facing read path should have one default corpus system:

```text
model -> corpus evidence API -> provider corpus index -> saved evidence -> V2 artifacts
```

Local storage should not compete with the provider index as a second corpus that the model must choose between. Local storage should hold the product's source registry, source versions, fetched web snapshots, upload metadata, evidence records, artifacts, eval data, and fallback/debug representations.

The current hybrid implementation is useful as an MVP, but its conceptual problem is that local chunks and provider file search both look like corpus stores. That makes prompts and tool policy muddy: the model can search two representations of similar information, and final citations do not prove which one actually supported the claim.

The target architecture should be:

```text
Project sources
  -> source versions and stable snapshots
  -> original files and useful companions uploaded to the persistent provider corpus index
  -> agents use hosted file_search for broad discovery
  -> agents use controlled read/write evidence tools for deep reading and audit
  -> every important editorial/factual output points to saved EvidenceItems
  -> final citations and shownotes are built from EvidenceItems
```

## Principles

1. OpenAI file search is allowed to be the primary reader.

   We should not force provider search results to be rediscovered in local chunks before they can count. Provider-native evidence is valid if we persist the retrieval result and can connect it to a known project source.

2. Raw hosted file search is not enough for final citations.

   A model can use hosted file search directly for broad research and source exploration. Those results become `observed` evidence when persisted, but important PDFs/documents should be deep-read through a controlled `read_source_with_model` tool that calls OpenAI Responses with `input_file`, saves quote/snippet/support-summary output as an EvidenceItem, and returns the saved `evidence_id` to the agent. The direct `input_file` should use a provider file uploaded for file input, for example OpenAI `purpose="user_data"`, rather than assuming the vector-store `purpose="assistants"` file id is accepted by every input-file endpoint. This prevents the final fact pass from depending on self-reported citations in a structured response.

3. Local chunks are not the source of truth for V2 quality.

   They are useful for fallback, regression evals, cheap local inspection, transcripts, web snapshots, and cases where local extraction gives better timestamps or page markers. They should not be the mandatory final citation layer for PDFs/documents. OpenAI-native file reading is the primary path for complex documents.

4. A claim is not supported because the model said it is supported.

   A claim is supported only if it references a saved evidence record. The evidence can come from hosted file search, model file reading, provider vector-store search, local transcript search, local chunk search, or a manually captured web snapshot.

5. The model should not choose between "OpenAI corpus" and "local corpus."

   Agents should see a single corpus API. Internally that API should prefer provider search and fall back to local tools only when the provider path is unavailable or produces weak evidence.

6. Provider indexes should be persistent at the project level.

   Per-run vector stores cause repeated uploads, higher latency, higher cost, and inconsistent retrieval state. A generation run should normally attach to an existing project corpus index and filter by selected sources.

7. Web evidence must be snapshot-backed.

   Web search and live URLs are useful for discovery, but final evidence should point to a saved source version: fetched HTML, rendered text, PDF, transcript, or other stable artifact uploaded to the provider index.

8. The storage design exists to improve the episode, not to preserve old ingestion assumptions.

   The goal is better source understanding, stronger story selection, and harder fact gates. Local parsing should be kept where it improves those outcomes.

## Trust Model

V2 should trust LLMs to read, synthesize, judge support, and write. It should not trust an LLM to invent its own audit trail.

The difference matters:

- acceptable: an LLM calls an evidence tool, receives saved evidence ids and snippets, then judges whether those snippets support a claim
- not acceptable: an LLM writes `source_id`, `locator`, and `supported=true` in JSON with no saved retrieval result behind it

So "system-owned" does not mean "pure deterministic semantic fact checking." It means the system controls evidence creation, stores the retrieval results, and constrains the verifier to judge over those saved results.

## Storage Layers

### 1. Source Registry

The source registry is the local product record of what the project contains.

It stores:

- source id
- project id
- title
- source type
- original URL or uploaded file pointer
- author/publisher/date metadata when known
- source reliability/authority hints
- current source version id
- active/deleted state

This layer does not need to expose full text to the model. It gives identity and lifecycle to the corpus.

### 2. Source Versions

Every uploaded file, fetched URL snapshot, rendered web snapshot, transcript, or generated page-marked companion should be represented as a source version.

Recommended fields:

- source version id
- source id
- content hash
- representation kind: `original_file`, `fetched_html`, `rendered_text`, `transcript`, `page_marked_text`, `provider_file`
- storage path for local artifact when available
- provider file id when uploaded
- provider vector store file id when indexed
- upload status
- created at
- supersedes source version id

Source versions are immutable. If a URL is refetched or a file changes, create a new version instead of mutating the old one.

### 3. Provider Corpus Index

Use a persistent project-level provider index by default.

For OpenAI, that means one vector store per project in the common case. The vector store contains files for active source versions. Each vector-store file should have attributes such as:

- `project_id`
- `source_id`
- `source_version_id`
- `source_type`
- `representation_kind`
- `content_hash`
- `authority`
- `is_active`

OpenAI vector store files support attributes, search responses include file id, filename, score, content chunks, and attributes, and vector stores support expiration policies. The implementation should use those capabilities rather than treating the provider store as an opaque black box.

Official docs:

- File search guide: https://developers.openai.com/api/docs/guides/tools-file-search
- Vector stores API: https://developers.openai.com/api/reference/resources/vector_stores

### 4. Run Corpus View

A generation run should not normally create a new vector store.

Instead, it should create a run corpus view:

- selected source ids
- selected source version ids
- provider vector store id
- filter expression or equivalent source whitelist
- temporary source overlays if the run has run-only attachments
- readiness summary

If provider filtering is not sufficient for a specific case, a run-level overlay vector store is acceptable, but it should attach existing provider files where possible instead of reuploading content.

### 5. Evidence Store

Evidence is the missing first-class storage layer.

Every source card, quote card, editorial atom, factual claim, citation link, and fact repair should be able to point to saved evidence.

Recommended `EvidenceItem` fields:

- evidence id
- generation run id
- project id
- source id
- source version id
- provider
- provider vector store id
- provider file id
- provider vector store file id
- response id
- tool call id
- retrieval channel: `provider_file_search`, `model_file_read`, `provider_vector_search`, `local_chunk_search`, `local_transcript_search`, `web_snapshot`
- query or task that produced the result
- search result index
- score, if available
- retrieved snippet text
- normalized quote text, if selected
- support summary and uncertainty, when the evidence was produced by model file reading
- locator text
- locator type: `page`, `section`, `timestamp`, `provider_snippet`, `source_level`, `unknown`
- evidence strength: `exact`, `snippet`, `source_level`, `weak`
- snippet hash
- created at

This is the layer final citations should use.

## Evidence Tools

The corpus interface should expose controlled tools rather than forcing agents to choose raw provider search or local search directly.

Recommended tools:

- `search_evidence(query, filters, purpose)`: searches the provider index first, persists returned results as EvidenceItems, and returns evidence ids plus snippets
- `read_evidence(evidence_id)`: returns the saved snippet, locator, source metadata, and retrieval channel
- `read_source_with_model(source_id | source_version_id, focus_question, claim_text, purpose, locator_hint)`: selects the best provider-backed source version, calls OpenAI Responses with `input_file`, asks for strongest support/quote/summary/uncertainty, persists the result as an EvidenceItem with retrieval channel `model_file_read`, and returns the saved evidence id
- `find_quotes(topic, filters)`: searches for quote-like evidence, persists candidates, and returns evidence ids
- `search_sources(query, filters)`: returns source-level candidates for triage, with evidence ids where snippets are available
- `fallback_local_search(query, filters)`: used internally by the corpus API when provider evidence is missing or weak, not exposed as a competing default path

Raw hosted file search should be exposed to triage, research, and citation linking for broad discovery. When it finds important source files, agents should call `read_source_with_model` for focused file reading and then `select_evidence` before SourceMap/research/citation use. Citation validation should enforce that linked evidence is already `selected` or `cited`; this is not only a prompt convention. For PDF/document source cards, provider snippets alone are too thin for a strong SourceMap entry: selected model-read evidence should be required when the card is built from hosted/provider search. The support judge remains constrained to saved evidence and must not search or read new files.

## Locator Policy

The system should stop treating every locator string as equally strong.

Use these levels:

### Exact

The evidence has a quote/snippet plus a precise locator such as page, timestamp, section, paragraph marker, or a page-marked text marker.

This can support citation-sensitive factual claims.

### Model File Read or Provider Snippet

The evidence has a retrieved/model-read snippet, quote, or support summary and source identity, but no precise page/timestamp/section.

This can support many claims when the support judge passes, but the shownotes UI should present it honestly as a source snippet or model-read source support rather than pretending to know a page number. Page numbers are optional, not a gate requirement.

### Source Level

The source broadly supports an editorial atom or background context, but there is no selected snippet.

This can anchor research memo material and treatment decisions. It should not be enough for a citation-sensitive claim in the final transcript.

### Weak

The model mentions a source but no retrieval result or saved snippet exists.

This should fail the fact gate for factual claims.

## Corpus Preparation

### Uploaded Files

For uploaded PDFs, documents, and other files:

1. Store the original file locally.
2. Create a source version with a content hash.
3. Upload the original file to the provider index.
4. Store provider file ids and vector-store file ids.
5. If exact locators matter and provider snippets do not expose enough location detail, generate a page-marked text companion and upload that too.

The page-marked companion is not a return to claim-first chunking. It is a locator-improvement representation.

### Web Sources

For URLs:

1. Save the original URL.
2. Fetch a stable HTML snapshot.
3. Capture rendered text when layout or dynamic content matters.
4. Optionally capture a PDF/screenshot/page image for layout-heavy pages.
5. Upload the stable snapshot representation to the provider index.
6. Use web search for discovery, but enroll useful discovered pages as source versions before they can be final evidence.

Final evidence should not depend only on a live URL that may change.

### Audio and Video

For audio/video:

1. Store the original media reference.
2. Create or import a transcript with timestamps.
3. Upload the transcript with timestamp markers to the provider index.
4. Treat timestamped transcript snippets as exact evidence.

### Existing Local Chunks

Keep existing chunks, but demote their role:

- fallback retrieval if provider search fails
- eval baseline against provider-first retrieval
- local debugging
- cheap quote discovery for small projects
- support for source types not accepted by provider upload

Do not build V2 quote cards from the first few chunks by default.

## Agent Contract

The model should see one conceptual corpus interface.

The implementation may use raw hosted file search for broad research, direct vector-store search for controlled evidence creation, and local fallback search when needed. The agent contract should hide that complexity behind evidence ids.

### Corpus Triage Agent

The first model-facing corpus step should produce a source map, not a draft.

It should:

- sample the selected corpus through provider search
- identify primary/official/expert/secondary sources
- detect duplicate or derivative sources
- identify strong source clusters
- mark weak coverage areas
- create source cards from saved evidence
- create initial quote cards from saved evidence

For small corpora, this can be a subroutine of the research agent. For large corpora, it should be a separate phase with its own artifact and gate.

This is not needed because OpenAI vector stores are weak. It is needed because V2 is editorial-first. Once claims are no longer the early compression mechanism, source selection must be explicit.

### Research Agent

The research agent should use the source map and provider-first corpus search to create editorial atoms and the research memo.

Every high-impact atom must reference evidence ids.

### Treatment Agent

The treatment agent should work mainly from the research memo, source cards, editorial atoms, and risk register. It should not broad-search the corpus by default.

It may request targeted evidence if an act decision depends on a missing source distinction.

### Act Dossier Compiler

The dossier should be evidence-backed, not chunk-preview-backed.

Each dossier should contain:

- treatment slice
- relevant atom ids
- evidence ids
- selected quotes
- risks and hedging requirements
- prior continuity ledger

### Act Writer

The writer should not use broad file search by default.

It receives a dossier and can request targeted reads only when the dossier is insufficient or a fact needs clarification.

### Citation and Fact Verifier

The verifier should extract claims from the final-ish draft and link each citation-sensitive claim to evidence ids.

It should use controlled evidence tools. Those tools should call provider search with search results included in the response, or direct vector-store search, and persist returned snippets before the verifier judges them. The final output should not be only claims and source ids; it should include evidence ids.

The verifier can decide whether evidence supports a claim. It cannot create evidence by assertion.

## Fact Gate

The fact gate should be system-owned over saved evidence.

It has two parts:

1. Structural checks that are deterministic.
2. LLM support judgment over saved evidence, where the judge cannot create new evidence.

Deterministic checks can verify:

- evidence id exists
- evidence belongs to the selected source set or allowed source version set
- source version is known and historically available
- retrieval channel is allowed for this claim type
- evidence strength is sufficient for `claim_type` and `source_need`
- locator/snippet is present when required
- quote claims are near-verbatim in the snippet
- score is above the configured threshold when the channel provides a score
- freshness/date metadata exists for stale/currentness-sensitive claims
- source-level evidence is not used for citation-sensitive factual claims

The LLM judge should decide:

- whether the saved snippet actually supports the claim wording
- whether causal/statistical wording is too strong
- whether attribution or hedging is required
- whether the evidence is adjacent but insufficient

The gate should fail when:

- a required factual claim has no evidence id
- evidence strength is `weak`
- claim wording is stronger than the evidence type allows
- citation links point only to source-level evidence for citation-sensitive claims
- deterministic checks fail
- the LLM support judge returns unsupported, overstrong, or needs-attribution for a blocking claim
- a quote claim has no verbatim or near-verbatim evidence
- stale/currentness-sensitive claims rely on undated evidence

The verifier model can diagnose issues, but the gate should not depend only on model self-reporting. It should depend on saved evidence plus an explicit support judgment over that evidence.

Source-level evidence is valid for editorial atoms, treatment choices, and background framing. It is too weak for citation-sensitive factual claims unless the claim's `source_need` says source-level support is acceptable.

## Project-Level Vector Store Lifecycle

### Default

Create one persistent provider corpus index per project.

On source add/update:

1. create a source version
2. upload/index the relevant representation
3. store provider ids
4. mark old source versions inactive if superseded

On source delete:

1. mark source inactive locally
2. remove or deactivate provider vector-store file only when no historical evidence still needs provider retrieval
3. preserve old evidence records for historical runs
4. preserve immutable local snapshots for any source version referenced by historical evidence

On generation:

1. reuse project vector store
2. filter to selected active source versions
3. create a run corpus view
4. do not reupload unchanged files

### Temporary Sources

For run-only sources:

- add them to a run overlay index, or
- attach them to the project index with `scope=run` and a TTL cleanup policy

The choice should be driven by provider API limitations and cleanup reliability. The model contract should remain the same either way.

### Expiration

Provider stores can use expiration policies, but V2 should not rely on expiration as the primary lifecycle mechanism.

For active projects, keep the project index warm. Use explicit cleanup on project delete, source delete, and stale temporary run overlays.

Historical runs must remain inspectable even after the active project index changes. That means:

- evidence records store the retrieved snippet, not only provider ids
- source versions are immutable
- local snapshots are retained while any evidence record references them
- provider files/vector-store files are retained while needed for historical replay, or the run is explicitly marked as no longer provider-replayable but still inspectable from saved snippets and local snapshots
- a run corpus manifest records which source versions and provider files were eligible at generation time

## Prompting and Tool Policy

Prompts should stop saying or implying:

```text
Use OpenAI file search for discovery, then resolve everything to local chunks.
```

The better rule is:

```text
Use the corpus tools to retrieve evidence. Final factual claims must cite saved evidence ids.
Evidence may be provider-native or local fallback. Source-level references are not enough for citation-sensitive claims.
```

Tool policy should expose:

- broad provider-first search to triage and research
- limited targeted reads to treatment when needed
- no broad search to writers by default
- controlled evidence search to the fact verifier
- local fallback only through the same corpus API, marked as fallback in evidence

## UI and Debugging

The UI should show:

- source map
- source cards with evidence ids
- quote cards with evidence ids
- tool traces
- evidence snippets behind each citation
- whether evidence is exact, provider snippet, source level, or weak
- provider/local retrieval channel
- fact gate failures by claim

This is how V2 avoids becoming a black box while still letting the model use provider-native reading.

## Evals

Before replacing the current hybrid path, run an eval across real projects.

Compare:

1. current local/hybrid V2
2. OpenAI-first with provider evidence
3. local-only fallback

Measure:

- source selection quality
- quote usefulness
- citation support correctness
- hallucinated citations
- treatment quality
- act-level listenability
- long-form repetition
- cost
- latency
- failure rate by source type

The goal is not to prove OpenAI always wins. The goal is to make the default path the one that produces better episodes more often, while keeping explicit fallback paths for known failure cases.

## Migration Plan

### Phase 1. Evidence Objects

- Add first-class evidence records.
- Persist provider search results and annotations.
- Make source cards, quote cards, atoms, and citations reference evidence ids.
- Add controlled evidence tools for search/read/quote discovery.

### Phase 2. Persistent Project Index

- Add project-level provider corpus indexes.
- Upload source versions once.
- Reuse the index across runs.
- Replace per-run full upload as the default.

### Phase 3. Provider-First Corpus Tools

- Replace direct local chunk search in V2 agents with a single corpus API.
- Make that API provider-first.
- Keep local search as fallback behind the same API.

### Phase 4. Agent Flow Update

- Add corpus triage/source-map phase or make it an explicit research-agent subphase.
- Build quote cards from evidence, not from first chunks.
- Build act dossiers from evidence and atoms.

### Phase 5. Fact Gate Update

- Require evidence ids for citation-sensitive claims.
- Validate evidence structure deterministically.
- Add an LLM support judge constrained to saved evidence.
- Store fact gate decisions as artifacts.

### Phase 6. UI and Evals

- Expose evidence and tool traces in the debug UI.
- Add eval fixtures for provider-first vs hybrid retrieval.
- Promote OpenAI-first to default only after evals show better or equal citation reliability and better editorial quality.

## Alignment With V2 Vision

This design keeps the V2 editorial architecture intact.

- LLMs remain capable agents, because they can search and inspect the corpus through provider-native tools.
- Deterministic infrastructure still owns gates, but the gate is over saved evidence records rather than local chunks.
- Editorial-first remains the core flow: source map, research memo, treatment, dossiers, act drafts, then citation/fact pass.
- Every editorial move becomes anchorable through evidence ids.
- Long-form memory remains in the continuity ledger and act dossiers.
- Provider capabilities are abstracted behind one corpus API instead of leaking two competing stores into prompts.
- Native source understanding becomes first-class because provider reading is the default, not an optional side channel.

## Non-Goals

- Do not keep local chunks as a mandatory confirmation layer for provider evidence.
- Do not upload the full corpus again for every run.
- Do not expose two equivalent corpus tools to the model and ask it to choose.
- Do not accept final citations that only contain source ids with no saved evidence.
- Do not remove local ingestion entirely; keep it where it improves locator quality, fallback behavior, transcripts, snapshots, and evals.

## Summary

The target V2 corpus architecture is not "local corpus plus OpenAI corpus."

It is:

```text
local source registry + persistent provider read index + first-class evidence store
```

OpenAI-first retrieval should be the normal model-facing path. Local chunks should become fallback and instrumentation, not the default citation substrate. The hard requirement is not local confirmation; the hard requirement is saved, inspectable evidence for every important editorial and factual move.
