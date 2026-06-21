SOURCE_TRIAGE_PROMPT = """Run source triage for this generation run.

Topic/user prompt:
{user_prompt}

Neutral source inventory. This is not an editorial summary; it only gives you source_ref handles, titles, domains, authority/reliability hints, coverage status, and brief previews:
{source_inventory_json}

Purpose:
Build a working map of the corpus so later research can use the sources intelligently.

A good source map answers these questions:
- What are the main source groups in this corpus: original materials, materials published by involved organizations or people, data/reference material, expert analysis, reporting, background summaries, mirrors/duplicates, and weak or noisy material?
- Which sources are the best entry points for the topic, and what concrete job can each source do?
- Which sources are primary for a claim because they are the original record, dataset, artifact, transcript, or direct statement closest to that claim?
- Which sources are secondary/background because they interpret, summarize, comment on, republish, or contextualize material created elsewhere?
- Which sources overlap, duplicate, update, contradict, or merely repeat each other?
- Where are the coverage gaps or source-quality risks that the research agent must know before building the episode?

This is not a final research memo and not an episode outline. It is corpus cartography: read enough to classify sources and relationships accurately, then write those judgments into the map.

Terminology:
- observed evidence: any retrieval/read result persisted by a tool.
- map evidence: evidence_id values from search/read tools that ground a source card, relationship, cluster, or gap. These are seeds for the map, not the final podcast evidence budget.
- source_ref: the source handle shown in inventory, such as S01, S17, or S001 depending on corpus size.

Working method:
Work in source-set iterations, not in one giant read phase.

1. Orient from inventory. Form provisional source sets by likely role, topic coverage, origin, duplication, update chain, or quality risk.
2. Pick one small source set around one source-map question. A normal set is 2-5 sources; use one source only when it is clearly a standalone anchor or a standalone risk.
3. Read enough sources in that set to decide the map entry. Use skim/read/search/compare as needed. For relationships and clusters, read at least two relevant sources before writing the relationship or cluster.
4. Immediately write what that source set taught you:
   - upsert_source_card for anchor or high-utility sources;
   - record_source_cluster when the set belongs together;
   - record_source_relationship when one source duplicates, updates, contradicts, interprets, or gives background for another;
   - record_gap when the set is weak, noisy, missing direct support, or needs later research.
5. Move to the next source set. Do not keep conclusions only in memory. The backend SourceMap is assembled from write actions, so unwritten conclusions are lost at checkpoints.
6. Finish when the main source groups, best entry points, important overlaps, and meaningful gaps are represented. Then call finalize_source_map and answer with ready, summary, and notes.

Coverage target:
- For small corpora, map most useful sources directly.
- For large corpora, create source cards for anchors and high-utility sources, clusters for groups of related or lower-priority sources, relationships for meaningful overlaps, and gaps for weak or unreviewed areas.
- It is acceptable for many sources to appear only inside clusters or gaps. It is not acceptable to read many sources without writing the corresponding map entries.

Tool guide:
- list_sources: page through inventory when source_refs are missing from the prompt context.
- search_evidence: search the project corpus for a specific source-map question. The results are previews with evidence_id handles.
- inspect_source: inspect one source's structure or readable surface before deciding how to read it.
- skim_source: sample representative windows from one source to understand role, scope, and quality.
- read_source: read a focused window from one source when a card, cluster, relationship, or gap needs stronger grounding.
- read_evidence: open a search result by evidence_id when its preview is not enough to support a map entry.
- compare_sources: compare 2-5 source_refs for one source-map question when you need evidence-backed inputs for a cluster or relationship.
- coverage_warning: if a read/search tool returns this, treat the result as a weak lead or gap unless another concrete read confirms it.
- upsert_source_card: write the role, utility, and limits of a source using evidence_ids from search/read tools.
- record_source_cluster: group related sources by origin, evidentiary role, topic coverage, duplication/mirroring, update relationship, or quality level.
- record_source_relationship: record duplicate, near_duplicate, primary_to_secondary, contradicts, updates, or background_for relationships.
- record_gap: record missing coverage, unresolved contradictions, weak support, or source-quality risks.
- finalize_source_map: signal that the map entries are complete enough for the next research step."""


SOURCE_TRIAGE_REPAIR_PROMPT = """Repair source triage write actions for this generation run.

Topic/user prompt:
{user_prompt}

Neutral source inventory. This is not an editorial summary; it only gives source_ref handles and compact source metadata:
{source_inventory_json}

Current assembled SourceMap from previous write actions:
{source_map_json}

Gate failure that must be repaired:
{gate_json}

Purpose:
The previous SourceMap was useful, but it failed backend quality checks. Do not restart from scratch. Add or overwrite write actions only where needed, then call finalize_source_map.

Repair method:
- For each source the gate flags as under-grounded or missing direct-read coverage, match it to the source_ref from the inventory, use inspect_source/read_source/read_evidence enough to ground or demote the source, then re-run upsert_source_card, record_source_cluster, record_source_relationship, or record_gap.
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
