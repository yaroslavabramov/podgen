"""Admit one bounded native editorial continuation, with an offline frozen preview."""

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
        "continue-editorial",
        help="Continue one revision and both readings with cumulative history and budgets",
    )
    parser.add_argument("--source-run-id", type=UUID, required=True)
    parser.add_argument("--run-id", type=UUID, help="Optional assertion of the deterministic descendant ID")
    parser.add_argument("--additional-input-tokens", type=int, required=True)
    parser.add_argument("--additional-output-tokens", type=int, default=0)
    parser.add_argument("--preview-only", action="store_true")
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.set_defaults(stop_after_plan=False)
    return parser


def command_for(args, journal):
    command = build_command(
        journal,
        args.source_run_id,
        run_id=args.run_id,
        additional_input=args.additional_input_tokens,
        additional_output=args.additional_output_tokens,
    )
    args.run_id = command.run_id
    return command


def prepare(repo, command, directory):
    journal = EvaluationJournal(repo, command.project_id)
    generation = generation_repo(repo)
    with journal.lock(command.run_id):
        materials = load_editorial_materials(
            generation,
            command.project_id,
            command.author_ref,
            case_id=command.case_id,
            fixed_ref=command.fixed_plan_input_ref,
        )
        existing = journal.run(command.run_id)
        if existing:
            if existing["input_fingerprint"] != digest(command_payload(command)):
                raise Conflict("run_id_reused_with_different_input")
            manifest = journal.manifest(command.run_id)
        else:
            manifest = create(command, journal, materials)
        verify(journal, command, manifest, materials)
        snapshot = load_preview_snapshot(journal, generation, command.run_id, "revise-1", materials)
        profile = load_profile(
            generation,
            command.project_id,
            ArtifactRef.model_validate(manifest["command"]["writer_profile_ref"]),
            "revise-1",
        )
        body = preview_request(command.run_id, "revise-1", profile, materials, snapshot=snapshot)
        check_visible(
            body, command.input_contract, max_visible_bytes=manifest["role_input_budget"]["visible_bytes"]
        )
        result = export_run(repo, command.project_id, command.run_id, directory)
        path = Path(directory) / "revise-1-preview.json"
        path.write_text(
            json.dumps(
                dict(request=body, endpoint_called=False, visible_bytes=visible_sizes(body)),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    return dict(**result, endpoint_called=False, preview=str(path))
