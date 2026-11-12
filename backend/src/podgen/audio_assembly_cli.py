"""Local exact-frame assembly of approved evaluation synthesis parts; no provider transport."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from podgen.domain.errors import ContractError
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.audio.assembly.contracts import AssemblyConfig
from podgen.runtime.evaluation.audio.assembly.export import export
from podgen.runtime.evaluation.audio.assembly.service import build
from podgen.runtime.evaluation.audio.journal import AudioJournal


def main(argv=None):
    parser = argparse.ArgumentParser(description="Assemble exact synthesis parts into one listening master")
    parser.add_argument("--project", type=UUID, required=True)
    parser.add_argument("--actor", type=UUID, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("build", "export"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--export-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    config = AssemblyConfig.model_validate_json(args.config.read_text())
    db = Database(os.environ["PODGEN_DATABASE_URL"])
    journal = AudioJournal(
        Repository(db, FileObjectStore(args.objects), args.actor, purpose="evaluation"),
        args.project,
        config.comparison_run_id,
    )
    try:
        if args.operation == "build":
            build(journal, config)
        result = export(journal, config, args.export_dir)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0
    except ContractError as error:
        print(json.dumps(dict(ok=False, code=error.code)), flush=True)
        return 2
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
