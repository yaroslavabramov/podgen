import asyncio
from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.research.hosted.corpus_acquisition import CorpusAcquisition, acquire_corpus, document_url
from podgen.sources.acquisition.contracts import AcquisitionResult
from tests.hosted_research_support import MemoryJournal


def settings():
    policy = dict(kind="Policy", artifact_id=uuid4(), revision_id=uuid4())
    return CorpusAcquisition(
        profile_ref={**policy, "kind": "ProcessingProfile"},
        rights=dict(access_class="public_web", storage_policy_ref=policy, publication_policy_ref=policy),
    )


def completed(journal, urls):
    journal.put(
        "result", dict(status="completed", material=dict(reported_sources=[dict(url=url) for url in urls]))
    )


def outcome(journal, status="failed"):
    return AcquisitionResult(
        acquisition_record_ref=journal.ref("attempt"),
        sources=[],
        status=status,
        diagnostics=["saved outcome"],
    )


def test_entire_corpus_resumes_past_old_document_limit_and_deduplicates_transport_urls():
    journal, config, operation = MemoryJournal(), settings(), uuid4()
    urls = [f"https://example.org/doc/{i}" for i in range(70)]
    completed(journal, [*urls, urls[0] + "#heading", "https://EXAMPLE.org:443/doc/0", urls[1]])
    calls = []

    async def acquire(repo, project, request):
        calls.append(request.url)
        if len(calls) == 36:
            raise RuntimeError("interrupted before response")
        return outcome(journal)

    with pytest.raises(RuntimeError):
        asyncio.run(acquire_corpus(journal, operation, config, acquire=acquire))
    result = asyncio.run(acquire_corpus(journal, operation, config, acquire=acquire))
    assert len(calls) == 71
    assert calls.count(urls[0]) == calls.count(urls[-1]) == 1
    assert calls.count(urls[35]) == 2
    assert len(result["sources"]) == 72
    assert all(row["acquisition"]["status"] == "failed" for row in result["sources"])
    assert result == asyncio.run(acquire_corpus(journal, operation, config, acquire=acquire))
    assert len(calls) == 71


def test_reuse_prior_selections_keeps_failures_and_successes_without_http():
    journal, config, old_id = MemoryJournal(), settings(), uuid4()
    urls = ["https://example.org/a", "https://example.org/b"]
    completed(journal, urls)
    ref = journal.ref("result").model_dump(mode="json")
    journal.put(
        f"source-acquisition:{old_id}:selection",
        dict(selection=config.model_dump(mode="json"), report_ref=ref),
    )
    journal.put(
        f"source-acquisition:{old_id}:result",
        dict(
            sources=[
                dict(
                    reported_url=url,
                    report_ref=ref,
                    acquisition=outcome(journal, status).model_dump(mode="json"),
                )
                for url, status in zip(urls, ["available", "failed"], strict=True)
            ]
        ),
    )
    config = config.model_copy(update={"reuse_selection_ids": [old_id]})

    async def no_network(*args):
        pytest.fail("reusable documents must not be fetched again")

    result = asyncio.run(acquire_corpus(journal, uuid4(), config, acquire=no_network))
    assert [row["acquisition"]["status"] for row in result["sources"]] == ["available", "failed"]
    with pytest.raises(ContractError, match="policy_mismatch"):
        asyncio.run(
            acquire_corpus(
                journal,
                uuid4(),
                settings().model_copy(update={"reuse_selection_ids": [old_id]}),
                acquire=no_network,
            )
        )


def test_url_dedup_does_not_merge_query_or_path_variants():
    assert document_url("https://EXAMPLE.org:443/a?q=1#top") == "https://example.org/a?q=1"
    assert document_url("https://example.org/a?q=1") != document_url("https://example.org/a?q=2")
    assert document_url("https://example.org/A") != document_url("https://example.org/a")
    with pytest.raises(ContractError):
        document_url("file:///tmp/report")


def test_malformed_reported_link_records_failure_without_losing_other_documents():
    journal, config = MemoryJournal(), settings()
    completed(journal, ["https://example.org:bad/page", "https://example.org/valid"])
    calls = []

    async def acquire(repo, project, request):
        calls.append(request.url)
        return outcome(journal)

    result = asyncio.run(acquire_corpus(journal, uuid4(), config, acquire=acquire))
    assert calls == ["https://example.org/valid"]
    assert result["sources"][0]["acquisition"]["diagnostics"] == ["invalid_reported_source_url"]
    assert len(result["sources"]) == 2
