"""Native run compilers and immutable previews advertise the pinned retrieval contract only."""

import json
from copy import deepcopy

import httpx
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.source_tool_contract import from_manifest
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing.source_tools import definitions

from tests.source_excerpt_cases import data_and_profile, release
from tests.test_source_tool_runtime import final, setup


def test_current_preview_and_legacy_replay_differ_only_in_pinned_tool_declarations(monkeypatch):
    materials, plan, *_rest, profile = data_and_profile()
    store, _, command, manifest = release(monkeypatch, materials, profile)
    assert from_manifest(manifest)["version"] == "frozen-corpus-tools/4"
    old = deepcopy(manifest)
    old["source_tool_contract"].update(version="frozen-corpus-tools/3", definitions=definitions())
    old["source_tool_contract"].pop("retrieval")
    from podgen.domain.contracts.common import ArtifactRef

    instructions = store.get(
        command.project_id, ArtifactRef.model_validate(manifest["prompt_refs"]["draft"])
    ).payload["template"]
    current = preview_request(
        command.run_id,
        "draft",
        profile("draft"),
        materials,
        snapshot=PreviewSnapshot(manifest, instructions),
        approved_plan=plan,
    )
    frozen = preview_request(
        command.run_id,
        "draft",
        profile("draft"),
        materials,
        snapshot=PreviewSnapshot(old, instructions),
        approved_plan=plan,
    )
    assert current["tools"] != frozen["tools"]
    current["tools"] = frozen["tools"]
    assert current == frozen


def test_search_diagnostics_are_archived_but_never_sent_to_model(monkeypatch):
    journal, profile, limits, corpus = setup(monkeypatch, turns=2)
    requests = []

    def execute(name, args, **options):
        corpus.last_search_diagnostics = dict(
            retrieval_profile="fixture", ranked=[dict(secret_diagnostic_score=0.125)]
        )
        return dict(hits=[], sources={}, next_offset=None, total_hits=0)

    corpus.execute = execute

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(body)
        assert "secret_diagnostic_score" not in json.dumps(body)
        call = dict(
            type="function_call",
            call_id="search1",
            name="search_sources",
            status="completed",
            arguments=json.dumps(dict(query="why", within=None, offset=0)),
        )
        return httpx.Response(
            200,
            json=dict(
                id=f"resp_{len(requests)}",
                model=body["model"],
                metadata=body["metadata"],
                status="completed",
                output=[call] if len(requests) == 1 else [final()],
                usage=dict(input_tokens=100, output_tokens=10),
            ),
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("test-secret", http_client=client), source_corpus=corpus
        )
        assert (
            gateway.invoke(
                journal.root, profile, limits=limits, totals=dict(input=0, output=0), guard=lambda: None
            )
            == '{"answer":"done"}'
        )
    assert any(event["event_kind"] == "source_search_diagnostics" for event in journal.events(journal.root))
