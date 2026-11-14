"""Prepare and operate one durable episode from an ordinary topic."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv
from pydantic import ValidationError

from podgen.domain.errors import ContractError
from podgen.episodes.production import providers
from podgen.episodes.production.contracts import ProductionConfig, ProductionSettings
from podgen.episodes.production.export import export, status
from podgen.episodes.production.journal import ProductionJournal
from podgen.episodes.production.prepare import prepare
from podgen.episodes.production.service import EpisodeProduction
from podgen.episodes.production.settings import from_references
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--project", type=UUID)
    root.add_argument("--actor", type=UUID)
    root.add_argument("--objects", type=Path)
    root.add_argument("--reference-state", type=Path)
    root.add_argument("--env-file", type=Path)
    root.add_argument("--run-id", type=UUID, required=True)
    commands = root.add_subparsers(dest="operation", required=True)
    for name in ("prepare", "start", "run", "status", "pause", "resume", "export"):
        child = commands.add_parser(name)
        if name in ("prepare", "start"):
            child.add_argument("--topic", required=True)
            child.add_argument("--output-language", default="ru")
            child.add_argument("--min-seconds", type=int, default=720)
            child.add_argument("--target-seconds", type=int, default=810)
            child.add_argument("--max-seconds", type=int, default=900)
            child.add_argument("--settings", type=Path)
            child.add_argument("--research-template", type=Path)
            child.add_argument("--audio-template", type=Path)
            child.add_argument("--max-research-jobs", type=int)
            child.add_argument("--audio-budget-usd")
        if name in ("start", "run", "resume"):
            child.add_argument("--step", action="store_true", help="Return when research needs another poll")
            child.add_argument("--poll-seconds", type=float, default=15)
            child.add_argument("--export-dir", type=Path, required=name == "start")
        if name == "export":
            child.add_argument("--export-dir", type=Path, required=True)
    return root


def repository(args):
    state = json.loads(args.reference_state.read_text()) if args.reference_state else {}
    project, actor = args.project or state.get("project"), args.actor or state.get("actor")
    objects = args.objects or state.get("objects")
    url = os.environ.get("PODGEN_DATABASE_URL") or state.get("database_url")
    if not all((project, actor, objects, url)):
        raise ContractError("episode_repository_configuration_required")
    database = Database(url)
    return Repository(database, FileObjectStore(Path(objects)), UUID(str(actor)), purpose="evaluation"), UUID(
        str(project)
    )


def configuration(args, repo, project):
    if args.settings:
        if any((args.research_template, args.audio_template, args.max_research_jobs, args.audio_budget_usd)):
            raise ContractError("episode_settings_inputs_are_mutually_exclusive")
        settings = ProductionSettings.model_validate_json(args.settings.read_bytes())
    else:
        if not all(
            (
                args.reference_state,
                args.research_template,
                args.audio_template,
                args.max_research_jobs is not None,
                args.audio_budget_usd is not None,
            )
        ):
            raise ContractError("episode_settings_or_explicit_reference_settings_required")
        settings = from_references(
            repo,
            project,
            args.reference_state,
            args.research_template,
            args.audio_template,
            max_research_jobs=args.max_research_jobs,
            audio_budget=args.audio_budget_usd,
        )
    return ProductionConfig(
        run_id=args.run_id,
        topic=args.topic,
        output_language=args.output_language,
        min_seconds=args.min_seconds,
        target_seconds=args.target_seconds,
        max_seconds=args.max_seconds,
        settings=settings,
    )


def operate(args, repo, project):
    journal = ProductionJournal(repo, project, args.run_id)
    if args.operation in ("prepare", "start"):
        prepare(repo, project, configuration(args, repo, project))
    if args.operation == "pause":
        return status(journal.request("paused"))
    if args.operation == "export":
        return export(journal, args.export_dir)
    row = journal.read()
    if args.operation in ("prepare", "status") or row["status"] == "assembled":
        if getattr(args, "export_dir", None):
            return export(journal, args.export_dir)
        return status(row)
    if args.operation == "resume":
        row = journal.request("running")
    if args.operation == "run" and row["desired_state"] == "paused":
        return status(row)
    if args.poll_seconds <= 0:
        raise ContractError("episode_poll_interval_must_be_positive")
    settings = ProductionConfig.model_validate(row["configuration"]).settings
    providers.credentials(repo, project, settings)
    runner = EpisodeProduction(
        repo,
        project,
        args.run_id,
        research_provider=providers.research_provider(settings),
        model_provider=providers.model_provider,
        audio_transport=providers.audio_transport(),
    )
    row = runner.run(wait=not args.step, poll_seconds=args.poll_seconds)
    return export(journal, args.export_dir) if args.export_dir else status(row)


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    repo = None
    try:
        repo, project = repository(args)
        result = operate(args, repo, project)
        print(json.dumps(result, ensure_ascii=False))
        return 2 if result["state"] == "needs_attention" else 0
    except (ContractError, ValidationError) as error:
        print(json.dumps(dict(ok=False, code=getattr(error, "code", "validation_error")), ensure_ascii=False))
        return 2
    finally:
        if repo is not None:
            repo.database.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
