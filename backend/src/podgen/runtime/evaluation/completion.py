"""Read committed attempts without re-running compilation, budgets or inference."""

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import Conflict

from .contracts import STAGES


def completed_result(generation, journal, command, manifest):
    generation.get_runtime(command.project_id, command.author_ref)
    for value in manifest["material_refs"]:
        generation.get_runtime(command.project_id, RuntimeRef.model_validate(value))
    refs = {}
    totals = {"input": 0, "output": 0}
    for stage in manifest.get("stages", STAGES):
        invocation = journal.invocation(command.run_id, stage)
        if not invocation or invocation["status"] != "completed" or not invocation["output_record_id"]:
            raise Conflict("completed_run_missing_output")
        record = journal.output(invocation)
        if (
            record.get("run_id") != str(command.run_id)
            or record.get("stage") != stage
            or record.get("invocation_id") != str(invocation["invocation_id"])
            or record.get("request_ref", {}).get("record_id") != str(invocation["request_record_id"])
        ):
            raise Conflict("completed_run_output_binding_mismatch")
        refs[stage] = RuntimeRef(
            record_kind="tool_execution", record_id=invocation["output_record_id"]
        ).model_dump(mode="json")
        profile_ref = manifest["command"][
            "reviewer_profile_ref" if stage == "review" else "writer_profile_ref"
        ]
        profile = generation.get(command.project_id, ArtifactRef.model_validate(profile_ref))
        for attempt in journal.attempts(command.run_id, stage):
            totals["input"] += attempt["input_tokens"] or 0
            totals["output"] += (
                attempt["output_tokens"]
                if attempt["output_tokens"] is not None
                else profile.payload["output_limit"]
            )
    if record.get("parent_script_ref") != refs["draft"]:
        raise Conflict("completed_run_parent_script_mismatch")
    return dict(
        run_id=str(command.run_id),
        case_id=command.case_id,
        status="completed_unaccepted",
        output_refs=refs,
        semantic_acceptance=False,
        production_ready=False,
        token_usage_or_reservation=totals,
    )
