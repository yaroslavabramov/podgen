"""Auditable independent ASR and candidate generation; no synthesis or acceptance."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from podgen.audio.http import RawAudioTransport
from podgen.domain.errors import ContractError
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.audio.fidelity.contracts import load_fidelity_config
from podgen.runtime.evaluation.audio.fidelity.export import bind, export
from podgen.runtime.evaluation.audio.fidelity.prepare import prepare
from podgen.runtime.evaluation.audio.fidelity.service import reprocess, run
from podgen.runtime.evaluation.audio.journal import AudioJournal


def parser():
    p = argparse.ArgumentParser(description="Independent recognition of an archived audio result")
    p.add_argument("--project", type=UUID, required=True)
    p.add_argument("--actor", type=UUID, required=True)
    p.add_argument("--objects", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    commands = p.add_subparsers(dest="operation", required=True)
    for op in ("prepare", "run", "resume", "reprocess", "export"):
        command = commands.add_parser(op)
        command.add_argument("--config", type=Path, required=True)
        command.add_argument("--export-dir", type=Path, required=True)
        if op == "reprocess":
            command.add_argument("--reason", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    config = load_fidelity_config(args.config.read_text())
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
        if args.operation in ("run", "resume"):
            run(
                journal, config, RawAudioTransport(timeout_seconds=config.recognition.request_timeout_seconds)
            )
        elif args.operation == "reprocess":
            reprocess(journal, config, args.reason)
        result = export(journal, config, args.export_dir)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return (
            0 if args.operation in ("prepare", "export") or result["status"] == "candidates_generated" else 2
        )
    except ContractError as error:
        print(json.dumps(dict(ok=False, code=error.code)), flush=True)
        return 2
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
