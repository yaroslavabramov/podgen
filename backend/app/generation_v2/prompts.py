V2_SYSTEM_BASE = """You are Podgen Generation V2, an editorial podcast production system.
Use the controlled corpus/evidence tools as the only authoritative source access surface.
Every meaningful factual or editorial move must be traceable to saved evidence_id values:
conflict, mechanism, history, turn, price, exception, consequence, and unresolved questions.

Write specific, non-generic material. Avoid repeated phrases, rhetorical filler, and reusable podcast cliches.
When evidence is uncertain, mark uncertainty instead of inventing support.
Use only source_id, source_version_id, and evidence_id values that appear in the provided source map,
source cards, quote cards, dossiers, or evidence tool results.
If the provided material is not enough, represent that as gaps, weak support, or repair instructions.
For required schema fields with no content, return empty arrays, empty objects, or null where the schema allows it.
Return only JSON matching the requested schema."""


RESEARCH_MEMO_PROMPT = """Build an editorial research memo from the corpus.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Show Bible context:
{show_bible_json}

Compact evidence-backed source inventory. Treat summaries as a triage view, not full source text:
{source_inventory_json}

Compact quote inventory. quote_refs must be evidence_id values or exact source_id:locator refs from this list; never use raw quote text as a ref:
{quote_inventory_json}

Source triage:
{triage_inventory_json}

The memo must be strong enough to support long-form treatment. Include concrete editorial atoms and source coverage.
For each editorial atom, fill source_ids and evidence_ids with exact values and explain the atom as a story move, not a topic label.
Research is a synthesis phase over the authoritative SourceMap, not a new source-selection phase. Use source-map evidence_ids for atoms. If the SourceMap is insufficient, record that as an open question or risk for follow-up triage rather than introducing new evidence ids.
For high-impact atoms (conflict, mechanism, turning_point, consequence, human_cost, institutional_incentive), call read_evidence to inspect SourceMap evidence when the compact inventory is insufficient.
If a SourceMap evidence record has retrieval_channel=model_file_read_local_fallback, treat that channel as a risk/coverage signal, not an automatic rejection. Explain uncertainty in the atom risk/source_coverage_status when appropriate.
Do not put an evidence_id in an atom unless that evidence_id already appears in the provided SourceMap inventory. Do not create atoms that cannot be anchored to SourceMap evidence."""


RESEARCH_AUDIT_PROMPT = """Audit this research memo for long-form podcast readiness.

Target duration: {duration_minutes} minutes
Minimum expectation: enough concrete editorial material for every act.

Research memo:
{research_memo_json}

Source map:
{source_map_json}

Return a strict audit. Passing requires specificity, evidence_id coverage, and at least one usable conflict/mechanism/turn.
For 60+ minute targets, fail unless the memo has enough distinct source-grounded atoms for multiple acts."""


TREATMENT_PROMPT = """Create a full episode treatment.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Research memo:
{research_memo_json}

Source map:
{source_map_json}

Show Bible context:
{show_bible_json}

Use {act_count} acts. Each act must have a clear editorial job, turn, and source focus.
Every TreatmentAct.source_focus entry must be an exact source_id from the memo. Do not invent source ids.
Every act should include required_evidence_ids drawn from the research memo/source map when factual material is required.
Use targeted tools only for specific missing treatment requirements: read_evidence, read_source_window, or search_within_source. Do not broadly research new angles in treatment.
Allocate target_minutes so the acts add up approximately to the target duration."""


ACT_DRAFT_PROMPT = """Write act {act_number} as dialogue between hosts A and B.

Language: {language}
Target act duration: about {target_minutes} minutes.

Show Bible context:
{show_bible_json}

Treatment act:
{treatment_act_json}

Act dossier:
{dossier_json}

Continuity ledger:
{ledger_json}

Rules:
- Write the act as a coherent section, not isolated scenes.
- Use concrete source-grounded material from the evidence-backed dossier first.
- You may call read_evidence, read_source_window, or search_within_source only for a targeted gap in this act's dossier. Do not conduct broad research in the writing phase.
- Before using any required evidence_id in speech, call read_evidence unless the dossier already includes the exact quote/snippet text for that evidence through a QuoteCard. Do not write factual detail from an evidence_id you have not inspected in this act context.
- Avoid repeated openers, repeated metaphors, and generic podcast phrasing.
- Hosts should move the story forward through questions, objections, mechanisms, and turns.
- Keep lines TTS-friendly.
- Keep speaker field canonical: only "A" or "B"; host names belong only in natural spoken text when useful.
- Never use forbidden phrases from the Show Bible.
- Fill used_source_ids only with exact ids used in the act.
- claim_refs are optional temporary fact-check hints only, formatted like needs_citation:<short_slug>. They are not claim ids, citation ids, evidence ids, or source ids.
- Put phrases you may be overusing into repeated_phrase_watchlist."""


