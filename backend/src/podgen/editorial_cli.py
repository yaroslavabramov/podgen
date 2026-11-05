"""Admission and offline wire preview. No provider client is constructed here."""

import json
from pathlib import Path

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.persistence.editorial_inputs import (
    generation_repo,
    import_fixed,
    import_shared,
    load_editorial_materials,
)
from podgen.runtime.evaluation.profiles import load_profile
from podgen.runtime.evaluation.validation import load_unique_json
from podgen.runtime.evaluation.wire_budget import check_visible, visible_sizes
from podgen.writing.projections import CONTRACT


def add_commands(sub):
    source = sub.add_parser("editorial-source-import")
    source.add_argument("--file", type=Path, required=True)
    source.add_argument("--source-path", required=True)
    source.add_argument("--scope", required=True)
    source.add_argument("--purpose", choices=["brief", "dossier", "primary", "fixed_plan"], required=True)
    source.add_argument("--out", type=Path, required=True)
    for name in ("editorial-import", "fixed-editorial-import"):
        command = sub.add_parser(name)
        command.add_argument("--input", type=Path, required=True)
        command.add_argument("--source-map", type=Path, required=True)
        command.add_argument("--out", type=Path, required=True)
        if name == "fixed-editorial-import":
            command.add_argument("--shared-ref", required=True)
    preview = sub.add_parser("preview")
    preview.add_argument("--shared-ref", required=True)
    preview.add_argument("--fixed-ref")
    preview.add_argument("--case", required=True)
    preview.add_argument(
        "--stage",
        choices=[
            "plan",
            "plan-review",
            "plan-revise",
            "plan-review-revised",
            "draft",
            "review",
            "revise",
            "anchor-repair",
            "plan-target-repair",
        ],
        required=True,
    )
    preview.add_argument("--profile", required=True)
    preview.add_argument("--run-id", type=__import__("uuid").UUID, required=True)
    for name in ("candidate", "plan-review", "approved-plan", "draft", "feedback"):
        preview.add_argument("--" + name, type=Path)
    preview.add_argument("--out", type=Path, required=True)


def execute(args, repo, wire_ref):
    if args.command == "editorial-source-import":
        from podgen.persistence.editorial_documents import import_document

        result = import_document(
            repo,
            args.project,
            args.file.read_bytes(),
            source_path=args.source_path,
            purpose=args.purpose,
            scope_key=args.scope,
        ).model_dump(mode="json")
    elif args.command in ("editorial-import", "fixed-editorial-import"):
        raw = args.input.read_bytes()
        data = load_unique_json(raw.decode())
        source_map = load_unique_json(args.source_map.read_text())
        if args.command == "editorial-import":
            ref = import_shared(repo, args.project, data, source_map, original_bytes=raw)
        else:
            ref = import_fixed(
                repo,
                args.project,
                data,
                source_map,
                wire_ref(args.shared_ref, RuntimeRef),
                original_bytes=raw,
            )
        result = ref.model_dump(mode="json")
    else:
        from podgen.runtime.evaluation.preview_inputs import validate_preview_inputs

        validate_preview_inputs(
            args.stage,
            fixed=args.fixed_ref is not None,
            inputs={
                name: getattr(args, name)
                for name in ("candidate", "plan_review", "approved_plan", "draft", "feedback")
            },
        )
        generation = generation_repo(repo)
        materials = load_editorial_materials(
            generation,
            args.project,
            wire_ref(args.shared_ref, RuntimeRef),
            case_id=args.case,
            fixed_ref=wire_ref(args.fixed_ref, RuntimeRef) if args.fixed_ref else None,
        )
        values = {
            name: load_unique_json(getattr(args, name).read_text()) if getattr(args, name) else None
            for name in ("candidate", "plan_review", "approved_plan", "draft", "feedback")
        }
        from podgen.runtime.evaluation.preview import preview_request

        role_stage = {"anchor-repair": "review", "plan-target-repair": "plan-review"}.get(
            args.stage, args.stage
        )
        profile = load_profile(generation, args.project, wire_ref(args.profile, ArtifactRef), role_stage)
        body = preview_request(args.run_id, args.stage, profile, materials, **values)
        result = dict(
            request=body, visible_bytes=visible_sizes(body), input_contract=CONTRACT, endpoint_called=False
        )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.command == "preview":
        check_visible(result["request"], CONTRACT)
    return {key: value for key, value in result.items() if key != "request"}
