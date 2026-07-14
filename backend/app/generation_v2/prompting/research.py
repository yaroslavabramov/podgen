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

Research workspace:
{workspace_json}

Task:
Create durable investigation jobs that will turn the SourceMap into source-grounded research findings. You are not writing the memo or planning the episode structure yet; you are assigning focused research work that later job agents can actually execute.

Long-form research should cover:
- central mechanism: how the key system/process works;
- context/setup: how the situation came to exist and what background matters;
- change: events, shifts, crises, reforms, reversals, or trajectory changes;
- tension/driver: tensions, tradeoffs, contradictions, causes, pressures, constraints, or conditions;
- impact: what changes, costs, benefits, risks, or stakes follow;
- exceptions/open questions: what remains uncertain, contested, missing, or weakly sourced.

{atom_kind_block}

Use upsert_research_job for each investigation job, then finalize_research_plan.
Planning is an editorial decision: use SourceMap clusters, relationships, quote cards, gaps, and source cards to choose job scopes. Do not read or search here; this agent only writes the plan.
If SourceMap pages report next_cursor or a SourceMap semantic field is truncated, use list_research_workspace_items with the matching source_map_cards, source_map_quotes, source_map_clusters, source_map_relationships, or source_map_gaps section before declaring coverage complete. These sections return one full validated item per page. Persisted jobs, job questions/outcomes, research gaps, workspace risks, and audit repair feedback are likewise recoverable through jobs, job_questions, job_question_outcomes, research_gaps, workspace_risks, and audit_feedback.

A good job has:
- one clear research_job label, such as mechanism, context, change, tension_driver, impact, exception_or_gap, or synthesis_gap;
- source_refs from SourceMap clusters/source cards when known;
- concrete questions that can be answered by reading/searching those sources;
- required_atom_kinds that the job is expected to produce;
- enough scope to compare sources when comparison matters, but not a single mega-job that says "research everything";
- allow_broad_search=true only when the SourceMap does not identify enough relevant sources, with questions narrow enough for targeted corpus search.

For 45+ minute episodes, normally create multiple jobs covering mechanism, context, change, tension/driver, impact, and unresolved risks. If the SourceMap already marks a group as duplicate, weak, or deferred, make that a lower-priority risk/gap job instead of spending a full investigation job on it.

Mini-examples:
- Good job: label="Mechanism", story_job="mechanism", source_refs=["S03","S07"], questions=["What process does each source say makes the system work?", "Where do the sources agree or differ?"], required_atom_kinds=["mechanism"], priority="high", allow_broad_search=false.
- Bad job: label="Research topic", story_job="general", source_refs=[], questions=["Find interesting facts"], required_atom_kinds=[], priority="normal", allow_broad_search=true.
- Good broad job: label="Missing impact", story_job="impact", source_refs=[], questions=["Search for concrete costs, tradeoffs, downstream effects, or stakes not covered by the mapped sources"], required_atom_kinds=["impact"], priority="normal", allow_broad_search=true.

Return only ResearchWorkflowCompletion JSON after the tools have persisted the plan.""".replace("{atom_kind_block}", ATOM_KIND_PROMPT_BLOCK)


RESEARCH_JOB_PROMPT = """Run one editorial research investigation job.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Current job and durable workspace state:
{job_payload_json}

Task:
Investigate this job's question(s) and persist useful findings.

What success looks like:
- You understand the relevant source(s) enough to decide whether the job's likely atom is supported, contradicted, partial, or unsupported.
- You save durable research_observation notes for intermediate findings.
- You save editorial atoms only when they are grounded research findings useful for later editorial planning.
- If the job stays weak, you preserve the reason as an observation and finish it as deferred.

How to work:
1. Orient to the current job payload: questions, source_refs, seed source cards/quotes, clusters, relationships, gaps, and existing atom drafts.
2. For named source_refs, start with skim_source or inspect_source when you do not yet understand the source's structure or role. Use search_within_source and read_source_window for targeted support after that.
3. For broad jobs, start with two to four targeted search_corpus calls using scope=all_sources, then narrow follow-up reads to the source_refs returned by those results.
4. Use read_evidence for seed evidence_ids or search results that look important. Use read_source_with_model for important PDF/document claims or when local windows are not enough to support a precise atom.
5. Use compare_evidence_for_question when multiple job sources need to be weighed together.
6. After meaningful reading, immediately call record_research_observation or upsert_editorial_atom to persist progress. Use record_research_risk for non-blocking source-quality or uncertainty risks, and record_research_gap for unresolved research gaps.
7. upsert_editorial_atom is only for substantive findings that later treatment can use as material. The tool validates the refs and attaches included evidence automatically. It is not for source-quality warnings, missing-source notes, weak-corroboration notes, or general cautions.
8. If a job cannot produce a strong atom, record the reason as an observation or research_gap, then finish it as deferred with a concrete reason.
9. If job.pages.questions or job.pages.observations has next_cursor, page it with list_research_workspace_items using current_job_questions or current_job_observations.
   If an existing atom_draft or global observation reports a truncated semantic field, page atom_drafts or observations from cursor 0; those sections return one full validated item at a time.
