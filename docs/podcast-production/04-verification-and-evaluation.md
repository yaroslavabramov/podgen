# Verification, release and evaluation

Verification answers two different questions about a particular script. Can the sourced account support what the listener hears? Does the spoken story make sense and hold attention? The current writing workflow commissions independent factual and editorial readings. The target release design also requires listening, metadata review and a rights decision for the final bundle.

## Factual review

Extract the consequential claims from the actual script. Include quantitative statements, causal links, attributions and chronology. A scene can imply that someone witnessed an event without saying so explicitly. Read enough adjacent speech to identify that implication. A fact in the dossier that the writer left out does not need a verdict in the episode.

**Claim set**

```text
script_ref: ArtifactRef
claims: Claim[]
coverage: CoverageUnit[]
extraction_profile_ref: ArtifactRef
unresolved_extraction_questions: [{selector: ScriptSelector, question}]

Claim = {
  claim_id, statement, occurrences: ScriptSelector[],
  kind: factual | quantitative | causal | historical | quotation | attribution | prediction,
  qualifiers: {entities[], time_scope, geographic_scope, conditions[], modality, attribution|null},
  importance: central | supporting | incidental,
  candidate_evidence_refs: ArtifactRef[], related_claim_ids: UUID[]
}
CoverageUnit = {
  selector: ScriptSelector,
  classification: assertion | nonfactual,
  claim_ids: UUID[], exclusion_reason: string|null
}
```

**Verification report**

```text
script_ref: ArtifactRef
claim_set_ref: ArtifactRef
coverage_audit: {
  reviewer_run_ids: UUID[], reviewed_script_ranges: ScriptSelector[],
  findings: Finding[], unresolved_omissions: ScriptSelector[]
}
claim_reviews: ClaimReview[]
cross_claim_findings: Finding[]
source_family_assessment_refs: ArtifactRef[]
calculation_refs: ArtifactRef[]
policy_ref: ArtifactRef
recommendation: accepted | changes_required | rejected
remaining_work: [{finding_id, proposed_action, research_request_ref|null}]

ClaimReview = {
  claim_id,
  verdict: supported | supported_with_limits | contradicted | insufficient | not_verifiable,
  evidence_reviews: [{evidence_ref, relation: supports|qualifies|contradicts,
                     source_applicability, original_inspection, rationale}],
  counterevidence_refs: ArtifactRef[], calculation_refs: ArtifactRef[],
  family_assessment_refs: ArtifactRef[],
  stated_limits: string[], audible_limit_selectors: ScriptSelector[],
  findings: Finding[], rationale, reused_from_report_ref: ArtifactRef|null
}
Finding = {
  finding_id, category, location_selectors: object[], explanation,
  basis_refs: ArtifactRef[], severity: critical|major|minor|advisory,
  blocking: boolean, policy_rule_id, required_outcome
}
```

The checker reads the retained passage or original and records the relevant receipt, scope, applicable time and the effect on the episode. A copied assertion can have two bibliographic entries and one independent source. For a disputed mechanism, distinguish a participant’s stated hypothesis, later corroboration and a demonstrated causal effect. Treat an unavailable page as a limit on verification. A search result cannot establish its full contents.

For a consequential calculation, name the input values, units, denominator, formula and exact sources. Recompute the result independently of the prose. The check is bound to the revision and the applicable time period.

```text
purpose: string
inputs: [{name, value_decimal, unit, evidence_refs: ArtifactRef[], source_selector}]
expression: RestrictedExpression
execution_profile_ref: ArtifactRef
rounding: {precision, mode, display_scale}
result: {value_decimal, unit, display_text}|null
status: computed | failed
error: {code, message}|null
limitations: string[]
```

## Editorial reading and recheck

The first reader receives the spoken script for a cold reading. Record the first place where the listener loses orientation, the actual words that cause it, and what can still be inferred from the surrounding passage. A later retrospective reading can locate the reason. Keep the distinction: seeing the intended answer can make an unexplained transition appear obvious.

