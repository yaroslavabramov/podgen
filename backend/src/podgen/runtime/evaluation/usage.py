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
        return False


def attempt_usage(journal, row, output_reservation):
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
