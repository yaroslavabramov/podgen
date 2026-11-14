"""Decode old immutable checkpoints without redoing completed external work."""

from dataclasses import asdict
from io import BytesIO
from uuid import uuid5

import pytest

from podgen.domain.contracts.common import CodeProducer
from podgen.sources.acquisition import service
from podgen.sources.acquisition.source_payload import received
from podgen.sources.acquisition.store import AcquisitionStore
from podgen.sources.extraction.contracts import ExtractedDocument
from tests.source_acquisition_support import ARTICLE, document, setup


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_source", [False, True])
async def test_legacy_received_and_extracted_checkpoints_resume_through_common_publisher(
    repo, scope, producer, monkeypatch, saved_source
):
    request, _ = setup(repo, scope, producer)
    monkeypatch.setattr(service, "_check_ssrf", lambda _: None)
    store = AcquisitionStore(repo, scope[0], request.operation_id)
    fetched = document()
    raw = repo.save_blob(scope[0], BytesIO(fetched.body), "text/plain")
    text = repo.save_blob(scope[0], BytesIO(ARTICLE.encode()), "text/plain; charset=utf-8")
    extracted = ExtractedDocument(ARTICLE, "legacy-synthetic", "1", title="Retained extraction title")
    original = dict(
        contract="source-received-original/1",
        request_ref=store.ref("request").model_dump(mode="json"),
        role="original",
        original_blob=raw.model_dump(mode="json"),
        **{key: value for key, value in asdict(fetched).items() if key != "body"},
    )
    for key, payload in (
        ("request", request.model_dump(mode="json")),
        ("original:original", original),
        (
            "original:extracted",
            dict(
                text_blob=text.model_dump(mode="json"),
                metadata={key: value for key, value in asdict(extracted).items() if key != "text"},
            ),
        ),
    ):
        repo.append_runtime(scope[0], store.ref(key), payload)
    old = None
    if saved_source:
        operation = uuid5(request.operation_id, "original:source")
        old = repo.create(
            scope[0],
            "SourceRevision",
            received(request, "original", fetched, raw, store.ref("original:original")),
            producer=CodeProducer(
                kind="code",
                component="source-acquisition",
                build_id="saved-source-acquisition/1",
                operation_id=operation,
            ),
            operation_id=operation,
        )
        repo.append_runtime(
            scope[0], store.ref("original:source"), dict(source_revision_ref=old.ref.model_dump(mode="json"))
        )

    class Never:
        async def fetch(self, *args, **kwargs):
            pytest.fail("legacy original must not be fetched again")

        async def extract(self, *args, **kwargs):
            pytest.fail("legacy extraction must not be repeated")

    result = await service.acquire_source(repo, scope[0], request, fetcher=Never(), extractors=Never())
    assert result.status == "available"
    current = repo.get(scope[0], result.sources[0].source_revision_ref)
    assert current.payload["title"] == extracted.title
    if old is not None:
        assert repo.get(scope[0], old.ref) == old
    assert (
        await service.acquire_source(repo, scope[0], request, fetcher=Never(), extractors=Never()) == result
    )
