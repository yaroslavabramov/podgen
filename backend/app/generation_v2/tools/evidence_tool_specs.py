from __future__ import annotations

from app.generation_v2.runtime import AgentTool


class EvidenceToolSpecsMixin:
    def agent_tools(self) -> list[AgentTool]:
        return [
            AgentTool(
                name="list_sources",
                description=(
                    "List neutral metadata for sources selected in this run: source_ref, title/domain, type, date, "
                    "canonical kind, coverage status, and a short brief_hint. "
                    "Use this at the start of source triage to plan which sources to skim/read. It does not create evidence. "
                    "Request up to 25 sources per page; large corpora should be paged deliberately rather than loaded from the prompt."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 25},
                        "cursor": {"type": "string"},
                    },
                    "required": ["limit", "cursor"],
                },
                handler=self.list_sources_tool,
            ),
            AgentTool(
                name="search_corpus",
                description=(
                    "Search the selected run corpus and persist returned passage matches as observed EvidenceItems. "
                    "This is a discovery preview tool, not a final evidence reader: use it to find leads for a specific investigation question, source comparison, quote, relation, or citation-sensitive claim. "
                    "It is not a substitute for skimming a source when you need to understand that source's overall role. "
                    "Use scope=all_sources for broad discovery, or scope=selected_sources with source_refs from the current prompt or prior tool results to target known sources. "
                    "The returned short_snippet is a preview with an evidence_id handle; call read_evidence, skim_source, inspect_source, or read_source_window when the preview is not enough before writing a durable conclusion."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "minLength": 3},
                        "purpose": {"type": "string"},
                        "scope": {"type": "string", "enum": ["all_sources", "selected_sources"]},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    "required": ["query", "purpose", "scope", "source_refs", "limit"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.search_corpus_tool, args),
            ),
            AgentTool(
                name="read_evidence",
                description=(
                    "Read one already-saved EvidenceItem by evidence_id. Use this to inspect selected evidence before writing, rewriting, or linking a claim. "
                    "The result is a readable detail payload with source_ref/title, locator, snippet, and model-read support notes when available. "
                    "Do not use read_evidence to discover new material; call search_corpus first if no evidence_id is known."
                ),
                parameters={
                    "type": "object",
                    "properties": {"evidence_id": {"type": "string"}},
                    "required": ["evidence_id"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.read_evidence_tool, args),
            ),
            AgentTool(
                name="inspect_source",
                description=(
                    "Inspect one source's readable structure: headings, chunk labels, page/window hints, and timestamp/section-like locators. "
                    "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface. "
                    "Use this when you need locators before exact reads; it does not create evidence."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ref": {"type": "string"},
                        "version_ref": {"type": "string"},
                        "max_items": {"type": "integer", "minimum": 1, "maximum": 40},
                    },
                    "required": ["source_ref", "max_items"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.inspect_source_tool, args),
            ),
            AgentTool(
                name="read_source_window",
                description=(
                    "Read one concrete local text window from a selected source and persist that window as an EvidenceItem. "
                    "This does not read the whole source; it opens an exact page, page range, heading, timestamp, section, beginning, or around-phrase window when local text/chunks are available. "
                    "If skim_source is available and the source is unfamiliar, skim first; use read_source_window as a follow-up for exact support, quote seeds, relationship evidence, or an editorial atom. "
                    "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface. "
                    "For PDF/document sources this uses extracted/page-marked text; use read_source_with_model when an important claim needs direct file-level support. "
                    "Use the returned evidence_id in durable write actions after you verify the readable detail."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ref": {"type": "string"},
                        "version_ref": {"type": "string"},
                        "locator": {"type": "string"},
                        "max_chars": {"type": "integer", "minimum": 200, "maximum": 3000},
                    },
                    "required": ["source_ref", "locator", "max_chars"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.read_source_window_tool, args),
            ),
            AgentTool(
                name="read_source_with_model",
                description=(
                    "Deep-read an uploaded source file with OpenAI input_file and persist the model-read result as an EvidenceItem. "
                    "Use this when an important PDF/document/source claim requires reading the actual file rather than relying on local parser chunks or search previews. "
                    "Provide source_ref, a focused question/claim, and optional locator_hint; version_ref is optional and should usually be omitted so the tool chooses the best provider-backed readable file. "
                    "The tool asks the model for strongest support, quote/snippet, support_summary, uncertainty, and optional locator, then returns an evidence_id. "
                    "If direct file reading is unavailable, the tool returns a human-readable coverage_warning. "
                    "Research atoms select included evidence automatically through upsert_editorial_atom. Citation linking may still select evidence explicitly. "
                    "Page numbers are useful when reported, but not required."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ref": {"type": "string"},
                        "version_ref": {"type": "string"},
                        "focus_question": {"type": "string"},
                        "claim_text": {"type": "string"},
                        "purpose": {"type": "string"},
                        "locator_hint": {"type": "string"},
                    },
                    "required": ["source_ref", "focus_question", "claim_text", "purpose", "locator_hint"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.read_source_with_model_tool, args),
            ),
            AgentTool(
                name="search_within_source",
                description=(
                    "Search for a phrase/topic inside exactly one selected source, persist matching windows as observed EvidenceItems, and return evidence ids. "
                    "Use this for targeted follow-up after skim_source, inspect_source, or corpus search finds a promising source. "
                    "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ref": {"type": "string"},
                        "version_ref": {"type": "string"},
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    "required": ["source_ref", "query", "limit"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.search_within_source_tool, args),
            ),
            AgentTool(
                name="skim_source",
                description=(
                    "Default first-read tool for one selected source. Skim representative windows and persist them as EvidenceItems. "
                    "Use this before judging a source's role, scope, structure, duplicate status, editorial usefulness, or whether it can support a research atom. "
                    "Pass source_ref; version_ref is optional and should usually be omitted so the tool chooses the best readable surface. "
                    "For normal triage or research orientation, request 3-5 windows, then use read_source_window or read_source_with_model for exact follow-up."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ref": {"type": "string"},
                        "version_ref": {"type": "string"},
                        "max_windows": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    "required": ["source_ref", "max_windows"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.skim_source_tool, args),
            ),
            AgentTool(
                name="find_quotes",
                description=(
                    "Find quote-like snippets about a specific topic and persist them as EvidenceItems. "
                    "Use when a direct quote or quote-like support is needed for a durable entry, research atom, or citation link. "
                    "This is not a generic source search tool; for general evidence use search_corpus. "
                    "Quote refs should be evidence_id values or source_ref:locator refs from returned evidence, never raw quote text."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    "required": ["topic", "source_refs", "limit"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.find_quotes_tool, args),
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
                        "limit": {"type": "integer", "minimum": 1, "maximum": 8},
                    },
                    "required": ["query", "limit"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.search_sources_tool, args),
            ),
            AgentTool(
                name="compare_source_evidence",
                description=(
                    "Retrieve comparable evidence previews from 2-5 selected sources for one source-map question: duplication, interpretive relationship, update chain, contradiction, coverage overlap, or quality gap. "
                    "The tool searches each source with the same question and returns evidence_id-backed inputs grouped by source_ref. "
                    "Use this when a cluster or relationship needs multiple sources read together; write the resulting cluster/relationship/gap afterward."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 5,
                        },
                        "question": {"type": "string"},
                        "limit_per_source": {"type": "integer", "minimum": 1, "maximum": 3},
                    },
                    "required": ["source_refs", "question", "limit_per_source"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.compare_source_evidence_tool, args),
            ),
            AgentTool(
                name="compare_evidence_for_question",
                description=(
                    "Retrieve comparable evidence previews from 2-5 selected sources for one research question inside the current investigation job. "
                    "Use this when an editorial atom needs several sources read together. After comparing, persist a research observation or an editorial atom draft."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 2,
                            "maxItems": 5,
                        },
                        "question": {"type": "string"},
                        "limit_per_source": {"type": "integer", "minimum": 1, "maximum": 3},
                    },
                    "required": ["source_refs", "question", "limit_per_source"],
                },
                handler=lambda args: self._call_with_research_commit_advisory(self.compare_evidence_for_question_tool, args),
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
                    "Triage write action: create/update a SourceCard from evidence returned by read/search tools. "
                    "Included evidence_ids are marked as selected for SourceMap lineage."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ref": {"type": "string"},
                        "summary": {"type": "string"},
                        "useful_for": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "gaps_or_cautions": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["source_ref", "summary", "useful_for", "evidence_ids", "gaps_or_cautions"],
                },
                handler=self.upsert_source_card_tool,
            ),
            AgentTool(
                name="create_quote_card",
                description=(
                    "Triage write action: create an optional QuoteCard from an evidence_id returned by read/search tools. "
                    "Use only for an obvious quote seed; quote text must come from the evidence snippet/window."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "evidence_id": {"type": "string"},
                        "quote": {"type": "string"},
                        "locator": {"type": "string"},
                        "narrative_use": {"type": "string"},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "confidence_reason": {"type": "string"},
                    },
                    "required": [
                        "evidence_id",
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
                    "Triage write action: record duplicate, near_duplicate, primary_to_secondary, contradicts, updates, or background_for relationships. "
                    "Use evidence_ids returned by read/search tools; included evidence_ids are marked as selected."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_ref": {"type": "string"},
                        "related_source_ref": {"type": "string"},
                        "relationship": {"type": "string"},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                    },
                    "required": ["source_ref", "related_source_ref", "relationship", "evidence_ids", "rationale"],
                },
                handler=self.record_source_relationship_tool,
            ),
            AgentTool(
                name="record_source_cluster",
                description=(
                    "Triage write action: group related sources into a compact source cluster by origin, evidentiary role, topic coverage, duplication/mirroring, update relationship, or quality level. "
                    "Use this to map corpus structure for later research, not to make final episode claims."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                        "primary_source_refs": {"type": "array", "items": {"type": "string"}},
                        "secondary_source_refs": {"type": "array", "items": {"type": "string"}},
                        "duplicate_source_refs": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                        "rationale": {"type": "string"},
                    },
                    "required": [
                        "label",
                        "source_refs",
                        "primary_source_refs",
                        "secondary_source_refs",
                        "duplicate_source_refs",
                        "evidence_ids",
                        "rationale",
                    ],
                },
                handler=self.record_source_cluster_tool,
            ),
            AgentTool(
                name="record_gap",
                description=(
                    "Triage write action: record a gap, contradiction, caution, missing coverage area, or source-quality warning. "
                    "Use evidence_ids when a read/search result grounds the gap; included evidence_ids are marked as selected."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "description": {"type": "string"},
                        "source_refs": {"type": "array", "items": {"type": "string"}},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["kind", "description", "source_refs", "evidence_ids"],
                },
                handler=self.record_gap_tool,
            ),
            AgentTool(
                name="finalize_source_map",
                description=(
                    "Write action: signal that triage write actions are complete and SourceMap should be assembled from selected evidence and write actions."
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
