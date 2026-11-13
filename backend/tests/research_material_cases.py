"""Saved hosted output and retained source fixture; all transport stays local."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

from podgen.research.hosted.journal import ResearchJournal
from podgen.research.materials.admission import prepare_materials
from podgen.sources.acquisition import service
from tests.source_acquisition_support import ARTICLE, Fetcher, setup

REPORT = (
    "# Complete research\n\n"
    + "Связное объяснение ёжика. " * 300
    + "[cite: 11]\n\n## Consequences\n\nThe connected conclusion. [cite: 2]\n\n**Sources:**\n2. [Missing source](https://example.org/missing)\n11. [Retained source](https://example.com/article)\n"
)


def saved(repo, scope, producer, monkeypatch, *, fallback=False, unavailable=False):
    project, _ = scope
    monkeypatch.setattr(service, "_check_ssrf", lambda url: None)
    request, _ = setup(repo, scope, producer)
    acquisition = asyncio.run(service.acquire_source(repo, project, request, fetcher=Fetcher()))
    value = acquisition.model_dump(mode="json")
    if fallback:
        value["sources"][0]["role"] = "jina"
    run, selection = uuid4(), uuid4()
    journal = ResearchJournal(repo, project, run)
    journal.put(
        "manifest",
        dict(
            config=dict(
                assignment=dict(
                    user_request="Full original research commission",
                    research_focus=["Not mandatory for writing"],
                )
            )
        ),
    )
    journal.put(
        "result",
        dict(
            status="completed",
            report_text_blob=journal.blob(REPORT.encode(), "text/markdown"),
            material=dict(
                reported_sources=[dict(url=request.url, title="native annotation", start=99999, end=100001)]
            ),
        ),
    )
    journal.put(
        f"source-acquisition:{selection}:result",
        dict(
            sources=[
                dict(
                    report_ref=journal.ref("result").model_dump(mode="json"),
                    reported_url=request.url,
                    acquisition=value,
                )
            ]
        ),
    )
    root = Path(__file__).resolve().parents[2]
    assignment = json.loads((root / "evals/editorial-inputs/v1/snow-pump/shared.json").read_text())[
        "assignment"
    ]
    assignment["key"] = "hosted-case"
    config = dict(
        operation_id=str(uuid4()),
        research_run_id=str(run),
        source_selection_ids=[str(selection)],
        assignment=assignment,
        source_assessments=[
            dict(
                report_number=11,
                status="unavailable",
                observation="Access withdrawn from the selected representation.",
            )
        ]
        if unavailable
        else [],
    )
    ref = prepare_materials(repo, project, config)
    return ref, config, acquisition, ARTICLE
