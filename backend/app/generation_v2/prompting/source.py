SOURCE_TRIAGE_PROMPT = """Run source triage for this generation run.

Topic/user prompt:
{user_prompt}

Neutral source inventory. This is not an editorial summary; it only tells you what sources and source versions exist:
{source_inventory_json}

Purpose:
Later agents should be able to trust the SourceMap without re-discovering the corpus from scratch. Your output is not the SourceMap itself; your write actions are.

You must work in two phases before returning JSON:
1. Retrieval/read plan: use hosted file_search for broad semantic discovery over uploaded source files, then decide which important sources need read_source_with_model, inspect_source, list_source_versions, skim_source, read_source_window, search_within_source, find_quotes, search_sources, or compare_sources.
2. Write actions: explicitly call select_evidence for evidence you want to keep, then upsert_source_card, create_quote_card, record_source_relationship, record_gap, and finalize_source_map.

Rules:
- Use only the provided source inventory or evidence tool results.
- Call tools unless the corpus is tiny and the inventory already contains explicit evidence ids. A source map built only from the prompt inventory is not acceptable.
- Hosted file_search is the primary broad retrieval tool over uploaded source versions.
- Use read_source_with_model for focused deep reading of important uploaded PDFs/documents/files found via file_search. Then inspect retrieval_channel. retrieval_channel=model_file_read is direct file-read; retrieval_channel=model_file_read_local_fallback is a risk/coverage signal, not an automatic rejection. Record the channel honestly in selected evidence and source-card cautions.
- Use list_sources/list_source_versions for source inventory checks, inspect_source for structure, read_source_window/search_within_source only for local text snapshots/transcripts/HTML/chunk fallback, find_quotes for quote candidates, and compare_sources for duplicates, primary/secondary relationships, contradictions, or updates.
- Backend assembles the authoritative SourceMap from write actions. The final JSON response must match SourceTriageCompletion only: ready, summary, notes. Do not return source_cards, quote_cards, or SourceMap-shaped data.
- Rank sources by authority, reliability, relevance, uniqueness, and usable evidence density.
- Identify clusters, duplicates/near-duplicates, primary-to-secondary relationships, contradictions, gaps, and top evidence ids by editorial function.
- SourceCard entries must be updated from tool results: source_id/source_version_ids must be exact inventory ids; evidence_ids must be saved EvidenceItem ids; useful_for must be editorial functions supported by evidence.
- QuoteCard entries must point to saved evidence_id values and include source_id, source_version_id, locator, narrative_use, and confidence.
- source_triage.top_evidence_by_function must contain saved evidence ids for the strongest functions you can ground.
- top_quote_refs must be evidence_id values or exact source_id:locator refs. Never use raw quote text as a ref.
- If a relationship cannot be grounded in selected evidence_ids, omit it or mark it as a gap/caution.
- Always call finalize_source_map when triage write actions are complete."""


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
