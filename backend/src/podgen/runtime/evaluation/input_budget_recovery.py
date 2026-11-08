"""One explicit local input allowance, bound to an exact unsent request and its evidence."""

from copy import deepcopy
from uuid import uuid5

from podgen.domain.errors import Conflict
from podgen.writing.projections import SINGLE_INPUT_TOKEN_LIMIT, VISIBLE_BYTES_LIMIT

from .contracts import RunFailure
from .input_budget_evidence import request_record, unsent_evidence
from .workflow import stages_for

KIND = "input_budget_retry"
AUTHORIZATION = "input-budget-retry/1"
DEFAULTS = dict(visible_bytes=VISIBLE_BYTES_LIMIT, input_tokens=SINGLE_INPUT_TOKEN_LIMIT)


def authorize_input_retry(journal, command, target_id, max_bytes, max_tokens, *, guard, allow_create=True):
    for stage in stages_for(command):
        rows = journal.attempts(command.run_id, stage)
        target = next((r for r in rows if r["invocation_id"] == target_id), None)
        if target is not None:
            break
    else:
        raise RunFailure("input_budget_retry_not_eligible", run_id=command.run_id)
    evidence, measured = unsent_evidence(journal, target)
    prior = effective_limits(journal, target)
    limits = dict(
        visible_bytes=prior["visible_bytes"] if max_bytes is None else max_bytes,
        input_tokens=prior["input_tokens"] if max_tokens is None else max_tokens,
    )
    if any(type(v) is not int or v < prior[k] for k, v in limits.items()) or limits == prior:
        raise RunFailure("input_budget_allowance_invalid", run_id=command.run_id)
    if limits["visible_bytes"] < measured["total"] or limits["input_tokens"] < (target["input_tokens"] or 0):
        raise RunFailure("input_budget_allowance_too_small", run_id=command.run_id, invocation_id=target_id)
    child = next(
        (r for r in rows if r["attempt_kind"] == KIND and r["parent_invocation_id"] == target_id), None
    )
    if child is not None:
        if request_record(journal, child)["authorization"]["limits"] != limits:
            raise Conflict("input_budget_allowance_changed")
        return child
    if not allow_create or rows[-1]["invocation_id"] != target_id:
        raise RunFailure("input_budget_retry_not_current", run_id=command.run_id, invocation_id=target_id)
    guard()
    body = deepcopy(journal.request(target))
    new_id = uuid5(target_id, "input-budget-retry:1")
    index = target["attempt_index"] + 1
    body["metadata"].update(
        podgen_invocation_id=str(new_id),
        podgen_attempt_index=str(index),
        podgen_parent_invocation_id=str(target_id),
    )
    return journal.prepare(
        command.run_id,
        stage,
        body,
        deepcopy(request_record(journal, target)["context"]),
        attempt_index=index,
        parent_invocation_id=target_id,
        attempt_kind=KIND,
        operation_kind="role",
        authorization=dict(
            kind=AUTHORIZATION,
            actor_id=str(journal.repo.actor_id),
            target_invocation_id=str(target_id),
            failure_event_ref=evidence.model_dump(mode="json"),
            measured_bytes=measured,
            limits=limits,
            maximum_new_attempts=1,
        ),
    )


def effective_limits(journal, row):
    """An allowance follows only identical operational retries, never other stages or repairs."""
    if not row["attempt_index"]:
        return DEFAULTS.copy()
    rows = journal.attempts(row["run_id"], row["stage"])
    body, context = journal.request(row), request_record(journal, row)["context"]
    while row["attempt_index"]:
        parent = next((r for r in rows if r["invocation_id"] == row["parent_invocation_id"]), None)
        if parent is None:
            raise Conflict("input_budget_authorization_mismatch")
        if row["attempt_kind"] == KIND:
            auth = request_record(journal, row)["authorization"]
            evidence, measured = unsent_evidence(journal, parent)
            limits = auth.get("limits", {})
            if (
                auth.get("kind") != AUTHORIZATION
                or auth.get("actor_id") != str(journal.repo.actor_id)
                or auth.get("target_invocation_id") != str(parent["invocation_id"])
                or auth.get("failure_event_ref") != evidence.model_dump(mode="json")
                or auth.get("measured_bytes") != measured
                or auth.get("maximum_new_attempts") != 1
                or set(limits) != set(DEFAULTS)
                or any(type(v) is not int or v < DEFAULTS[k] for k, v in limits.items())
                or limits["visible_bytes"] < measured["total"]
                or limits["input_tokens"] < (parent["input_tokens"] or 0)
                or context != request_record(journal, parent)["context"]
            ):
                raise Conflict("input_budget_authorization_mismatch")
            original = journal.request(parent)
            if {k: v for k, v in body.items() if k != "metadata"} != {
                k: v for k, v in original.items() if k != "metadata"
            }:
                raise Conflict("input_budget_request_changed")
            return limits
        if row["attempt_kind"] != "credit_retry":
            break
        row = parent
    return DEFAULTS.copy()
