"""Document transition atomicity, immutable byte reuse and package compatibility."""

import asyncio
import io
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event
from uuid import uuid4, uuid5

import pytest
from psycopg.errors import SerializationFailure
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError

from podgen.domain.errors import AccessDenied
from podgen.domain.packages import ArtifactPackage
from podgen.persistence.artifact_preparation import prepare
from podgen.persistence.database import query
from podgen.persistence.packages import PackageImporter
from podgen.sources.acquisition import service
from podgen.sources.acquisition.store import AcquisitionStore, Publication
from podgen.sources.extraction.contracts import ExtractedDocument
from tests.source_acquisition_support import ARTICLE, Fetcher, setup
from tests.test_artifact_publication import transaction_guard
from tests.test_packages import package


class Interrupted(BaseException):
    pass


class Extractor:
    def __init__(self):
        self.calls = 0

    async def extract(self, document, profile, *, role):
        self.calls += 1
        return ExtractedDocument(ARTICLE, "saved-text", "1", title="Saved title", coverage="complete")


@pytest.mark.parametrize("boundary", ["publication", "put"])
@pytest.mark.parametrize("change", ["delete", "demote"])
def test_document_owner_serializes_acl_changes(repo, scope, producer, monkeypatch, boundary, change):
    request, _ = setup(repo, scope, producer)
    store = AcquisitionStore(repo, scope[0], request.operation_id)
    entered, release, changing = Event(), Event(), Event()
    if boundary == "publication":
        acquired = asyncio.run(
            service.acquire_source(repo, scope[0], request, fetcher=Fetcher(), extractors=Extractor())
        )
        source = repo.get(scope[0], acquired.sources[0].source_revision_ref)
        payload = source.payload | dict(source_id=str(uuid4()))
        prepared = prepare(repo, scope[0], "SourceRevision", payload)

        def publish(publication):
            descriptor = publication.create("new:source", "SourceRevision", payload)
            entered.set()
            assert release.wait(10)
            publication.put("new:source", dict(source_revision_ref=descriptor.ref.model_dump(mode="json")))
            return descriptor

        def operation():
            return store.publication(publish, prepared=prepared)
    else:
        original = store._put

        def put(tx, key, payload):
            entered.set()
            assert release.wait(10)
            return original(tx, key, payload)

        monkeypatch.setattr(store, "_put", put)

        def operation():
            return store.put("new:checkpoint", dict(complete=True))

    def modify_role():
        with repo.database.transaction() as tx:
            changing.set()
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a"
                if change == "delete"
                else "UPDATE project_members SET role='viewer' WHERE project_id=:p AND actor_id=:a",
                p=scope[0],
                a=repo.actor_id,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        publishing = pool.submit(operation)
        try:
            assert entered.wait(10)
            revoking = pool.submit(modify_role)
            assert changing.wait(10)
            with pytest.raises(FutureTimeout):
                revoking.result(timeout=0.15)
            assert not publishing.done()
        finally:
            release.set()
        assert publishing.result(timeout=10) is not None
        revoking.result(timeout=10)
    with pytest.raises(AccessDenied):
        store.put("after:withdrawal", dict(complete=True))


@pytest.mark.parametrize("checkpoint", ["source", "outcome"])
@pytest.mark.asyncio
async def test_serialization_retry_repeats_only_rolled_back_document_sql(
    repo, scope, producer, monkeypatch, checkpoint
):
    request, _ = setup(repo, scope, producer)
    fetcher, extractor = Fetcher(), Extractor()
    original = Publication.put
    rolled_back = []

    def retry(self, key, payload):
        value = original(self, key, payload)
        if key == "original:" + checkpoint and not rolled_back:
            rolled_back.append(payload)
            raise DBAPIError("synthetic serialization retry", {}, SerializationFailure("fixture"))
        return value

    monkeypatch.setattr(Publication, "put", retry)
    result = await service.acquire_source(repo, scope[0], request, fetcher=fetcher, extractors=extractor)
    assert result.status == "available" and len(fetcher.calls) == extractor.calls == 1
    assert len(rolled_back) == 1
    missing = (
        rolled_back[0]["source_revision_ref"]
        if checkpoint == "source"
        else rolled_back[0]["source"]["source_revision_ref"]
    )
    with repo.database.read_transaction() as tx:
        assert not list(
            query(
                tx,
                "SELECT revision_id FROM artifact_revisions WHERE project_id=:p AND revision_id=CAST(:id AS uuid)",
                p=scope[0],
                id=missing["revision_id"],
            ).scalars()
        )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(service, "_check_ssrf", lambda _: None)


