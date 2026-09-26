from app.generation_v2.research_kinds import ATOM_KIND_PROMPT_BLOCK


RESEARCH_PLANNER_PROMPT = """Plan editorial research over the already-triaged project corpus.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Show Bible context:
{show_bible_json}

SourceMap summary from triage:
{source_map_summary_json}

Initial durable research workspace snapshot:
{workspace_json}

Task:
Create durable investigation jobs that will turn the SourceMap into source-grounded research findings. You are not writing the memo or planning the episode structure yet; you are assigning focused research work that later job agents can actually execute.

The persisted CorpusBrief in the workspace is the authoritative handoff from triage. Start from its coverage questions
and research_handoff items. coverage_outcomes may be empty when triage only mapped sources; in that case handoff
items are research queues, not answers or coverage decisions. Preserve any explicit legacy outcomes when present, but
do not invent covered/partial/missing states in planning. Refine job scope or add a job only when the brief leaves a
concrete unresolved investigation. Do not create generic jobs merely to fill a taxonomy or a target count.

{atom_kind_block}

Use upsert_research_job for each investigation job, then finalize_research_plan.
Planning is an editorial decision: use CorpusBrief first, then SourceMap clusters, relationships, quote cards, gaps, assessments, and source cards to refine job scopes. Do not read or search here; this agent only writes the plan. Sources marked deferred, unusable, unreadable, or out of scope are cautions/gaps, not active job inputs.
If CorpusBrief or SourceMap context reports omitted or truncated material, use list_research_workspace_items with corpus_brief_summary, corpus_brief_intent, corpus_brief_cautions, corpus_brief_questions, corpus_brief_findings, corpus_brief_outcomes, corpus_brief_handoff, corpus_brief_source_sets, corpus_brief_gap_records, source_map_cards, source_map_assessments, source_map_quotes, source_map_clusters, source_map_relationships, source_map_gaps, or source_map_gap_records. These sections return whole validated items up to the requested limit; follow the returned next_cursor because the output budget may reduce a page. Persisted jobs, job questions/outcomes, research gaps, workspace risks, and audit repair feedback are likewise recoverable through their named sections.

A good job has:
- one clear research_job label, such as mechanism, context, change, tension_driver, impact, exception_or_gap, or synthesis_gap;
- source_refs as the allowed source scope for the job; preferred_source_refs/candidate_source_refs are starting leads,
  not the whole answer;
- concrete questions that can be answered by reading/searching those sources;
- required_atom_kinds that the job is expected to produce;
- enough scope to compare sources when comparison matters, but not a single mega-job that says "research everything";
- allow_broad_search=true only when the SourceMap does not identify enough relevant sources, with questions narrow enough for targeted corpus search.

Use context, mechanism, change, tension/driver, impact, and exceptions as editorial lenses when the actual topic calls for them, not as mandatory boxes. If the brief marks material missing or deferred, preserve that state instead of pretending it is an active source-backed job.

Mini-examples:
- Good job: label="Mechanism", story_job="mechanism", source_refs=["S03","S07"], questions=["What process does each source say makes the system work?", "Where do the sources agree or differ?"], required_atom_kinds=["mechanism"], priority="high", allow_broad_search=false.
- Bad job: label="Research topic", story_job="general", source_refs=[], questions=["Find interesting facts"], required_atom_kinds=[], priority="normal", allow_broad_search=true.
- Good broad job: label="Missing impact", story_job="impact", source_refs=[], questions=["Search for concrete costs, tradeoffs, downstream effects, or stakes not covered by the mapped sources"], required_atom_kinds=["impact"], priority="normal", allow_broad_search=true.

Successfully calling finalize_research_plan ends this agent immediately. Do not produce a trailing JSON response after that tool is accepted.""".replace(
    "{atom_kind_block}", ATOM_KIND_PROMPT_BLOCK
)


