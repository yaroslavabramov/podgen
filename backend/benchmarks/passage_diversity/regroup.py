"""Grouping-only qualification of saved full rankings; no models or index writes."""

import argparse
import json
import time
from pathlib import Path
from uuid import UUID

from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.database import Database
from podgen.persistence.objects import FileObjectStore
from podgen.persistence.repository import Repository
from podgen.persistence.source_reading import read_sources
from podgen.research.materials.loading import load_materials
from podgen.retrieval.config import PASSAGE_POLICY
from podgen.retrieval.diversity import group, selector
from podgen.retrieval.preview import page
from podgen.writing.source_tools import SourceCorpus


def identity(row):
    return tuple(row[k] for k in ("source_id", "start", "end"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("state", "review", "out", "shared-ref"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    state = json.loads(args.state.read_text())
    repo = Repository(
        Database(state["database_url"]),
        FileObjectStore(Path(state["objects"])),
        UUID(state["actor"]),
        purpose="generation",
    )
    materials = load_materials(
        repo,
        UUID(state["project"]),
        RuntimeRef.model_validate(json.loads(args.shared_ref.read_text())),
        case_id="marine-longitude",
    )
    documents = SourceCorpus(materials).documents
    cases = [
        r
        for r in json.loads(args.review.read_text())["events"]
        if r["event_kind"] == "source_search_diagnostics"
    ]
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.monotonic()
    for number, case in enumerate(cases, 1):
        data = case["data"]
        rows = [dict(r, aliases=[selector(r)]) for r in data["ranked"]]
        policy = dict(data["retrieval_profile"], retrieval_policy=PASSAGE_POLICY)
        ranked = group(rows, documents)
        answer = page(ranked, documents, data["query"], 0, policy)
        old = {identity(r) for r in rows[:6]}
        owners = {identity(a): i for i, r in enumerate(ranked) for a in r["aliases"]}
        freed = len(old) - len({owners[s] for s in old})
        newly = [r for r in ranked[:6] if not any(identity(a) in old for a in r["aliases"])]
        with read_sources(documents.values()):

            def original(row):
                return dict(selector(row), text=documents[row["source_id"]].text[row["start"] : row["end"]])

            groups = []
            for row in ranked:
                assert all(identity(a) in {identity(r) for r in rows} for a in row["aliases"])
                for variant in row.get("variants", []):
                    for version in variant["versions"]:
                        assert original(version)["text"] == version["text"]
                        context = dict(version["context"], source_id=version["source_id"])
                        assert original(context)["text"] == context["text"]
                if len(row["aliases"]) > 1 and row["rank"] <= 6:
                    groups.append(
                        dict(row, text=original(row)["text"], originals=[original(a) for a in row["aliases"]])
                    )
            result = dict(
                number=number,
                query=data["query"],
                input_rows=len(rows),
                output_rows=len(ranked),
                freed_top6_slots=freed,
                before_top6=[original(r) for r in rows[:6]],
                after_ranked_top6=ranked[:6],
                after_page=answer,
                merged_groups=groups,
                newly_visible_passages=[original(r) for r in newly],
            )
        results.append(result)
        (args.out / f"query-{number:02}.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        )
    summary = dict(
        qualification="grouping_only_on_saved_full_rankings",
        queries=len(results),
        inference_calls=0,
        index_writes=0,
        seconds=time.monotonic() - started,
        freed_top6_slots=sum(r["freed_top6_slots"] for r in results),
        changed_queries=[r["number"] for r in results if r["freed_top6_slots"]],
        newly_visible_passages=sum(len(r["newly_visible_passages"]) for r in results),
        limitation="Does not requalify SQL candidate selection, inference, or streaming result persistence.",
    )
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
