"""Actual-audio adjudication of frozen fidelity candidates; never synthesizes or edits a script."""

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
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewConfig
from podgen.runtime.evaluation.audio.adjudication.export import bind, export
from podgen.runtime.evaluation.audio.adjudication.prepare import prepare
from podgen.runtime.evaluation.audio.adjudication.service import reprocess, run
from podgen.runtime.evaluation.audio.journal import AudioJournal


def parser():
    p = argparse.ArgumentParser(description="Resolve supplied fidelity cases against actual audio")
    p.add_argument("--project", type=UUID, required=True)
    p.add_argument("--actor", type=UUID, required=True)
    p.add_argument("--objects", type=Path, required=True)
    p.add_argument("--env-file", type=Path)
    commands = p.add_subparsers(dest="operation", required=True)
    for op in ("prepare", "run", "resume", "retry", "reprocess", "export"):
        child = commands.add_parser(op)
        child.add_argument("--config", type=Path, required=True)
        child.add_argument("--export-dir", type=Path, required=True)
        if op == "retry":
            child.add_argument("--window-number", type=int, required=True)
        if op in ("retry", "reprocess"):
            child.add_argument("--reason", required=True)
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    config = ReviewConfig.model_validate_json(args.config.read_text())
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
        if args.operation in ("run", "resume", "retry"):
            run(
                journal,
                config,
                RawAudioTransport(
                    timeout_seconds=config.reviewer.request_timeout_seconds,
                    auth_header="x-goog-api-key",
                    auth_prefix="",
                ),
                retry_window=getattr(args, "window_number", None),
                reason=getattr(args, "reason", None),
            )
        elif args.operation == "reprocess":
            reprocess(journal, config, args.reason)
        result = export(journal, config, args.export_dir)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if args.operation in ("prepare", "export") or result["all_cases_resolved"] else 2
    except ContractError as error:
        print(json.dumps(dict(ok=False, code=error.code)), flush=True)
        return 2
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