RESEARCH_JOB_REPAIR_BLOCK = """Repair mode:
- This job is repairing the exact gap in job.repair.
- If evidence resolves it, persist the resolving observation/atom first, then update the same gap_id as resolved.
- If it remains unresolved, update the same gap_id as open or deferred with the concrete remaining problem.
"""


RESEARCH_JOB_PROMPT = """Run one editorial research leaf job.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Initial job and durable workspace snapshot:
{job_payload_json}

Task:
Answer this leaf question by working through its requirements. Persist material that later treatment can use.

Main loop:
1. Pick one unfinished requirement.
2. Start from that requirement's finding_leads and residual_gap_leads. If it has no explicit leads, use job.question_level_leads as a compact index and choose the relevant leads yourself.
3. Read only the evidence/source text needed for this requirement. Open extra sources from the allowed pool only for a concrete missing link, weak support, or conflict. Do not traverse the whole pool and do not seek ideal completeness.
4. If a useful read advances the requirement but one more source/read is needed before deciding, first save an observation for this requirement, then continue the same requirement.
5. If the completion_criterion is met, immediately save one or more editorial atoms with the substantive answer. Treatment receives atoms, not observations or outcome notes.
6. If the criterion is not met, save a structured research gap and defer only the affected requirement.
7. Only after a durable write for the current requirement, move to the next requirement.
8. Finish after every requirement is answered or honestly deferred.

Evidence discipline:
- Triage findings/gaps are leads to verify, not finished answers.
- Call read_evidence for every evidence_id you use in an atom or outcome.
- Evidence IDs are meaningful only next to the finding/atom/outcome they support.
- Do not pool or swap citations: each atom's evidence_ids must directly support that atom's exact claim; each requirement_outcome's evidence_ids must support that requirement's answer.
- Evidence lists need not be identical. A requirement_outcome and its supporting atoms must share only the evidence_ids that directly support that requirement.

Gap handling:
- Compare each job-level residual gap to the current requirement by meaning. If evidence resolves it, reflect that in an atom and the requirement_outcome.
- If a residual gap remains open, save one structured gap and defer only the affected requirement.
- Do not duplicate a job-level gap across unrelated requirements.

Atoms:
- upsert_editorial_atom is for substantive source-grounded findings: context, mechanism, change, tension, driver, impact, exception, or a genuinely unresolved open question.
- A job can produce zero, one, or multiple atoms. Related requirements may share one atom when the same claim and evidence answer them; unrelated requirements need separate atoms.
- If required_atom_kinds are listed, create those kinds when evidence supports them. Missing a required atom kind defers only a requirement whose completion criterion remains unmet; do not invent a deferred requirement solely because an expected kind is absent.
- Write every author-written atom field in the requested Language. Examples show structure, not output language.

Finish contract:
- Include exactly one question_outcome for every supplied question.
- Include exactly one requirement_outcome for every coverage requirement.
- For each answered requirement_outcome, set supporting_atom_ids to atom_id values whose claims directly answer that requirement; those atoms must share at least one supporting evidence_id with the outcome.
- A question_outcome is answered only when all requirements linked to that question are answered; otherwise it is deferred.
- Use status="mapped" only when every requirement is answered. If any requirement remains unresolved, use job status="deferred" with a concrete deferred_reason.

{repair_instruction_block}

{atom_kind_block}

Mini-examples:
- Bad atom: title="Background", claim="Several sources discuss this topic.", story_value="Useful context."
- Bad atom: title="Source quality warning", claim="This source needs corroboration.", story_value="Be careful with this source." Use record_research_risk or record_research_gap instead.
- Good atom: kind="mechanism", story_weight="core", title="Escrow release is the operating mechanism", claim="The evidence says customers pay into escrow first, the provider verifies delivery, and funds are released only after verification; the mechanism is the sequence of payment, verification, and release.", story_value="Gives the listener the operating model needed before judging impact.", treatment_role_hint="Can anchor a mechanism act.", evidence_ids=["ev_mechanism_1"]

Successfully calling finish_research_job ends this agent immediately. Do not produce a trailing JSON response after that tool is accepted.""".replace(
    "{atom_kind_block}", ATOM_KIND_PROMPT_BLOCK
)


