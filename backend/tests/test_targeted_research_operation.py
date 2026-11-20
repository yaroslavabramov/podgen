"""Whole-operation measurements on 1/6/40 discovered documents, including replay."""

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.material import report as parse_report
from podgen.research.materials.resolver import ResearchMaterialResolver
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects, Measurements
from podgen.runtime.evaluation.journal import EvaluationJournal
from tests.source_acquisition_support import document
from tests.test_research_material_resolver import fixture


@pytest.mark.parametrize("count", [1, 6, 40])
@pytest.mark.parametrize("anchored", [False, True])
def test_complete_targeted_operation(repo, scope, producer, monkeypatch, count, anchored):
    materials, _, acquire, fetcher = fixture(repo, scope, producer, monkeypatch)
    urls = [f"https://example.com/narrow-{i}" for i in range(count)]
    for url in urls:
        fetcher.documents[url] = document("Ёжик и точные часы. " * 100, url=url)
    repo.objects = MeasuredObjects(repo.objects.root)
    measured = Measurements(repo.database, repo.objects)
    resolver = ResearchMaterialResolver(EvaluationJournal(repo, scope[0]), materials, acquire=acquire)
    run_id, operation = uuid4(), uuid4()
    args = dict(question="Каковы точные слова?", reason="Проверить реплику.")
    if anchored:
        args.update(research_contract="targeted-script-context/1", anchors=[], script_context=None)
    with measured.phase("prepare"):
        pending = resolver.resolve(
            "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
        )
    child = ResearchJournal(repo, scope[0], UUID(pending.result["child_run_id"]))
    text = (
        json.dumps(
            dict(
                answer="Точная цитата.",
                limitations="",
                evidence=[dict(url=urls[0], quote="Ёжик и точные часы.", relation="Исходные слова.")],
            ),
            ensure_ascii=False,
        )
        if anchored
        else "# Answer\n\nТочная цитата.\n\n**Sources:**\n"
        + "\n".join(f"{i + 1}. [Record]({url})" for i, url in enumerate(urls))
    )
    if anchored:
        sources = (Path(__file__).parent / "fixtures/gemini_deep_research_sources.md").read_text()
        text = f"```json\n{text}\n```\n\n{sources}"
    # Same discovery list; only the narrow answer identifies the excerpt actually needed.
    material = parse_report(
        dict(
            steps=[
                dict(
                    type="model_output",
                    content=[
                        dict(
                            type="text",
                            text=text,
                            annotations=[dict(type="url_citation", url=url) for url in urls],
                        )
                    ],
                )
            ]
        )
    )
    material.pop("text")
    child.put(
        "result",
        dict(
            status="completed", report_text_blob=child.blob(text.encode(), "text/markdown"), material=material
        ),
    )
    before = len(fetcher.calls)
    with measured.phase("resolve"):
        result = resolver.resolve(
            "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
        )
    after = len(fetcher.calls)
    with measured.phase("replay"):
        again = resolver.resolve(
            "request_research", args, operation_id=operation, run_id=run_id, guard=lambda: None
        )
    assert again == result
    assert len(fetcher.calls) == after
    assert after - before == (1 if anchored else count)
    metrics = dict(
        measured.snapshot(),
        discovered_documents=count,
        variant="anchored" if anchored else "legacy",
        fetch_calls=after - before,
        paid_provider_calls=0,
    )
    destination = os.environ.get("PODGEN_TARGETED_MEASUREMENTS")
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{metrics['variant']}-{count}.json").write_text(json.dumps(metrics, indent=2) + "\n")
