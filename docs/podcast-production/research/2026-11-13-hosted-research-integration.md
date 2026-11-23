# Running and retaining a hosted research job

This implementation note covers the first stage of the [research design](2026-11-13-research-engine.md). `podgen.research.hosted` prepares one Gemini Deep Research job through the Interactions API. `podgen.sources` acquires separate versions of the documents linked from the resulting report. Both use the project's existing repository, object store and access controls.

## Assignment and provider profile

`ResearchConfig` contains the run ID, full assignment, English instructions and provider profile. The assignment supplies `user_request`, `output_language` and `scope.source_mode=open_web`. A supplied-only assignment cannot start open-web research. An optional `episode_spec_ref` binds the exact EpisodeSpec. The manifest stores the name of the credential variable, while the value is supplied at execution.

The November longitude trial used `deep-research-preview-04-2026`, REST `v1beta/interactions`, API revision `2026-05-20`, background execution and provider-side storage. The actual submitted input was compared byte for byte with the prepared request. These identifiers describe that recorded run.

## CLI

In an installed backend environment, supply an existing project, actor, object-store root and `PODGEN_DATABASE_URL`. Use the same identifiers across steps. [Backend setup](../../../backend/README.md)

```sh
python -m podgen.hosted_research_cli \
  --project PROJECT_UUID --actor ACTOR_UUID --run-id RUN_UUID \
  --objects OBJECT_STORE --env-file .env \
  prepare --config research.json --export-dir EXPORT
```

`prepare` stores the configuration, complete request and hash locally. Commands with the same global arguments include the following.

```text
approve --request-hash HASH --reason REASON --accept-variable-provider-cost --export-dir EXPORT
resume --export-dir EXPORT
export --export-dir EXPORT
cancel --export-dir EXPORT
reprocess --export-dir EXPORT
acquire-sources --source-config sources.json --export-dir EXPORT
```

`approve` authorizes one submission of the exact request at variable provider cost. `resume` then submits it or advances the known job. Each invocation can be interrupted between provider exchanges. `cancel` addresses the known job. `reprocess` parses the archived completed response without network access. Run `export` to inspect status, manifest, complete request and available results. The provider controls its internal research budget. [Provider documentation](https://ai.google.dev/gemini-api/docs/deep-research)

## Recovery and accounting

The journal records an intent before POST and retains the complete received response and HTTP metadata. If submission may have reached the provider and the job ID is unknown, the state becomes `outcome_unknown`. A failed GET leaves the known job intact for the next resume. The runtime distinguishes failed, cancelled, incomplete, action-required and continuation-required responses. An unparsable completed job retains the raw response and `result_processing_required` for local recovery.

Migration 0032 introduced a `hosted-research-head/1` row per project, run and immutable manifest. It tracks the latest exchange, latest valid job snapshot, submission count and whether the job was saved. The intent and head update commit together. The first valid receipt, identified job and head likewise commit together. The run lock serializes actions and the revision guards against stale writes. Polling checks a bounded set of currently authorized references. Full history is traversed on export.

A failed poll cannot replace the last valid snapshot. A changed provider job ID preserves its receipt and marks the run `needs_attention`. If a legacy journal has no head, rebuild it under the run lock. A saved legacy receipt can recover a missing job without another submission. The ability to rebuild this derived state for a read does not confer authority to submit.

## What a reported citation means

Export includes `report.md`, `reported-sources.json`, `result.json` and raw provider responses. All text blocks in `model_output` are retained in order. The mapping records each block's provider path and its range in the joined report. Thoughts and tool events remain in the raw response.

Provider-native annotation offsets are stored verbatim as `provider_native_unvalidated`. For the longitude trial the first text block contained 16,237 bytes, while one annotation ended at 57,024. The next block contained 57,560 bytes and no annotations. These values do not establish an offset unit or origin. Interpret the old `provider_native_bytes_within_content_block` label as unverified. Exact citation bindings must be established separately.

Links from annotations and report text begin as `reported_only`. The external provider may have consulted other material. A PDF, page or archive capture fetched later records what Podgen actually obtained.

## Source acquisition

A `SourceSelection` file supplies an operation ID, extraction profile, rights and an optional list of reported URLs. The default selects every distinct reported URL. Each completed document saves its source occurrences, acquisition result, `SourceRevision` and `SourceRepresentation`. Resume continues the selection from those completed items.

The referenced `ProcessingProfile` has kind `extraction`, implementation `saved-source-acquisition/1`, version `1` and an `AcquisitionProfile` configuration. Rights include the access class and real storage and publication policy references. Fallbacks depend on that pinned profile. Text is extracted from the saved original. The acquisition result distinguishes unavailable documents, service pages and extraction failures. [Source acquisition](../source-acquisition-runtime.md)

Transport tests exercise approval hashes, uncertain submits, poll failures, receipt recovery, multiblock reports, acquisition and export. Assess research quality by reading the full report, following its strongest claims to the source, and comparing the separate planner and editor decisions. [Corpus runtime](../research-corpus-runtime.md)
