"""The first local revision previews exactly its inherited assignment and composition."""

from dataclasses import replace

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from .source import KEY, own_plan, seed


def attach(snapshot, journal, run_id, stage):
    if KEY not in snapshot.manifest:
        return snapshot
    plan = own_plan(journal, snapshot.manifest)
    if stage == "revise-1":
        from ..revision_invocation import source_state

        script, ref, readings, _ = seed(journal, snapshot.manifest)
        values = dict(draft=script, reader_feedback=readings["reader"], fact_feedback=readings["fact"])
        if plan:
            values["approved_plan"] = plan["accepted_plan"]
        return replace(
            snapshot,
            script=script,
            composition=source_state(journal, ref, script),
            continuation_inputs=values,
        )
    if stage in ("reader-review-1", "fact-review-1"):
        row = journal.invocation(run_id, "revise-1")
        if row is None or row["status"] != "completed":
            raise ContractError("preview_editorial_revision_missing")
        script = journal.output(row)["output"]
        return replace(snapshot, continuation_inputs=dict(draft=script))
    return snapshot


def resolve(snapshot, inputs):
    if snapshot is None or snapshot.continuation_inputs is None:
        return inputs
    result = dict(inputs)
    for key, value in snapshot.continuation_inputs.items():
        if result.get(key) is not None and digest(result[key]) != digest(value):
            raise ContractError("preview_editorial_continuation_binding_mismatch")
        result[key] = value
    return result
