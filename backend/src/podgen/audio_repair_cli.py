"""Re-render one confirmed fragment verbatim, reassemble locally and prepare fresh verification."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from podgen.audio.synthesis.transport import HttpSynthesisTransport
from podgen.domain.errors import ContractError
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.audio.journal import AudioJournal
from podgen.runtime.evaluation.audio.repair.contracts import RepairConfig
from podgen.runtime.evaluation.audio.repair.export import bind, export
from podgen.runtime.evaluation.audio.repair.prepare import prepare
from podgen.runtime.evaluation.audio.repair.service import reprocess, run


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Confirmed whole-fragment repair with exact frozen synthesis input"
    )
    parser.add_argument("--project", type=UUID, required=True)
    parser.add_argument("--actor", type=UUID, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    commands = parser.add_subparsers(dest="operation", required=True)
    for op in ("prepare", "run", "resume", "retry", "reprocess", "export"):
        child = commands.add_parser(op)
        child.add_argument("--config", type=Path, required=True)
        child.add_argument("--export-dir", type=Path, required=True)
        if op in ("retry", "reprocess"):
            child.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    config = RepairConfig.model_validate_json(args.config.read_text())
    db = Database(os.environ["PODGEN_DATABASE_URL"])
    journal = AudioJournal(
        Repository(db, FileObjectStore(args.objects), args.actor, purpose="evaluation"),
        args.project,
        config.comparison_run_id,
    )
    try:
        with journal.lock():
            prepare(journal, config)
        bind(journal, config, args.export_dir)
        operation_status = None
        if args.operation in ("run", "resume", "retry"):
            operation_status = run(
                journal,
                config,
                HttpSynthesisTransport(timeout_seconds=config.request_timeout_seconds),
                retry_reason=args.reason if args.operation == "retry" else None,
            )
        elif args.operation == "reprocess":
            operation_status = reprocess(journal, config, args.reason)
        result = export(journal, config, args.export_dir)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return (
            0 if args.operation in ("prepare", "export") or operation_status == "verification_required" else 2
        )
    except ContractError as error:
        print(json.dumps(dict(ok=False, code=error.code)), flush=True)
        return 2
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
