"""Controlled whole-episode baseline A. No live calls unless the operator runs `run`."""

import argparse
import json
import os
import sys
from pathlib import Path
from uuid import UUID

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import ContractError
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.contracts import RunFailure, RunLimits, WritingRunCommand
from podgen.runtime.evaluation.export import export_run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.profiles import create_profile
from podgen.runtime.evaluation.service import WritingEvaluation
from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.openai_provider import OpenAIProvider


def wire_ref(value, model):
    data = json.loads(Path(value[1:]).read_text() if value.startswith("@") else value)
    return model.model_validate(data)


def parser():
    root = argparse.ArgumentParser(description="Frozen whole-episode baseline A: draft / review / revise")
    root.add_argument("--env-file", type=Path)
    root.add_argument("--project", type=UUID, required=True)
    root.add_argument("--actor", type=UUID, required=True)
    root.add_argument("--objects", type=Path, required=True)
    sub = root.add_subparsers(dest="command", required=True)
    profile = sub.add_parser("profile-create")
    profile.add_argument("--model", required=True)
    profile.add_argument("--context-limit", type=int, required=True)
    profile.add_argument("--output-limit", type=int, default=65536)
    profile.add_argument(
        "--reasoning-effort", choices=["none", "low", "medium", "high", "xhigh"], default="high"
    )
    profile.add_argument("--verbosity", choices=["low", "medium", "high"], default="high")
    profile.add_argument("--timeout-seconds", type=int, default=1800)
    profile.add_argument("--background", action="store_true")
    profile.add_argument(
        "--store",
        action="store_true",
        help="Explicitly authorize provider response storage; required for background retrieval",
    )
    profile.add_argument("--operation-id", type=UUID)
    profile.add_argument("--out", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--run-id", type=UUID, required=True)
    run.add_argument("--case", choices=["gps-clock", "snow-pump"], required=True)
    run.add_argument(
        "--author-ref",
        required=True,
        help="Exact author RuntimeRef JSON or @file; never the complete staging report",
    )
    run.add_argument("--writer-profile", required=True)
    run.add_argument("--reviewer-profile", required=True)
    run.add_argument("--repeat-index", type=int, default=0)
    run.add_argument("--max-total-input-tokens", type=int, default=900000)
    run.add_argument("--max-total-output-tokens", type=int, default=196608)
    run.add_argument("--max-elapsed-seconds", type=int, default=7200)
    run.add_argument("--review-anchor-policy", choices=["strict", "bounded-selection-v1"], default="strict")
    run.add_argument("--export-dir", type=Path, required=True)
    for command in ["resume", "inspect"]:
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--run-id", type=UUID, required=True)
        command_parser.add_argument("--export-dir", type=Path, required=True)
        if command == "resume":
            command_parser.add_argument("--repair-review-anchors", action="store_true")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.env_file:
        from dotenv import load_dotenv

        load_dotenv(args.env_file, override=False)
    if not os.environ.get("PODGEN_DATABASE_URL"):
        print(json.dumps({"ok": False, "code": "missing_database_url"}), file=sys.stderr)
        return 2
    repo = Repository(
        Database(os.environ["PODGEN_DATABASE_URL"]),
        FileObjectStore(args.objects),
        args.actor,
        purpose="evaluation",
    )
    try:
        if args.command == "profile-create":
            profile = create_profile(
                repo,
                args.project,
                model=args.model,
                context_limit=args.context_limit,
                output_limit=args.output_limit,
                reasoning_effort=args.reasoning_effort,
                verbosity=args.verbosity,
                timeout_seconds=args.timeout_seconds,
                background=args.background,
                store=args.store,
                operation_id=args.operation_id,
            )
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(profile.ref.model_dump(mode="json"), indent=2) + "\n")
            print(
                json.dumps(
                    dict(
                        profile_ref=profile.ref.model_dump(mode="json"),
                        configuration=profile.payload,
                        qualified=False,
                    ),
                    ensure_ascii=False,
                )
            )
            return 0
        if args.command == "inspect":
            print(
                json.dumps(export_run(repo, args.project, args.run_id, args.export_dir), ensure_ascii=False)
            )
            return 0
        journal = EvaluationJournal(repo, args.project)
        if args.command == "resume":
            command = WritingRunCommand.model_validate(journal.manifest(args.run_id)["command"])
        else:
            command = WritingRunCommand(
                run_id=args.run_id,
                project_id=args.project,
                actor_id=args.actor,
                case_id=args.case,
                author_ref=wire_ref(args.author_ref, RuntimeRef),
                writer_profile_ref=wire_ref(args.writer_profile, ArtifactRef),
                reviewer_profile_ref=wire_ref(args.reviewer_profile, ArtifactRef),
                repeat_index=args.repeat_index,
                review_anchor_policy=args.review_anchor_policy,
                limits=RunLimits(
                    max_total_input_tokens=args.max_total_input_tokens,
                    max_total_output_tokens=args.max_total_output_tokens,
                    max_elapsed_seconds=args.max_elapsed_seconds,
                ),
            )

        def provider(profile):
            return OpenAIProvider(
                os.environ.get("OPENAI_API_KEY", ""),
                timeout_seconds=profile.timeout_policy.get("rpc_timeout_seconds", 1800),
            )

        print(
            json.dumps(
                dict(event="run_started", run_id=str(command.run_id), case_id=command.case_id),
                ensure_ascii=False,
            ),
            flush=True,
        )
        try:
            result = WritingEvaluation(repo, provider).run(
                command, repair_review_anchors=getattr(args, "repair_review_anchors", False)
            )
        finally:
            if journal.run(command.run_id):
                export_run(repo, args.project, command.run_id, args.export_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ContractError, RunFailure, ProviderFault, ValueError) as error:
        code = getattr(error, "code", "invalid_cli_configuration")
        print(
            json.dumps(
                dict(ok=False, code=code, run_id=str(args.run_id) if hasattr(args, "run_id") else None)
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