10. Finish the job with finish_research_job. Include exactly one question_outcome for every planned question, preserving the supplied question text; each outcome must be answered with job evidence_ids or explicitly deferred with a concrete note.

Repair jobs:
- If the current job payload has repair_for_gap_id or repair_gap, the job is repairing that exact blocking gap.
- If you resolve it, persist the substantive finding first with upsert_editorial_atom or record_research_observation, then call record_research_gap with the same gap_id, original category/severity/treatment_relevance, status="resolved", evidence_ids from the resolving read(s), and a resolution_note.
- If you cannot resolve it, call record_research_gap with the same gap_id, status="open" or "deferred", original or stronger severity/treatment_relevance, and a resolution_note explaining what is still missing; then finish the job as deferred.

A strong editorial atom is not a topic and not an act outline. It states a source-grounded finding: context, mechanism, change, tension, driver, impact, exception, or genuinely unresolved open question.
open_question is only for findings that remain unresolved or weak after reading. Supported claims with caveats belong in an atom plus risk or source_coverage_status.
Atom weight:
- story_weight="core" only for findings strong enough to anchor a treatment act.
- story_weight="supporting" for material that supports a larger act but should not be the main act anchor.
- story_weight="caveat" for exceptions, unresolved questions, and bounded warnings.
- treatment_role_hint should explain in one sentence how treatment may use the atom.
Research gap relevance:
- treatment_relevance="central" only when the episode cannot responsibly proceed without resolving or tightly bounding the gap.
- treatment_relevance="supporting" when it affects a segment or caution.
- treatment_relevance="minor" when it should not shape treatment.

{atom_kind_block}

Mini-examples:
- Bad atom: title="Background", claim="Several sources discuss this topic.", story_value="Useful context."
- Bad atom: title="Source quality warning", claim="This source needs corroboration.", story_value="Be careful with this source." Use record_research_risk or record_research_gap instead.
- Good atom: kind="mechanism", story_weight="core", title="The process depends on two linked constraints", claim="The source evidence shows a concrete process with parts, dependencies, and constraints; the episode can explain that process before judging its impact.", story_value="Gives the listener the operating model needed for the rest of the episode.", treatment_role_hint="Can anchor a mechanism act."
- Good risk: "S01 is close to an actor being described; use it for that actor's own account, but avoid making it the only support for a contested claim."
- Good gap: category="weak_corroboration", severity="non_blocking", treatment_relevance="supporting", question="Do independent sources corroborate this source's explanation?", source_refs=["S01"], evidence_ids=[], status="open", resolution_note="".
- Good repaired gap: gap_id="gap_timing", category="chronology_detail", severity="blocking", treatment_relevance="central", status="resolved", evidence_ids=["ev_123"], resolution_note="The cited source directly resolves the timing question."
- Good observation: "S01 and S02 disagree on timing. One is closer to the event, while the other is a later interpretation. Need one more read before turning this into a change atom."

Return only ResearchWorkflowCompletion JSON after the job is finished or explicitly deferred.""".replace("{atom_kind_block}", ATOM_KIND_PROMPT_BLOCK)


RESEARCH_FINALIZER_PROMPT = """Finalize the editorial research memo from persisted workspace state.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Show Bible context:
{show_bible_json}

Durable research workspace:
{workspace_json}

Task:
Review the persisted investigation jobs, observations, atom_drafts, research_gaps, and risks.
If any workspace or SourceMap page reports next_cursor, use list_research_workspace_items with that section until the material needed for finalization is covered. The jobs, job_questions, job_question_outcomes, research_gaps, workspace_risks, and audit_feedback sections return full persisted semantic fields one item at a time; pass job_id when paging one job's questions or outcomes.
If any atom_draft or observation reports a truncated semantic field, page atom_drafts or observations from cursor 0 even when the initial page has no next_cursor. Those sections return one full validated item at a time; follow next_cursor through the collection.

