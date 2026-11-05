"""Read committed attempts without re-running compilation, budgets or inference."""

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import Conflict

from .contracts import STAGES
from .usage import attempt_usage


def completed_result(generation, journal, command, manifest):
    if command.input_contract == "editorial-role-packets/1":
        from podgen.persistence.editorial_inputs import load_editorial_materials

        load_editorial_materials(
            generation,
            command.project_id,
            command.author_ref,
            case_id=command.case_id,
            fixed_ref=command.fixed_plan_input_ref,
        )
    elif command.plan_mode == "own-plan":
        from podgen.persistence.writing_sources import own_plan_sources

        own_plan_sources(generation, command.project_id, command.author_ref, command.case_id)
    else:
        generation.get_runtime(command.project_id, command.author_ref)
    for value in manifest["material_refs"]:
        if "record_kind" in value:
            generation.get_runtime(command.project_id, RuntimeRef.model_validate(value))
        else:
            generation.get(command.project_id, ArtifactRef.model_validate(value))
    proof = None
    if command.plan_mode == "own-plan":
        from .planning import verify_clearance

        proof = verify_clearance(journal, command.run_id)
    from .finalization import final_ref, finalization_view, read_finalization

    final = (
        read_finalization(journal, command.run_id)
        if command.input_contract == "editorial-role-packets/1"
        else None
    )
    refs = {}
    totals = {"input": 0, "output": 0}
    stages = [*proof["executed_stages"], *STAGES] if proof else manifest.get("stages", STAGES)
    for stage in stages:
        if stage == "revise" and final is not None:
            continue
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
        if proof and stage in STAGES and record.get("accepted_plan_ref") != proof["candidate_plan_ref"]:
            raise Conflict("completed_run_plan_binding_mismatch")
        refs[stage] = RuntimeRef(
            record_kind="tool_execution", record_id=invocation["output_record_id"]
        ).model_dump(mode="json")
        from .workflow import profile_for

        profile_ref = profile_for(command, stage).model_dump(mode="json")
        profile = generation.get(command.project_id, ArtifactRef.model_validate(profile_ref))
        for attempt in journal.attempts(command.run_id, stage):
            usage = attempt_usage(journal, attempt, profile.payload["output_limit"])
            for key in totals:
                totals[key] += usage[key]
    if final is None and record.get("parent_script_ref") != refs["draft"]:
        raise Conflict("completed_run_parent_script_mismatch")
    if final is not None:
        refs["revise"] = final_ref(command.run_id).model_dump(mode="json")
    result = dict(
        run_id=str(command.run_id),
        case_id=command.case_id,
        status="completed_unaccepted",
        output_refs=refs,
        semantic_acceptance=False,
        production_ready=False,
        token_usage_or_reservation=totals,
    )

    if final is not None:
        result["finalization"] = finalization_view(final, final_ref(command.run_id))
    return result