```text
episode_spec_ref: ArtifactRef
story_plan_ref: ArtifactRef
script_ref: ArtifactRef
listener_state_ref: ArtifactRef
policy_ref: ArtifactRef
first_read_ref: RuntimeRef                    # finalized ToolExecution(record_first_read)
spoken_view_hash: string
dimensions: [{dimension, assessment, evidence_selectors: ScriptSelector[], rationale}]
findings: Finding[]
listener_answer: {main_answer, causal_chain[], important_limits[], unanswered_promises[]}
revision_comparison: {prior_script_ref, target_finding_ids[],
  repaired[], preserved_strengths[], regressions[], preferred_action}|null
recommendation: accepted | changes_required | rejected
```

```text
FirstBreak = {observation_mode: prefix|retrospective, through: ScriptSelector,
  at: ScriptSelector, last_clear_at: ScriptSelector|null,
  understood_so_far: string, missing_link: string}
BreakFollowUp = {prefix_observation_ref: RuntimeRef,
  resolution: explained_later|unresolved, resolution_at: ScriptSelector|null,
  downstream_effect: string, scope: utterance|scene|act|episode}
```

A mandatory finding names a defect that warrants another draft. An equally good rearrangement or optional piece of polish can remain optional. For factual repairs, request a concrete, source-supported result such as narrowing the population, correcting the attribution or deleting the unsupported inference. Use a qualification when a remaining assertion would otherwise mislead the listener.

The recheck records the disposition of prior evidence against this script.

```text
dependency_dispositions: [{
  prior_input_ref: ArtifactRef, current_input_refs: ArtifactRef[],
  affected_selectors: ScriptSelector[],
  disposition: retained | replaced | removed | lineage_only,
  basis, checked_against_script_ref: ArtifactRef, reviewer_run_id: UUID
}]
```

After revision, both reviewers assess the same new script. Recheck changed assertions and any later conclusion that depends on them. Keep evidence and dispositions for unchanged decisions through the [reading audit](reading-audit-contract.md). A wrong outline, lost provider response and incomplete source investigation have different remedies. Keep their terminal states distinguishable.

## Release and accompanying material

The script, master, transcript, chapters and show notes need exact references. The title and description can introduce claims absent from the script. Review those claims before publication. A chapter time comes from the accepted audio timeline. Store every assessment against the subject it actually examined.

```text
ShowNotes.payload = {
  script_ref, transcript_ref, audio_master_ref,
  title, description,
  chapters: [{chapter_id, title, summary, start_frame, script_selectors[]}],
  citations: [{citation_id, source_revision_ref, evidence_refs[],
              display_metadata, public_locator|null, supported_script_selectors[]}],
  disclosures: [{kind, text}], uncertainty_notes: [{text, script_selectors[]}]
}
ShowNotesSelector = {show_notes_ref, field, item_id|null, char_start, char_end}
ShowNotesReview.payload = {
  show_notes_ref, script_ref, verification_report_ref, policy_ref,
  reviewed_fields: ShowNotesSelector[],
  content_checks: [{statement, selectors: ShowNotesSelector[],
                   evidence_refs[], calculation_refs[], verdict, rationale}],
  dependency_dispositions: DependencyDisposition[],
  findings: Finding[], recommendation: accepted|changes_required|rejected
}
```

## Evaluation design

Separate development cases from held-out cases. Protect annotations, known errors and listening labels from the generation path, including search and tools. Pin the case, source corpus, models, behavior, profiles and evaluator for every comparison. Record failed and stopped runs, actual provider usage and human judgments alongside successful episodes.

