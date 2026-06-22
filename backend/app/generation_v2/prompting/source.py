SOURCE_TRIAGE_PROMPT = """Run source triage for this generation run.

Topic/user prompt:
{user_prompt}

Corpus inventory summary. This is not the source list. Use list_sources to load source_ref handles and compact source metadata:
{source_inventory_json}

Mission:
Build a working map of the corpus so later research can use sources intelligently. This is corpus cartography, not a research memo and not an episode outline.

SourceMap questions:
- What source groups exist in this corpus?
- Which sources are good entry points for the topic, and what job can each do?
- Which sources are closest to the original record, artifact, dataset, transcript, or direct statement for a claim?
- Which sources interpret, summarize, republish, contextualize, duplicate, update, contradict, or merely repeat other sources?
- What coverage gaps or source-quality risks should later research know?

Handles:
- observed evidence: any retrieval/read result persisted by a tool.
- map evidence: evidence_id values from search/read tools that ground a source card, relationship, cluster, or gap. These are seeds for the map, not the final podcast evidence budget.
- source_ref: the source handle shown in inventory, such as S01, S17, or S001 depending on corpus size.

Workflow:
1. Load inventory with list_sources, normally limit=25 and cursor="". Page again from next_cursor until it is empty. Form provisional source sets by likely role, topic coverage, origin, duplication, update chain, or quality risk.
2. Pick one small source set around one source-map question. A normal set is 2-5 sources; use one source only when it is clearly a standalone anchor or a standalone risk.
3. For each not-yet-examined source in the set, normally start with skim_source(source_ref, max_windows=3-5) to understand its role and scope. If corpus search already surfaced a precise passage, read_evidence may substitute for that first skim for this specific question.
4. Use read_source_window after skim/search when you need exact support, a quote seed, a concrete locator, or relationship evidence. Sources may cover several topics; it is fine to read multiple windows from one source when that source is doing multiple jobs. For relationships and clusters, read at least two relevant sources before writing the relationship or cluster.
5. Immediately write what that source set taught you:
   - upsert_source_card for anchor or high-utility sources;
   - record_source_cluster when the set belongs together;
   - record_source_relationship when one source duplicates, updates, contradicts, interprets, or gives background for another;
   - create_quote_card only when a read/search result contains an obvious reusable quote seed;
   - record_gap when the set is weak, noisy, missing direct support, or needs later research.
6. Move to the next source set. Do not keep conclusions only in memory. The backend SourceMap is assembled from write actions, so unwritten conclusions are lost at checkpoints.
7. Finish when the main source groups, best entry points, important overlaps, and meaningful gaps are represented. Call finalize_source_map before the final response.

Quality bar:
- For small and medium corpora, most usable sources should appear in at least one SourceCard, source cluster, source relationship, quote card, or recorded gap/caution.
- For large corpora, cover as many source groups as the tool budget allows. Do not stop after only the first obvious cluster if other source groups remain unexamined.
- Every promoted SourceCard needs evidence_ids that support its source role, utility, or limits.
- Every substantive cluster or relationship needs evidence_ids from at least two relevant sources.
- Do not treat reading as a cost problem. Read what is needed to classify the corpus accurately, then write the map entry.

Tool policy:
- list_sources loads source_ref handles and compact metadata.
- skim_source is the default first-read tool for a source you have not examined.
- read_source_window is for exact follow-up after skim/search.
- search_corpus is for targeted cross-corpus questions, quote leads, or relationship checks; it does not replace skimming when you need the source's overall role.
- read_evidence opens a search result when the preview is not enough.
- compare_source_evidence retrieves comparable evidence from 2-5 source_refs for a cluster or relationship.
- Write map content through upsert_source_card, record_source_cluster, record_source_relationship, create_quote_card, and record_gap.

Mini-example:
If S03 and S08 appear to repeat the same material, skim both sources, read one confirming window if needed, then record_source_relationship with relationship="duplicate" or "near_duplicate" and/or record_source_cluster. Do not create two high-utility SourceCards unless they add distinct material.

Final response:
Return SourceTriageCompletion JSON only:
{{"ready": boolean, "summary": string, "notes": string[]}}
Do not include SourceMap content in the final response; write SourceMap content through tools."""


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
1. Start from the capsules in this set. Decide what needs to be verified by reading or searching.
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

Use finalize_source_map when ready. If a known gap is not yet represented, use record_gap first, then finalize_source_map.

Return SourceTriageCompletion JSON only:
{{"ready": boolean, "summary": string, "notes": string[]}}"""


SOURCE_TRIAGE_REPAIR_PROMPT = """Repair source triage write actions for this generation run.

Topic/user prompt:
{user_prompt}

Corpus inventory summary. Use list_sources if you need source_ref handles or compact source metadata:
{source_inventory_json}

Current assembled SourceMap from previous write actions:
{source_map_json}

Gate failure that must be repaired:
{gate_json}

Purpose:
The previous SourceMap was useful, but it failed backend quality checks. Do not restart from scratch. Add or overwrite write actions only where needed, then call finalize_source_map.

Repair method:
- For each source the gate flags as under-grounded, invalid, unwritten, or missing persisted gap/caution work, use list_sources if needed to recover its source_ref, then use inspect_source/read_source_window/read_evidence/search_corpus enough to ground or demote the source. Re-run upsert_source_card, record_source_cluster, record_source_relationship, create_quote_card, or record_gap.
- If a source should not be promoted, do not create new claims from it; record_gap explaining why support is unavailable.
- If gate.unpersisted_completion_gap_notes is non-empty, call record_gap for those exact unresolved cautions before finalizing.
- If the gate lists invalid evidence/source references, ungrounded relationships, or missing function evidence, repair those by finding valid evidence or overwriting the affected write actions using source_refs from inventory.
- Preserve useful existing source work unless the gate identifies it as invalid.
- If you cannot repair a required gate issue with available tools, return ready=false and explain the exact blocker in summary/notes. Otherwise call finalize_source_map with ready=true."""


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
