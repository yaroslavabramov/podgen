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

Product context:
The treatment is the contract for writing. It decides what each act earns, which evidence it depends on, and how the listener's understanding changes.

Use {act_count} acts. Each act must have a clear editorial job, turn, and source focus.
Every TreatmentAct.source_focus entry must be an exact source_id from the memo. Do not invent source ids.
Every act should include required_evidence_ids drawn from the research memo/source map when factual material is required.
Use targeted tools only for specific missing treatment requirements: read_evidence, read_source_window, or search_within_source. Do not broadly research new angles in treatment.
Allocate target_minutes from editorial difficulty and listener work, not equal blocks.
Avoid generic act labels. The title/story_role/opening_question/main_turn/ending_turn should make clear what changes in that act."""


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
- Host B must add substance: skepticism, synthesis, counterexample, pressure test, or listener-facing reframing. Do not default to B asking and A explaining.
- Start from the act's current editorial job. Do not reset to beginner exposition if previous acts/ledger already established the basics.
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
- Reduce repeated phrasing and improve transitions.
- Keep the listener work of the act intact: repair weak lines without changing the act into a different episode section."""


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

Read as a complete episode, not as independent acts.
Check repetition, pacing, weak sections, unsupported leaps, missing payoffs, flat transitions, forbidden phrases, and host dynamic drift.
Use read_evidence for suspicious leaps tied to existing evidence ids. Use search_evidence only to check whether a weak passage appears repairable from the selected corpus; do not introduce new facts or new story angles in critique.
If the draft should be rewritten before citation finalization, set passed=false and provide concrete rewrite_instructions."""
