"""One real PostgreSQL search and native model loop, with no paid calls."""

import json
from copy import deepcopy
from uuid import uuid4

import httpx2
import pytest

from podgen.domain.errors import AccessDenied, Conflict
from podgen.retrieval import RetrievalUnavailable
from podgen.retrieval.config import streaming_profile
from podgen.runtime.evaluation.journal import EvaluationJournal
from podgen.runtime.evaluation.source_tool_contract import bind, from_manifest
from podgen.runtime.evaluation.streaming_search_index import StreamingSearchIndex
from podgen.runtime.models.gateway import ModelGateway
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.runtime.models.research_tools import ResearchTools
from podgen.runtime.models.search_argument_errors import KEY, VERSION
from podgen.writing.source_tools import SourceCorpus
from tests.source_search_support import TinyModels
from tests.test_research_material_resolver import fixture
from tests.test_research_runtime import answer, invoke, research_setup
from tests.test_source_tool_runtime import final


def test_foreign_version_returns_tool_error_and_model_corrects_next_call(repo, scope, producer, monkeypatch):
    materials, _, _, _ = fixture(repo, scope, producer, monkeypatch)
    monkeypatch.setattr("podgen.retrieval.streaming.models", lambda _: TinyModels())
    index = StreamingSearchIndex(
        EvaluationJournal(repo, scope[0]), materials.shared_ref, streaming_profile(), run_id=uuid4()
    )
    corpus = SourceCorpus(materials, retrieval=streaming_profile(), index=index)
    first_args = dict(query="mechanism", within=None, offset=0, result_version=None, aliases_of=None)
    foreign = corpus.execute("search_sources", first_args, read_id="E0")["result_version"]
    args = dict(first_args, query="chronology", result_version=foreign, offset=6)
    journal, profile, limits, _ = research_setup(monkeypatch)
    journal.root["stage"] = "fact-review"
    contract = journal.snapshot["source_tool_contract"]
    assert from_manifest(journal.snapshot)[KEY] == VERSION
    historical = deepcopy(journal.snapshot)
    historical["source_tool_contract"].pop(KEY)
    assert KEY not in from_manifest(historical)
    body = bind(journal.request(journal.root), "fact-review", profile, contract)
    description = next(tool["description"] for tool in body["tools"] if tool.get("name") == "search_sources")
    assert "keep query and within exactly unchanged" in description
    assert "A rephrased query" in description
    journal.root["request_record_id"] = journal.store_request(body)
    delivered = []

    def call(arguments, turn):
        return dict(
            type="function_call",
            call_id=f"search_{turn}",
            name="search_sources",
            status="completed",
            arguments=json.dumps(arguments),
        )

    def handler(request):
        body = json.loads(request.content)
        if request.url.path.endswith("/input_tokens"):
            return httpx2.Response(200, json=dict(input_tokens=100))
        turn = int(body["metadata"]["podgen_source_turn"])
        if turn == 0:
            return answer(body, turn, [call(args, turn)])
        outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
        result = json.loads(outputs[-1]["output"][0]["text"])
        delivered.append(result)
        if turn == 1:
            assert result["error"] == "source_search_result_version_mismatch"
            assert "hits" not in result and "result_version" not in result
            assert "result_version=null" in result["next_action"]
            assert result["retry_arguments"] == dict(args, result_version=None, offset=0)
            return answer(body, turn, [call(result["retry_arguments"], turn)])
        assert result["result_version"] != foreign and result["hits"]
        return answer(body, turn, [final()])

    with httpx2.Client(transport=httpx2.MockTransport(handler)) as client:
        gateway = ModelGateway(
            journal, lambda _: OpenAIProvider("local", http_client=client), source_corpus=corpus
        )
        assert invoke(gateway, journal.root, profile, limits) == '{"answer":"done"}'
    assert len(delivered) == 2
    # Historical calls still stop on the same client error. Access/storage errors
    # are never translated, even with the new policy enabled.
    old = ResearchTools(journal, journal.root, corpus, None, lambda: None)
    with pytest.raises(Conflict, match="source_search_result_version_mismatch"):
        old.execute("search_sources", args, read_id="E3")
    protected = ResearchTools(
        journal, journal.root, corpus, None, lambda: None, search_argument_policy=VERSION
    )
    for failure in (
        AccessDenied(),
        RetrievalUnavailable("source_search_index_unavailable"),
        Conflict("source_search_result_binding_changed"),
    ):

        def fail(*a, _failure=failure, **kw):
            raise _failure

        with monkeypatch.context() as patch:
            patch.setattr(corpus, "execute", fail)
            with pytest.raises(type(failure)) as raised:
                protected.execute("search_sources", args, read_id="E4")
            assert raised.value is failure
