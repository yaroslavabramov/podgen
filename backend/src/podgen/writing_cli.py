"""Frozen whole-episode writing and independent readings; model calls require run or resume."""

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
from podgen.runtime.evaluation.credit_recovery import recovery_hint
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
    root = argparse.ArgumentParser(description="Frozen episode writing with independent listener and factual readings")
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
    profile.add_argument(
        "--roles",
        nargs="+",
        choices=["writer", "editorial_reviewer", "planner", "plan_reviewer"],
        default=["writer", "editorial_reviewer"],
    )
    profile.add_argument("--operation-id", type=UUID)
    profile.add_argument("--out", type=Path, required=True)
    script = sub.add_parser("script-import")
    script.add_argument("--file", type=Path, required=True, help="Pure full WriteOutput JSON")
    script.add_argument("--out", type=Path, required=True)
    run = sub.add_parser("run")
    run.add_argument("--run-id", type=UUID, required=True)
    run.add_argument("--case", required=True)
    run.add_argument("--fixed-plan-input-ref")

    run.add_argument(
        "--author-ref",
        required=True,
        help="Exact author RuntimeRef JSON or @file; never the complete staging report",
    )
    run.add_argument("--writer-profile", required=True)
    run.add_argument("--reviewer-profile", required=True)
    run.add_argument("--plan-mode", choices=["fixed-plan", "own-plan"], default="fixed-plan")
    run.add_argument("--planner-profile")
    run.add_argument("--plan-reviewer-profile")
    run.add_argument("--max-plan-revisions", type=int, choices=[0, 1], default=1)
    run.add_argument(
        "--review-mode", choices=["informed-v1", "independent-readings/1"], default="independent-readings/1"
    )
    run.add_argument("--max-script-revisions", type=int, choices=[0, 1, 2], default=2)
    run.add_argument("--initial-script-ref", help="Saved complete script RuntimeRef JSON or @file")
    run.add_argument("--repeat-index", type=int, default=0)
    run.add_argument("--max-total-input-tokens", type=int, default=196608)
    run.add_argument("--max-total-output-tokens", type=int, default=131072)
    run.add_argument("--max-elapsed-seconds", type=int, default=7200)
    run.add_argument("--review-anchor-policy", choices=["strict", "bounded-selection-v1"], default="strict")
    run.add_argument("--export-dir", type=Path, required=True)
    for command in ["resume", "inspect"]:
        command_parser = sub.add_parser(command)
        command_parser.add_argument("--run-id", type=UUID, required=True)
        command_parser.add_argument("--export-dir", type=Path, required=True)
        if command == "resume":
            command_parser.add_argument("--repair-review-anchors", action="store_true")
            action = command_parser.add_mutually_exclusive_group()
            action.add_argument("--retry-credit-refusal", type=UUID, metavar="INVOCATION_UUID")
            action.add_argument("--repair-plan-targets", type=UUID, metavar="INVOCATION_UUID")
    from podgen.editorial_cli import add_commands

    add_commands(sub)
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
        if args.command in (
            "editorial-source-import",
            "editorial-import",
            "fixed-editorial-import",
            "preview",
        ):
            from podgen.editorial_cli import execute

            print(json.dumps(execute(args, repo, wire_ref), ensure_ascii=False))
            return 0
        if args.command == "script-import":
            from podgen.runtime.evaluation.script_import import import_script

            ref = import_script(repo, args.project, args.file.read_bytes())
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(ref.model_dump(mode="json"), indent=2) + "\n")
            print(json.dumps(dict(script_ref=ref.model_dump(mode="json")), ensure_ascii=False))
            return 0
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
                roles=args.roles,
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
            data = journal.manifest(args.run_id)["command"]
            command = WritingRunCommand.model_validate(
                {"input_contract": "legacy-author-materials/1", "review_mode": "informed-v1", **data}
            )
        else:
            command = WritingRunCommand(
                run_id=args.run_id,
                project_id=args.project,
                actor_id=args.actor,
                case_id=args.case,
                fixed_plan_input_ref=wire_ref(args.fixed_plan_input_ref, RuntimeRef)
                if args.fixed_plan_input_ref
                else None,
                author_ref=wire_ref(args.author_ref, RuntimeRef),
                writer_profile_ref=wire_ref(args.writer_profile, ArtifactRef),
                reviewer_profile_ref=wire_ref(args.reviewer_profile, ArtifactRef),
                plan_mode=args.plan_mode,
                planner_profile_ref=wire_ref(args.planner_profile, ArtifactRef)
                if args.planner_profile
                else None,
                plan_reviewer_profile_ref=wire_ref(args.plan_reviewer_profile, ArtifactRef)
                if args.plan_reviewer_profile
                else None,
                max_plan_revisions=args.max_plan_revisions,
                review_mode=args.review_mode,
                max_script_revisions=args.max_script_revisions,
                initial_script_ref=wire_ref(args.initial_script_ref, RuntimeRef)
                if args.initial_script_ref
                else None,
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
                command,
                repair_review_anchors=getattr(args, "repair_review_anchors", False),
                retry_credit_refusal=getattr(args, "retry_credit_refusal", None),
                repair_plan_targets=getattr(args, "repair_plan_targets", None),
            )
        finally:
            if journal.run(command.run_id):
                export_run(repo, args.project, command.run_id, args.export_dir)
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] != "needs_attention" else 2
    except (ContractError, RunFailure, ProviderFault, ValueError) as error:
        code = getattr(error, "code", "invalid_cli_configuration")
        print(
            json.dumps(
                dict(
                    ok=False,
                    code=code,
                    run_id=str(args.run_id) if hasattr(args, "run_id") else None,
                    invocation_id=str(error.invocation_id) if getattr(error, "invocation_id", None) else None,
                    message=recovery_hint(code),
                ),
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
