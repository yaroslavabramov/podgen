"""One controlled report-off fact reading; preview is free, run requires its exact approved hash."""

import argparse
import difflib
import json
import os
from pathlib import Path
from uuid import UUID

from dotenv import load_dotenv

from podgen.domain.canonical import digest
from podgen.persistence.database import Database
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects, Measurements
from podgen.runtime.evaluation.fact_source_experiment.preparation import prepare
from podgen.runtime.evaluation.fact_source_experiment.service import run
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.runtime.models.openai_provider import OpenAIProvider


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--project", type=UUID, required=True)
    parser.add_argument("--actor", type=UUID, required=True)
    parser.add_argument("--objects", type=Path, required=True)
    parser.add_argument("--source-run-id", type=UUID, required=True)
    parser.add_argument("--run-id", type=UUID, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("operation", choices=("preview", "run"))
    parser.add_argument("--approved-request-hash")
    args = parser.parse_args(argv)
    if args.env_file:
        load_dotenv(args.env_file, override=False)
    database = Database(os.environ["PODGEN_DATABASE_URL"])
    objects = MeasuredObjects(args.objects)
    repo = Repository(database, objects, args.actor, purpose="evaluation")
    generation = Repository(database, objects, args.actor, purpose="generation")
    journal = EvaluationJournal(repo, args.project)
    metrics = Measurements(database, objects)
    try:
        if args.operation == "preview":
            with metrics.phase("preview"):
                result = prepare(repo, generation, journal, args.source_run_id, args.run_id)
            before, after = result.source_body, result.body
            write(args.out / "before.request.json", before)
            write(args.out / "after.request.json", after)
            write(args.out / "source-inventory.json", result.sources)
            write(args.out / "model-profile.json", result.profile.model_dump(mode="json"))
            write(
                args.out / "comparison.json",
                dict(
                    approved_request_hash=digest(after),
                    source_request_hash=digest(before),
                    source_documents=sum(row["sha256"] is not None for row in result.sources),
                    source_documents_exact=True,
                    source_inventory_hash=digest(result.sources),
                    history_view=result.manifest["source_tool_contract"].get("history_view"),
                    removed_payload_fields=sorted(set(request_payload(before)) - set(request_payload(after))),
                    removed_tools=sorted(
                        {tool["name"] for tool in before["tools"]} - {tool["name"] for tool in after["tools"]}
                    ),
                    inference_settings_exact={
                        key: before.get(key) == after.get(key)
                        for key in (
                            "model",
                            "max_output_tokens",
                            "reasoning",
                            "tool_choice",
                            "parallel_tool_calls",
                            "truncation",
                            "stream",
                            "store",
                            "background",
                            "prompt_cache_options",
                            "include",
                        )
                    },
                    script_exact=request_payload(before)["script"] == request_payload(after)["script"],
                    factual_remit_exact=request_payload(before)["factual_remit"]
                    == request_payload(after)["factual_remit"],
                    audit_context_exact=request_payload(before)["audit_context"]
                    == request_payload(after)["audit_context"],
                ),
            )
            for name, body in (("before", before), ("after", after)):
                (args.out / f"{name}.instructions.md").write_text(body["instructions"])
            (args.out / "full-request.diff").write_text(
                "".join(
                    difflib.unified_diff(
                        json.dumps(before, ensure_ascii=False, indent=2).splitlines(True),
                        json.dumps(after, ensure_ascii=False, indent=2).splitlines(True),
                        fromfile="saved-fact-review.request.json",
                        tofile="retained-sources-only.request.json",
                    )
                )
            )
        else:
            if not args.approved_request_hash:
                parser.error("run requires the reviewed preview's --approved-request-hash")
            run(
                repo,
                generation,
                journal,
                args.source_run_id,
                args.run_id,
                lambda profile: OpenAIProvider(
                    os.environ["OPENAI_API_KEY"],
                    timeout_seconds=profile.timeout_policy["rpc_timeout_seconds"],
                ),
                args.approved_request_hash,
                metrics,
                args.out,
            )
    finally:
        write(args.out / (args.operation + "-measurements.json"), metrics.snapshot())
        database.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
