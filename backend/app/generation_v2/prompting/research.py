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
- origin/history: how the situation came to exist;
- turning points: changes, crises, reforms, or reversals;
- conflict/incentives: actors, constraints, incentives, disagreements, or institutional logic;
- consequences/tradeoffs: what the system enables, prevents, costs, or risks;
- exceptions/research gaps: what remains uncertain, contested, missing, or weakly sourced.

{atom_kind_block}

Use upsert_research_job for each investigation job, then finalize_research_plan.
Planning is an editorial decision: use SourceMap clusters, relationships, quote cards, gaps, and source cards to choose job scopes. Do not read or search here; this agent only writes the plan.

A good job has:
- one clear research_job label, such as mechanism, history_origin, turning_point, conflict_incentive, consequence_tradeoff, exception_or_risk, or synthesis_gap;
- source_refs from SourceMap clusters/source cards when known;
- concrete questions that can be answered by reading/searching those sources;
- required_atom_kinds that the job is expected to produce;
- enough scope to compare sources when comparison matters, but not a single mega-job that says "research everything";
- allow_broad_search=true only when the SourceMap does not identify enough relevant sources, with questions narrow enough for targeted corpus search.

For 45+ minute episodes, normally create multiple jobs covering mechanism, origin/history, turning points, conflict/incentives, consequences/tradeoffs, and unresolved risks. If the SourceMap already marks a group as duplicate, weak, or deferred, make that a lower-priority risk/gap job instead of spending a full investigation job on it.

Mini-examples:
- Good job: label="Mechanism", story_job="mechanism", source_refs=["S03","S07"], questions=["What process does each source say makes the system work?", "Where do the sources agree or differ?"], required_atom_kinds=["mechanism"], priority="high", allow_broad_search=false.
- Bad job: label="Research topic", story_job="general", source_refs=[], questions=["Find interesting facts"], required_atom_kinds=[], priority="normal", allow_broad_search=true.
- Good broad job: label="Missing consequences", story_job="consequence_tradeoff", source_refs=[], questions=["Search for concrete costs, tradeoffs, or downstream effects not covered by the mapped sources"], required_atom_kinds=["consequence"], priority="normal", allow_broad_search=true.

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
9. Finish the job with finish_research_job.

Repair jobs:
- If the current job payload has repair_for_gap_id or repair_gap, the job is repairing that exact blocking gap.
- If you resolve it, persist the substantive finding first with upsert_editorial_atom or record_research_observation, then call record_research_gap with the same gap_id, original category/severity, status="resolved", evidence_ids from the resolving read(s), and a resolution_note.
- If you cannot resolve it, call record_research_gap with the same gap_id, status="open" or "deferred", severity="blocking", and a resolution_note explaining what is still missing; then finish the job as deferred.

A strong editorial atom is not a topic and not an act outline. It states a source-grounded finding: mechanism, history, turning point, conflict, incentive, consequence, exception, human cost, or genuinely unresolved question.
unanswered_question is only for findings that remain unresolved or weak after reading. Supported claims with caveats belong in an atom plus risk or source_coverage_status.

{atom_kind_block}

Mini-examples:
- Bad atom: title="Background", claim="Several sources discuss this topic.", story_value="Useful context."
- Bad atom: title="Source quality warning", claim="This source needs corroboration.", story_value="Be careful with this source." Use record_research_risk or record_research_gap instead.
- Good atom: kind="mechanism", title="The system works because two actors exchange enforceable obligations", claim="The source evidence shows a concrete process with actors, assets, and constraints; the episode can explain that process before judging its consequences.", story_value="Gives the listener the operating model needed for the rest of the episode."
- Good risk: "S01 is close to an actor being described; use it for that actor's own account, but avoid making it the only support for a contested claim."
- Good gap: category="weak_corroboration", severity="non_blocking", question="Do independent sources corroborate this source's explanation?", source_refs=["S01"], evidence_ids=[], status="open", resolution_note="".
- Good repaired gap: gap_id="gap_timing", category="chronology_detail", severity="blocking", status="resolved", evidence_ids=["ev_123"], resolution_note="The cited source directly resolves the timing question."
- Good observation: "S01 and S02 disagree on timing. One is closer to the actor's account, while the other is a later interpretation. Need one more read before turning this into a turning_point atom."

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

Duplicate audit comes first:
- atom_drafts are raw outputs from separate jobs, so overlaps are expected.
- Compare every active atom_draft by title, claim/explanation, story_value, kind, source refs, and evidence_ids before finalizing.
- Same finding means the atoms answer the same research question or would give treatment the same story beat, even when titles, wording, language, job labels, source refs, or evidence differ.
- Likely duplicates: one atom restates another with extra citations; one atom is a narrow example of the same bounded exception; one atom repeats the same background/turning-point claim.
- Not duplicates: same topic but different research function, such as mechanism vs consequence, event vs conflict, origin vs later turning point, or claim vs unresolved gap.
- For each same-kind duplicate group, call merge_editorial_atoms before finalize_research_memo. Keep the atom with the clearest claim as primary and merge the other evidence/source refs into it.
- Do not call finalize_research_memo until this duplicate audit is complete. If no merge is needed, source_coverage should say that the finalizer checked atom overlap and kept only distinct findings.

Use record_research_risk for any important contradiction, missing coverage, weak support, or uncertainty that should survive into later planning.
Use record_research_gap only for structured unresolved research gaps. Mark severity="blocking" only when treatment should not proceed without repair; use severity="non_blocking" or "optional" for caveats that should not block treatment.
Then call finalize_research_memo with title, thesis, research_synthesis, and source_coverage.
Do not repeat already recorded risks or research gaps in finalize_research_memo.risk_register. That field is only for a genuinely additional caution that cannot be represented by record_research_risk or record_research_gap; otherwise pass an empty list or omit it.

The final memo is a research synthesis, not an episode treatment. Do not write acts, act numbers, scene order, host moves, or episode outline. The thesis should answer what the research currently supports. research_synthesis should summarize the confirmed mechanisms, timeline, disputes, consequences, weak claims, and unresolved research gaps. Source coverage should say what is well supported, partial, deferred, or risky.

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
- Grounding: important claims have evidence_ids and readable source context.
- Story functions: mechanism, history/origin, turning point, consequence, conflict or institutional incentive, and real uncertainties for long-form topics.
- Gaps and risks: blocking gaps should stop treatment; non-blocking cautions should be passed as risks.
- SourceMap coverage: major clusters are represented by atoms, risks, or explicit deferred reasons.

Use tools sparingly:
- read_evidence only when a cited evidence_id looks suspicious or too thin.
- search_corpus only when the memo likely missed material already present in the corpus.
- Do not use tools to build new findings; this is an audit.

Issue routing:
- target_step="research_finalizer": duplicate atoms, stale source-quality atoms, bad memo formatting, risks/gaps placed in the wrong field.
- target_step="editorial_research": missing/weak atom, weak grounding, missing SourceMap cluster coverage when corpus likely has relevant material.
- target_step="source_acquisition": the corpus lacks necessary sources or perspectives.
- target_step="treatment": non-blocking caution treatment must respect.
- target_step="none": no repair needed.

If passed=true, repair_targets, missing_material, and required_followups must be empty. Keep non-blocking caveats only as issues with severity="minor".
Pass only if there are no blocking or major issues that must be repaired before treatment.
Return ResearchAudit JSON. For every failure, include structured issues with issue_type, severity, target_step, description, and suggested_repair."""