RESEARCH_FINALIZER_PROMPT = """Finalize the editorial research memo from persisted workspace state.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Show Bible context:
{show_bible_json}

Initial durable research workspace snapshot:
{workspace_json}

Task:
Review the persisted investigation jobs, atom_index, research_gaps, and risks. Observations are available on demand; they are not copied into the initial prompt.
If any workspace or SourceMap page reports next_cursor, use list_research_workspace_items with that section until the material needed for finalization is covered. The jobs, job_questions, job_question_outcomes, job_requirement_outcomes, research_gaps, workspace_risks, and audit_feedback sections return whole persisted semantic fields up to the requested limit; always continue from the returned next_cursor and pass job_id when paging one job's questions or outcomes.
Page atom_index until its next_cursor is empty so every active finding is included in the duplicate scan. If an index entry looks like a duplicate candidate or its preview is insufficient, fetch that exact full item with atom_drafts using the atom number as cursor (A01 -> cursor 0). Page observations only when a specific finding, gap, or risk cannot be understood without them.

Duplicate audit comes first:
- atom_index entries represent raw outputs from separate jobs, so overlaps are expected.
- Compare every active index entry by title, claim, story value, kind, source refs, and evidence_count. Read full claim/explanation for plausible same-kind duplicates before deciding.
- Same finding means the atoms answer the same research question or would give treatment the same story beat, even when titles, wording, language, job labels, source refs, or evidence differ.
- Likely duplicates: one atom restates another with extra source support; one atom is a narrow example of the same bounded exception; one atom repeats the same context/change claim.
- Not duplicates: same topic but different research function, such as mechanism vs impact, event vs tension, context vs later change, or claim vs unresolved gap.
- For each same-kind duplicate group, call merge_editorial_atoms before finalize_research_memo. Keep the atom with the clearest claim as primary and merge the other evidence/source refs into it.
- Do not call finalize_research_memo until this duplicate audit is complete. If no merge is needed, source_coverage should say that the finalizer checked atom overlap and kept only distinct findings.

Use record_research_risk for any important contradiction, missing coverage, weak support, or uncertainty that should survive into later planning.
Use record_research_gap only for structured unresolved research gaps. Mark treatment_relevance="central" for gaps that touch the user prompt's central promise or would become important if treatment gives that topic a full act. Central open/deferred gaps must not be hidden as ordinary risk_register caveats. Mark severity="blocking" only when treatment should not proceed without repair; use severity="non_blocking" or "optional" for caveats that should not block treatment.
Then call finalize_research_memo with title, thesis, research_synthesis, and source_coverage.
Do not repeat already recorded risks or research gaps in finalize_research_memo.risk_register. That field is only for a genuinely additional caution that cannot be represented by record_research_risk or record_research_gap; otherwise pass an empty list. Always supply the field.

The final memo is an internal research digest, not an episode treatment and not the episode's editorial angle. Do not write acts, act numbers, scene order, host moves, logline, or episode outline. The thesis is a short supported_answer: the most compact answer the evidence currently supports, not a chronological recap. research_synthesis is an audit/debug digest of confirmed context, mechanisms, changes, tensions, drivers, impacts, weak claims, and unresolved research gaps; it is not passed to treatment as story structure. Source coverage should say what is well supported, partial, deferred, or risky.

The final ResearchMemo is assembled from persisted atom_drafts by the backend; do not restate those atoms after finalization.

Successfully calling finalize_research_memo ends this agent immediately. Do not produce a trailing JSON response after that tool is accepted."""


