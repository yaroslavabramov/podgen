RESEARCH_MEMO_PROMPT = """Build an editorial research memo from the project corpus.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Show Bible context:
{show_bible_json}

Product context:
The downstream treatment and writers need editorial material, not a list of topics. Strong atoms explain why this story matters and what changes for the listener: conflict, mechanism, history, turning point, consequence, exception, human cost, institutional incentive, and unresolved question.

Full neutral source inventory. This is the project corpus available to research; it may include sources that triage did not promote into SourceCards:
{full_source_inventory_json}

SourceMap seed inventory. Treat these summaries as triage orientation, not full source text and not the final evidence budget:
{source_map_inventory_json}

Compact quote inventory. quote_refs must be evidence_id values or exact source_ref:locator refs from this list; never use raw quote text as a ref:
{quote_inventory_json}

Source triage map:
{triage_inventory_json}

Task:
- State the thesis and narrative spine in concrete terms.
- Build editorial atoms that each do a distinct job for the future episode.
- Search, read, compare, and select corpus evidence as needed before creating atoms.
- For each atom, fill source_ids and evidence_ids with exact values from selected saved EvidenceItems and explain the atom as a story move, not a topic label.
- Separate supported material from open questions and risks.

The memo must be strong enough to support long-form treatment.
Research is an agentic investigation phase over the provided corpus. The SourceMap is a starting map: use it to orient, avoid duplicates, and find seed leads, but do not treat SourceMap evidence_ids as the only allowed support.
Use search_corpus, find_quotes, search_within_source, compare_source_evidence, read_source_window, and read_source_with_model to create new EvidenceItems when the memo needs stronger support. Call select_evidence for any new EvidenceItem you use in an editorial atom.
For high-impact atoms (conflict, mechanism, turning_point, consequence, human_cost, institutional_incentive), inspect the supporting evidence with read_evidence or a direct source-read tool before including the atom.
If a read tool returns coverage_warning, treat that warning as a source-coverage risk, not an automatic rejection. Explain uncertainty in the atom risk/source_coverage_status when appropriate.
Use evidence_id values shown in the SourceMap/quote inventory or returned by the provided tools. If you discover new support with search/read tools, call select_evidence before using that evidence_id in an atom. Use source_ref only for choosing or relating sources; evidence_id is the handle for support."""


RESEARCH_AUDIT_PROMPT = """Audit this research memo for long-form podcast readiness.

Target duration: {duration_minutes} minutes
Minimum expectation: enough concrete editorial material for every act.

Research memo:
{research_memo_json}

Source map:
{source_map_json}

Judge the memo as if approving or blocking treatment work.
Passing requires specificity, evidence_id coverage, and at least one usable conflict/mechanism/turn.
For 60+ minute targets, fail unless the memo has enough distinct source-grounded atoms for multiple acts."""
