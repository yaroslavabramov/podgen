"""One compiler boundary for all modern semantic projections and offline previews."""

from podgen.writing import prompt_cache
from podgen.writing.author_reply import offered, wire_schema
from podgen.writing.model_references import bind_schema, preview_references
from podgen.writing.projection_versions import CURRENT
from podgen.writing.projections import role_payload, validate_model_content

from .compiler import response_request


def compile_role_request(
    run_id,
    stage,
    profile,
    materials,
    instructions,
    schema,
    *,
    projection_version=CURRENT,
    reference_catalog=None,
    prompt_cache_layout=None,
    source_tool_contract=None,
    messages_tool_policy=None,
    **inputs,
):
    prompt_cache.validate(prompt_cache_layout, projection_version)
    payload = role_payload(
        stage,
        materials,
        projection_version=projection_version,
        research_navigation=bool(
            source_tool_contract
            and source_tool_contract["version"] in ("frozen-corpus-tools/7", "frozen-corpus-tools/8")
        ),
        **inputs,
    )
    if (
        source_tool_contract
        and source_tool_contract["version"] in ("frozen-corpus-tools/7", "frozen-corpus-tools/8")
        and stage == "draft"
    ):
        from .research_annex import attach as attach_annex

        attach_annex(
            payload,
            materials,
            inputs.get("approved_plan")
            or (materials.fixed.plan.model_dump(mode="json") if materials.fixed else None),
        )
    if source_tool_contract and source_tool_contract["version"] in (
        "frozen-corpus-tools/7",
        "frozen-corpus-tools/8",
    ):
        payload["research_results"] = dict(tool="list_research_results", offset=0, version=None)
    references = reference_catalog or preview_references(materials, projection_version)
    if references is not None:
        payload = references.translate(payload)
        schema = bind_schema(schema, references)
    if offered(stage, projection_version):
        schema = wire_schema(schema, speaker_labels=materials.speaker_labels)
    validate_model_content(dict(instructions=instructions, schema=schema), materials.documents)
    body = response_request(
        run_id,
        stage,
        profile,
        payload,
        instructions,
        schema,
        prompt_cache_layout=prompt_cache_layout,
        messages_tool_policy=messages_tool_policy,
    )
    from .source_tool_contract import bind

    return bind(body, stage, profile, source_tool_contract)


def compile_reading_request(
    run_id,
    stage,
    profile,
    materials,
    instructions,
    schema,
    *,
    reference_catalog=None,
    revision_comparison=None,
    reading_audit_context=None,
    prompt_cache_layout=None,
    source_tool_contract=None,
    messages_tool_policy=None,
    **inputs,
):
    from podgen.writing.reading_views import reading_payload

    from .reading_workflow import base_stage

    payload = reading_payload(
        base_stage(stage),
        materials,
        prompt_cache_layout=prompt_cache_layout,
        research_navigation=bool(
            source_tool_contract
            and source_tool_contract["version"] in ("frozen-corpus-tools/7", "frozen-corpus-tools/8")
        ),
        **inputs,
    )
    if (
        source_tool_contract
        and source_tool_contract["version"] in ("frozen-corpus-tools/7", "frozen-corpus-tools/8")
        and base_stage(stage) == "revise"
    ):
        from .research_annex import attach as attach_annex

        attach_annex(
            payload,
            materials,
            inputs.get("approved_plan")
            or (materials.fixed.plan.model_dump(mode="json") if materials.fixed else None),
        )
    if (
        source_tool_contract
        and source_tool_contract["version"] in ("frozen-corpus-tools/7", "frozen-corpus-tools/8")
        and base_stage(stage) != "reader-review"
    ):
        payload["research_results"] = dict(tool="list_research_results", offset=0, version=None)
    if reading_audit_context is not None:
        from podgen.writing.reading_audit.input import attach
        from podgen.writing.revision.verification import bind_assignments

        payload = attach(payload, reading_audit_context, fact=base_stage(stage) == "fact-review")
        schema = bind_assignments(schema, reading_audit_context)
    if revision_comparison is not None:
        from podgen.writing.revision.verification import STAGES, bind_assignments

        if stage not in STAGES:
            from podgen.domain.errors import ContractError

            raise ContractError("revision_verification_stage_mismatch")
        payload["revision_check"] = revision_comparison
        schema = bind_assignments(schema, revision_comparison)
    references = reference_catalog or preview_references(materials, inputs.get("projection_version", CURRENT))
    if references is not None:
        payload = references.translate(payload)
        schema = bind_schema(schema, references)
    if offered(stage, inputs.get("projection_version", CURRENT)):
        schema = wire_schema(schema, speaker_labels=materials.speaker_labels)
    from podgen.writing.revision.context import COMPACT_VERSION
    from podgen.writing.story_views import selected_labels

    stable_support_ids = None
    if base_stage(stage) == "revise" and inputs.get("revision_context_contract") == COMPACT_VERSION:
        plan = inputs.get("approved_plan") or (
            materials.fixed.plan.model_dump(mode="json") if materials.fixed else None
        )
        stable_support_ids = sorted(selected_labels(plan, materials.packet)) if plan is not None else []
        if references is not None:
            stable_support_ids = references.translate({"material_refs": stable_support_ids})["material_refs"]
    body = response_request(
        run_id,
        stage,
        profile,
        payload,
        instructions,
        schema,
        prompt_cache_layout=prompt_cache_layout,
        stable_support_ids=stable_support_ids,
        messages_tool_policy=messages_tool_policy,
    )
    from podgen.writing.research_materials.fact_source_mode import KEY, request

    from .source_tool_contract import bind

    return request(bind(body, stage, profile, source_tool_contract), (source_tool_contract or {}).get(KEY))
