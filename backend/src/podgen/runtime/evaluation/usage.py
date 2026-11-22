"""Conservative input counts and known output usage/reservations across every physical attempt."""

from .contracts import RunFailure
from .credit_recovery import refusal_evidence


def refused_without_generation(journal, row):
    if (
        row["status"] != "failed"
        or row["provider_response_id"] is not None
        or row["output_record_id"] is not None
    ):
        return False
    try:
        refusal_evidence(journal, row)
        return True
    except RunFailure:
        from .schema_refusal import evidence as schema_refusal

        return schema_refusal(journal, row) is not None


def unsent_without_generation(journal, row):
    from .count_failure_evidence import count_failure_evidence
    from .input_budget_evidence import unsent_evidence
    from .local_readmission import KIND

    if row.get("attempt_kind") == KIND:
        return True
    if count_failure_evidence(journal, row) is not None:
        return True
    if row["status"] == "validation_failed":
        try:
            unsent_evidence(journal, row)
        except RunFailure:
            try:
                unsent_evidence(journal, row, total_input=True)
            except RunFailure:
                pass
            else:
                return True
        else:
            return True
    return False


def attempt_usage(journal, row, output_reservation):
    from .local_readmission import KIND
    from .source_tool_contract import applies, from_manifest

    if row.get("attempt_kind") == KIND:
        return dict(input=0, output=0)

    if row["operation_kind"] == "role" and applies(
        row["stage"], from_manifest(journal.manifest(row["run_id"]))
    ):
        from .model_turns import usage

        value = usage(journal, row, output_reservation)
        return dict(input=value["input"], output=value["output"])
    if unsent_without_generation(journal, row):
        return dict(input=0, output=0)
    return dict(
        input=row["input_tokens"] or 0,
        output=row["output_tokens"]
        if row["output_tokens"] is not None
        else 0
        if refused_without_generation(journal, row)
        else output_reservation,
    )


def prior_attempt_usage(journal, invocation, profile):
    prior = [
        attempt_usage(journal, row, profile.output_limit)
        for row in journal.attempts(invocation["run_id"], invocation["stage"])
        if row["attempt_index"] < invocation["attempt_index"]
    ]
    return {key: sum(row[key] for row in prior) for key in ("input", "output")}
