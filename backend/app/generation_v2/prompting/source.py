SOURCE_TRIAGE_PLANNER_PROMPT = """Plan source triage for this generation run.

Topic/user prompt:
{user_prompt}

Current triage workspace:
{workspace_json}

Your job:
Create a persisted plan that breaks the corpus into source sets. A source set is a small group of sources that should be understood together because they cover the same subtopic, come from the same origin chain, appear to duplicate or update each other, or need comparison before their role is clear.

Available information:
Use list_source_capsules to page through neutral orientation capsules. Request pages with limit 8 unless you have a specific reason to use a smaller page. A capsule is not an editorial decision; it is a compact view of what appears inside a source: title, domain, outline hints, representative windows, and readability warnings.

What to do:
1. Page through source capsules until the corpus is covered, following next_cursor until it is empty.
2. Create source sets with upsert_triage_source_set. A normal set is 2-6 sources. A one-source set is fine for a standalone anchor, a unique primary artifact, or a standalone weak/noisy source.
3. Every source_ref must end up either in a planned source set or in deferred_sources with a reason.
4. Call finalize_triage_workspace_plan after the plan covers the corpus.

Planning quality:
- Do not classify sources from title/domain alone when capsule text suggests a different role.
- Prefer sets that will let the next agent compare sources directly.
- For small and medium corpora, plan broad coverage.
- For very large corpora, it is acceptable to defer low-signal groups, but the deferral must be explicit and reasoned.

Return SourceTriageCompletion JSON only after finalize_triage_workspace_plan is accepted:
{{"ready": boolean, "summary": string, "notes": string[]}}"""


SOURCE_TRIAGE_SET_PROMPT = """Map one source set for this generation run.

Topic/user prompt:
{user_prompt}

Current source set and workspace state:
{source_set_json}

Your job:
Understand this source set well enough to persist its role in the corpus map. The backend will assemble SourceMap from your write tools; notes kept only in the conversation are not durable.

How evidence works here:
- search/read/skim tools create or return evidence_id handles for concrete passages, windows, or model-read results.
- Use those evidence_id handles when writing source cards, clusters, relationships, quote cards, observations, or gaps.
- Search previews are leads. Use read_evidence or read_source_window when the exact text matters.

How to work:
1. Start from the capsules in this set. Decide what needs to be verified by reading or searching. If a compact field is truncated, or this agent resumed after a checkpoint, call list_triage_source_sets with section="current_set" first, then page section="observations" from cursor "0" until next_cursor is empty. Those pages restore full validated purpose, rationale, findings, questions, and observation text. Use section="plan_summary" or section="deferred_sources" when those workspace previews are truncated or omitted.
2. Use skim_source for source-level orientation when a capsule is not enough.
3. Use read_source_window or read_evidence for exact support, quote seeds, source limits, or relationship evidence.
4. Use search_corpus only inside the current set for targeted questions. Use compare_source_evidence when several sources need comparable evidence for a cluster or relationship.
5. After useful reading, persist the finding. Use:
   - upsert_source_card when a source has a meaningful role or limitation;
   - record_source_cluster when the set belongs together;
   - record_source_relationship when sources duplicate, update, interpret, contradict, or contextualize each other;
   - create_quote_card when a passage is an obvious reusable quote seed;
   - record_gap when support is weak, missing, noisy, incomplete, or deferred;
   - record_triage_observation when the finding is useful but not yet a final SourceMap entry.
6. Finish this set with finish_triage_source_set.

Quality bar:
- Read enough to understand the source set. The goal is not to minimize reading; the goal is to avoid unpersisted wandering.
- If a set contains multiple sources, compare at least two relevant sources before writing a cluster or relationship.
- If a source covers multiple roles, multiple reads may be appropriate.
- A mapped set should leave durable SourceMap writes or observations that another agent can recover without this conversation.

Return SourceTriageCompletion JSON only after finish_triage_source_set is accepted:
{{"ready": boolean, "summary": string, "notes": string[]}}"""


SOURCE_TRIAGE_FINALIZER_PROMPT = """Finalize source triage for this generation run.

Topic/user prompt:
{user_prompt}

Current triage workspace:
{workspace_json}

Assembled SourceMap from persisted write actions:
{source_map_json}

Your job:
Decide whether the persisted SourceMap is ready for downstream research. You are not doing broad new reading in this step. Use workspace progress and the assembled SourceMap to finalize, or record explicit gaps if the map is incomplete.

Finalize criteria:
- Main source groups are represented by source cards, clusters, relationships, quote seeds, or recorded gaps.
- Important overlaps, duplicates, update chains, contradictions, and weak/noisy areas are visible to downstream research.
- For small and medium corpora, most usable sources are represented somewhere in the map or explicitly deferred.
- The summary should explain what the map covers and what research should watch.

If plan_summary or deferred-source previews are truncated or omitted, use list_triage_source_sets with section="plan_summary" or section="deferred_sources" and follow next_cursor. Use finalize_source_map when ready. If a known gap is not yet represented, use record_gap first, then finalize_source_map.

Return SourceTriageCompletion JSON only:
{{"ready": boolean, "summary": string, "notes": string[]}}"""


SOURCE_ACQUISITION_PROMPT = """Acquire additional sources for corpus coverage.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Current corpus gap report:
{gap_report_json}

Current source inventory summary:
{source_inventory_json}

Round limits:
- Candidate budget this round: {candidate_budget}
- Ingest budget this round: {ingest_budget}

You have OpenAI web_search for discovery only. Web search content is not final evidence.
Final support must come later from ingested project sources through source versions, OpenAI file_search, selected EvidenceItem records, citation validation, and support judgment.

Allowed actions:
1. Use web_search to discover authoritative, relevant candidate URLs for the listed gap_ids.
2. Call ingest_source_candidate for URLs you want added to the project corpus. You must include gap_ids, expected editorial function, rationale, confidence, and source_type_hint.
3. Call reject_source_candidate for candidates you considered but rejected.
4. Call finalize_source_acquisition_round when the round is complete.

Vetting rules:
- Prefer original records, direct statements from involved people or organizations, credible reporting, expert analysis, transcripts, datasets, reference material, and deeply reported source material.
- Reject search-result pages, generic SEO summaries, thin aggregators, social posts, unsupported file types, and candidates that do not map to a known gap_id.
- Do not add many same-domain pages unless they are official/primary and each covers a distinct gap.
- Do not rely on web_search snippets as support. Use snippets only to decide whether a URL should be ingested.
- If no trustworthy candidate exists for a gap, reject or leave it unresolved explicitly.

Return only SourceAcquisitionCompletion JSON. The backend records accepted/rejected candidates and will rebuild corpus prepare + source triage after ingestion."""
