"""Bind explicit supplemental selections to an exact plan and its accepted review."""

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from .source_tool_recovery import lineage


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
        rows = []
        for stage in ("plan", "plan-revise", "plan-review", "plan-review-revised"):
            row = journal.invocation(current, stage)
            if row is not None and row["status"] == "completed":
                rows.append((stage, row, journal.output(row)))
        accepted = {
            record["candidate_plan_ref"]["record_id"]: record["candidate_plan_hash"]
            for stage, _, record in rows
            if stage.startswith("plan-review") and record["output"].get("decision") == "accept"
        }
        for stage, row, record in rows:
            if stage.startswith("plan-review"):
                if record["output"].get("decision") != "accept":
                    continue
                plan_hash = record["candidate_plan_hash"]
            else:
                if str(row["output_record_id"]) not in accepted:
                    continue
                plan_hash = digest(record["output"])
                if accepted[str(row["output_record_id"])] != plan_hash:
                    raise ContractError("research_annex_plan_binding_changed")
            selected = materials.runtime_annex.setdefault(plan_hash, [])
            for ancestor in lineage(journal, row):
                for event in journal.events(ancestor):
                    if event["event_kind"] != "source_tool_result":
                        continue
                    from podgen.domain.contracts.common import RuntimeRef

                    data = journal.read(
                        RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
                    )["data"]
                    if data["name"] == "select_research_result" and data["result"].get("selected"):
                        selected.append(
                            dict(result_id=data["arguments"]["result_id"], reason=data["arguments"]["reason"])
                        )


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
