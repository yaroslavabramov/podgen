# System contract

These are the design contracts for a publication-quality episode. The [episode coordinator](episode-production.md) implements a subset through research, independent script readings and audio assembly. The numbered specifications also cover publication, withdrawal and human approval.

## Ownership and identity

The listener supplies a topic and the production constraints. An editorial owner integrates research and writing decisions, including the central question, the working answer and the selected narrative. This owner is persisted state with an explicit update operation. Research, writing, verification and audio may use different workers and providers. A worker returns a candidate to the owner rather than modifying the accepted story in place.

```text
ArtifactRef = {artifact_id: UUID, revision_id: UUID, kind: ArtifactKind}
RuntimeRef = {record_kind: RuntimeRecordKind, record_id: UUID}
BlobRef = {storage_key: string, sha256: string, byte_length: integer, media_type: string}
ProducerRef =
  {kind: model, invocation_id: UUID, profile_ref: ArtifactRef}
  | {kind: code, component: string, build_id: string, operation_id: UUID}
  | {kind: user, actor_id: UUID, command_id: UUID}
  | {kind: human_review, review_session_id: UUID, rater_id: UUID}
ArtifactRevision = {
  ref: ArtifactRef, schema_version: string,
  project_id: UUID, episode_id: UUID|null,
  input_refs: ArtifactRef[], payload: object,
  content_hash: string, producer: ProducerRef, created_at: Timestamp
}
```

