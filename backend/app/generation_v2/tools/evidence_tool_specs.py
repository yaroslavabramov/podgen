from __future__ import annotations

from app.generation_v2.runtime import AgentTool


class EvidenceToolSpecsMixin:
    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool(
                name="list_sources",
                description=(
                    "List neutral metadata for sources selected in this run: source_id, title/url/domain, type, date, "
                    "active source_version_ids, provider status, authority/reliability hints, and a short preview. "
                    "Use this at the start of source triage to plan which sources to skim/read. It does not create evidence."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "filters": {"type": "object", "additionalProperties": True},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "cursor": {"type": "string"},
                    },
                    "required": ["filters", "limit", "cursor"],
                },
                handler=self.list_sources_tool,
            ),
            AgentTool(
                name="search_evidence",
                description=(
                    "Search the selected run corpus for source-backed evidence and persist every returned result as an EvidenceItem. "
                    "Use this when you need support for a specific editorial atom, act gap, quote, or citation-sensitive claim. "
                    "Pass source_ids=[] to search all sources selected for this run; pass source_ids to target known sources. "
                    "Returned evidence_id values are authoritative and must be copied into atoms, quote refs, citations, and repairs. "
                    "For final required factual claims, use evidence_strength exact or snippet; source_level or weak evidence is not sufficient. "
                    "Provider retrieval channels generally mean OpenAI vector/file search found a saved source version; local fallback channels are usable but should be treated with more caution and checked for locator strength."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "purpose": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["query", "purpose", "source_ids", "limit"],
                },
                handler=self.search_evidence_tool,
            ),
            AgentTool(
                name="list_source_versions",
                description=(
                    "List available source versions/representations for one source: original files, page-marked text, rendered/article text, transcript, provider file status, and local readability. "
                    "Use this before inspect_source or read_source_window when you need the best representation for exact reading."
                ),
                parameters={
                    "type": "object",
                    "properties": {"source_id": {"type": "string"}},
                    "required": ["source_id"],
                },
                handler=self.list_source_versions_tool,
            ),
            AgentTool(
                name="read_evidence",
                description=(
                    "Read one already-saved EvidenceItem by evidence_id. Use this to inspect selected evidence before writing, rewriting, or linking a claim. "
                    "Copy source_id, source_version_id, locator_text, snippet_text, retrieval_channel, evidence_strength, and evidence_id exactly from this result when citing it. "
                    "Do not use read_evidence to discover new material; call search_evidence first if no evidence_id is known."
                ),
                parameters={
                    "type": "object",
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                },
                handler=self.read_evidence_tool,
            ),
            AgentTool(
                name="inspect_source",
                description=(
                    "Inspect a source/source version structure: headings, chunk labels, page/window hints, timestamp/section-like locators, and available representations. "
                    "This is for orientation before exact reads; it does not select evidence for the SourceMap."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "max_items": {"type": "integer", "minimum": 1, "maximum": 80},
                    },
                    "required": ["source_id", "source_version_id", "max_items"],
                },
                handler=self.inspect_source_tool,
            ),
            AgentTool(
                name="read_source",
                description=(
                    "Read a concrete window from a selected source/source version and persist that window as an EvidenceItem. "
                    "Use source_id when you want the best readable version, or source_version_id when you need a specific version. "
                    "Strict schema requires both source_id and source_version_id; put an empty string in the one you are not using. "
                    "locator may be a heading/page/timestamp/search phrase; leave empty to read the beginning. "
                    "Returned evidence_id, source_id, source_version_id, locator_text, snippet_text, retrieval_channel, and evidence_strength are the values to copy into SourceMap, QuoteCard, atoms, or citations."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "locator": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 6000},
                    },
                    "required": ["source_id", "source_version_id", "locator", "max_chars"],
                },
                handler=self.read_source_tool,
            ),
            AgentTool(
                name="read_source_window",
                description=(
                    "Fallback/debug reader for local text snapshots, HTML, transcripts, and chunk windows. "
                    "Open an exact page, page range, heading, timestamp, section, beginning, or around-phrase window when local text is available. "
                    "For complex PDFs/documents, prefer read_source_with_model because it lets OpenAI read the uploaded file directly. "
                    "The returned window is persisted as observed EvidenceItem; call select_evidence if you want it in the SourceMap."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "locator": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 6000},
                    },
                    "required": ["source_id", "source_version_id", "locator", "max_chars"],
                },
                handler=self.read_source_tool,
            ),
            AgentTool(
                name="read_source_with_model",
                description=(
                    "Deep-read an uploaded source file with OpenAI input_file and persist the model-read result as an EvidenceItem. "
                    "Use this after hosted file_search identifies an important PDF/document/source, or when citation support requires reading the actual file rather than local parser chunks. "
                    "Provide source_id or source_version_id, a focused question/claim, and optional locator_hint. "
                    "Strict schema requires source_id, source_version_id, focus_question, claim_text, purpose, and locator_hint; put an empty string in unused optional fields or in the unused id field. "
                    "The backend selects the best provider-backed source version, asks the model for strongest support, quote/snippet, support_summary, uncertainty, and optional locator, then returns an evidence_id. "
                    "If provider input_file reading is unavailable, the tool may return retrieval_channel=model_file_read_local_fallback; treat that channel as a risk/coverage signal and reflect it in confidence/support instead of pretending it was a direct file read. "
                    "Call select_evidence if the result should enter SourceMap/research/citations. Page numbers are useful when reported, but not required."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "focus_question": {"type": "string"},
                        "claim_text": {"type": "string"},
                        "purpose": {"type": "string"},
                        "locator_hint": {"type": "string"},
                    },
                    "required": ["source_id", "source_version_id", "focus_question", "claim_text", "purpose", "locator_hint"],
                },
                handler=self.read_source_with_model_tool,
            ),
            AgentTool(
                name="search_within_source",
                description=(
                    "Search for a phrase/topic inside exactly one selected source, persist matching windows as observed EvidenceItems, and return evidence ids. "
                    "Use this for targeted follow-up after file_search finds a promising source."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 12},
                    },
                    "required": ["source_id", "source_version_id", "query", "limit"],
                },
                handler=self.search_within_source_tool,
            ),
            AgentTool(
                name="skim_source",
                description=(
                    "Skim one selected source/source version and persist representative windows as EvidenceItems. "
                    "Use this before judging a source's role, structure, likely authority, duplicate status, or editorial usefulness. "
                    "The result includes outline/section labels and evidence_id-backed windows; copy those evidence ids into the SourceMap when used."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "max_windows": {"type": "integer", "minimum": 1, "maximum": 12},
                    },
                    "required": ["source_id", "source_version_id", "max_windows"],
                },
                handler=self.skim_source_tool,
            ),
            AgentTool(
                name="find_quotes",
                description=(
                    "Find quote-like snippets about a specific topic and persist them as EvidenceItems. "
                    "Use during source triage, research, and citation linking when a direct quote or quote-like support is needed. "
                    "This is not a generic source search tool; for general evidence use search_evidence. "
                    "Quote refs should be evidence_id values or source_id:locator refs from returned evidence, never raw quote text."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                    },
                    "required": ["topic", "source_ids", "limit"],
                },
                handler=self.find_quotes_tool,
            ),
            AgentTool(
                name="search_sources",
                description=(
                    "Search for source-level candidates and evidence-backed snippets for corpus/source triage only. "
                    "Use this to rank sources, detect source clusters, identify primary/secondary or duplicate relationships, and find gaps. "
                    "Do not use search_sources in treatment, writing, rewriting, critique, or final citation linking."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 30},
                    },
                    "required": ["query", "limit"],
                },
                handler=self.search_sources_tool,
            ),
            AgentTool(
                name="compare_sources",
                description=(
                    "Compare selected sources for a specific question during triage: duplicate/near-duplicate, primary vs secondary, updates, contradictions, or coverage gaps. "
                    "Internally retrieves and persists evidence per source, then returns evidence_id-backed comparison inputs. "
                    "Use it only in source triage/research, not drafting or final citation linking."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "question": {"type": "string"},
                        "limit_per_source": {"type": "integer", "minimum": 1, "maximum": 6},
                    },
                    "required": ["source_ids", "question", "limit_per_source"],
                },
                handler=self.compare_sources_tool,
            ),
            AgentTool(
                name="select_evidence",
                description=(
                    "Write action: explicitly select an observed EvidenceItem as working support for the SourceMap, research, quote, relationship, gap, or citation. "
                    "Only selected evidence can enter SourceMap. Provide the evidence_id exactly as returned by file_search/read/search tools."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "purpose": {"type": "string"},
                        "role": {"type": "string"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["evidence_id", "purpose", "role", "rationale"],
                },
                handler=self.select_evidence_tool,
            ),
            AgentTool(
                name="upsert_source_card",
                description=(
                    "Write action: create/update a SourceCard from selected evidence. evidence_ids must already be selected with select_evidence."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "useful_for": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "gaps_or_cautions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["source_id", "summary", "useful_for", "evidence_ids", "gaps_or_cautions"],
                },
                handler=self.upsert_source_card_tool,
            ),
            AgentTool(
                name="create_quote_card",
                description=(
                    "Write action: create a QuoteCard from selected evidence. quote text must come from the selected EvidenceItem snippet/window."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "source_id": {"type": "string"},
                        "source_version_id": {"type": "string"},
                        "quote": {"type": "string"},
                        "locator": {"type": "string"},
                        "narrative_use": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "confidence_reason": {"type": "string"},
                    },
                    "required": [
                        "evidence_id",
                        "source_id",
                        "source_version_id",
                        "quote",
                        "locator",
                        "narrative_use",
                        "confidence",
                        "confidence_reason",
                    ],
                },
                handler=self.create_quote_card_tool,
            ),
            AgentTool(
                name="record_source_relationship",
                description=(
                    "Write action: record duplicate, near_duplicate, primary_to_secondary, contradicts, updates, or background_for relationships using selected evidence ids."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_id": {"type": "string"},
                        "related_source_id": {"type": "string"},
                        "relationship": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["source_id", "related_source_id", "relationship", "evidence_ids", "rationale"],
                },
                handler=self.record_source_relationship_tool,
            ),
            AgentTool(
                name="record_gap",
                description=(
                    "Write action: record a gap, contradiction, or caution discovered during triage/research, with selected evidence ids where applicable."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "description": {"type": "string"},
                        "source_ids": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["kind", "description", "source_ids", "evidence_ids"],
                },
                handler=self.record_gap_tool,
            ),
            AgentTool(
                name="finalize_source_map",
                description=(
                    "Write action: signal that triage write actions are complete and SourceMap should be assembled by the backend from selected evidence and write actions."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "summary": {"type": "string"},
                        "ready": {"type": "boolean"},
                    },
                    "required": ["summary", "ready"],
                },
                handler=self.finalize_source_map_tool,
            ),
        ]

__all__ = ["EvidenceToolSpecsMixin"]
