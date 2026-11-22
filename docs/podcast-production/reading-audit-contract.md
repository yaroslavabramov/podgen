# Contextual reading audit — reading-audit/1

This contract is additive to the current reader/fact reports. It changes neither author work, planning, models, role order nor revision limits. Complete native outputs remain immutable reports. It does not establish factual truth or production readiness.

New-run pins: `reading_audit_contract=reading-audit/1`, `reader_review_schema_version=listener-experience/14`, `fact_review_schema_version=fact-revision/5`, `revision_verification_contract=editorial-recheck/3`. Historical pins retain their exact schemas and requests. A legacy continuation inherits its legacy pins; it cannot fabricate checked decisions.

## Output fields

These are the complete persisted reports. The `/4` factual recheck wire is the compact delta described below.

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

### Factual admission for new runs: decision-findings/2

New production manifests and unsaved previews now pin
`reading_audit_consistency_contract=decision-findings/2`. The audit, fact schema
and recheck schema remain `reading-audit/1`, `fact-revision/5` and
`editorial-recheck/3`; response fields are unchanged. Only the new consistency pin
selects the stricter factual validation and its schema descriptions. `/1` and an
absent pin retain their exact prior semantics and descriptions.

The fact reviewer establishes the claim actually conveyed, including its scope,
attribution and dependencies. A stronger imaginable reading is not silently
substituted for the spoken account. Bounded interpretations and necessary
inferences can be supported by inspected premises without a verbatim assertion in
a source. This does not excuse a concrete asserted detail that remains unverified.

For factual reports, `keep` cannot coexist with an `unchecked` claim, coverage
range or applicable area. `contradicted` and `unsupported` decisions must link to
an important/blocking finding. The author can correct, narrow or remove a
dispensable assertion and its dependent wording instead of receiving another
research task or spoken warning. Optional improvements of supported wording remain
compatible with `keep`. Non-claim material and inapplicable areas use justified
`not_applicable`; this cannot discharge an asserted but unchecked fact. These are
checks on the model's explicit report, not a semantic claim extractor or proof of
exhaustive verification.

In recheck, a `retained` decision carries its previous contextual verdict and all
its offered frozen evidence bindings into its current decision(s). Changing that
verdict or dropping its evidence requires `reassessed`, not an unchanged identifier
with a different meaning. The existing exact revision comparison and late-finding
contract bind required reopenings to changed speech, dependent conclusions, new
evidence, an unresolved assignment or a substantive prior omission. The server
checks relational consistency; the reviewer must demonstrate the actual reason.
The same round alone does not justify rereading accepted evidence.

Production's first factual review and factual recheck have new content-addressed
English instruction bodies. They request retained-source investigation, then
offered quick web lookup before extended research, verbatim script anchors for
external questions, and actual inspected source citations. Search navigation and
research synthesis do not become primary evidence merely by being returned.
The tool owner pins these operational capabilities separately. Old assets and
persisted prompt bodies are unchanged; resume/continuation uses its saved pins.

No paid comparison has yet established the new instructions' editorial quality.
Local tests cover admission contradictions, preserving evidence across recheck,
bounded contextual admission and frozen `/1` compatibility. They do not establish
that a model will find every real error or avoid every false claim.

### Retained evidence carried by admission: decision-findings/3

Archived production manifests may pin `decision-findings/3`. The model-facing schema,
descriptions, prompts and response fields are exactly those of `/2`; only factual
admission changes. Before validation, the server carries every frozen evidence
reference of a `retained` previous decision that the reviewer did not cite again.
A successor keeping the previous decision ID receives it; when a retained decision
is split without such a successor, every successor receives it. Unknown IDs remain
validation errors. A changed retained verdict is still rejected, and `reassessed`
remains the way to replace an evidential basis. The persisted output contains the
carried references and therefore resolves and binds them like cited evidence. The
raw `wire_output` keeps the reviewer's own citations, and
`reading_audit.retained_evidence_carried` lists each carried reference. Replay
repeats the same deterministic normalization. Under `/2` a retained decision that
omits frozen evidence is rejected with `audit_retained_evidence_missing`.

#### Local readmission of a saved `/2` rejection

`resume --readmit-reading INVOCATION_UUID` admits the rejected current reading
again under its successor contract without a provider call. It is eligible only
for the latest attempt of a reading stage, in `validation_failed`, with a complete
saved provider response and exactly one `output_validation_error` whose code and
the run's pin have a declared successor. The only one declared is
`audit_retained_evidence_missing` under `decision-findings/2`, readmitted under
`decision-findings/3`. Another reported error, a missing or incomplete response,
or a non-current attempt is refused with `local_readmission_not_eligible` or
`local_readmission_not_current`.

