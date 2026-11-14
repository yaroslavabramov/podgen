"""Read a structurally validated immutable outcome with fresh authorization."""

from .reading_cycle import outcome_ref


def read_outcome(journal, run_id):
    from .reading_validation import read_validated

    return read_validated(journal, run_id)


def audit_outcome(journal, run_id):
    """Explicit full replay, independent of a previously persisted validation."""
    from .reading_audit_replay import audit_outcome as replay

    return replay(journal, run_id)


def outcome_result(outcome, command):
    kept = outcome["status"] == "keep"
    result = dict(
        run_id=str(command.run_id),
        case_id=command.case_id,
        status="completed_unaccepted" if kept else "needs_attention",
        review_status=outcome["status"],
        failure_code=None if kept else "independent_review_" + outcome["status"],
        output_refs=outcome["output_refs"],
        final_script_ref=outcome["script_ref"],
        outcome_ref=outcome_ref(command.run_id).model_dump(mode="json"),
        token_usage_or_reservation=outcome["token_usage_or_reservation"],
        semantic_acceptance=False,
        production_ready=False,
    )
    result.update(
        {
            key: outcome[key]
            for key in ("revisions_completed", "max_total_revisions", "usage_ledger")
            if key in outcome
        }
    )
    return result
