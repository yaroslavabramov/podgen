# Platform and reliable execution

The current system is a Python backend. Domain contracts are defined with Pydantic. PostgreSQL stores versioned state and operational records; the object-store interface retains immutable payloads, source bytes and media. Evaluation and episode commands exercise the same application services. The original deployment design also describes an HTTP API, a TypeScript client, a workflow engine and observability. The CLI does not require a deployed web application.

## Canonical storage

A stable artifact identity groups immutable revisions. The project and actor determine access to them. Canonical hashing must specify UTF-8 serialization, property order, number representation and absent versus null fields across languages. The [shared contracts and golden vectors](../../contracts/) cover the Python/TypeScript boundary. A blob reference includes a digest, type and byte length. An object must be published and readable before committed records can resolve it.

[Blob publication](blob-publication.md) defines the object-store handoff. [Document publication](document-publication.md) keeps outcomes and dependencies of a bounded operation together. Persist full research reports and scripts. Store derived previews separately with their own coverage.

## Manifests, admission and delivery

A task manifest declares its initial authorized inputs, model, tools, schema and purpose. When research discovers a new document, record the chain that admits it and derive the next manifest from the previous one. Access is checked again immediately before the external request. A tool result on disk becomes a model-visible receipt only when it actually enters the dispatched request.

```text
InputAdmissionRecord = {
  admission_id, project_id, task_run_id,
  parent_manifest_id, next_manifest_id,
  admitted_refs: ArtifactRef[], admitted_runtime_refs: RuntimeRef[],
  rejected_candidates: [{locator, reason_code}],
  origin: tool_result | editor_handoff | user_input,
  origin_id, policy_ref, scope_decision, created_at
}
ReadReceipt = {
  receipt_id, project_id, task_run_id, invocation_id, tool_call_id|null,
  manifest_id, artifact_ref, representation_ref|null,
  preview_ref|null, derived_from_ref|null,
  requested_selector, returned_selectors[],
  display_kind: preview | exact_text | rendered_page | audio,
  returned_content_hash, has_more, continuation|null
}
```

Each model request uses the role's compact projection. Technical traces can retain complete envelopes, transport receipts and intermediate tool results for recovery without transmitting that history to the listener, writer or reviewer.

## Task and provider boundaries

The coordinator owns adoption of results and the current workflow head. Tasks have stable identities and explicit base revisions. A following worker can finish a recorded result after a crash; its attempt must not accidentally submit the same paid operation twice. An unknown submission outcome remains distinct from a rejection or a completed charge. A concurrent access withdrawal or cancellation requires a fresh decision before sending anything else.

```text
ToolResult<T> =
  {ok:true, data:T, warnings:Warning[], continuation:null|{cursor:string}}
  | {ok:false, error:ToolError}
ToolError = {
  code:string, message:string, retryable:boolean,
  recoverable_actions:[{action, explanation}], details_ref:null|ArtifactRef
}
Warning = {code, message, affected_refs:ArtifactRef[]}
```

```text
ModelProfile.payload = {
  provider, api_family, model_id, model_revision_or_alias,
  adapter_version,
  allowed_roles[], supported_modalities[], context_limit, output_limit,
  structured_output_mode, tool_call_mode, parallel_tool_policy,
  generation_parameters, timeout_policy,
  data_retention_policy_ref, caching_policy, fallback_profile_refs[],
  configured_languages[]
}
CapabilityReport.payload = {
  profile_ref, tested_configuration_fingerprint, adapter_version,
  checks: [{capability, expected, observed, outcome, diagnostic_refs[]}],
  observed_limits, probe_run_ids:UUID[], probe_invocation_ids:UUID[], limitations[]
}
ModelInvocation = {
  invocation_id, task_run_id, step_id, manifest_id,
  profile_ref, prompt_ref, toolset_ref,
  context_assembly_ref: RuntimeRef|null,
  request_blob_ref, request_hash, provider_request_id|null,
  response_blob_ref|null, usage, status, failure,
  replaces_invocation_id|null, started_at, completed_at|null
}
```

The [private execution journal](private-execution-journal-v3.md) is the implementation reference for intents, counted requests, provider receipts, tool returns and charge changes. The [budget contract](input-budget-recovery.md) separates provider count receipts, reservations, actual token/cache charges and the authorization to increase a refused limit. [Technical recovery](technical-recovery.md) describes eligible operator actions.

## Invalidation and reuse

A new document, corrected script or changed policy affects specific dependents. Preserve historical lineage separately from active semantic dependencies. The subject owner decides how a replacement changes the material. Runtime access rules and publication eligibility still apply even when an editorial dependency is removed.

```text
DependencyRevalidationEvent = {
  event_id, subject_ref:ArtifactRef, assessment_ref:ArtifactRef,
  authority:{kind:acceptance_record|integration_event, record_id:UUID},
  dispositions:DependencyDisposition[],
  expected_validity_version, policy_ref, created_at
}
```

A search index can share immutable computations by content and configuration. Its project memberships and authorization must remain independent. Batch reads inside a bounded operation; preserve fresh authorization at public reads and external actions. [Reusable source index](reusable-source-index.md) and [full-operation persistence](full-operation-persistence.md) specify the boundaries.

## Deployment and acceptance

Measure a complete episode or representative saved operation, including database queries, bytes transferred, peak memory, provider work and recovery. Measure growth with document count and history. A successful unit test for one query does not establish the cost of a fact-checker walking an entire corpus.

Qualify cancellation during a request, a response recorded after an executor dies, repeated delivery of an operation, revision during assembly, object-store failure, and deletion followed by restore. Pin workflow and profile versions across upgrades. The target UI exposes topic, progress, revision, playback and export through the same authorization and mutation paths as other clients.