The grant is a new attempt `local_readmission` (`local-readmission/1`) of the
parent's operation kind. Its request record repeats the parent's request and
context with new identity metadata and stores the actor, target, failure event,
pinned and successor contracts, and a single-use limit. The parent attempt, its
paid usage and its rejection stay unchanged, as does the manifest pin. The
readmission reuses the parent's exact saved response text; an anchor-selection
parent replays its saved choices against the original rejected review. Its own
usage is zero. Commission, current evidence and source reads belong to the
parent lineage. The completed report records `reading_audit.admission_contract`.
Every replay re-derives the grant from the parent before trusting the report. A
second readmission or another failure of the readmitted response is not retried.

Offline checks: `backend/tests/test_fact_admissibility.py` and the PostgreSQL run
`backend/tests/test_local_readmission.py`.

## Shared packet memory projection

The first shared projection is pinned as `reading_audit_memory_projection=shared-packet-evidence/1`. A previous packet-evidence reference is projected to `from_review=current` and the matching current `B` alias only when full canonical content (including supports), its hash, and all frozen source selectors (document identity, ref, hash and exact ranges) match. Evidence numbering alone or equal text alone is insufficient. A shared packet excerpt is not repeated in model-facing `previous_evidence`. The complete previous report, evidence and original provenance remain immutable; the new binding records the prior-report hash, exact old-reference-to-current-B mapping, and current packet fingerprint. Actual delivered packet evidence is checked against that fingerprint before persistence and on replay.

Unique prior source reads retain full content and receipts under `H` aliases even if their text also appears in a packet excerpt. Only fully identical retained receipts may share an `H` alias; different read provenance remains distinct. All previous decisions, contextual meanings/scopes, authored memory summaries, coverage, assignments and revision reconstruction are preserved. No semantic fields are shortened. Requests without this pin, including their continuations, keep the exact previous projection and aliases. Prompts, models, response fields and limits do not change.

New runs pin `shared-packet-evidence/2`. When a decision carries forward, its
references in both the structured evidence list and the reviewer's explanation
must still identify the same frozen passages. This applies to the request seen
by the next fact checker and to the full report expanded from its confirmation.
The original review, source receipts and verbatim script anchors remain immutable.

### Delta factual rechecks: decision-findings/4

New production manifests pin `/4` together with the English instruction in
`writing/prompts/current/fact-verification.md`. First factual audits and reader
reports retain their complete forms. Subsequent fact reviews receive the full
current script, exact before/after edit windows, original assignments, previous
contextual decisions and authenticated evidence. They do not reconstruct an old
script. Each quoted anchor appears once: `anchor_version` identifies its version;
unambiguous unchanged turns are placed at their current indices. Assignment
quotes retain the previous coordinates, with current indices or change-window
indices supplied separately. With memory projection `/2`, repeated identical
turns can retain their decisions when the particular occurrence is unambiguous.
Deleting one of two indistinguishable neighbouring turns still requires an
explicit reassessment.

The recheck wire contains one explicit `retained_confirmation`, current full
coverage and judgment, only new/reassessed `audit.claim_checks`, and only
reassessed/retired `previous_dispositions`. Every edited, removed or ambiguous
prior anchor requires an explicit update. Changed surrounding context or evidence
may also justify updating an otherwise aligned decision. Prior decision IDs stay
reserved unless explicitly reassessed; split and merged claims select their own
evidence. No prior evidence is copied automatically into these claims.

The backend expands confirmation from the complete native prior records, remaps
only unambiguous unchanged anchors and resolves every evidence reference. It
persists the usual complete `AuditedFact` report for writers, audit memory,
aggregation and export. The separate wire output and `recheck_delta` proof retain
the model's exact confirmation and exceptions. Unsupported, contradicted or
unchecked retained decisions cannot disappear behind a global keep. Unknown
anchors, references or unfinished coverage reject admission through the same
ordinary audit path.

For unaffected supported decisions, a uniquely readable H passage may be offered
as H plus exact current S/start/end instead of repeated text. `read_source` returns
the original text and its normal pagination; reopening is optional for unchanged
confirmed work. Evidence needed by affected or unresolved decisions, evidence
with multiple selectors and evidence unavailable through the offered read tool
stays inline. The complete evidence and provenance are always retained in the
backend and reauthorized; this projection does not grant access or turn a locator
into a new E receipt.

Saved `/2` and `/3` schemas, instructions, payloads, evidence transfer rules and
existing local-readmission grants keep their pins. `/4` changes new ordinary
rechecks; it does not replace the separately authorized local admission of a
previously rejected response.
