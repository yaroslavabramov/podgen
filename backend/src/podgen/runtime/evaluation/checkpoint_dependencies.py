"""Resolve immutable control and byte ownership; the writer checks live permissions."""

from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.references import walk_values
from podgen.persistence.database import query
from podgen.persistence.private_journal.contracts import PrivateObjectUse
from podgen.persistence.read_authorization import _read_connection


def resolve(phase, kind, data, by_kind):
    journal = phase.journal
    payloads = [view["payload"] for view in phase.views]
    if getattr(phase, "semantic_result", None) is not None:
        payloads.append(phase.semantic_result.payload)
    local_aliases = {view["record_id"] for view in phase.views}
    references = {
        ref.record_id
        for payload in payloads
        for _, _, ref in walk_values(payload)
        if isinstance(ref, RuntimeRef)
        and str(ref.record_id) not in local_aliases
        and not journal.execution.generic_input(phase.run_id, str(ref.record_id))
    }
    references.update(owner.record_id for _, owner in phase.private_uses)
    by_alias = {str(identity): journal.execution.reference_owner(str(identity)) for identity in references}
    missing = [identity for identity in references if by_alias[str(identity)] is None]
    if missing:
        with _read_connection(journal.repo) as tx:
            rows = query(
                tx,
                """SELECT entry_id,archive_ids FROM execution_entries
                WHERE project_id=:p AND run_id=:r AND archive_ids && CAST(:ids AS uuid[])""",
                p=journal.project,
                r=phase.run_id,
                ids=missing,
            ).mappings()
            by_alias.update({str(alias): row["entry_id"] for row in rows for alias in row["archive_ids"]})
    sources = {entry for entry in by_alias.values() if entry is not None}
    parents = set(sources) if not getattr(phase, "semantic_result", None) else set()
    continuation = journal.execution.continuation(phase.run_id, phase.root["invocation_id"])
    if continuation:
        parents.add(continuation)
        sources.add(continuation)
    objects = {f"byte{slot}": obj for slot, obj in enumerate(phase.blobs.values())}
    for slot, (ref, owner_ref) in enumerate(phase.private_uses):
        owner_entry = by_alias[str(owner_ref.record_id)]
        objects[f"reuse{slot}"] = PrivateObjectUse(ref, owner_entry)
        parents.add(owner_entry)
    if kind == "provider_receipt" or data.get("rejection"):
        parents.add(UUID(data["submit_entry_id"]))
    if kind == "structured_decision":
        parents.add(phase.structured_decision["provider_entry_id"])
    if kind in ("tool_intent", "tool_result"):
        tool = by_kind.get("source_tool_result", by_kind.get("source_tool_call"))["data"]
        data["provider_entry_id"] = tool.get("provider_entry_id") or str(
            by_alias[tool["provider_response_ref"]["record_id"]]
        )
        parents.add(UUID(data["provider_entry_id"]))
        if tool.get("decision_entry_id"):
            data["decision_entry_id"] = tool["decision_entry_id"]
            parents.add(UUID(tool["decision_entry_id"]))
    return parents, objects, sources