@pytest.mark.parametrize("checkpoint", ["source", "outcome"])
@pytest.mark.asyncio
async def test_artifacts_and_document_checkpoint_commit_together(
    repo, scope, producer, monkeypatch, checkpoint
):
    request, _ = setup(repo, scope, producer)
    fetcher, extractor = Fetcher(), Extractor()
    original = Publication.put

    def interrupt(self, key, payload):
        if key == "original:" + checkpoint:
            raise Interrupted()
        return original(self, key, payload)

    monkeypatch.setattr(Publication, "put", interrupt)
    with pytest.raises(Interrupted):
        await service.acquire_source(repo, scope[0], request, fetcher=fetcher, extractors=extractor)
    store = AcquisitionStore(repo, scope[0], request.operation_id)
    assert store.get("original:original") is not None
    assert store.get("original:" + checkpoint) is None
    rolled_back = (
        ["source"] if checkpoint == "source" else ["metadata-source", "extractor-profile", "representation"]
    )
    with repo.database.read_transaction() as tx:
        assert not list(
            query(
                tx,
                "SELECT revision_id FROM artifact_revisions WHERE project_id=:p AND operation_id=ANY(:ids)",
                p=scope[0],
                ids=[uuid5(request.operation_id, "original:" + key) for key in rolled_back],
            ).scalars()
        )
    if checkpoint == "outcome":
        assert store.get("original:extracted") is not None
    monkeypatch.setattr(Publication, "put", original)
    result = await service.acquire_source(repo, scope[0], request, fetcher=fetcher, extractors=extractor)
    assert result.status == "available" and len(fetcher.calls) == extractor.calls == 1
    assert (
        await service.acquire_source(repo, scope[0], request, fetcher=fetcher, extractors=extractor) == result
    )


@pytest.mark.asyncio
async def test_source_pipeline_storage_never_holds_a_transaction(repo, scope, producer, monkeypatch, request):
    finalize = request.addfinalizer
    request, _ = setup(repo, scope, producer)
    transaction_guard(repo, monkeypatch, finalize)
    original = repo.objects.read
    reads = []

    def read(project, blob):
        reads.append(blob.sha256)
        return original(project, blob)

    monkeypatch.setattr(repo.objects, "read", read)
    result = await service.acquire_source(repo, scope[0], request, fetcher=Fetcher(), extractors=Extractor())
    assert result.status == "available"
    # Newly fetched bytes and completed extraction are passed to publication,
    # not fetched again from storage for SourceRevision/SourceRepresentation.
    assert reads == []


