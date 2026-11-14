"""Bind explicit supplemental selections to an exact plan and its accepted review."""

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from .research_selection import load


def restore(journal, run_id, materials):
    if not hasattr(materials, "runtime_annex"):
        return
    materials.runtime_annex.clear()
    if journal.run(run_id) is None:
        return
    inherited = getattr(getattr(materials, "fixed", None), "source_run_id", None)
    pending, seen = [run_id] + ([inherited] if inherited else []), set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        manifest = journal.manifest(current)
        for key in ("planning_continuation", "editorial_continuation"):
            if manifest.get(key):
                from uuid import UUID

                pending.append(UUID(manifest[key]["parent_run_id"]))
        for plan_hash, selections in load(journal, current).items():
            materials.runtime_annex.setdefault(plan_hash, []).extend(selections)


def attach(payload, materials, plan):
    if plan is None:
        return
    selected = getattr(materials, "runtime_annex", {}).get(digest(plan), [])
    admissions = {row.source_id: row for row in getattr(materials, "runtime_admissions", ())}
    reports = []
    for selection in selected:
        row = admissions.get(selection["result_id"])
        if row is None or row.catalog.get("kind") != "research_report":
            raise ContractError("selected_research_annex_missing")
        if row.source_id in {report["result_id"] for report in reports}:
            continue
        reports.append(
            dict(
                result_id=row.source_id,
                title=row.title,
                selection_reason=selection["reason"],
                text=row.text[:],
                reported_source_map=row.catalog.get("reported_source_map", {}),
                kind="research_synthesis_not_source_evidence",
            )
        )
    if reports:
        payload["supplemental_research"] = reports
