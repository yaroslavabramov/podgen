"""Prepare, approve, submit/resume and export a single durable hosted research job."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from pydantic import ValidationError

from podgen.domain.errors import ContractError
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.research.hosted.contracts import Approval, ResearchConfig
from podgen.research.hosted.export import export, write_json
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.provider import GeminiResearchProvider
from podgen.research.hosted.service import advance, approve, manifest, prepare
from podgen.research.hosted.sources import SourceSelection, acquire_report_sources
from podgen.research.hosted.transport import HttpTransport


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    for name in ("project", "actor", "run-id"):
        root.add_argument("--" + name, type=UUID, required=True)
    root.add_argument("--objects", type=Path, required=True)
    root.add_argument("--env-file", type=Path)
    commands = root.add_subparsers(dest="operation", required=True)
    for name in ("prepare", "approve", "resume", "cancel", "reprocess", "export", "acquire-sources"):
        command = commands.add_parser(name)
        command.add_argument("--export-dir", type=Path, required=True)
        if name == "prepare":
            command.add_argument("--config", type=Path, required=True)
        if name == "acquire-sources":
            command.add_argument("--source-config", type=Path, required=True)
        if name == "approve":
            command.add_argument("--request-hash", required=True)
            command.add_argument("--reason", required=True)
            command.add_argument("--accept-variable-provider-cost", action="store_true", required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    db = Database(os.environ["PODGEN_DATABASE_URL"])
    journal = ResearchJournal(
        Repository(db, FileObjectStore(args.objects), args.actor), args.project, args.run_id
    )
    try:
        operation = None
        if args.operation == "prepare":
            prepare(journal, ResearchConfig.model_validate_json(args.config.read_bytes()))
        else:
            export(journal, args.export_dir)  # Check directory ownership before any paid action.
            if args.operation == "approve":
                approve(
                    journal,
                    Approval(
                        request_hash=args.request_hash,
                        reason=args.reason,
                        accepts_variable_provider_cost=args.accept_variable_provider_cost,
                    ),
                )
            elif args.operation == "acquire-sources":
                selection = SourceSelection.model_validate_json(args.source_config.read_bytes())
                acquired = asyncio.run(acquire_report_sources(journal, selection))
                write_json(args.export_dir / ("sources-" + str(selection.operation_id) + ".json"), acquired)
            elif args.operation != "export":
                config = ResearchConfig.model_validate(manifest(journal)["config"])
                provider = GeminiResearchProvider(HttpTransport(config.profile.request_timeout_seconds))
                operation = advance(journal, provider, operation=args.operation)
        result = export(journal, args.export_dir)
        if operation and operation.get("operation_error"):
            result["operation_error"] = operation["operation_error"]
            write_json(args.export_dir / "status.json", result)
        print(json.dumps(result, ensure_ascii=False))
        return (
            0
            if result["state"] in ("prepared", "in_progress", "completed", "cancelled")
            and not result.get("operation_error")
            else 2
        )
    except (ContractError, ValidationError) as error:
        failure = dict(ok=False, code=getattr(error, "code", "validation_error"), detail=str(error))
        # The operation may have saved a receipt before local parsing failed.
        # Refresh the observable state without concealing the original error.
        try:
            failure.update(export(journal, args.export_dir))
            write_json(args.export_dir / "status.json", failure)
        except (ContractError, ValueError, OSError):
            pass
        print(json.dumps(failure, ensure_ascii=False))
        return 2
    finally:
        db.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
