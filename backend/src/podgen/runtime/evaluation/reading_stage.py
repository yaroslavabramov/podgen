"""One immutable reading/rewrite invocation, bound to the exact full script and source readings."""

from uuid import uuid5

from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import Conflict, ContractError
from podgen.writing import author_plan
from podgen.writing.craft_reference import pinned_reference
from podgen.writing.model_references import CATALOG_KEY, run_references
from podgen.writing.plan_selection import from_manifest as selection_from_manifest
from podgen.writing.projection_versions import script_handoff
from podgen.writing.reading_views import reading_audience
from podgen.writing.revision import verification
from podgen.writing.revision.context import from_manifest as revision_context_from_manifest
from podgen.writing.roles import WriteOutput

from . import author_material
from .contracts import RunFailure
from .profiles import load_profile
from .reading_workflow import VERSION, base_stage
from .review_repair import apply_anchor_repair, prepare_anchor_repair
from .revision_invocation import prepare as prepare_revision
from .role_request import compile_reading_request
from .scripts import script_view
from .stage import StageResult
from .usage import prior_attempt_usage
from .validation import load_unique_json
from .workflow import output_model, profile_for


def execute_reading_stage(
    stage,
    *,
    journal,
    gateway,
    generation,
    command,
    manifest,
    materials,
    totals,
    guard,
    script,
    script_ref,
    readings=None,
    reading_refs=None,
    decision=None,
    approved_plan=None,
    repair_review_anchors=False,
):
    references = run_references(materials, manifest)
    base = base_stage(stage)
    recheck_pin = manifest.get(verification.KEY)
    recheck = None
    if verification.applies(stage, recheck_pin):
        from . import recheck_source

        _, recheck = recheck_source.load(journal, command.run_id, stage, script_ref=script_ref, script=script)
    model = output_model(
        stage,
        reader_schema_version=manifest.get("reader_review_schema_version"),
        fact_schema_version=manifest.get("fact_review_schema_version"),
        revision_verification_contract=recheck_pin,
    )
    revision = prepare_revision(
        journal,
        stage,
        manifest,
        script=script,
        script_ref=script_ref,
        readings=readings,
        reading_refs=reading_refs,
        speakers=materials.speaker_labels,
        approved_plan=approved_plan
        or (materials.fixed.plan.model_dump(mode="json") if materials.fixed else None),
    )
    if revision:
        model = revision.model
    profile_ref = profile_for(command, stage)
    profile = load_profile(generation, command.project_id, profile_ref, stage)
    context = dict(
        review_workflow=VERSION,
        role_projection_version=manifest["role_projection_version"],
        role_input_contract=command.input_contract,
        script_ref=script_ref,
        script_hash=digest(script),
        reading_refs=reading_refs if base == "revise" else None,
        profile_ref=profile_ref.model_dump(mode="json"),
        prompt_ref=manifest["prompt_refs"][stage],
        manifest_ref=RuntimeRef(
            record_kind="context_assembly", record_id=uuid5(command.run_id, "manifest")
        ).model_dump(mode="json"),
        materials_hash=manifest["materials_hash"],
    )
    if references is not None:
        context[CATALOG_KEY] = references.as_payload()
    if recheck is not None:
        context["revision_verification"] = dict(contract=recheck_pin, comparison_hash=digest(recheck))
    if revision:
        context.update(revision.context)
    from podgen.writing.scene_plan import is_scene_plan

    selected = approved_plan or (materials.fixed.plan.model_dump(mode="json") if materials.fixed else None)
    if script_handoff(manifest["role_projection_version"]) and (
        base == "revise" or base == "fact-review" and is_scene_plan(selected)
    ):
        context["approved_plan_hash"] = digest(selected) if selected is not None else None
    invocation = journal.invocation(command.run_id, stage)
    if invocation is None:
        instructions = generation.get(
            command.project_id, ArtifactRef.model_validate(context["prompt_ref"])
        ).payload["template"]
        body = compile_reading_request(
            command.run_id,
            stage,
            profile,
            materials,
            instructions,
            revision.schema if revision else manifest["response_schemas"][stage],
            script=script,
            audience=reading_audience(materials.packet.assignment, manifest),
            readings=readings,
            decision=decision,
            approved_plan=approved_plan,
            projection_version=manifest["role_projection_version"],
            reference_catalog=references,
            plan_selection_contract=selection_from_manifest(manifest),
            author_plan_handoff=author_plan.from_manifest(manifest),
            revision_context_contract=revision_context_from_manifest(manifest),
            revision_comparison=recheck,
            craft_reference=pinned_reference(manifest),
            **(revision.inputs if revision else {}),
        )
        invocation = journal.prepare(command.run_id, stage, body, context)
    saved = journal.read(RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"]))
    if any(saved["context"].get(key) != value for key, value in context.items()):
        raise Conflict("independent_reading_binding_mismatch")
    if (
        invocation["status"] == "validation_failed"
        and invocation["operation_kind"] == "role"
        and base in ("reader-review", "fact-review")
        and repair_review_anchors
    ):
        invocation = prepare_anchor_repair(
            journal, invocation, draft=script, draft_ref=script_ref, guard=guard
        )
    while True:
        prior = prior_attempt_usage(journal, invocation, profile)
        if invocation["status"] == "completed":
            break
        raw = gateway.invoke(
            invocation,
            profile,
            limits=command.limits,
            totals={key: totals[key] + prior[key] for key in totals},
            guard=guard,
        )
        try:
            raw = author_material.unwrap(raw, stage, manifest.get("role_projection_version"))
            if isinstance(raw, author_material.MaterialRequest):
                author_material.pause(
                    journal,
                    command,
                    invocation,
                    raw,
                    script_ref=script_ref,
                    script=script,
                    totals=totals,
                    prior=prior,
                    profile=profile,
                )
            load_unique_json(raw)
            repair_choices = None
            if invocation["operation_kind"] == "anchor_repair":
                repaired, repair_choices = apply_anchor_repair(journal, invocation, raw, draft=script)
                parsed = model.model_validate(repaired)
            else:
                parsed = model.model_validate_json(raw)
            wire_output = parsed.model_dump(mode="json")
            if references is not None:
                parsed = model.model_validate(references.translate(wire_output, inbound=True))
            revised = None
            if revision:
                output, composition, revised = revision.apply(wire_output)
            elif base in ("reader-review", "fact-review"):
                parsed.validate_against(WriteOutput.model_validate(script))
                if recheck is not None:
                    parsed.verify_assignments(recheck)
                expected = "script_only" if base == "reader-review" and recheck is None else "informed"
                if parsed.reading_mode != expected:
                    raise ValueError("reading_access_mode_mismatch")
                if base == "fact-review" and any(
                    set(finding.support_labels) - set(materials.material_catalog)
                    for finding in parsed.findings
                ):
                    raise ValueError("fact_finding_unknown_support_label")
                if parsed.recommendation == "needs_material" and not parsed.research_requests:
                    raise ValueError("needs_material_requires_anchored_question")
            elif {row.speaker for row in parsed.utterances} != set(materials.speaker_labels):
                raise ValueError("speaker_labels_mismatch")
        except (ValidationError, ValueError, ContractError) as error:
            journal.event(invocation, "output_validation_error", data={"code": str(error)})
            invocation = journal.transition(invocation, "validation_failed")
            if (
                base in ("reader-review", "fact-review")
                and invocation["operation_kind"] == "role"
                and repair_review_anchors
            ):
                invocation = prepare_anchor_repair(
                    journal, invocation, draft=script, draft_ref=script_ref, guard=guard
                )
                continue
            raise RunFailure(
                "independent_reading_output_invalid",
                run_id=command.run_id,
                invocation_id=invocation["invocation_id"],
            ) from None
        if not revision:
            output = parsed.model_dump(mode="json")
        record = dict(
            contract="independent-reading-output/1",
            run_id=str(command.run_id),
            stage=stage,
            invocation_id=str(invocation["invocation_id"]),
            request_ref=RuntimeRef(
                record_kind="tool_execution", record_id=invocation["request_record_id"]
            ).model_dump(mode="json"),
            script_ref=script_ref,
            script_hash=digest(script),
            reading_refs=context["reading_refs"],
            output=output,
            spoken_view=script_view(
                invocation["invocation_id"], output, journal.read(RuntimeRef.model_validate(script_ref))
            )
            if base == "revise"
            else [],
            repair_choices=repair_choices,
            semantic_acceptance=False,
            production_ready=False,
        )
        if references is not None:
            record["wire_output"] = wire_output
        if revision:
            record.update(revision=revised, composition=composition, wire_output=wire_output)
        ref = journal.record(record, record_id=uuid5(invocation["invocation_id"], "output"))
        journal.transition(invocation, "completed", output_ref=ref)
        break
    invocation = journal.invocation(command.run_id, stage)
    record = journal.output(invocation)
    if record["script_ref"] != script_ref or record["script_hash"] != digest(script):
        raise Conflict("independent_reading_binding_mismatch")
    if revision:
        revision.verify(record)
    return StageResult(
        record["output"],
        RuntimeRef(record_kind="tool_execution", record_id=invocation["output_record_id"]),
        (invocation["input_tokens"] or 0) + prior["input"],
        (invocation["output_tokens"] if invocation["output_tokens"] is not None else profile.output_limit)
        + prior["output"],
    )
