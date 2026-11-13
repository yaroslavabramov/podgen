"""Navigation and complete selected sections; no report-to-fact rewriting."""

from urllib.parse import urlsplit

from podgen.domain.errors import ContractError


def texts(materials):
    loaded = {doc.path: doc.text for doc in materials.documents}
    return {spec.key: loaded[spec.source_path] for spec in materials.packet.documents}


def contents(packet):
    rows = packet.sections
    return [
        dict(
            material_id=row.material_id,
            title=row.title,
            kind=row.kind,
            length=row.end - row.start,
            previous_material=rows[index - 1].material_id if index else None,
            next_material=rows[index + 1].material_id if index + 1 < len(rows) else None,
            reported_source_ids=list(dict.fromkeys(item.source_id for item in row.occurrences)),
        )
        for index, row in enumerate(rows)
    ]


def source_catalog(materials, *, offset=0, limit=None, include_reported_url=False):
    loaded = {doc.path: doc for doc in materials.documents}
    specs = {spec.key: spec for spec in materials.packet.documents}
    result = []
    for source in materials.packet.sources[offset : None if limit is None else offset + limit]:
        resolved = source.resolved_url
        if resolved and urlsplit(resolved).hostname == "vertexaisearch.cloud.google.com":
            resolved = None
        result.append(
            dict(
                source_id=source.source_id,
                reported_citation_number=source.report_number,
                title=source.title,
                title_origin=source.title_origin,
                reported_domain=source.reported_label,
                status=source.status,
                retained_version=source.acquisition_role,
                length=len(loaded[specs[source.document].source_path].text) if source.document else None,
                resolved_url=resolved,
                coverage=source.coverage,
                limitations=list(source.limitations),
            )
        )
    if include_reported_url:
        for row, source in zip(
            result, materials.packet.sources[offset : None if limit is None else offset + limit], strict=True
        ):
            row["reported_url"] = source.reported_url
    return result


def planning_view(materials, *, paginated=False):
    from podgen.writing.projections import assignment_view, validate_model_content

    catalog = (
        dict(tool="list_sources", offset=0, initial_source_count=len(materials.packet.sources))
        if paginated
        else source_catalog(materials)
    )
    view = dict(research_contents=contents(materials.packet), source_catalog=catalog)
    validate_model_content(dict(assignment=assignment_view(materials.packet), **view), materials.documents)
    return view


def selected_sections(materials, selected):
    loaded = texts(materials)
    allowed = {row.material_id for row in materials.packet.sections}
    if set(selected) - allowed:
        raise ContractError("research_unknown_selected_material")
    return [
        dict(
            card_id=row.material_id,
            kind=row.kind,
            title=row.title,
            text=loaded[row.document][row.start : row.end],
            reported_source_ids=list(dict.fromkeys(item.source_id for item in row.occurrences)),
        )
        for row in materials.packet.sections
        if row.material_id in selected
    ]


def fact_bibliography(materials):
    return [
        dict(
            source=row["title"],
            source_id=row["source_id"],
            status=row["status"],
            retained_version=row["retained_version"],
            title_origin=row["title_origin"],
            length=row["length"],
            reported_citation_number=row["reported_citation_number"],
            resolved_url=row["resolved_url"],
            coverage=row["coverage"],
            limitations=row["limitations"],
            bibliography_status="source_catalog_metadata_not_claim_evidence",
        )
        for row in source_catalog(materials)
    ]
