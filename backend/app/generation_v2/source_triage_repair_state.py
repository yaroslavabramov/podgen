from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any


TRIAGE_REPAIR_RESERVED = "reserved"
TRIAGE_REPAIR_IN_PROGRESS = "in_progress"
TRIAGE_REPAIR_COMPLETED = "completed"
TRIAGE_REPAIR_PENDING = frozenset({TRIAGE_REPAIR_RESERVED, TRIAGE_REPAIR_IN_PROGRESS})


def triage_repair_operation(workspace: dict[str, Any]) -> dict[str, Any] | None:
    operation = workspace.get("repair_operation")
    if not isinstance(operation, dict):
        return None
    if operation.get("status") not in {*TRIAGE_REPAIR_PENDING, TRIAGE_REPAIR_COMPLETED}:
        return None
    return deepcopy(operation)


def pending_triage_repair(workspace: dict[str, Any]) -> dict[str, Any] | None:
    operation = triage_repair_operation(workspace)
    if operation is None or operation.get("status") not in TRIAGE_REPAIR_PENDING:
        return None
    return operation


def reserve_triage_repair(
    workspace: dict[str, Any],
    *,
    gate_payload: dict[str, Any],
    completion_payload: dict[str, Any],
    repair_sets: list[dict[str, Any]],
) -> dict[str, Any] | None:
    existing = triage_repair_operation(workspace)
    if existing is not None:
        return existing if existing.get("status") in TRIAGE_REPAIR_PENDING else None
    now = _now()
    operation = {
        "status": TRIAGE_REPAIR_RESERVED,
        "payload": {
            "gate": deepcopy(gate_payload),
            "completion": deepcopy(completion_payload),
            "repair_sets": deepcopy(repair_sets),
            "repair_set_ids": [str(item.get("set_id") or "") for item in repair_sets if item.get("set_id")],
        },
        "reserved_at": now,
        "updated_at": now,
    }
    workspace["repair_operation"] = operation
    workspace["status"] = "repair_planned"
    return deepcopy(operation)


def start_triage_repair(workspace: dict[str, Any]) -> dict[str, Any] | None:
    operation = pending_triage_repair(workspace)
    if operation is None:
        return None
    now = _now()
    operation["status"] = TRIAGE_REPAIR_IN_PROGRESS
    operation.setdefault("started_at", now)
    operation["updated_at"] = now
    workspace["repair_operation"] = operation
    workspace["status"] = "repair_in_progress"
    return deepcopy(operation)


def complete_triage_repair(workspace: dict[str, Any]) -> bool:
    operation = triage_repair_operation(workspace)
    if operation is None:
        return False
    if operation.get("status") == TRIAGE_REPAIR_COMPLETED:
        return True
    if operation.get("status") not in TRIAGE_REPAIR_PENDING:
        return False
    now = _now()
    operation["status"] = TRIAGE_REPAIR_COMPLETED
    operation["completed_at"] = now
    operation["updated_at"] = now
    workspace["repair_operation"] = operation
    return True


def prepare_pending_triage_repair(workspace: dict[str, Any]) -> dict[str, Any]:
    current = deepcopy(workspace)
    operation = pending_triage_repair(current)
    if operation is None:
        return current
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    repair_set_id_list = [str(item) for item in payload.get("repair_set_ids") or [] if str(item).strip()]
    repair_set_ids = set(repair_set_id_list)
    existing_by_id = {
        str(item.get("set_id") or ""): deepcopy(item)
        for item in current.get("source_sets") or []
        if isinstance(item, dict) and item.get("set_id")
    }
    for item in payload.get("repair_sets") or []:
        if isinstance(item, dict) and item.get("set_id"):
            existing_by_id.setdefault(str(item["set_id"]), deepcopy(item))
    source_sets: list[dict[str, Any]] = []
    for item in current.get("source_sets") or []:
        if not isinstance(item, dict):
            continue
        set_id = str(item.get("set_id") or "")
        source_set = deepcopy(item)
        if set_id in repair_set_ids and source_set.get("status") == "in_progress":
            source_set["status"] = "planned"
            source_set["resume_reason"] = "Targeted triage repair stopped before durable completion."
        source_sets.append(source_set)
    present = {str(item.get("set_id") or "") for item in source_sets}
    source_sets.extend(
        existing_by_id[set_id]
        for set_id in repair_set_id_list
        if set_id not in present and set_id in existing_by_id
    )
    current["source_sets"] = source_sets
    current["current_set"] = None
    current["status"] = "repair_planned"
    return current


def carry_pending_triage_repair(
    existing: dict[str, Any],
    workspace: dict[str, Any],
    *,
    source_order: list[str],
) -> dict[str, Any]:
    operation = pending_triage_repair(existing)
    if operation is None:
        return workspace
    payload = operation.get("payload") if isinstance(operation.get("payload"), dict) else {}
    raw_sets = payload.get("repair_sets") or [
        item for item in existing.get("source_sets") or [] if isinstance(item, dict) and item.get("repair")
    ]
    repair_sets = [
        _repair_set_for_new_attempt(item, source_order=source_order)
        for item in raw_sets
        if isinstance(item, dict)
    ]
    carried_payload = dict(payload)
    carried_payload["repair_sets"] = deepcopy(repair_sets)
    carried_payload["repair_set_ids"] = [
        str(item.get("set_id") or "") for item in repair_sets if item.get("set_id")
    ]
    operation["payload"] = carried_payload
    workspace["repair_operation"] = operation
    workspace["source_sets"] = repair_sets
    workspace["status"] = "repair_planned"
    return workspace


def _repair_set_for_new_attempt(source_set: dict[str, Any], *, source_order: list[str]) -> dict[str, Any]:
    current = deepcopy(source_set)
    valid_refs = set(source_order)
    current["source_refs"] = [str(ref) for ref in current.get("source_refs") or [] if str(ref) in valid_refs]
    current.update(
        {
            "status": "planned",
            "findings_summary": "",
            "evidence_ids": [],
            "written_actions": [],
            "observations": [],
            "deferred_reason": "",
            "repair": True,
            "resume_reason": "Targeted triage repair resumed in a new corpus attempt.",
            "updated_at": _now(),
        }
    )
    current.pop("started_at", None)
    return current


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "TRIAGE_REPAIR_COMPLETED",
    "TRIAGE_REPAIR_IN_PROGRESS",
    "TRIAGE_REPAIR_RESERVED",
    "carry_pending_triage_repair",
    "complete_triage_repair",
    "pending_triage_repair",
    "prepare_pending_triage_repair",
    "reserve_triage_repair",
    "start_triage_repair",
    "triage_repair_operation",
]
