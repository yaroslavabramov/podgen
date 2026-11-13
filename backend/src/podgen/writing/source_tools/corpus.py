from dataclasses import dataclass
from hashlib import sha256

from pydantic import ValidationError

from podgen.domain.errors import ContractError

from .contracts import METHODS, Read
from .search import search

PAGE_CHARS = 2400
MAX_EXPLICIT_CHARS = 9600


@dataclass(frozen=True)
class Source:
    title: str
    text: str
    digest: str


def _same_document(left, right):
    if left == right:
        return bool(left)
    from podgen.research.hosted.corpus_acquisition import document_url

    try:
        return document_url(left) == document_url(right)
    except (ValueError, ContractError):
        return False


class SourceCorpus:
    """The invocation's admitted, immutable primary documents. Local paths never cross the boundary."""

    def __init__(self, materials, *, retrieval=None, index=None):
        loaded = {doc.path: doc for doc in materials.documents}
        self.documents = {}
        self.cards = {}
        source_ids = {}
        self.research_materials = None
        self.material_resolver = None
        self.supplemental_reports = {}
        self.admission_catalog = {}
        self._materials = materials
        if getattr(materials.packet, "contract", None) == "research-editorial-input/1":
            from podgen.writing.research_materials.tools import initialize

            initialize(self, materials, Source)
        else:
            for source in materials.packet.documents:
                if source.purpose != "primary":
                    continue
                doc = loaded.get(source.source_path)
                if (
                    doc is None
                    or doc.sha256 != source.sha256
                    or sha256(doc.text.encode()).hexdigest() != source.sha256
                ):
                    raise ContractError("source_tool_frozen_document_mismatch")
                key = f"S{len(self.documents) + 1}"
                source_ids[source.key] = key
                self.documents[key] = Source(source.title, doc.text, doc.sha256)
            for card_number, card in enumerate(materials.packet.dossier.knowledge_cards, 1):
                self.cards[f"M{card_number}"] = list(
                    dict.fromkeys(
                        (source_ids[e.document], e.start, e.end)
                        for e in card.excerpts
                        if e.document in source_ids
                    )
                )

        self.retrieval = None
        for admission in getattr(materials, "runtime_admissions", ()):
            self.admit(admission)
        self.last_search_diagnostics = None
        if retrieval is not None:
            from podgen.retrieval.hybrid import HybridSearch

            if index is None:
                raise ContractError("source_search_index_required")
            search_type = HybridSearch
            if retrieval.get("version") == "hybrid-local/2":
                from podgen.retrieval.streaming import StreamingSearch

                search_type = StreamingSearch
                index.access.admission_references.extend(
                    row.reference for row in getattr(materials, "runtime_admissions", ())
                )
            self.retrieval = search_type(self.documents, self.cards, retrieval, index=index)

    def admit(self, admission):
        from podgen.persistence.text_source import VerifiedText

        actual = (
            admission.text.sha256
            if isinstance(admission.text, VerifiedText)
            else sha256(admission.text.encode()).hexdigest()
        )
        if actual != admission.sha256:
            raise ContractError("source_admission_hash_mismatch")
        report = admission.catalog.get("kind") == "research_report"
        target = self.supplemental_reports if report else self.documents
        import re

        if not re.fullmatch(r"X[1-9]\d*" if report else r"S[1-9]\d*", admission.source_id):
            raise ContractError("source_admission_identifier_invalid")
        value = Source(admission.title, admission.text, admission.sha256)
        if admission.source_id in target and (
            target[admission.source_id].digest,
            target[admission.source_id].title,
        ) != (value.digest, value.title):
            raise ContractError("source_admission_binding_changed")
        target[admission.source_id] = value
        self.admission_catalog[admission.source_id] = admission.catalog
        if not report and self.research_materials is not None:
            reported_url = admission.catalog.get("reported_url")
            aliases = {
                key
                for key, source in self.research_sources.items()
                if reported_url and _same_document(source.reported_url, reported_url)
            }
            for alias, section in self.research_sections.items():
                if aliases & {item.source_id for item in section.occurrences} and admission.source_id not in {
                    key for key, _, _ in self.cards[alias]
                }:
                    self.cards[alias].append((admission.source_id, 0, len(admission.text)))
        if self.retrieval is not None:
            if admission.reference not in self.retrieval.index.access.admission_references:
                self.retrieval.index.access.admission_references.append(admission.reference)

    def refresh(self):
        if self.retrieval is not None:
            self.retrieval.refresh()

    def execute(self, name, arguments, *, read_id, max_read_chars=PAGE_CHARS, default_read_chars=PAGE_CHARS):
        if self.research_materials is not None and self.research_materials.authorize is not None:
            self.research_materials.authorize()
        if self.retrieval is not None and name in ("read_source", "read_material"):
            self.retrieval.index.check_access()
        self.last_search_diagnostics = None
        if name == "read_material" and self.research_materials is not None:
            from podgen.writing.research_materials.tools import read

            return read(
                self,
                arguments,
                read_id=read_id,
                max_read_chars=max_read_chars,
                default_read_chars=default_read_chars,
            )
        if name not in METHODS:
            return {"error": "unknown_source_tool"}
        try:
            model = METHODS[name][0]
            if (
                name == "search_sources"
                and self.retrieval is not None
                and self.retrieval.profile["version"] == "hybrid-local/2"
            ):
                from podgen.writing.research_materials.runtime_tools import PersistentSearch

                model = PersistentSearch
            args = model.model_validate(arguments)
        except ValidationError as error:
            return {
                "error": "invalid_source_tool_arguments",
                "details": error.errors(include_input=False, include_url=False),
            }
        if isinstance(args, Read):
            doc = self.documents.get(args.source_id)
            if doc is None:
                if self.research_materials is not None and args.source_id in self.research_sources:
                    return dict(
                        error="source_not_readable",
                        source_id=args.source_id,
                        status=self.research_sources[args.source_id].status,
                    )
                return {"error": "unknown_source_id"}
            stop = len(doc.text) if args.end is None else args.end
            if not 0 <= args.start < stop <= len(doc.text):
                return {"error": "invalid_source_range", "source_length": len(doc.text)}
            if type(max_read_chars) is not int or not PAGE_CHARS <= max_read_chars <= MAX_EXPLICIT_CHARS:
                raise ContractError("invalid_source_tool_read_budget")
            if type(default_read_chars) is not int or not PAGE_CHARS <= default_read_chars <= max_read_chars:
                raise ContractError("invalid_source_tool_default_read_budget")
            page_size = default_read_chars if args.end is None else max_read_chars
            end = min(stop, args.start + page_size)
            return dict(
                passage=dict(
                    read_id=read_id,
                    source_id=args.source_id,
                    source=doc.title,
                    start=args.start,
                    end=end,
                    text=doc.text[args.start : end],
                ),
                source_length=len(doc.text),
                next_start=end if end < stop else None,
            )
        if self.retrieval is not None:
            from podgen.retrieval import QueryRejected

            try:
                result = self.retrieval.search(args)
                self.last_search_diagnostics = self.retrieval.diagnostics(args)
                return result
            except QueryRejected as error:
                return dict(error=str(error))
        return search(self.documents, self.cards, args)

    def locate(self, locator):
        """Open a persisted read in this packet; reject a locator that belongs to another source."""
        if self.research_materials is not None and self.research_materials.authorize is not None:
            self.research_materials.authorize()
        try:
            key, fingerprint, start, end = locator.split(":")
            doc = self.documents[key]
            start, end = int(start), int(end)
        except (KeyError, ValueError) as error:
            raise ContractError("unknown_source_read_locator") from error
        if (
            doc.digest != fingerprint
            or not 0 <= start < end <= len(doc.text)
            or end - start > MAX_EXPLICIT_CHARS
        ):
            raise ContractError("invalid_source_read_locator")
        return dict(source=doc.title, source_id=key, start=start, end=end, text=doc.text[start:end])