Duplicate audit comes first:
- atom_drafts are raw outputs from separate jobs, so overlaps are expected.
- Compare every active atom_draft by title, claim/explanation, story_value, kind, source refs, and evidence_count before finalizing.
- Same finding means the atoms answer the same research question or would give treatment the same story beat, even when titles, wording, language, job labels, source refs, or evidence differ.
- Likely duplicates: one atom restates another with extra source support; one atom is a narrow example of the same bounded exception; one atom repeats the same context/change claim.
- Not duplicates: same topic but different research function, such as mechanism vs impact, event vs tension, context vs later change, or claim vs unresolved gap.
- For each same-kind duplicate group, call merge_editorial_atoms before finalize_research_memo. Keep the atom with the clearest claim as primary and merge the other evidence/source refs into it.
- Do not call finalize_research_memo until this duplicate audit is complete. If no merge is needed, source_coverage should say that the finalizer checked atom overlap and kept only distinct findings.

Use record_research_risk for any important contradiction, missing coverage, weak support, or uncertainty that should survive into later planning.
Use record_research_gap only for structured unresolved research gaps. Mark treatment_relevance="central" for gaps that touch the user prompt's central promise or would become important if treatment gives that topic a full act. Central open/deferred gaps must not be hidden as ordinary risk_register caveats. Mark severity="blocking" only when treatment should not proceed without repair; use severity="non_blocking" or "optional" for caveats that should not block treatment.
Then call finalize_research_memo with title, thesis, research_synthesis, and source_coverage.
Do not repeat already recorded risks or research gaps in finalize_research_memo.risk_register. That field is only for a genuinely additional caution that cannot be represented by record_research_risk or record_research_gap; otherwise pass an empty list or omit it.

The final memo is an internal research digest, not an episode treatment and not the episode's editorial angle. Do not write acts, act numbers, scene order, host moves, logline, or episode outline. The thesis is a short supported_answer: the most compact answer the evidence currently supports, not a chronological recap. research_synthesis is an audit/debug digest of confirmed context, mechanisms, changes, tensions, drivers, impacts, weak claims, and unresolved research gaps; it is not passed to treatment as story structure. Source coverage should say what is well supported, partial, deferred, or risky.

The final ResearchMemo is assembled from persisted atom_drafts. Do not restate all atoms in the final JSON response; the final JSON response is only ResearchWorkflowCompletion.

Return only ResearchWorkflowCompletion JSON after finalize_research_memo is accepted."""


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
- Distinct editorial atoms: enough specific findings, not repeated drafts of the same point.
- Grounding: important claims have evidence_previews with locators/snippets/source context.
- Story functions: mechanism, context/setup, change, tension or driver, impact, and real uncertainties for long-form topics.
- Gaps and risks: blocking gaps should stop treatment; non-blocking cautions should be passed as risks.
- SourceMap coverage: major clusters are represented by atoms, risks, or explicit deferred reasons.

Use tools deliberately:
- For 45+ minute work, read every evidence_preview marked read_priority="required" before passing audit.
- The backend marks core atom grounding and suspicious previews as required: partial/weak coverage, single-source major claims, or open-question atoms.
- Read recommended previews when they are likely to anchor treatment or clarify a risk.
- read_evidence takes evidence_ref values such as E001 from audit_input_json, not backend UUIDs.
- If an atom reports evidence_preview_omitted_count > 0, use list_audit_evidence with its atom_ref and page through the omitted previews before deciding that its grounding is insufficient.
- If audit_input_json reports omitted records or a truncated memo/workspace field, use list_research_workspace_items with the matching section and continue until next_cursor is empty. memo_atoms returns one full validated atom at a time; list_audit_evidence pages that atom's evidence separately.
- search_corpus only when the memo likely missed material already present in the corpus.
- Do not use tools to build new findings; this is an audit.

Issue routing:
- target_step="research_finalizer": duplicate atoms, stale source-quality atoms, bad memo formatting, risks/gaps placed in the wrong field.
- target_step="research_audit": audit did not perform required evidence reads.
- target_step="editorial_research": missing/weak atom, weak grounding, missing SourceMap cluster coverage when corpus likely has relevant material.
- target_step="source_acquisition": the corpus lacks necessary sources or perspectives.
- target_step="treatment": non-blocking caution treatment must respect.
- target_step="none": no repair needed.
Use issue_type="needs_research_repair" when existing corpus likely has material but research did not extract it. Use "source_acquisition_needed" when the corpus lacks the needed source/perspective. Use "treatment_caution" only for non-blocking constraints. Use "no_issue" only with target_step="none".

If passed=true, repair_targets, missing_material, and required_followups must be empty. Keep non-blocking caveats only as issues with severity="minor".
Pass only if there are no blocking or major issues that must be repaired before treatment.
Invalid audit: passed=true together with blocking=true, a blocking/major issue, a repair target, missing material, or a required followup. In every such case passed must be false.
Return ResearchAudit JSON. For every failure, include structured issues with issue_type, severity, target_step, description, and suggested_repair."""
