"""An explicit bounded descendant command, including a provider-free preparation path."""

import json
from pathlib import Path
from uuid import UUID

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import Conflict
from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials

from ..contracts import command_payload
from ..export import export_run
from ..journal import EvaluationJournal
from ..preview import preview_request
from ..preview_snapshot import load_preview_snapshot
from ..profiles import load_profile
from ..wire_budget import check_visible, visible_sizes
from .manifest import create
from .source import build_command, verify


def add_command(sub):
    parser = sub.add_parser(
        "continue-planning", help="One additional revision/review pair in a new frozen run"
    )
    parser.add_argument("--run-id", type=UUID, help="Optional assertion of the deterministic descendant ID")
    parser.add_argument("--source-run-id", type=UUID, required=True)
    parser.add_argument("--max-additional-plan-revisions", type=int)
    parser.add_argument("--preview-only", action="store_true", help="Freeze and export; no model calls")
    parser.add_argument("--export-dir", type=Path, required=True)


def command_for(args, journal):
    command = build_command(
        journal,
        args.source_run_id,
        args.run_id,
        limit=args.max_additional_plan_revisions,
    )
    args.run_id = command.run_id
    return command


def prepare(repo, command, directory):
    journal = EvaluationJournal(repo, command.project_id)
    generation = generation_repo(repo)
    with journal.lock(command.run_id):
        materials = load_editorial_materials(
            generation, command.project_id, command.author_ref, case_id=command.case_id
        )
        existing = journal.run(command.run_id)
        if existing is not None:
            if existing["input_fingerprint"] != digest(command_payload(command)):
                raise Conflict("run_id_reused_with_different_input")
            manifest = journal.manifest(command.run_id)
        else:
            manifest = create(command, journal, materials)
        candidate, review = verify(journal, command, manifest, materials)
        snapshot = load_preview_snapshot(journal, generation, command.run_id, "plan-revise", materials)
        profile = load_profile(
            generation,
            command.project_id,
            ArtifactRef.model_validate(manifest["command"]["planner_profile_ref"]),
            "plan-revise",
        )
        body = preview_request(
            command.run_id,
            "plan-revise",
            profile,
            materials,
            snapshot=snapshot,
            candidate=candidate["output"],
            plan_review=review["output"],
        )
        check_visible(body, command.input_contract)
        result = export_run(repo, command.project_id, command.run_id, directory)
        path = Path(directory) / "plan-revise-preview.json"
        path.write_text(
            json.dumps(
                dict(request=body, endpoint_called=False, visible_bytes=visible_sizes(body)),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    return dict(**result, endpoint_called=False, preview=str(path))
