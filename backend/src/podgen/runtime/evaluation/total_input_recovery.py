"""Explicit run-wide input ceiling, authorized by one archived, proven unsent refusal."""

from copy import deepcopy
from uuid import uuid5

from podgen.domain.errors import Conflict

from .contracts import RunFailure
from .input_budget_evidence import request_record, unsent_evidence
from .source_tool_contract import applies, from_manifest
from .source_tool_recovery import evidence_target, resume_authorization, retry_request

KIND = "total_input_retry"
AUTHORIZATION = "total-input-budget-retry/1"
# The same unsent-turn retry, granted against a run-cost-budget/1 refusal.
COST_AUTHORIZATION = "run-cost-budget-retry/1"
COST_FAILURE = "run_cost_budget_exceeded"


def _attempts(journal, run_id):
    return [row for stage in journal.manifest(run_id)["stages"] for row in journal.attempts(run_id, stage)]


def _charged_before(journal, target, rows):
    from .model_turns import usage
    from .usage import refused_without_generation

    tools = from_manifest(journal.manifest(target["run_id"]))
    charged = (
        journal.manifest(target["run_id"])
        .get("editorial_continuation", {})
        .get("inherited_usage", {})
        .get("input", 0)
    )
    for row in rows:
        if row["operation_kind"] == "role" and applies(row["stage"], tools):
            charged += usage(journal, row, 0)["input"]
        elif any(
            event["event_kind"] == "submit_intent" for event in journal.events(row)
        ) and not refused_without_generation(journal, row):
            charged += row["input_tokens"] or 0
        if row["invocation_id"] == target["invocation_id"]:
            return charged
    raise Conflict("total_input_retry_origin_missing")


def _evidence(journal, target):
    receipt, _ = unsent_evidence(journal, target, total_input=True)
    physical = evidence_target(journal, target)
    rejected = physical[1] if physical else target
    return receipt, rejected["input_tokens"]


def effective_ceiling(journal, run_id):
    """Validate every immutable grant against its original failure and exact request, in run order."""
    ceiling = journal.manifest(run_id)["command"]["limits"]["max_total_input_tokens"]
    rows = _attempts(journal, run_id)
    previous = {}
    for row in rows:
        if row["attempt_kind"] == KIND:
            parent = previous.get(row["parent_invocation_id"])
            if parent is None:
                raise Conflict("total_input_retry_origin_missing")
            record = request_record(journal, row)
            auth = record.get("authorization") or {}
            if auth.get("kind") == COST_AUTHORIZATION:
                # Validated by effective_cost_limit; it does not change the token ceiling.
                previous[row["invocation_id"]] = row
                continue
            receipt, count = _evidence(journal, parent)
            charged = _charged_before(journal, parent, rows)
            requested = auth.get("max_total_input_tokens")
            if (
                auth.get("kind") != AUTHORIZATION
                or auth.get("actor_id") != str(journal.repo.actor_id)
                or auth.get("target_invocation_id") != str(parent["invocation_id"])
                or auth.get("failure_event_ref") != receipt.model_dump(mode="json")
                or auth.get("previous_ceiling") != ceiling
                or auth.get("charged_input_before") != charged
                or auth.get("request_input_tokens") != count
                or auth.get("maximum_new_attempts") != 1
                or type(requested) is not int
                or requested <= ceiling
                or charged + count <= ceiling
                or requested < charged + count
                or record["context"] != request_record(journal, parent)["context"]
                or {k: v for k, v in auth.items() if k == "source_tool_resume"}
                != resume_authorization(journal, parent)
            ):
                raise Conflict("total_input_authorization_mismatch")
            body, original = journal.request(row), retry_request(journal, parent)
            if {k: v for k, v in body.items() if k != "metadata"} != {
                k: v for k, v in original.items() if k != "metadata"
            }:
                raise Conflict("total_input_retry_request_changed")
            ceiling = requested
        previous[row["invocation_id"]] = row
    return ceiling


def _failure_code(journal, receipt):
    return journal.read(receipt)["data"].get("code")


def _same_request(journal, row, parent):
    body, original = journal.request(row), retry_request(journal, parent)
    return {k: v for k, v in body.items() if k != "metadata"} == {
        k: v for k, v in original.items() if k != "metadata"
    }


def effective_cost_limit(journal, run_id):
    """The pinned USD limit raised only by validated grants, in run order; None without a budget."""
    from . import cost_budget

    budget = cost_budget.active(journal.manifest(run_id))
    if budget is None:
        return None
    limit, previous = budget["limit_microusd"], {}
    for row in _attempts(journal, run_id):
        auth = (
            (request_record(journal, row).get("authorization") or {}) if row["attempt_kind"] == KIND else {}
        )
        if auth.get("kind") == COST_AUTHORIZATION:
            parent = previous.get(row["parent_invocation_id"])
            if parent is None:
                raise Conflict("total_input_retry_origin_missing")
            receipt, count = _evidence(journal, parent)
            requested = auth.get("max_limit_microusd")
            if (
                _failure_code(journal, receipt) != COST_FAILURE
                or auth.get("actor_id") != str(journal.repo.actor_id)
                or auth.get("target_invocation_id") != str(parent["invocation_id"])
                or auth.get("failure_event_ref") != receipt.model_dump(mode="json")
                or auth.get("previous_limit_microusd") != limit
                or auth.get("request_input_tokens") != count
                or auth.get("maximum_new_attempts") != 1
                or type(requested) is not int
                or requested <= limit
                or request_record(journal, row)["context"] != request_record(journal, parent)["context"]
                or {k: v for k, v in auth.items() if k == "source_tool_resume"}
                != resume_authorization(journal, parent)
            ):
                raise Conflict("run_cost_authorization_mismatch")
            if not _same_request(journal, row, parent):
                raise Conflict("total_input_retry_request_changed")
            limit = requested
        previous[row["invocation_id"]] = row
    return limit


