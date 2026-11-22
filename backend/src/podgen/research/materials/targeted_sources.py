"""Resolve only explicit structured citations, never the provider's whole bibliography."""

from podgen.domain.errors import ContractError


def originals(resolver, store, answer, guard):
    sources, source_map, resolved = [], [], []
    for url in dict.fromkeys(row.url for row in answer.evidence):
        guard()
        resolution = resolver.document(store, url, guard)
        source_id = resolution.result.get("source_id")
        resolved.append((url, source_id))
        source_map.append(
            dict(
                reported_url=url,
                report_numbers=[],
                source_id=source_id,
                availability=resolution.result.get(
                    "availability", resolution.result.get("status", "unavailable")
                ),
            )
        )
    admissions = {a.source_id: a for a in resolver.restore(run_id=store.run_id, guard=guard)}
    by_path = {d.path: d for d in resolver.materials.documents}
    if len(by_path) != len(resolver.materials.documents):
        raise ContractError("targeted_research_document_binding_mismatch")
    documents = {}
    for spec in resolver.materials.packet.documents:
        document = by_path.get(spec.source_path)
        if document is None or document.sha256 != spec.sha256:
            raise ContractError("targeted_research_document_binding_mismatch")
        documents[spec.key] = document
    for url, source_id in resolved:
        admission = admissions.get(source_id)
        if admission is None and source_id is not None:
            from podgen.sources.material_requests import SourceAdmission

            known = next(s for s in resolver.materials.packet.sources if s.source_id == source_id)
            document = documents[known.document]
            admission = SourceAdmission(
                source_id,
                known.title,
                document.text,
                document.sha256,
                document.ref,
                known.model_dump(mode="json"),
            )
        if admission is not None:
            sources.append((url, admission))
    return sources, source_map
