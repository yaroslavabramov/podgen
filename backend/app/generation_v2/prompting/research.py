RESEARCH_MEMO_PROMPT = """Build an editorial research memo from the selected corpus evidence.

Topic/user prompt:
{user_prompt}

Target duration: {duration_minutes} minutes
Language: {language}

Show Bible context:
{show_bible_json}

Product context:
The downstream treatment and writers need editorial material, not a list of topics. Strong atoms explain why this story matters and what changes for the listener: conflict, mechanism, history, turning point, consequence, exception, human cost, institutional incentive, and unresolved question.

Compact evidence-backed source inventory. Treat summaries as a triage view, not full source text:
{source_inventory_json}

Compact quote inventory. quote_refs must be evidence_id values or exact source_id:locator refs from this list; never use raw quote text as a ref:
{quote_inventory_json}

Source triage:
{triage_inventory_json}

Task:
- State the thesis and narrative spine in concrete terms.
- Build editorial atoms that each do a distinct job for the future episode.
- For each atom, fill source_ids and evidence_ids with exact values and explain the atom as a story move, not a topic label.
- Separate supported material from open questions and risks.

The memo must be strong enough to support long-form treatment.
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

Judge the memo as if approving or blocking treatment work.
Passing requires specificity, evidence_id coverage, and at least one usable conflict/mechanism/turn.
For 60+ minute targets, fail unless the memo has enough distinct source-grounded atoms for multiple acts."""