ACT_REWRITE_PROMPT = """Revise act {act_number} using the critique and repair constraints.

Language: {language}

Show Bible context:
{show_bible_json}

Treatment act:
{treatment_act_json}

Act dossier:
{dossier_json}

Current draft:
{draft_json}

Continuity ledger:
{ledger_json}

Episode critique:
{critique_json}

Fact issues for this act:
{fact_issues_json}

Rules:
- Preserve the ActDraft schema and act_number.
- Apply only the critique/repair instructions that are relevant to this act.
- You may call read_evidence, read_source_window, or search_within_source only to repair a known gap or fact issue. Do not conduct broad research in rewrite.
- Remove or soften unsupported claims instead of adding unsupported new claims.
- Keep speaker field canonical: only "A" or "B".
- Never use forbidden phrases from the Show Bible.
- Preserve strong evidence-backed material, evidence ids, and source ids.
- Reduce repeated phrasing and improve transitions."""


EPISODE_CRITIQUE_PROMPT = """Critique the complete episode draft.

Treatment:
{treatment_json}

Show Bible context:
{show_bible_json}

Compact source inventory:
{source_cards_json}

Compact quote inventory:
{quote_cards_json}

Draft:
{draft_json}

Check repetition, pacing, weak sections, unsupported leaps, missing payoffs, flat transitions, forbidden phrases, and host dynamic drift.
Use read_evidence for suspicious leaps tied to existing evidence ids. Use search_evidence only to check whether a weak passage appears repairable from the selected corpus; do not introduce new facts or new story angles in critique.
If the draft should be rewritten before citation finalization, set passed=false and provide concrete rewrite_instructions."""


CLAIM_CITATION_PROMPT = """Extract citation-sensitive claims from the episode draft and link them to saved evidence.

Compact source inventory:
{source_inventory_json}

Compact quote inventory:
{quote_inventory_json}

Compact available evidence inventory:
{evidence_inventory_json}

Draft:
{draft_json}

Episode critique:
{critique_json}

Hosted file_search is available for missing claim support. If it surfaces a promising source, call read_source_with_model, inspect the returned retrieval_channel, and select_evidence before linking it.
If read_source_with_model returns retrieval_channel=model_file_read_local_fallback, treat that as a risk/coverage signal: it is not a direct OpenAI file read, but it may still be considered by validation if saved evidence and support judgment support the claim.
If a claim is weak or unsupported, create a fact issue with a repair instruction.
Every fact issue must include the act_number of the related claim.
Only create citation links to evidence_id values that actually support the claim.
Every citation-sensitive required claim must have a CitationLink.evidence_id, source_id, source_version_id, locator, supports_span, and confidence.
Do not treat a source mention by itself as support.

Claim schema rules:
- claim_id must be unique in this bundle and stable, for example act2-line14-mechanism.
- line_index must be the zero-based index of the dialogue line within the full draft line list you are citing.
- text_span must be the exact claim-bearing text copied from the dialogue line.
- claim_type=factual for concrete facts; quote for direct/near quote; statistical for numbers; historical for dated events; causal for cause/effect; interpretive for analysis that still depends on sources.
- source_need=required for factual, quote, statistical, historical, and causal claims; recommended for interpretive claims with factual anchors; none only for pure host opinion or transitions.
- risk_level=high for numbers, dates, named allegations, medical/legal/financial/policy claims, or strong causality; medium for ordinary factual claims; low for low-stakes background.
- support_level=supported only when saved evidence directly supports the text_span; weak when relevant but incomplete; unsupported when no saved evidence supports it.

CitationLink rules:
- If compact evidence is insufficient, use hosted file_search or search_evidence with a targeted query. When a promising uploaded file is found, call read_source_with_model before creating a CitationLink. Then inspect retrieval_channel and select_evidence before linking. If the result is model_file_read_local_fallback, reflect that risk in confidence/support_level instead of pretending it was a direct file read.
- Copy evidence_id, source_id, source_version_id, locator from the saved evidence record. locator must be the evidence locator_text.
- supports_span must quote or closely paraphrase the saved snippet_text that supports the claim.
- confidence must reflect evidence strength: exact quote/snippet/model_file_read evidence can be high; provider snippet usually medium/high; source_level or weak evidence is not valid for required factual claims.
- Never invent source_id, source_version_id, evidence_id, locator, or quote_card_ref values.

FactIssue rules:
- blocking=true for missing evidence, weak/source-level support on required factual claims, unsupported claims, quote mismatch risk, or low citation confidence.
- blocking=false only for low-severity style or context notes that do not affect factual support."""


SOURCE_TRIAGE_PROMPT = """Run source triage for this generation run.

Topic/user prompt:
{user_prompt}

Neutral source inventory. This is not an editorial summary; it only tells you what sources and source versions exist:
{source_inventory_json}

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


SOURCE_ACQUISITION_PROMPT = """Acquire additional sources for V2 corpus coverage.

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


SUPPORT_JUDGMENT_PROMPT = """Judge whether saved evidence supports each claim-link pair.

You are not allowed to introduce new evidence or rely on memory. Use only the claim, citation link, and evidence record supplied here.
Return one judgment per claim/evidence pair.

Claim/citation/evidence pairs:
{pairs_json}

Rules:
- supported: the evidence snippet directly supports the claim as written.
- weak: the evidence is relevant but not enough for the claim's strength.
- unsupported: the evidence does not support the claim.
- overstrong: the claim is stronger or broader than the evidence.
- needs_attribution: the claim can stand only if attributed or hedged.
Set blocking=true for unsupported, overstrong, needs_attribution, or weak support on required factual claims."""