@pytest.mark.asyncio
async def test_finished_document_transition_work(repo, scope, producer, monkeypatch, record_property):
    from podgen.persistence.repository import Repository

    request, _ = setup(repo, scope, producer)
    production = Repository(repo.database, repo.objects, repo.actor_id)
    phase = ["acquire"]
    work, phases = Counter(), defaultdict(Counter)

    def count(conn, cursor, statement, parameters, context, many):
        work["sql"] += 1
        phases[phase[0]]["sql"] += 1
        for label, fragment in (
            ("roles", "SELECT role FROM project_members"),
            ("artifact_frontiers", "AS access_graph"),
            ("artifact_bodies", "SELECT * FROM artifact_revisions"),
        ):
            if fragment in statement:
                work[label] += 1
                phases[phase[0]][label] += 1

    for name in ("create", "put"):
        original = getattr(Publication, name)

        def stage(self, key, *args, _name=name, _original=original, **kwargs):
            previous, phase[0] = phase[0], _name + ":" + key
            try:
                return _original(self, key, *args, **kwargs)
            finally:
                phase[0] = previous

        monkeypatch.setattr(Publication, name, stage)
    original_read, original_iter = repo.objects.read, repo.objects.iter_read

    def read(project, ref):
        body = original_read(project, ref)
        work["object_reads"] += 1
        work["object_bytes_read"] += len(body)
        return body

    def chunks(project, ref, **kwargs):
        work["object_streams"] += 1
        for chunk in original_iter(project, ref, **kwargs):
            work["object_bytes_read"] += len(chunk)
            yield chunk

    monkeypatch.setattr(repo.objects, "read", read)
    monkeypatch.setattr(repo.objects, "iter_read", chunks)
    event.listen(repo.database.engine, "before_cursor_execute", count)
    fetcher, extractor = Fetcher(), Extractor()
    try:
        result = await service.acquire_source(
            production, scope[0], request, fetcher=fetcher, extractors=extractor
        )
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", count)
    record_property(
        "finished_document_transition",
        dict(total=dict(work), phases={key: dict(value) for key, value in phases.items()}),
    )
    assert result.status == "available" and len(fetcher.calls) == extractor.calls == 1
    assert work["object_reads"] == 0 and work["object_streams"] == 2


async def representation(repo, scope, producer):
    request, _ = setup(repo, scope, producer)
    result = await service.acquire_source(repo, scope[0], request, fetcher=Fetcher(), extractors=Extractor())
    return repo.get(scope[0], result.sources[0].representation_refs[0])


@pytest.mark.asyncio
async def test_package_binary_representation_with_direct_blob_needs_no_text_decode(
    repo, scope, producer, tmp_path, monkeypatch, request
):
    existing = await representation(repo, scope, producer)
    binary = repo.save_blob(scope[0], io.BytesIO(b"\xff\x00\x80PDF image bytes"), "application/pdf")
    raw = package()
    raw["external_refs"] = {str(i): ref.model_dump(mode="json") for i, ref in enumerate(existing.input_refs)}
    raw["artifacts"] = [
        dict(
            key="binary",
            kind="SourceRepresentation",
            payload=existing.payload
            | dict(content_blob=binary.model_dump(mode="json"), canonical_text_blob=None),
            input_keys=[],
            audience="project",
            identity_of=None,
        )
    ]
    transaction_guard(repo, monkeypatch, request.addfinalizer)
    result = PackageImporter(repo).import_package(
        *scope, ArtifactPackage.model_validate(raw), tmp_path, producer=producer
    )
    assert result[0].payload["content_blob"] == binary.model_dump(mode="json")


@pytest.mark.asyncio
async def test_package_text_evidence_can_use_external_representation_without_package_blobs(
    repo, scope, producer, tmp_path, monkeypatch, request
):
    existing = await representation(repo, scope, producer)
    source = existing.payload["source_revision_ref"]
    selector = dict(
        kind="text", block_ids=[], char_start=0, char_end=len(ARTICLE), exact=ARTICLE, prefix="", suffix=""
    )
    raw = package()
    raw["external_refs"] = dict(source=source, representation=existing.ref.model_dump(mode="json"))
    raw["artifacts"] = [
        dict(
            key="evidence",
            kind="EvidenceSpan",
            payload=dict(
                source_revision_ref=source,
                representation_ref={"$artifact": "representation"},
                selector=selector,
                context_selector=selector,
                exact_text=ARTICLE,
                extraction_warnings=[],
                transformation="none",
            ),
            input_keys=[],
            audience="project",
            identity_of=None,
        )
    ]
    transaction_guard(repo, monkeypatch, request.addfinalizer)
    result = PackageImporter(repo).import_package(
        *scope, ArtifactPackage.model_validate(raw), tmp_path, producer=producer
    )
    assert result[0].payload["exact_text"] == ARTICLE
