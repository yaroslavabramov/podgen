"""Immutable attempt lineage: operational retries never turn into an extra semantic repair."""

from uuid import uuid5

from podgen.domain.errors import Conflict

from .reading_workflow import is_reading
from .technical_recovery_policy import MAX_STAGE_REPLACEMENTS, STATES


def validate_lineage(rows, stage):
    previous = {}
    repairs = readmissions = 0
    for row in rows:
        parent = previous.get(row["parent_invocation_id"])
        if row["attempt_index"] == 0:
            valid = (
                row["attempt_kind"] == "role"
                and row["operation_kind"] == "role"
                and parent is None
                and row["parent_invocation_id"] is None
            )
        else:
            valid = (
                parent is not None
                and parent["attempt_index"] == row["parent_attempt_index"] < row["attempt_index"]
            )
            if row["attempt_kind"] == "anchor_repair":
                repairs += 1
                valid = (
                    valid
                    and is_reading(stage)
                    and row["operation_kind"] == "anchor_repair"
                    and parent["operation_kind"] == "role"
                    and repairs == 1
                )
            elif row["attempt_kind"] == "plan_target_repair":
                repairs += 1
                valid = (
                    valid
                    and stage in ("plan-review", "plan-review-revised")
                    and row["operation_kind"] == "plan_target_repair"
                    and parent["operation_kind"] == "role"
                    and repairs == 1
                )
            elif row["attempt_kind"] in ("input_budget_retry", "total_input_retry"):
                valid = (
                    valid
                    and parent["operation_kind"] == row["operation_kind"] == "role"
                    and parent["status"] == "validation_failed"
                )
            elif row["attempt_kind"] == "credit_retry":
                valid = valid and parent["operation_kind"] == row["operation_kind"]
            elif row["attempt_kind"] == "local_readmission":
                readmissions += 1
                valid = (
                    valid
                    and is_reading(stage)
                    and parent["operation_kind"] == row["operation_kind"]
                    and parent["status"] == "validation_failed"
                    and readmissions == 1
                )
            elif row["attempt_kind"] == "technical_retry":
                valid = (
                    valid
                    and parent["operation_kind"] == row["operation_kind"]
                    and parent["status"] in STATES
                    and sum(r["attempt_kind"] == "technical_retry" for r in previous.values())
                    < MAX_STAGE_REPLACEMENTS
                )
            else:
                valid = False
        if not valid:
            raise Conflict("invalid_attempt_parent")
        previous[row["invocation_id"]] = row


def attempt_identity(run_id, stage, index, kind, parent):
    if index == 0:
        return uuid5(run_id, "invocation:" + stage)
    if kind == "anchor_repair":
        return uuid5(run_id, "invocation:" + stage + ":anchor-repair:1")
    if kind == "total_input_retry":
        return uuid5(parent["invocation_id"], "total-input-budget-retry:1")
    if kind == "input_budget_retry":
        return uuid5(parent["invocation_id"], "input-budget-retry:1")
    if kind == "technical_retry":
        return uuid5(parent["invocation_id"], "technical-recovery:1")
    if kind == "local_readmission":
        return uuid5(parent["invocation_id"], "local-readmission:1")
    return uuid5(
        parent["invocation_id"],
        "plan-target-repair:1" if kind == "plan_target_repair" else "credit-refusal-retry:1",
    )


def validate_preparation(rows, stage, index, kind, operation, parent_id):
    parent = next((row for row in rows if row["invocation_id"] == parent_id), None)
    if index == 0:
        valid = not rows and kind == "role" and operation == "role" and parent_id is None
    elif parent and rows[-1]["invocation_id"] == parent_id and index == rows[-1]["attempt_index"] + 1:
        if kind == "anchor_repair":
            valid = (
                is_reading(stage)
                and operation == "anchor_repair"
                and parent["operation_kind"] == "role"
                and parent["status"] == "validation_failed"
                and not any(row["attempt_kind"] == "anchor_repair" for row in rows)
            )
        elif kind == "plan_target_repair":
            valid = (
                stage in ("plan-review", "plan-review-revised")
                and operation == "plan_target_repair"
                and parent["operation_kind"] == "role"
                and parent["status"] == "validation_failed"
                and not any(row["attempt_kind"] == "plan_target_repair" for row in rows)
            )
        elif kind in ("input_budget_retry", "total_input_retry"):
            valid = (
                operation == parent["operation_kind"] == "role" and parent["status"] == "validation_failed"
            )
        elif kind == "technical_retry":
            valid = (
                operation == parent["operation_kind"]
                and parent["status"] in STATES
                and sum(row["attempt_kind"] == "technical_retry" for row in rows) < MAX_STAGE_REPLACEMENTS
            )
        elif kind == "local_readmission":
            valid = (
                is_reading(stage)
                and operation == parent["operation_kind"]
                and parent["status"] == "validation_failed"
                and not any(row["attempt_kind"] == "local_readmission" for row in rows)
            )
        else:
            valid = (
                kind == "credit_retry"
                and operation == parent["operation_kind"]
                and parent["status"] == "failed"
            )
    else:
        valid = False
    if not valid:
        raise Conflict("invalid_attempt_parent")
    return parent
