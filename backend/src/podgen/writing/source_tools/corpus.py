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


class SourceCorpus:
    """The invocation's admitted, immutable primary documents. Local paths never cross the boundary."""

    def __init__(self, materials, *, retrieval=None, index=None):
        loaded = {doc.path: doc for doc in materials.documents}
        self.documents = {}
        self.cards = {}
        source_ids = {}
        self.research_materials = None
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

        self.last_search_diagnostics = None
        self.retrieval = None
        if retrieval is not None:
            from podgen.retrieval.hybrid import HybridSearch

            if index is None:
                raise ContractError("source_search_index_required")
            self.retrieval = HybridSearch(self.documents, self.cards, retrieval, index=index)

    def execute(self, name, arguments, *, read_id, max_read_chars=PAGE_CHARS):
        if self.research_materials is not None and self.research_materials.authorize is not None:
            self.research_materials.authorize()
        if self.retrieval is not None and name in ("read_source", "read_material"):
            self.retrieval.index.check_access()
        self.last_search_diagnostics = None
        if name == "read_material" and self.research_materials is not None:
            from podgen.writing.research_materials.tools import read

            return read(self, arguments, read_id=read_id, max_read_chars=max_read_chars)
        if name not in METHODS:
            return {"error": "unknown_source_tool"}
        try:
            args = METHODS[name][0].model_validate(arguments)
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
            page_size = PAGE_CHARS if args.end is None else max_read_chars
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