Every artifact reference resolves to one exact revision and kind in an authorized project. A runtime record, search hit or model call has an operational identity. It can document provenance without becoming a domain artifact. See the [canonical representation](06-platform-and-runtime.md#canonical-storage).

### Episode specification

```text
EpisodeSpec.payload = {
  topic: string, main_question: string|null,
  audience: {description: string, assumed_knowledge: string[]},
  language: string,
  format: solo|dialogue,
  speakers: [{speaker_id: UUID, display_name: string,
              role_description: string, voice_profile_ref: ArtifactRef}],
  duration: {target_seconds: integer, tolerance_seconds: integer, mode: target|strict},
  temporal: {mode: current|historical_as_known|retrospective,
             requested_as_of: Timestamp|null, knowledge_as_of: Timestamp,
             subject_period: {from: string|null, through: string|null}|null},
  source_mode: open_web|supplied_only,
  supplied_inputs: [{input_id: UUID, origin: SuppliedInput}],
  preferences: string[], required_topics: string[], excluded_topics: string[],
  policy_ref: ArtifactRef, source_policy_ref: ArtifactRef,
  delivery: {audio_formats: (mp3|wav)[], transcript_formats: (json|vtt)[]}
}
SuppliedInput = {kind: url, url: string}
  | {kind: upload, upload_id: UUID}
  | {kind: source_revision, source_revision_ref: ArtifactRef}
```

The edition carries both the requested language and the language of source material. A time-sensitive claim can depend on the period being described, the date of observation, or the date the underlying material became available. Keep these distinct so an episode can reconstruct what a historical actor could know.

## Data and trust boundaries

- Preserve the original response and complete semantic fields. A size limit returns a validation error. A derived preview has its own source reference, range and coverage.
- Keep the original document, its extracted representation, an exact span, an interpretation, a script utterance and its recording independently addressable.
- Extract checkable claims from the script the listener will hear. A quote that matches the source still requires a judgment about scope, context and independence.
- Treat documents and earlier model outputs as untrusted input. Project rights control reading, model delivery, publication and deletion.
- Archive the request, tools, model profile, inputs and response for a model action. A read becomes evidence of model delivery only when the selected passage actually reaches the model.
- History and qualification have different lifetimes. A revised artifact retains its parent for traceability. Its current eligibility depends on the new evidence and checks.

## Inputs, profiles and qualification

The input manifest identifies the material available to a task. A discovery during the task enters through a new admitted manifest. A worker's ability to name an artifact does not grant access. A shared configuration catalog exposes reviewed configuration kinds across authorized projects. It cannot make another project's research, voice recordings or evaluation inputs readable.

```text
InputManifest = {
  manifest_id: UUID, parent_manifest_id: UUID|null,
  project_id: UUID, task_id: UUID,
  revision_refs: ArtifactRef[], runtime_refs: RuntimeRef[],
  source_scope: SourceScope,
  policy_ref: ArtifactRef, prompt_ref: ArtifactRef,
  toolset_ref: ArtifactRef, model_profile_ref: ArtifactRef
}
SourceScope = {
  mode: open_web|supplied_only, source_policy_ref: ArtifactRef,
  authorized_input_ids: UUID[], authorized_source_refs: ArtifactRef[],
  network_rule_id: string|null, external_processing_rule_id: string
}
TaskSpec = {
  task_id: UUID, role: string, objective: string, why_it_matters: string,
  input_manifest_id: UUID, acceptance_questions: string[],
  allowed_actions: string[], output_kind: ArtifactKind,
  unresolved_context: string[],
  operational_limits: {max_model_calls: integer, max_tool_calls: integer,
                       max_elapsed_seconds: integer}
}
TaskResult = {
  task_id: UUID,
  status: answered|partial|needs_material|unanswerable|failed,
  output_refs: ArtifactRef[], remaining_questions: string[],
  next_action: string|null, failure: Failure|null
}
Failure = {code: string, message: string, retryable: boolean,
           details_ref: ArtifactRef|null}
TaskResultRecord = {result_id: UUID, run_id: UUID, payload: TaskResult}
```

Acceptance binds the subject, kind of review, reviewer or policy, and exact inputs. Profile qualification describes a tested operating range such as a language, format, duration and provider version. Evaluation may exercise a candidate before it qualifies for production.

```text
AcceptanceRecord = {
  record_id: UUID, subject_ref: ArtifactRef,
  check_kind: factual|editorial|audio|show_notes|profile_qualification,
  decision: accepted|changes_required|rejected,
  policy_ref: ArtifactRef, reviewed_input_refs: ArtifactRef[],
  assessment_scope: QualificationScope|null,
  findings: [{report_ref: ArtifactRef, finding_id: UUID}],
  reviewer_ref: ProducerRef, created_at: Timestamp
}
QualificationScope = {
  supported_cells: [{roles: string[], language: string, format: solo|dialogue,
    duration_interval: {min_seconds: integer, max_seconds: integer},
    companion_profile_refs: ArtifactRef[]}]
}
RevocationRecord = {
  revocation_id: UUID, acceptance_record_id: UUID,
  reason_code: string, basis_refs: ArtifactRef[],
  reviewer_ref: ProducerRef, created_at: Timestamp
}
```

## Revisions and dependency changes

Assess each affected dependency against a specific revision. A change to an original may invalidate a current interpretation while preserving the historical trace. The subject owner records whether the new version still uses the evidence, replaces it or removes the claim. Code checks references and coverage. A reviewer decides whether the new interpretation is sound. Access restrictions continue to apply independently of editorial acceptance.

```text
DependencyDisposition = {
  prior_input_ref: ArtifactRef, current_input_refs: ArtifactRef[],
  disposition: retained|replaced|removed|lineage_only,
  subject_selectors: SubjectSelector[], basis: string
}
DependencyRevalidationEvent = {
  event_id: UUID, subject_ref: ArtifactRef, assessment_ref: ArtifactRef,
  authority: {kind: acceptance_record|integration_event, record_id: UUID},
  dispositions: DependencyDisposition[], expected_validity_version: integer,
  policy_ref: ArtifactRef, created_at: Timestamp
}
```

## Run and release

One episode may have many attempts, alternative drafts and rejected edits. An adopted operation has a single owner and an idempotent commit. A technical stop records the incomplete operation and preserves everything received so far.

```text
EpisodeRun = {
  episode_run_id: UUID, project_id: UUID, episode_id: UUID,
  episode_spec_ref: ArtifactRef, purpose: production|evaluation,
  evaluation_case_ref: ArtifactRef|null, generation_epoch: integer,
  status: queued|running|waiting|needs_attention|cancel_requested|cancelled|succeeded|failed,
  phase: orienting|researching|planning|drafting|editing|verifying|producing_audio|ready|null,
  editorial_state_ref: ArtifactRef, waiting_reason: string|null,
  failure: Failure|null, checkpoint_refs: ArtifactRef[],
  result_release_ref: ArtifactRef|null, created_at: Timestamp,
  started_at: Timestamp|null, ended_at: Timestamp|null
}
```

A release names an immutable set of validated artifacts. The verifier checks that the chosen script appears in the performance plan, the master contains the rendering of that script, time references resolve into the same master, and the show notes and rights apply to those exact versions. A new issue can prevent current publication while preserving an earlier release as history.

```text
ReleaseManifest.payload = {
  purpose: production|evaluation, evaluation_case_ref: ArtifactRef|null,
  episode_spec_ref: ArtifactRef, dossier_ref: ArtifactRef,
  story_plan_ref: ArtifactRef, script_ref: ArtifactRef,
  verification_report_ref: ArtifactRef, editorial_review_ref: ArtifactRef,
  performance_plan_ref: ArtifactRef, audio_master_ref: ArtifactRef,
  audio_qa_ref: ArtifactRef, transcript_ref: ArtifactRef,
  show_notes_ref: ArtifactRef, show_notes_review_ref: ArtifactRef,
  policy_ref: ArtifactRef
}
```

Module responsibilities and release tests are in the [implementation plan](07-implementation-plan.md).
