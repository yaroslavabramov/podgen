"""Immutable attempt lineage: operational retries never turn into an extra semantic repair."""

from uuid import uuid5

from podgen.domain.errors import Conflict


def validate_lineage(rows, stage):
    previous = {}
    repairs = 0
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
                    and stage == "review"
                    and row["operation_kind"] == "anchor_repair"
                    and parent["operation_kind"] == "role"
                    and repairs == 1
                )
            elif row["attempt_kind"] == "credit_retry":
                valid = valid and parent["operation_kind"] == row["operation_kind"]
            else:
                valid = False
        if not valid:
            raise Conflict("invalid_attempt_parent")
        previous[row["invocation_id"]] = row


def attempt_identity(run_id, stage, index, kind, parent):
    if index == 0:
        return uuid5(run_id, "invocation:" + stage)
    if kind == "anchor_repair":
        return uuid5(run_id, "invocation:review:anchor-repair:1")
    return uuid5(parent["invocation_id"], "credit-refusal-retry:1")


def validate_preparation(rows, stage, index, kind, operation, parent_id):
    parent = next((row for row in rows if row["invocation_id"] == parent_id), None)
    if index == 0:
        valid = not rows and kind == "role" and operation == "role" and parent_id is None
    elif parent and rows[-1]["invocation_id"] == parent_id and index == rows[-1]["attempt_index"] + 1:
        if kind == "anchor_repair":
            valid = (
                stage == "review"
                and operation == "anchor_repair"
                and parent["operation_kind"] == "role"
                and parent["status"] == "validation_failed"
                and not any(row["attempt_kind"] == "anchor_repair" for row in rows)
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
