"""Local source-result action, deliberately separate from paid run/resume."""

import json
from uuid import UUID

from podgen.domain.contracts.common import BlobRef, RuntimeRef

from .journal import EvaluationJournal
from .source_result_recovery import recover


def add_command(sub):
    parser = sub.add_parser(
        "recover-source-result", help="Recover a saved unavailable source; stop before counting"
    )
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--invocation-id", type=UUID, required=True)
    parser.add_argument("--call-id", required=True)
    from pathlib import Path

    parser.add_argument("--export-dir", type=Path, required=True)


def execute(args, repo):
    outcome = recover(
        repo,
        args.project,
        args.run_id,
        stage=args.stage,
        invocation_id=args.invocation_id,
        call_id=args.call_id,
    )
    return export_prepared(repo, args.project, outcome, args.export_dir)


def export_prepared(repo, project, outcome, folder):
    journal = EvaluationJournal(repo, project)
    reference = RuntimeRef.model_validate(outcome["next_request_ref"])
    record = journal.read(reference)
    directory = folder / str(reference.record_id)
    directory.mkdir(parents=True, exist_ok=True)
    for field in ("request_blob", "wire_request_blob"):
        data = journal.read_blob(reference, BlobRef.model_validate(record[field]))
        (directory / (field + ".json")).write_bytes(data)
    (directory / "outcome.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n")
    return dict(outcome, export_directory=str(directory))
