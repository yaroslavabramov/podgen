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
from podgen.runtime.evaluation.wire_budget import check_visible, input_budget_for_profile, visible_sizes
from podgen.writing.projections import CONTRACT


def add_commands(sub, *, research=False):
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
    from podgen.writing.behavior import NAMES

    if research:
        preview.set_defaults(_research=True)
        preview.add_argument("--behavior-profile", choices=NAMES)
        preview.add_argument("--editorial-pass", type=int, choices=(1, 2, 3))
    preview.add_argument(
        "--stage",
        choices=[
            "plan",
            "plan-review",
            "plan-revise",
            "plan-review-revised",
            "reader-review",
            "fact-review",
            "reader-review-1",
            "fact-review-1",
            "reader-review-2",
            "fact-review-2",
            "revise-1",
            "revise-2",
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
    for name in (
        "candidate",
        "plan-review",
        "approved-plan",
        "draft",
        "feedback",
        "reader-feedback",
        "fact-feedback",
        "composition",
    ):
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
                name: getattr(args, name, None)
                for name in (
                    "candidate",
                    "plan_review",
                    "approved_plan",
                    "draft",
                    "feedback",
                    "reader_feedback",
                    "fact_feedback",
                    "composition",
                )
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
            name: load_unique_json(getattr(args, name, None).read_text())
            if getattr(args, name, None)
            else None
            for name in (
                "candidate",
                "plan_review",
                "approved_plan",
                "draft",
                "feedback",
                "reader_feedback",
                "fact_feedback",
                "composition",
            )
        }
        from podgen.runtime.evaluation.journal import EvaluationJournal
        from podgen.runtime.evaluation.preview import preview_request
        from podgen.runtime.evaluation.preview_snapshot import load_preview_snapshot

        role_stage = {"anchor-repair": "review", "plan-target-repair": "plan-review"}.get(
            args.stage, args.stage
        )
        profile = load_profile(generation, args.project, wire_ref(args.profile, ArtifactRef), role_stage)
        snapshot = load_preview_snapshot(
            EvaluationJournal(repo, args.project), generation, args.run_id, args.stage, materials
        )
        selected_behavior = getattr(args, "behavior_profile", None)
        if snapshot is None and getattr(args, "_research", False):
            selected_behavior = selected_behavior or "existing/1"
        body = preview_request(
            args.run_id,
            args.stage,
            profile,
            materials,
            snapshot=snapshot,
            behavior_profile=selected_behavior,
            editorial_pass=getattr(args, "editorial_pass", None),
            **values,
        )
        result = dict(
            request=body, visible_bytes=visible_sizes(body), input_contract=CONTRACT, endpoint_called=False
        )
        from podgen.writing.behavior import resolve_behavior
        from podgen.writing.model_references import CATALOG_KEY, preview_references, run_references

        references = (
            run_references(materials, snapshot.manifest)
            if snapshot
            else preview_references(
                materials,
                resolve_behavior(selected_behavior, getattr(args, "editorial_pass", None) or 1).projection,
            )
        )
        if references is not None:
            result[CATALOG_KEY] = references.as_payload()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.command == "preview":
        budget = (
            snapshot.manifest.get("role_input_budget", input_budget_for_profile("existing/1"))
            if snapshot
            else input_budget_for_profile(selected_behavior or "production/1")
        )
        check_visible(result["request"], CONTRACT, max_visible_bytes=budget["visible_bytes"])
    return {key: value for key, value in result.items() if key != "request"}
