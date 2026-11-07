"""Preview only the candidate/review bindings the descendant can actually execute."""

from dataclasses import replace

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError

from .source import KEY, verify


def attach(snapshot, journal, run_id, stage, materials):
    if snapshot.manifest.get(KEY) and stage in ("plan-revise", "plan-review-revised"):
        from ..contracts import WritingRunCommand

        values = bindings(
            journal,
            WritingRunCommand.model_validate(snapshot.manifest["command"]),
            snapshot.manifest,
            materials,
            stage,
        )
        return replace(snapshot, planning_inputs=values)
    return snapshot


def bindings(journal, command, manifest, materials, stage):
    candidate, review = verify(journal, command, manifest, materials)
    if stage == "plan-revise":
        return dict(candidate=candidate["output"], plan_review=review["output"])
    if stage == "plan-review-revised":
        row = journal.invocation(command.run_id, "plan-revise")
        if row is None or row["status"] != "completed":
            raise ContractError("preview_continuation_revision_missing")
        return dict(candidate=journal.output(row)["output"])
    return None


def validate(stage, snapshot, inputs):
    if snapshot is None or KEY not in snapshot.manifest or not stage.startswith("plan"):
        return
    expected = snapshot.planning_inputs
    if (
        expected is None
        or set(expected) - set(inputs)
        or any(digest(inputs[key]) != digest(value) for key, value in expected.items())
    ):
        raise ContractError("preview_continuation_binding_mismatch")