```text
EvaluationCase.payload = {
  case_family, split: development|calibration|holdout, topic_group_id,
  mode: component|episode|failure_recovery,
  episode_spec_ref, input_refs[], source_access_policy_ref,
  reference_annotation_ref, scoring_policy_ref, allowed_candidate_profile_refs[],
  supported_combination: {language, duration_seconds, format},
  perturbations[], prohibited_shortcuts[]
}
EvaluationResult.payload = {
  case_ref, candidate_profile_refs[], repeat_index,
  output_refs[], run_ids: UUID[], evaluator_profile_refs[],
  measurements[], human_assessment_refs[], findings[],
  outcome: pass|fail|inconclusive, exclusions_with_reasons[],
  started_at, completed_at
}
```

Assess competing writers on the same dossier and plan; assess whole systems on identical source access from the original brief. Include one strong model with simple tools as the baseline. Compare complete episodes blind when possible. Use a common audio treatment for a writing comparison so an attractive voice cannot hide a weak script. Sample multiple topics, formats, lengths and independent runs before claiming a general improvement.

Private reference annotations capture what the evidence supports and the range of acceptable explanations. Human assessments bind the actual artifacts, blind assignment, reasons, listened ranges and any missing observation.

```text
EvaluationAnnotation.payload = {
  episode_spec_ref: ArtifactRef, source_snapshot_refs: ArtifactRef[],
  source_assertions: [{annotation_id: UUID, statement: string,
    evidence_refs: ArtifactRef[], required_qualifiers: string[],
    importance: central|supporting|incidental}],
  expected_understanding: [{question: string, answer_criteria: string[],
    acceptable_alternatives: string[], basis_refs: ArtifactRef[]}],
  forbidden_inferences: [{statement: string, reason: string, basis_refs: ArtifactRef[]}],
  known_uncertainties: [{description: string, basis_refs: ArtifactRef[]}],
  scoring_policy_ref: ArtifactRef, annotator_assignment_ids: UUID[]
}
HumanAssessment.payload = {
  case_ref: ArtifactRef, evaluated_output_refs: ArtifactRef[], rubric_ref: ArtifactRef,
  assignment: {assignment_id: UUID, anonymous_rater_id: string,
    role: factchecker|editor|listener, blind_candidate_ids: string[],
    presentation_order: string[], task: source_audit|editorial_review|
      full_comprehension|voluntary_interest|pairwise_preference},
  ratings: [{dimension: string, score: integer|null, abstain_reason: string|null,
    rationale: string, evidence_selectors: SubjectSelector[]}],
  fact_checks: [{statement: string, location: SubjectSelector,
    verdict: ClaimReview.verdict, evidence_refs: ArtifactRef[], rationale: string}],
  comprehension: {free_answer: string, stated_limits: string[], transfer_answer: string}|null,
  audio_coverage: {subject: {kind: master, audio_master_ref: ArtifactRef}
      | {kind: evaluation_reading, recording_ref: RuntimeRef, script_ref: ArtifactRef,
         spoken_view_hash: string}, sample_rate_hz: integer, sample_frames: integer,
    listened_ranges: [{start_frame: integer, end_frame: integer}],
    completion: full|partial, omitted_ranges_with_reasons: [{start_frame,end_frame,reason}]}|null,
  voluntary_observation: {outcome: not_started|completed|stopped_content|
      stopped_external|technical_failure|observation_end|lost_followup|unknown_reason,
    last_frame: integer|null,
    location: ScriptSelector|null, stated_reason: string|null,
    prior_topic_interest: string|null, prior_familiarity: string|null}|null,
  pairwise_preference: {winner_blind_id: string|null, outcome: preferred|tie|abstain,
    rationale: string}|null,
  completed_at: Timestamp
}
```

Calibrate factual and editorial reviewers against human readings. Include known consequential mistakes, harmless paraphrases, unsupported implications and passages that genuinely need qualification. A model's `keep` is one observation. A useful decision also depends on the severity of real misses, unnecessary features or corrections, and the listening outcome. See the [acceptance matrix](07-implementation-plan.md#end-to-end-acceptance).
