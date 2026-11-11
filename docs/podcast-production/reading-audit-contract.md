# Contextual reading audit — reading-audit/1

This contract is additive to the current reader/fact reports. It changes neither author work, planning, models, role order nor revision limits. Complete native outputs remain immutable reports. It does not establish factual truth or production readiness.

New-run pins: `reading_audit_contract=reading-audit/1`, `reader_review_schema_version=listener-experience/14`, `fact_review_schema_version=fact-revision/5`, `revision_verification_contract=editorial-recheck/3`. Historical pins retain their exact schemas and requests. A legacy continuation inherits its legacy pins; it cannot fabricate checked decisions.

## Output fields

Existing `listener_answer`, `findings`, `research_requests`, `revision_scope`, `strengths`, `recommendation` and other report fields remain. Audit-only fields never become author tasks.

Both roles add:

- `audit`: role-specific complete report, described below.
- `finding_links[]`: `{finding_index, decision_ids[]}` connects every finding to the current audit's actual contextual assessment.
- `assignment_results[]`: the existing exact `{assignment_id, outcome, judgment, followup_finding}` contract. Empty on the initial audit; subsequent reviews account for every actual writer task. Open required work remains incompatible with `keep` and optional-only follow-up.
- `previous_dispositions[]`: `{previous_decision_id, disposition: retained|reassessed|retired, current_decision_ids[], context_assessment, reason}`. Every previous own-role decision is accounted for exactly once. Retained/reassessed decisions link current decisions. A retired decision explicitly explains why it no longer applies. Reuse requires the agent's assessment of meaning, context and evidence; unchanged bytes are insufficient.
- `late_findings[]`: `{finding_index, reason_kind, previous_decision_ids[], previous_coverage_indices[], assignment_ids[], current_decision_ids[], grounds, substantive_consequence}`. One entry for each later mandatory finding. `reason_kind` is `unresolved_assignment`, `introduced_or_regression`, `context_or_evidence_changed`, or `substantive_missed_issue`. References must resolve to the exact bound prior audit, current decisions or writer tasks. Grounds and consequence are actual prose, not just an enum. Real substantive omissions remain reportable. The server validates structure and links, not whether the judgment is true.
- `effect_on_episode`: null on the initial audit; on reread, an assessment of the edit's effect on the whole episode.

### Fact audit

`audit = {coverage[], areas[], claim_checks[], overall_judgment}`.

`claim_checks[]` entries contain `{decision_id, anchors[], contextual_meaning, context_scope, claim_types[], verdict, source_refs[], rationale, memory_summary}`. `claim_types` distinguishes `factual`, `causal`, `chronological`, and `generalization`. `verdict` is `supported`, `contradicted`, `unsupported`, or `unchecked`. Scope is the claim listeners hear in this account; a possible isolated literal interpretation is not silently substituted for it.

`source_refs[] = {from_review: current|previous, evidence_id}`. Current packet evidence receives simple aliases `B1…` mapped to exact fields actually delivered to the fact checker; current successful source reads retain `E1…`. Prior retained evidence receives `H1…`, with its original identifier and full frozen source text available in the memory view. Thus prior `E1` and current `E1` cannot collide. Persisted evidence bindings retain original review/invocation/request refs, source selectors and source identities; aliases never replace that provenance. Every reference is resolved against actual supplied evidence or successful archived reads. Read receipts retain the physical request, invocation and provider response alongside the semantic root request. A mandatory finding citing retained `H` evidence resolves to the canonical frozen source locator, so the author receives the full existing passage without another source-tool call.

### Reader audit

`audit = {coverage[], areas[], judgments[], overall_judgment}`.

`judgments[]` entries contain `{decision_id, anchors[], contextual_meaning, context_scope, aspects[], verdict, rationale, memory_summary}`. `verdict` is `acceptable`, `needs_change`, or `unchecked`. `aspects` covers `story_development`, `clarity`, `transitions`, `repetition`, `ending`, `voice`, and `idiom`. This is a judgment about the listening experience, not a plot summary. The reader receives no fact-source materials or fact audit history.

### Coverage and unchecked work

`coverage[] = {first_utterance_index, last_utterance_index, status, decision_ids[], explanation}`. Ordered contiguous ranges must cover every current utterance exactly once, including explicit unchecked gaps. Decision identifiers use `D1`, `D2`, and so on. `areas[] = {aspect, status, decision_ids[], explanation}` accounts for each role-specific aspect exactly once; a reviewed area must reference a checked decision of that same kind. Fact areas are `factual_accuracy`, `causality`, `chronology`, `generalization`; reader areas are the seven aspects above.

`reviewed` names the decisions that record the work performed. `unchecked` means a range/aspect or extracted claim was not evaluated sufficiently to return a supported substantive verdict; its explanation states what remains unknown or was not done. `not_applicable` explicitly identifies non-claim material or an inapplicable aspect and explains that classification. Empty findings imply nothing about coverage. Checking dates does not mark causal inference or generalizations reviewed automatically. Range coverage is a structural completeness check, not proof that all claims were extracted or that judgments are correct. Materiality and whether missing evidence requires `needs_material` remain explicit reviewer decisions, not a blanket automatic rewrite gate.

## Persistence and memory

Full `rationale`, contextual meanings, source receipts and audit outputs are persisted without semantic truncation. `memory_summary` is a separately authored compact field (at most 1600 characters; oversize is a validation error). Model memory projects these summaries plus exact contextual meanings/scopes, verdicts, current references, full relevant frozen evidence and the prior recommendation. Full reports remain separate immutable artifacts.

Both rechecks receive their own previous audit decisions, exact before/after reconstruction, actual writer assignments and prior verdict. The model uses local aliases, not hashes. Server context pins the prior audit output, script, manifest, source snapshots and projected-memory hash. Every disposition and late finding is bound to that exact context. No acceptance is copied by text hash, and an explicit unchecked result is never invented as an approved decision.

## Consistency of audit decisions and findings

New runs additionally pin `reading_audit_consistency_contract=decision-findings/1`. Every current reader `needs_change` and fact `contradicted` or `unsupported` decision must appear in `finding_links` for an actual finding. Linking another positive decision does not account for the negative one. The finding explicitly records the observation and requested change; its priority remains the reviewer's choice. An optional finding can accompany `keep`; important/blocking findings still lead the existing aggregator to `revise`. Unchecked work does not automatically create a finding or mandatory repair; unsupported decisions require an explicit finding whose priority follows the existing role rules. The server neither reinterprets the claim nor decides its materiality.

This consistency check runs before report persistence and again before combining the two reviews, including saved-outcome and rewrite verification. Address-only anchor repair keeps its existing path and preserves decisions and findings. No schema fields, prompts, models, stages or revision limits change. Saved manifests without the new consistency pin retain their original validation, binding and aggregation semantics; continuations inherit that pin's presence or absence without upgrading the parent run.
