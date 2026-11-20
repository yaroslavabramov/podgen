"""Run matched, resumable whole-episode and sectional fact readings on a saved revision."""

import argparse
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from accounting import collect, combined
from dotenv import load_dotenv
from material import load, relevant
from scopes import Partition, handoff
from trial import Trial

from podgen.persistence.database import Database
from podgen.persistence.private_journal.state import head
from podgen.persistence.repository import Repository
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects, Measurements
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.models.openai_provider import OpenAIProvider


def write(path, value):
    from tempfile import NamedTemporaryFile

    with NamedTemporaryFile("w", dir=path.parent, encoding="utf-8", delete=False) as output:
        json.dump(value, output, ensure_ascii=False, indent=2, default=str)
        output.write("\n")
    Path(output.name).replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--source-run", type=UUID, required=True)
    parser.add_argument("--stage", default="revise-2", choices=["revise-1", "revise-2"])
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("operation", choices=["preview", "partition", "whole", "split", "receipts"])
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    load_dotenv()
    db = Database(state["database_url"])
    objects = MeasuredObjects(Path(state["objects"]))
    repo = Repository(db, objects, UUID(state["actor"]), purpose="evaluation")
    generation = Repository(db, objects, repo.actor_id, purpose="generation")
    journal = EvaluationJournal(repo, UUID(state["project"]))
    metrics = Measurements(db, objects)
    folder = args.destination
    folder.mkdir(parents=True, exist_ok=True)
    try:
        with metrics.phase("prepare"):
            case = load(repo, generation, journal, args.source_run, args.stage)
            trial = Trial(
                case,
                repo,
                generation,
                journal,
                lambda p: OpenAIProvider(
                    os.environ["OPENAI_API_KEY"], timeout_seconds=p.timeout_policy["rpc_timeout_seconds"]
                ),
                metrics,
            )
            root = uuid5(args.source_run, f"fact-scope-comparison/2:{args.stage}")
            count = len(case.script["utterances"])

        def call(label, kind, start, stop, proofs=None, sections=None, issues=None):
            proofs = (
                proofs
                if proofs is not None
                else {f"H{i}": r for i, r in enumerate(relevant(case, start, stop), 1)}
            )
            try:
                with metrics.phase(label):
                    record = trial.execute(root, label, kind, start, stop, proofs, sections, issues)
            finally:
                unit_id = uuid5(root, label)
                state = head(repo, journal.project, unit_id) if journal.run(unit_id) else None
                if state:
                    write(folder / (label + "-usage.json"), state["accounting"])
            write(folder / (label + ".json"), record)
            print(label, "saved", flush=True)
            return record

        if args.operation == "receipts":
            labels = ["partition"]
            partition_run = uuid5(root, "partition")
            partition = (
                journal.invocation(partition_run, "fact-review") if journal.run(partition_run) else None
            )
            if partition and partition["status"] == "completed":
                ranges = Partition.model_validate(journal.output(partition)["output"]).ranges(count)
                labels.extend(f"fragment-{start}-{stop}" for start, stop in ranges)
            labels.append("synthesis")
            units = {label: collect(journal, uuid5(root, label)) for label in ["whole", *labels]}
            write(folder / "receipts.json", units)
            write(
                folder / "accounting.json",
                dict(whole=combined([units["whole"]]), split=combined(units[label] for label in labels)),
            )
        elif args.operation == "preview":
            write(
                folder / "binding.json",
                dict(
                    **case.binding,
                    documents=len(case.inventory),
                    utterances=count,
                    retained_passages=len(case.evidence),
                    model=case.profile.model_revision_or_alias,
                ),
            )
            for kind in ("partition", "whole"):
                proofs = (
                    {}
                    if kind == "partition"
                    else {f"H{i}": r for i, r in enumerate(relevant(case, 0, count), 1)}
                )
                body, _ = trial.request(uuid5(root, kind), kind, 0, count, proofs, None)
                write(folder / (kind + ".request.json"), body)
        elif args.operation == "whole":
            call("whole", "whole", 0, count)
        else:
            record = call("partition", "partition", 0, count, {})
            if args.operation == "split":
                parts = []
                for start, stop in Partition.model_validate(record["output"]).ranges(count):
                    parts.append((start, call(f"fragment-{start}-{stop}", "fragment", start, stop)))
                sections, proofs, issues = handoff(parts)
                call("synthesis", "synthesis", 0, count, proofs, sections, issues)
    finally:
        write(folder / (args.operation + "-metrics.json"), metrics.snapshot())
        db.engine.dispose()


if __name__ == "__main__":
    main()
