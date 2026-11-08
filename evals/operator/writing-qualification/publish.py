"""Export verbatim spoken text, editorial decisions, and itemized costs for a single trial."""

import argparse
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path

from matrix import ROOT, read, save

sys.path.insert(0, str(ROOT / "evals/operator/author-openings"))
from results import estimate


def write_episode(script_file, destination):
    turns = read(script_file)["output"]["utterances"]
    solo = len({line["speaker"] for line in turns}) == 1
    text = "\n\n".join(
        row["text"] if solo else f"**{row['speaker']}**\n\n{row['text']}"
        for row in turns
    )
    destination.write_text(text + "\n")


def receipts(run, seen, rates):
    for request in sorted((run / "archive").rglob("request.json")):
        for file in request.parent.glob("*-response_blob.json"):
            if "count_" in file.name:
                continue
            physical = file.with_name(
                file.name.replace("-response_blob", "-wire_response_blob")
            )
            actual = physical if physical.exists() else file
            response = read(actual)
            if not response.get("usage"):
                continue
            identifier = response["id"]
            if identifier in seen:
                if seen[identifier] != response["usage"]:
                    raise ValueError("Conflicting usage receipts for " + identifier)
                continue
            seen[identifier] = response["usage"]
            provider = "anthropic" if physical.exists() else "openai"
            cost = estimate(response, provider, rates)
            if cost is None:
                raise ValueError(f"Missing tariff for {response['model']}")
            yield dict(
                session=run.name,
                stage=str(request.parent.relative_to(run / "archive")),
                provider=provider,
                response_id=identifier,
                model=response["model"],
                usage=response["usage"],
                usd=cost,
                receipt_sha256=hashlib.sha256(actual.read_bytes()).hexdigest(),
            )


def publish(trial, destination):
    summary = read(trial / "terminal.json")
    destination.mkdir(parents=True, exist_ok=True)
    pricing = ROOT / "evals/reports/2026-11-08-author-openings/pricing.json"
    rates = read(pricing)["rates_per_million_usd"]
    calls, seen, observations = [], {}, []
    for number, identifier in enumerate(summary["sessions"], 1):
        run = trial / identifier
        export = read(run / "run.json")
        calls.extend(receipts(run, seen, rates))
        out = destination / f"session-{number}"
        out.mkdir(exist_ok=True)
        observations.append(
            dict(
                id=identifier,
                status=export["status"],
                reason=export["failure_code"],
                review_status=export.get("review_status"),
                unknown_usage=export["unknown_usage_invocations"],
            )
        )
        for stage in export["invocations"]:
            returned = run / (stage + ".json")
            if not returned.exists():
                continue
            row = read(returned)
            save(
                out / (stage + ".json"), row.get("wire_output", row.get("output", row))
            )
            if (run / (stage + ".txt")).exists():
                write_episode(returned, out / (stage + ".md"))
                if not (destination / "first-draft.md").exists() and stage == "draft":
                    write_episode(returned, destination / "first-draft.md")
        for item in ("planning-clearance", "review-outcome"):
            original = run / (item + ".json")
            if original.exists():
                save(out / original.name, read(original))
        final = run / "candidate-final.json"
        if final.exists():
            write_episode(final, destination / "last-script.md")
    cost = sum((Decimal(row["usd"]) for row in calls), Decimal(0))
    save(
        destination / "receipts.json",
        dict(
            estimated_usd=str(cost),
            rates_per_million_usd=rates,
            publisher_links=read(pricing)["sources"],
            calls=calls,
        ),
    )
    save(destination / "result.json", dict(**summary, observations=observations))
    print(f"{len(calls)} generations, estimated USD {cost}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trial", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    publish(args.trial, args.destination)
