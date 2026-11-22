"""Compact retained evidence only when its exact current read_source path is unambiguous.

project() has already authenticated every selector and full retained text. This
projection does not reopen those bytes, infer a source from a title, or change the
backend evidence catalog used for admission and replay.
"""

from .evidence import available_snapshots


def compact(view, catalog, materials, snapshot_index, *, read_source_available=True):
    if not read_source_available:
        return
    if getattr(materials.packet, "contract", None) == "research-editorial-input/1":
        readable = {
            row.document: row.source_id
            for row in materials.packet.sources
            if row.document and row.status in ("available", "partial")
        }
    else:
        readable = {
            row.key: f"S{i}"
            for i, row in enumerate(
                (doc for doc in materials.packet.documents if doc.purpose == "primary"), 1
            )
        }
    readable.update(
        {
            "admission:" + row.source_id: row.source_id
            for row in getattr(materials, "runtime_admissions", ())
            if row.catalog.get("kind") != "research_report"
        }
    )
    available = available_snapshots(materials, snapshot_index)
    needed = {
        ref["evidence_id"]
        for row in view["previous_decisions"]
        if row["requires_update"] or row["verdict"] != "supported"
        for ref in row["source_refs"]
        if ref["from_review"] == "previous"
    }
    result = []
    for row in view["previous_evidence"]:
        alias = row["evidence_id"]
        value = catalog[alias]
        selectors = value["source_selectors"]
        if alias in needed or len(selectors) != 1:
            result.append(row)
            continue
        selector = selectors[0]
        source_id = readable.get(selector["key"])
        source = available.get(selector["key"])
        if (
            source_id is None
            or source is None
            or any(source[key] != selector[key] for key in ("sha256", "ref"))
        ):
            result.append(row)
            continue
        start, end = selector["start"], selector["end"]
        result.append(
            dict(
                evidence_id=alias,
                original_id=row["original_id"],
                kind=row["kind"],
                source=row["content"]["source"],
                source_id=source_id,
                start=start,
                end=end,
                read_source=dict(tool="read_source", source_id=source_id, start=start, end=end),
            )
        )
    view["previous_evidence"] = result
