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

Available evidence-backed source cards. Treat summary text as a triage view, not the full source:
{source_cards_json}

Available evidence-backed quote cards. quote_refs should refer to quote text snippets or locators from this list:
{quote_cards_json}

Source triage / map:
{source_map_json}

The memo must be strong enough to support long-form treatment. Include concrete editorial atoms and source coverage.
For each editorial atom, fill source_ids and evidence_ids with exact values and explain the atom as a story move, not a topic label.
If you use a corpus tool result, preserve its evidence_id in the relevant atom."""


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
- Use concrete source-grounded material from the evidence-backed dossier.
- Avoid repeated openers, repeated metaphors, and generic podcast phrasing.
- Hosts should move the story forward through questions, objections, mechanisms, and turns.
- Keep lines TTS-friendly.
- Keep speaker field canonical: only "A" or "B"; host names belong only in natural spoken text when useful.
- Never use forbidden phrases from the Show Bible.
- Fill used_source_ids only with exact ids used in the act.
- Put claim_refs on dialogue lines when they contain citation-sensitive claims that should be linked later.
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

Source cards:
{source_cards_json}

Quote cards:
{quote_cards_json}

Draft:
{draft_json}

Check repetition, pacing, weak sections, unsupported leaps, missing payoffs, flat transitions, forbidden phrases, and host dynamic drift.
If the draft should be rewritten before citation finalization, set passed=false and provide concrete rewrite_instructions."""


CLAIM_CITATION_PROMPT = """Extract citation-sensitive claims from the episode draft and link them to saved evidence.

Source cards:
{source_cards_json}

Quote cards:
{quote_cards_json}

Available evidence records:
{evidence_json}

Draft:
{draft_json}

Episode critique:
{critique_json}

If a claim is weak or unsupported, create a fact issue with a repair instruction.
Every fact issue must include the act_number of the related claim.
Only create citation links to evidence_id values that actually support the claim.
Every citation-sensitive required claim must have a CitationLink.evidence_id, source_id, source_version_id, locator, supports_span, and confidence.
Do not treat a source mention by itself as support."""


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
