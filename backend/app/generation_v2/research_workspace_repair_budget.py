from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


TARGETED_REPAIR_BUDGET = "targeted_repairs_used"
AUDIT_RERUN_BUDGET = "audit_reruns_used"
TREATMENT_REWRITE_BUDGET = "treatment_rewrites_used"
REPAIR_BUDGET_LIMIT = 1

REPAIR_RESERVED = "reserved"
REPAIR_IN_PROGRESS = "in_progress"
REPAIR_COMPLETED = "completed"
PENDING_REPAIR_STATUSES = frozenset({REPAIR_RESERVED, REPAIR_IN_PROGRESS})


def repair_budget(workspace: dict[str, Any]) -> dict[str, int]:
    raw = workspace.get("repair_budget") if isinstance(workspace.get("repair_budget"), dict) else {}
    operations = repair_operations(workspace)
    return {
        TARGETED_REPAIR_BUDGET: max(
            int(raw.get(TARGETED_REPAIR_BUDGET) or 0),
            int(workspace.get("research_repair_cycle") or 0),
            _completed_operation_count(operations, TARGETED_REPAIR_BUDGET),
        ),
        AUDIT_RERUN_BUDGET: max(
            int(raw.get(AUDIT_RERUN_BUDGET) or 0),
            _completed_operation_count(operations, AUDIT_RERUN_BUDGET),
        ),
        TREATMENT_REWRITE_BUDGET: max(
            int(raw.get(TREATMENT_REWRITE_BUDGET) or 0),
            _completed_operation_count(operations, TREATMENT_REWRITE_BUDGET),
        ),
    }


def repair_operations(workspace: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = workspace.get("repair_operations")
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): deepcopy(operation)
        for name, operation in raw.items()
        if isinstance(operation, dict)
        and str(operation.get("status") or "") in {*PENDING_REPAIR_STATUSES, REPAIR_COMPLETED}
    }


def repair_operation(workspace: dict[str, Any], budget_name: str) -> dict[str, Any] | None:
    operation = repair_operations(workspace).get(budget_name)
    return deepcopy(operation) if operation is not None else None


def pending_repair_operation(workspace: dict[str, Any], budget_name: str) -> dict[str, Any] | None:
    operation = repair_operation(workspace, budget_name)
    if operation is None or operation.get("status") not in PENDING_REPAIR_STATUSES:
        return None
    return operation


