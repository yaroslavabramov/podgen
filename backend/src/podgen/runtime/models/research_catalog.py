"""Paged navigation over immutable source metadata and supplemental report citation maps."""

from podgen.domain.canonical import digest
from podgen.writing.research_materials.views import source_catalog

PAGE = 6


def versioned_page(rows, args, version, field):
    if args.get("version") not in (None, version):
        return dict(error="source_catalog_changed", catalog_version=version, restart_offset=0)
    offset = args["offset"]
    chosen = rows[offset : offset + PAGE]
    following = offset + len(chosen)
    return {
        field: chosen,
        "catalog_version": version,
        "total_entries": len(rows),
        "next_offset": following if following < len(rows) else None,
    }


def source_page(corpus, args):
    materials = corpus._materials
    count, offset = len(materials.packet.sources), args["offset"]
    version = digest(
        dict(
            initial=materials.packet.model_dump(mode="json"),
            added=[[row.source_id, row.sha256] for row in materials.runtime_admissions],
        )
    )
    if args.get("version") not in (None, version):
        return dict(error="source_catalog_changed", catalog_version=version, restart_offset=0)
    rows = source_catalog(materials, offset=offset, limit=PAGE, include_reported_url=True)
    additions = [row for row in materials.runtime_admissions if row.catalog.get("kind") != "research_report"]
    extra_start = max(0, offset - count)
    for row in additions[extra_start : extra_start + PAGE - len(rows)]:
        metadata = {
            key: row.catalog[key]
            for key in ("reported_url", "resolved_url", "status", "coverage", "limitations", "title_origin")
            if key in row.catalog
        }
        rows.append(
            dict(
                source_id=row.source_id,
                title=row.title,
                length=len(row.text),
                retained_version=row.catalog.get("acquisition_role"),
                **metadata,
            )
        )
    total, following = count + len(additions), offset + len(rows)
    return dict(
        sources=rows,
        catalog_version=version,
        total_sources=total,
        next_offset=following if following < total else None,
    )


def report_page(corpus, args):
    return report_catalog(corpus._materials, args)


def report_catalog(materials, args):
    reports = [row for row in materials.runtime_admissions if row.catalog.get("kind") == "research_report"]
    version = digest([[row.source_id, row.sha256] for row in reports])
    # Only metadata for the requested page is materialized; text remains lazy.
    metadata = [
        dict(
            result_id=row.source_id,
            title=row.title,
            kind="research_synthesis_not_source_evidence",
            reader="read_research_result",
            source_mapping="list_research_sources",
        )
        for row in reports
    ]
    return versioned_page(metadata, args, version, "reports")


def citation_page(corpus, args):
    catalog = corpus.admission_catalog.get(args["result_id"])
    if catalog is None or catalog.get("kind") != "research_report":
        return dict(error="unknown_research_result")
    mapping = catalog.get("reported_source_map", [])
    if isinstance(mapping, dict):
        mapping = [dict(reported_reference=key, source_id=value) for key, value in mapping.items()]
    return dict(
        versioned_page(mapping, args, digest(mapping), "sources"),
        result_id=args["result_id"],
        relationship="reported_citations_not_verified_support",
    )
