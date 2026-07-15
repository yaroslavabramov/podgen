TREATMENT_PROMPT = """Create a full episode treatment.

Treatment input:
{treatment_input_json}

Show Bible context:
{show_bible_json}

Product context:
The treatment is the contract for writing. It turns research atoms into a listener-facing act sequence: what each act earns, why it belongs there, which sources it leans on, and how the listener's understanding changes.

Task:
Create a coherent act sequence from the research_atoms. episode_brief.act_count_range is rough pacing guidance, not a quota: choose the count from the research shape and do not split or pad material merely to hit the range. Use fewer acts for a clean linear argument, more acts when the listener genuinely needs separate turns. Each act must have a clear editorial job, turn, source focus, and atom anchors.
Write every human-readable output field in episode_brief.output_language.

The treatment creates its own title, logline, and act sequence from the atoms. Preserve episode_brief.supported_answer as the episode-level supported answer while deciding the sequence; do not invent a different answer or a separate synthesis.

Use the handles from treatment_input_json:
- source_focus: use source_ref values like S01.
- required_atom_refs: use atom_ref values like A01.
- selected_quotes contains the complete exact quotes chosen during research. Each research atom lists its related Q refs in selected_quote_refs; use the quote text and locator when it improves the act's opening, turn, or payoff. Q refs are orientation only and are attached to the final treatment by the backend.
- Keys ending in _preview contain text plus completeness metadata. Use only their text as editorial material; complete=false means the full persisted field is longer, not that you may invent the missing part.
- research_atoms, source_coverage, source_cautions, audit_constraints, and repair_instructions are complete validated fields/lists. Respect every supplied caution, constraint, and repair instruction.
- source_coverage is the research-status handoff: use supported entries to judge confidence, and preserve every partial/deferred finding or gap as a bound on the plan. Do not silently turn it into a resolved fact; use a caution or planning_gap when it affects the episode structure.

Use atom weights:
- core atoms can anchor acts.
- supporting atoms can strengthen an act but should not be the main anchor alone.
- caveat atoms are constraints or bounded exceptions; do not build a full act around a caveat unless core atoms carry the act.

Audit cautions are constraints, not story beats. Do not build an act around a minor caveat. If the episode structure needs weak/partial/caveat material as a major act, add a concise planning_gap instead of hiding the weakness in prose.
Do not say "the research is incomplete" in the episode. Either use uncertainty as a bounded caveat or route it through planning_gaps.

Allocate target_minutes from editorial difficulty and listener work, not equal blocks.
Avoid generic act labels. The title/story_role/opening_question/main_turn/ending_turn should make clear what changes in that act.
Every act must contain at least one required_atom_ref. Number acts exactly 1..N with no duplicates, use only supplied source_ref/atom_ref handles, keep every required text field non-empty, and make every target_minutes positive.
For every research atom with story_weight="core", add exactly one core_atom_disposition. Use disposition="used" with the act_number of an act whose required_atom_refs contains that atom. Use disposition="omitted" with act_number=null and a concrete editorial reason. This makes omission explicit; it does not require forcing every core atom into the episode.
If repair_instructions is present, correct every listed contract error while preserving supported material.
If planning_gaps is non-empty, still return your best TreatmentPlan draft, but the backend gate will route the gaps back to research before writing.
Invalid output examples: duplicate act numbers; blank turns; an act without required_atom_refs; an unknown S/A handle; zero or negative target_minutes; a used core atom pointing to an act that does not contain it; an omitted core atom that still appears in an act.
Return only TreatmentPlan JSON with exactly the schema fields. Do not write dialogue, scenes, transcript, commentary, or act prose."""


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
Use read_evidence for suspicious leaps tied to existing evidence ids. Use search_corpus only to check whether a weak passage appears repairable from the selected corpus; do not introduce new facts or new story angles in critique.
If the draft should be rewritten before citation finalization, set passed=false and provide concrete rewrite_instructions."""
