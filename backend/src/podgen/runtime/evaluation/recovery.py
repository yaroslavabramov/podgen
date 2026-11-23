"""Explicit action admission precedes run status mutation; retries never happen by default."""

from podgen.domain.errors import ContractError

from .contracts import RunFailure
from .credit_recovery import authorize_credit_retry
from .plan_target_recovery import authorize_plan_target_repair
from .workflow import stages_for


def authorize_recovery(
    journal,
    command,
    *,
    credit_target=None,
    plan_target=None,
    budget_target=None,
    max_visible_bytes=None,
    max_input_tokens=None,
    total_input_target=None,
    max_total_input_tokens=None,
    max_total_usd=None,
    readmission_target=None,
    guard,
    allow_create=True,
):
    if (budget_target is None) != (max_visible_bytes is None and max_input_tokens is None):
        raise ContractError("input_budget_target_and_allowance_required")
    if (total_input_target is None) != (max_total_input_tokens is None and max_total_usd is None) or (
        max_total_input_tokens is not None and max_total_usd is not None
    ):
        raise ContractError("total_input_target_and_allowance_required")
    targets = (credit_target, plan_target, budget_target, total_input_target, readmission_target)
    if sum(value is not None for value in targets) > 1:
        raise ContractError("one_recovery_action_required")
    if readmission_target is not None:
        from .local_readmission import authorize as authorize_readmission

        row = authorize_readmission(
            journal, command, readmission_target, guard=guard, allow_create=allow_create
        )
        prefix = "local_readmission"
    elif total_input_target is not None:
        from .total_input_recovery import authorize_total_input_retry

        row = authorize_total_input_retry(
            journal,
            command,
            total_input_target,
            max_total_input_tokens,
            guard=guard,
            allow_create=allow_create,
            max_total_usd=max_total_usd,
        )
        prefix = "total_input_retry"
    elif budget_target is not None:
        from .input_budget_recovery import authorize_input_retry

        row = authorize_input_retry(
            journal,
            command,
            budget_target,
            max_visible_bytes,
            max_input_tokens,
            guard=guard,
            allow_create=allow_create,
        )
        prefix = "input_budget_retry"
    elif credit_target is not None:
        row = authorize_credit_retry(
            journal, command.run_id, credit_target, guard=guard, allow_create=allow_create
        )
        prefix = "credit_refusal_retry"
    elif plan_target is not None:
        row = authorize_plan_target_repair(
            journal, command.run_id, plan_target, guard=guard, allow_create=allow_create
        )
        prefix = "plan_target_repair"
    else:
        return None
    if not allow_create:
        return row
    if row["status"] in ("failed", "validation_failed", "outcome_unknown"):
        code = (
            "credit_refusal_retry_outcome_unknown"
            if row["status"] == "outcome_unknown"
            else prefix + "_action_used"
        )
        raise RunFailure(code, run_id=command.run_id, invocation_id=row["invocation_id"])
    if row["status"] == "completed":
        blocked = [journal.invocation(command.run_id, stage) for stage in stages_for(command)]
        blocked = [
            item
            for item in blocked
            if item and item["status"] in ("failed", "validation_failed", "outcome_unknown")
        ]
        if blocked:
            raise RunFailure(
                prefix + "_action_used", run_id=command.run_id, invocation_id=blocked[-1]["invocation_id"]
            )
    return row