def authorize_total_input_retry(
    journal, command, target_id, ceiling, *, guard, allow_create=True, max_total_usd=None
):
    rows = _attempts(journal, command.run_id)
    target = next((row for row in rows if row["invocation_id"] == target_id), None)
    if target is None:
        raise RunFailure("total_input_retry_not_eligible", run_id=command.run_id)
    receipt, count = _evidence(journal, target)
    if _failure_code(journal, receipt) == COST_FAILURE or max_total_usd is not None:
        return _authorize_cost(
            journal, command, rows, target, receipt, count, max_total_usd, guard, allow_create
        )
    charged = _charged_before(journal, target, rows)
    child = next(
        (row for row in rows if row["attempt_kind"] == KIND and row["parent_invocation_id"] == target_id),
        None,
    )
    if child is not None:
        if request_record(journal, child)["authorization"]["max_total_input_tokens"] != ceiling:
            raise Conflict("total_input_allowance_changed")
        effective_ceiling(journal, command.run_id)
        return child
    previous = effective_ceiling(journal, command.run_id)
    if (
        type(ceiling) is not int
        or ceiling <= previous
        or charged + count <= previous
        or ceiling < charged + count
    ):
        raise RunFailure("total_input_allowance_invalid", run_id=command.run_id, invocation_id=target_id)
    if not allow_create or rows[-1]["invocation_id"] != target_id:
        raise RunFailure("total_input_retry_not_current", run_id=command.run_id, invocation_id=target_id)
    guard()
    return _retry_attempt(
        journal,
        command,
        target,
        dict(
            kind=AUTHORIZATION,
            actor_id=str(journal.repo.actor_id),
            target_invocation_id=str(target_id),
            failure_event_ref=receipt.model_dump(mode="json"),
            previous_ceiling=previous,
            max_total_input_tokens=ceiling,
            charged_input_before=charged,
            request_input_tokens=count,
            maximum_new_attempts=1,
            **resume_authorization(journal, target),
        ),
    )


def _retry_attempt(journal, command, target, authorization):
    body = deepcopy(retry_request(journal, target))
    new_id = uuid5(target["invocation_id"], "total-input-budget-retry:1")
    index = target["attempt_index"] + 1
    body["metadata"].update(
        podgen_invocation_id=str(new_id),
        podgen_attempt_index=str(index),
        podgen_parent_invocation_id=str(target["invocation_id"]),
    )
    return journal.prepare(
        command.run_id,
        target["stage"],
        body,
        deepcopy(request_record(journal, target)["context"]),
        attempt_index=index,
        parent_invocation_id=target["invocation_id"],
        attempt_kind=KIND,
        operation_kind="role",
        authorization=authorization,
    )


def _authorize_cost(journal, command, rows, target, receipt, count, max_total_usd, guard, allow_create):
    from . import cost_budget

    target_id = target["invocation_id"]
    if _failure_code(journal, receipt) != COST_FAILURE or max_total_usd is None:
        raise RunFailure("run_cost_retry_not_eligible", run_id=command.run_id, invocation_id=target_id)
    requested = cost_budget.microusd(max_total_usd)
    child = next(
        (row for row in rows if row["attempt_kind"] == KIND and row["parent_invocation_id"] == target_id),
        None,
    )
    if child is not None:
        if request_record(journal, child)["authorization"].get("max_limit_microusd") != requested:
            raise Conflict("run_cost_allowance_changed")
        effective_cost_limit(journal, command.run_id)
        return child
    previous = effective_cost_limit(journal, command.run_id)
    if previous is None or requested <= previous:
        raise RunFailure("run_cost_allowance_invalid", run_id=command.run_id, invocation_id=target_id)
    if not allow_create or rows[-1]["invocation_id"] != target_id:
        raise RunFailure("total_input_retry_not_current", run_id=command.run_id, invocation_id=target_id)
    guard()
    return _retry_attempt(
        journal,
        command,
        target,
        dict(
            kind=COST_AUTHORIZATION,
            actor_id=str(journal.repo.actor_id),
            target_invocation_id=str(target_id),
            failure_event_ref=receipt.model_dump(mode="json"),
            previous_limit_microusd=previous,
            max_limit_microusd=requested,
            request_input_tokens=count,
            maximum_new_attempts=1,
            **resume_authorization(journal, target),
        ),
    )