RESEARCH_AUDIT_PROMPT = """Audit the research step before treatment.

Target duration: {duration_minutes} minutes.

Inputs:
- audit_input_json: compact research memo, workspace, and SourceMap summary.
- show_bible_json: topic/style context if present.

Audit input:
{audit_input_json}

Show bible:
{show_bible_json}

Task:
Decide whether treatment can safely plan from this research memo. Do not write treatment, acts, outline, script, or new research atoms.

Check:
- Assigned work: every persisted research question and every coverage_requirement is answered or explicitly deferred, and every CorpusBrief handoff item remains represented by a linked job and outcome.
- Distinct editorial atoms: persisted findings are specific and do not contain unmerged drafts of the same point.
- Grounding: important claims have evidence_previews with locators/snippets/source context.
- Gaps and risks: blocking gaps should stop treatment; non-blocking cautions should be passed as risks.
- Explicit handoff coverage: sources or questions selected by CorpusBrief are represented by findings, risks, or explicit deferred reasons. Do not invent a requirement to cover every SourceMap cluster.

Do not demand a minimum atom count, a mandatory context/mechanism/change/impact taxonomy, or extra material merely because the target duration is long. Those are editorial lenses, not pass/fail boxes.

Use tools deliberately:
- Read every evidence_preview marked read_priority="required" before passing audit.
- The backend marks core atom grounding and suspicious previews as required: partial/weak coverage, single-source major claims, or open-question atoms.
- Read recommended previews when they are likely to anchor treatment or clarify a risk.
- read_evidence takes evidence_ref values such as E001 from audit_input_json, not backend UUIDs.
- job_question_outcomes and job_requirement_outcomes pages expose evidence_refs for the evidence behind each answer. Read every ref listed in required_evidence_refs before accepting that answer as grounded. If an answered outcome has unavailable_evidence_count > 0, treat the missing current-run evidence as a grounding problem.
- If an atom reports evidence_preview_omitted_count > 0, use list_audit_evidence with its atom_ref and page through the omitted previews before deciding that its grounding is insufficient.
- If audit_input_json reports omitted records or a truncated memo/workspace/CorpusBrief field, use list_research_workspace_items with the matching section and continue from its returned next_cursor until empty. Use jobs, job_questions, job_question_outcomes, job_requirement_outcomes, corpus_brief_questions, corpus_brief_findings, corpus_brief_outcomes, corpus_brief_handoff, and corpus_brief_gap_records for the assigned-work audit; pass job_id when paging one job. Pages return whole records up to the requested limit and may be smaller under the output budget; list_audit_evidence pages atom evidence separately.
- search_corpus only when the memo likely missed material already present in the corpus.
- Do not use tools to build new findings; this is an audit.

Issue routing:
- target_step="research_finalizer": duplicate atoms, stale source-quality atoms, bad memo formatting, risks/gaps placed in the wrong field.
- target_step="research_audit": audit did not perform required evidence reads.
- target_step="editorial_research": an assigned handoff question was lost, a persisted finding is weakly grounded, or existing corpus evidence likely resolves a concrete recorded gap.
- target_step="source_acquisition": the corpus lacks necessary sources or perspectives.
- target_step="treatment": non-blocking caution treatment must respect.
- target_step="none": no repair needed.
Use issue_type="needs_research_repair" when existing corpus likely has material but research did not extract it. Use "source_acquisition_needed" when the corpus lacks the needed source/perspective. Use "treatment_caution" only for non-blocking constraints. Use "no_issue" only with target_step="none".

If passed=true, repair_targets, missing_material, and required_followups must be empty. Keep non-blocking caveats only as issues with severity="minor".
Pass only if there are no blocking or major issues that must be repaired before treatment.
Invalid audit: passed=true together with blocking=true, a blocking/major issue, a repair target, missing material, or a required followup. In every such case passed must be false.
Return ResearchAudit JSON. For every failure, include structured issues with issue_type, severity, target_step, description, and suggested_repair."""
