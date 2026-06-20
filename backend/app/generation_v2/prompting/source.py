SOURCE_TRIAGE_PROMPT = """Run source triage for this generation run.

Topic/user prompt:
{user_prompt}

Neutral source inventory. This is not an editorial summary; it only tells you what sources and source versions exist:
{source_inventory_json}

Purpose:
Later agents should understand the corpus landscape before doing their own deep research. Your output is not the SourceMap itself; your write actions are.

Terminology:
- observed evidence: any retrieval/read result persisted by a tool.
- selected evidence: observed evidence you explicitly keep with select_evidence.
- triage seed evidence: selected evidence used to ground SourceCards, QuoteCards, relationships, gaps, and source utility. It is not the final podcast evidence budget.

You must work in two phases before returning JSON:
1. Retrieval/read plan: use hosted file_search for broad semantic discovery over uploaded source files, then decide which sources need enough inspection to map authority, reliability, uniqueness, relationships, and obvious quote seeds. Use read_source_with_model, inspect_source, list_source_versions, skim_source, read_source_window, search_within_source, find_quotes, search_sources, or compare_sources as needed.
2. Write actions: explicitly call select_evidence for evidence you want to keep, then upsert_source_card, create_quote_card, record_source_relationship, record_gap, and finalize_source_map.

Rules:
- Use only the provided source inventory or evidence tool results.
- Call tools unless the corpus is tiny and the inventory already contains explicit evidence ids. A source map built only from the prompt inventory is not acceptable.
- Hosted file_search is the primary broad retrieval tool over uploaded source versions.
- Use read_source_with_model for focused deep reading of every PDF/document/file source you promote into a SourceCard. Hosted file_search snippets are discovery leads, not enough support for promoted PDF/document SourceCards. Then inspect retrieval_channel. retrieval_channel=model_file_read is direct file-read and must be selected for promoted PDF/document SourceCards; retrieval_channel=model_file_read_local_fallback is a risk/coverage signal and does not satisfy direct deep-read coverage. Record the channel honestly in selected evidence and source-card cautions.
- Use list_sources/list_source_versions for source inventory checks, inspect_source for structure, read_source_window/search_within_source only for local text snapshots/transcripts/HTML/chunk fallback, find_quotes for quote candidates, and compare_sources for duplicates, primary/secondary relationships, contradictions, or updates.
- Backend assembles the authoritative SourceMap from write actions. The final JSON response must match SourceTriageCompletion only: ready, summary, notes. Do not return source_cards, quote_cards, or SourceMap-shaped data.
- Rank/map sources by authority, reliability, relevance, uniqueness, and whether they deserve follow-up research. Do not try to finish the episode's research inside triage.
- Identify clusters, duplicates/near-duplicates, primary-to-secondary relationships, contradictions, gaps, and top evidence ids by editorial function.
- SourceCard entries must be updated from tool results: source_id/source_version_ids must be exact inventory ids; evidence_ids must be saved EvidenceItem ids; useful_for must be editorial functions supported by evidence.
- If the source is a PDF/document/native file and you want it in a SourceCard, include at least one selected evidence_id produced by read_source_with_model with retrieval_channel=model_file_read.
- QuoteCard entries must point to saved evidence_id values and include source_id, source_version_id, locator, narrative_use, and confidence.
- source_triage.top_evidence_by_function is a seed/debug index for grounded triage evidence by function. Fill it when you have strong selected seed evidence, but do not treat it as the final evidence plan for the episode.
- top_quote_refs must be evidence_id values or exact source_id:locator refs. Never use raw quote text as a ref.
- If a relationship cannot be grounded in selected evidence_ids, omit it or mark it as a gap/caution.
- If your final summary or notes mention a gap, lacuna, missing primary source, missing direct read, caution, or further source needed, you must first call record_gap for it. Completion notes may summarize persisted gaps, but cannot introduce unpersisted gaps.
- Treat authority labels conservatively: primary means original law/regulator/government/source-owner data or an actor speaking about its own actions; academic papers, journalism, explainers, encyclopedia pages, and repository copies are secondary/tertiary even when reliable.
- Always call finalize_source_map when triage write actions are complete."""


SOURCE_TRIAGE_REPAIR_PROMPT = """Repair source triage write actions for this generation run.

Topic/user prompt:
{user_prompt}

Neutral source inventory. This is not an editorial summary; it only tells you what sources and source versions exist:
{source_inventory_json}

Current assembled SourceMap from previous write actions:
{source_map_json}

Gate failure that must be repaired:
{gate_json}

Purpose:
The previous SourceMap was useful, but it failed backend quality checks. Do not restart from scratch. Add or overwrite write actions only where needed, then call finalize_source_map.

Repair rules:
- For every source_id in gate.missing_model_read_source_ids, if the source still belongs in the SourceMap, call read_source_with_model for that exact source_id, with an empty source_version_id unless you need a specific version.
- Select at least one resulting evidence item only if retrieval_channel is model_file_read and evidence_strength is exact or snippet.
- Re-run upsert_source_card for each repaired source_id and include the selected model_file_read evidence_id in evidence_ids.
- Hosted file_search snippets, local fallback reads, parser windows, and model_file_read_local_fallback do not satisfy this repair.
- If a missing_model_read source should not be promoted, do not create new claims from it; record_gap explaining why direct support is unavailable. Prefer repairing by direct model read when possible because existing SourceCards cannot be trusted without it.
- If gate.unpersisted_completion_gap_notes is non-empty, call record_gap for those exact unresolved cautions before finalizing.
- If gate.invalid_evidence_ids, gate.invalid_source_ids, gate.invalid_source_version_ids, gate.ungrounded_relationships, or gate.missing_function_evidence are non-empty, repair those by selecting valid evidence or overwriting the affected write actions.
- Preserve useful existing source work unless the gate identifies it as invalid.
- Backend assembles the authoritative SourceMap from write actions. The final JSON response must match SourceTriageCompletion only: ready, summary, notes.
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
- Prefer primary/official documents, credible reporting, academic/technical papers, regulator/institution pages, original transcripts, data pages, and deeply reported source material.
- Reject search-result pages, generic SEO summaries, thin aggregators, social posts, unsupported file types, and candidates that do not map to a known gap_id.
- Do not add many same-domain pages unless they are official/primary and each covers a distinct gap.
- Do not rely on web_search snippets as support. Use snippets only to decide whether a URL should be ingested.
- If no trustworthy candidate exists for a gap, reject or leave it unresolved explicitly.

Return only SourceAcquisitionCompletion JSON. The backend records accepted/rejected candidates and will rebuild corpus prepare + source triage after ingestion."""
