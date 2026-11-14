"""Document transition atomicity, immutable byte reuse and package compatibility."""

import asyncio
import io
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from threading import Event
from uuid import uuid4

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
from podgen.sources.acquisition.store import AcquisitionStore
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

        from podgen.domain.contracts.common import RuntimeRef
        from podgen.persistence import artifact_batch
        from podgen.persistence.archive_authorization import archive_write
        from podgen.persistence.publication_plan import ArtifactNode, PublicationBatch, RuntimeNode

        node = ArtifactNode("source", "SourceRevision", payload, producer, uuid4())
        checkpoint = RuntimeNode(
            "checkpoint",
            RuntimeRef(record_kind="acquisition_record", record_id=uuid4()),
            dict(source={"$artifact": "source"}),
        )
        batch = PublicationBatch(artifacts=(node,), runtimes=(checkpoint,), prepared=prepared)
        insert = artifact_batch.insert_rows

        def blocked(tx, table, rows, **options):
            result = insert(tx, table, rows, **options)
            if table == "artifact_revisions":
                entered.set()
                assert release.wait(10)
            return result

        monkeypatch.setattr(artifact_batch, "insert_rows", blocked)

        def operation():
            return archive_write(repo, scope[0], lambda tx: artifact_batch.publish(repo, tx, scope[0], batch))
    else:
        original = store._put

        def put(tx, key, payload):
            entered.set()
            assert release.wait(10)
            return original(tx, key, payload)

        monkeypatch.setattr(store, "_put", put)

        def operation():
            return store.put("request", request.model_dump(mode="json"))

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
    from podgen.persistence import artifact_batch

    original, rolled_back = artifact_batch.insert_rows, []

    def retry(tx, table, rows, **options):
        value = original(tx, table, rows, **options)
        final = table == "artifact_revisions" and any(row["kind"] == "SourceRepresentation" for row in rows)
        outcome = table == "runtime_records" and any(
            json.loads(row.get("journal_binding", "{}")).get("stage") == "outcome" for row in rows
        )
        if (final if checkpoint == "source" else outcome) and not rolled_back:
            rolled_back.append(True)
            raise DBAPIError("synthetic serialization retry", {}, SerializationFailure("fixture"))
        return value

    monkeypatch.setattr(artifact_batch, "insert_rows", retry)
    result = await service.acquire_source(repo, scope[0], request, fetcher=fetcher, extractors=extractor)
    assert result.status == "available" and len(fetcher.calls) == extractor.calls == 1
    assert rolled_back == [True]
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx,
                "SELECT count(*) FROM artifact_revisions WHERE project_id=:p AND kind='SourceRevision'",
                p=scope[0],
            ).scalar_one()
            == 1
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
    from podgen.persistence import artifact_batch

    original = artifact_batch.insert_rows

    def interrupt(tx, table, rows, **options):
        value = original(tx, table, rows, **options)
        final = table == "artifact_revisions" and any(row["kind"] == "SourceRepresentation" for row in rows)
        outcome = table == "runtime_records" and any(
            json.loads(row.get("journal_binding", "{}")).get("stage") == "outcome" for row in rows
        )
        if final if checkpoint == "source" else outcome:
            raise Interrupted()
        return value

    monkeypatch.setattr(artifact_batch, "insert_rows", interrupt)
    with pytest.raises(Interrupted):
        await service.acquire_source(repo, scope[0], request, fetcher=fetcher, extractors=extractor)
    store = AcquisitionStore(repo, scope[0], request.operation_id)
    assert store.get("original:original") is not None
    assert store.get("original:extracted") is not None
    assert store.get("original:outcome") is None
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx,
                "SELECT count(*) FROM artifact_revisions WHERE project_id=:p AND kind IN ('SourceRevision','SourceRepresentation')",
                p=scope[0],
            ).scalar_one()
            == 0
        )
    monkeypatch.setattr(artifact_batch, "insert_rows", original)
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
            ("revision_inserts", "INSERT INTO artifact_revisions"),
            ("version_bumps", "validity_version=validity_version+"),
            ("roles", "SELECT role FROM project_members"),
            ("artifact_frontiers", "AS access_graph"),
            ("artifact_bodies", "SELECT * FROM artifact_revisions"),
            ("dependency_body_batches", "SELECT * FROM artifact_revisions WHERE revision_id=ANY"),
        ):
            if fragment in statement:
                work[label] += 1
                phases[phase[0]][label] += 1

    from podgen.persistence import artifact_batch

    original_publish = artifact_batch.publish

    def stage(repo, tx, project, batch, **options):
        previous = phase[0]
        phase[0] = "final-document" if batch.artifacts else "receipt:" + batch.runtimes[0].journal.stage
        try:
            return original_publish(repo, tx, project, batch, **options)
        finally:
            phase[0] = previous

    monkeypatch.setattr(artifact_batch, "publish", stage)
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
    assert phases["final-document"]["revision_inserts"] == 1
    assert phases["final-document"]["version_bumps"] == 1
    assert (
        work["artifact_bodies"] == work["dependency_body_batches"] + 2
    )  # Profile read and one batch identity lookup.
    assert store_checkpoint_absent(repo, scope[0], request.operation_id)


def store_checkpoint_absent(repo, project, operation):
    store = AcquisitionStore(repo, project, operation)
    return store.get("original:source") is None and store.get("original:representation") is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_reprocess_preserves_source_identity_unless_metadata_changes(repo, scope, producer, changed):
    from podgen.sources.acquisition.contracts import ReExtractionRequest
    from podgen.sources.acquisition.reprocess import reprocess_source

    request, _ = setup(repo, scope, producer)

    class First:
        async def extract(self, *args, **kwargs):
            return ExtractedDocument(
                ARTICLE, "synthetic-metadata", "1", title="Original title", published_date="2026-01-02"
            )

    first = await service.acquire_source(repo, scope[0], request, fetcher=Fetcher(), extractors=First())
    source = first.sources[0].source_revision_ref

    class Second:
        async def extract(self, *args, **kwargs):
            return ExtractedDocument(
                ARTICLE, "synthetic-metadata", "1", title="Corrected title" if changed else "Original title"
            )

    command = ReExtractionRequest(
        operation_id=uuid4(),
        source_revision_ref=source,
        profile_ref=request.profile_ref,
        reason="Synthetic metadata recheck",
    )
    second = await reprocess_source(repo, scope[0], command, extractors=Second())
    result = repo.get(scope[0], second.sources[0].source_revision_ref)
    assert (result.ref != source) == changed
    assert result.payload["title"] == ("Corrected title" if changed else "Original title")
    assert result.payload["published_at"]["value"] == "2026-01-02"