def reserve_repair_operation_in_workspace(
    workspace: dict[str, Any],
    budget_name: str,
    *,
    operation_kind: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    existing = repair_operation(workspace, budget_name)
    if existing is not None:
        if existing.get("status") in PENDING_REPAIR_STATUSES and existing.get("kind") == operation_kind:
            return existing
        return None
    if repair_budget(workspace).get(budget_name, 0) >= REPAIR_BUDGET_LIMIT:
        return None
    now = _now()
    operation = {
        "kind": operation_kind,
        "status": REPAIR_RESERVED,
        "payload": deepcopy(payload or {}),
        "reserved_at": now,
        "updated_at": now,
    }
    operations = repair_operations(workspace)
    operations[budget_name] = operation
    workspace["repair_operations"] = operations
    return deepcopy(operation)


def start_repair_operation_in_workspace(
    workspace: dict[str, Any],
    budget_name: str,
) -> dict[str, Any] | None:
    operation = pending_repair_operation(workspace, budget_name)
    if operation is None:
        return None
    now = _now()
    operation["status"] = REPAIR_IN_PROGRESS
    operation.setdefault("started_at", now)
    operation["updated_at"] = now
    operations = repair_operations(workspace)
    operations[budget_name] = operation
    workspace["repair_operations"] = operations
    return deepcopy(operation)


def complete_repair_operation_in_workspace(workspace: dict[str, Any], budget_name: str) -> bool:
    operation = repair_operation(workspace, budget_name)
    if operation is None:
        return False
    if operation.get("status") == REPAIR_COMPLETED:
        return True
    if operation.get("status") not in PENDING_REPAIR_STATUSES:
        return False
    now = _now()
    operation["status"] = REPAIR_COMPLETED
    operation["completed_at"] = now
    operation["updated_at"] = now
    operations = repair_operations(workspace)
    operations[budget_name] = operation
    workspace["repair_operations"] = operations
    budget = repair_budget(workspace)
    budget[budget_name] = max(1, budget.get(budget_name, 0))
    workspace["repair_budget"] = budget
    if budget_name == TARGETED_REPAIR_BUDGET:
        workspace["research_repair_cycle"] = max(1, int(workspace.get("research_repair_cycle") or 0))
    return True


def consume_repair_budget_in_workspace(workspace: dict[str, Any], budget_name: str) -> bool:
    """Compatibility helper for non-model callers that complete work atomically."""
    operation = reserve_repair_operation_in_workspace(
        workspace,
        budget_name,
        operation_kind="legacy_immediate",
    )
    if operation is None:
        return False
    start_repair_operation_in_workspace(workspace, budget_name)
    return complete_repair_operation_in_workspace(workspace, budget_name)


async def load_persisted_repair_operation(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    budget_name: str,
    pending_only: bool = False,
) -> dict[str, Any] | None:
    from app.generation_v2.research_workspace_persistence import load_research_workspace

    workspace = await load_research_workspace(db, generation_run_id)
    if not workspace:
        return None
    if pending_only:
        return pending_repair_operation(workspace, budget_name)
    return repair_operation(workspace, budget_name)


async def reserve_persisted_repair_operation(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    budget_name: str,
    operation_kind: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    from app.generation_v2.research_workspace_persistence import (
        load_research_workspace,
        save_research_workspace,
    )

    workspace = await load_research_workspace(db, generation_run_id)
    if not workspace:
        return None
    operation = reserve_repair_operation_in_workspace(
        workspace,
        budget_name,
        operation_kind=operation_kind,
        payload=payload,
    )
    if operation is None:
        return None
    await save_research_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    return operation


async def start_persisted_repair_operation(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    budget_name: str,
) -> dict[str, Any] | None:
    from app.generation_v2.research_workspace_persistence import (
        load_research_workspace,
        save_research_workspace,
    )

    workspace = await load_research_workspace(db, generation_run_id)
    if not workspace:
        return None
    operation = start_repair_operation_in_workspace(workspace, budget_name)
    if operation is None:
        return None
    await save_research_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    return operation


async def complete_persisted_repair_operation(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    budget_name: str,
) -> bool:
    from app.generation_v2.research_workspace_persistence import (
        load_research_workspace,
        save_research_workspace,
    )

    workspace = await load_research_workspace(db, generation_run_id)
    if not workspace or not complete_repair_operation_in_workspace(workspace, budget_name):
        return False
    await save_research_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    return True


async def consume_persisted_repair_budget(
    db: AsyncSession,
    *,
    generation_run_id: uuid.UUID,
    budget_name: str,
) -> bool:
    """Compatibility helper for work completed in the same transaction."""
    from app.generation_v2.research_workspace_persistence import (
        load_research_workspace,
        save_research_workspace,
    )

    workspace = await load_research_workspace(db, generation_run_id)
    if not workspace or not consume_repair_budget_in_workspace(workspace, budget_name):
        return False
    await save_research_workspace(db, generation_run_id=generation_run_id, workspace=workspace)
    return True


def _completed_operation_count(operations: dict[str, dict[str, Any]], budget_name: str) -> int:
    operation = operations.get(budget_name)
    return int(bool(operation and operation.get("status") == REPAIR_COMPLETED))


def _now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "AUDIT_RERUN_BUDGET",
    "PENDING_REPAIR_STATUSES",
    "REPAIR_BUDGET_LIMIT",
    "REPAIR_COMPLETED",
    "REPAIR_IN_PROGRESS",
    "REPAIR_RESERVED",
    "TARGETED_REPAIR_BUDGET",
    "TREATMENT_REWRITE_BUDGET",
    "complete_persisted_repair_operation",
    "complete_repair_operation_in_workspace",
    "consume_persisted_repair_budget",
    "consume_repair_budget_in_workspace",
    "load_persisted_repair_operation",
    "pending_repair_operation",
    "repair_budget",
    "repair_operation",
    "repair_operations",
    "reserve_persisted_repair_operation",
    "reserve_repair_operation_in_workspace",
    "start_persisted_repair_operation",
    "start_repair_operation_in_workspace",
]
