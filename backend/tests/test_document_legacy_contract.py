"""Legacy/1 product history remains readable and unchanged across whole-document resume."""

from dataclasses import asdict
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid5

import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import CodeProducer
from podgen.domain.contracts.evidence import SourceRevision
from podgen.persistence.database import query
from podgen.persistence.repository import Repository
from podgen.sources.acquisition import service
from podgen.sources.acquisition.store import AcquisitionStore
from podgen.sources.extraction.contracts import ExtractedDocument
from tests.source_acquisition_support import ARTICLE, document, setup


def seed(repo, scope, producer, stage):
    request, profile = setup(repo, scope, producer)
    project = scope[0]
    store = AcquisitionStore(repo, project, request.operation_id)
    keys = []

    def checkpoint(key, payload):
        # Ordinary legacy publication, deliberately bypassing AcquisitionStore.put(v2).
        repo.append_runtime(project, store.ref(key), payload)
        keys.append(key)

    def old_producer(key):
        return CodeProducer(
            kind="code",
            component="source-acquisition",
            build_id="saved-source-acquisition/1",
            operation_id=uuid5(request.operation_id, key),
        )

    checkpoint("request", request.model_dump(mode="json"))
    fetched = document()
    raw = repo.save_blob(project, BytesIO(fetched.body), "text/plain")
    checkpoint(
        "original:original",
        dict(
            contract="source-received-original/1",
            role="original",
            request_ref=store.ref("request").model_dump(mode="json"),
            original_blob=raw.model_dump(mode="json"),
            **{key: value for key, value in asdict(fetched).items() if key != "body"},
        ),
    )
    source = repo.create(
        project,
        "SourceRevision",
        SourceRevision(
            source_id=request.source_id,
            original_blob=raw,
            requested_uri=fetched.requested_url,
            resolved_uri=fetched.resolved_url,
            redirect_chain=[],
            acquired_at=fetched.acquired_at,
            response_metadata=dict(status=200, content_type="text/plain"),
            creators=[],
            language=request.language,
            acquisition_kind="http",
            acquisition_record_ref=store.ref("original:original"),
            rights=request.rights,
            source_metadata_evidence=[dict(reported_url=request.url, acquisition_role="original")],
            limitations=[],
        ).model_dump(mode="json"),
        producer=old_producer("original:source"),
        operation_id=uuid5(request.operation_id, "original:source"),
    )
    checkpoint("original:source", dict(source_revision_ref=source.ref.model_dump(mode="json")))
    extraction = ExtractedDocument(
        ARTICLE,
        "synthetic-legacy-extractor",
        "test-only/1",
        title="SYNTHETIC retained legacy title" if stage == "metadata_extracted" else None,
        published_date="2025-01-02" if stage == "metadata_extracted" else None,
    )
    representation = None
    if stage != "source":
        text = repo.save_blob(project, BytesIO(ARTICLE.encode()), "text/plain; charset=utf-8")
        checkpoint(
            "original:extracted",
            dict(
                text_blob=text.model_dump(mode="json"),
                metadata={key: value for key, value in asdict(extraction).items() if key != "text"},
            ),
        )
    if stage in ("representation", "completed"):
        configuration = dict(acquisition=profile.model_dump(mode="json"), extractor=extraction.name)
        extractor_profile = repo.create(
            project,
            "ProcessingProfile",
            dict(
                profile_kind="extraction",
                implementation=extraction.name,
                version=extraction.version,
                configuration=configuration,
                input_profile_refs=[request.profile_ref.model_dump(mode="json")],
            ),
            producer=old_producer("original:extractor-profile"),
            operation_id=uuid5(request.operation_id, "original:extractor-profile"),
        )
        representation = repo.create(
            project,
            "SourceRepresentation",
            dict(
                source_revision_ref=source.ref.model_dump(mode="json"),
                representation_kind="html_text",
                content_blob=text.model_dump(mode="json"),
                canonical_text_blob=text.model_dump(mode="json"),
                extractor=dict(
                    profile_ref=extractor_profile.ref.model_dump(mode="json"),
                    name=extraction.name,
                    version=extraction.version,
                    configuration_hash=digest(configuration),
                ),
                normalization_version="preserve-extractor-text/1",
                blocks=[],
                extraction_report=dict(
                    coverage="unknown", missing_regions=[], reading_order_issues=[], warnings=[]
                ),
                language=request.language,
                is_transformation=False,
            ),
            producer=old_producer("original:representation"),
            operation_id=uuid5(request.operation_id, "original:representation"),
        )
        checkpoint(
            "original:representation",
            dict(
                representation_ref=representation.ref.model_dump(mode="json"),
                status="available",
                diagnostics=[],
                title=None,
                published_date=None,
            ),
        )
    completed = None
    if stage == "completed":
        source_value = dict(
            source_revision_ref=source.ref.model_dump(mode="json"),
            representation_refs=[representation.ref.model_dump(mode="json")],
            role="original",
            acquisition_record_ref=store.ref("original:original").model_dump(mode="json"),
            status="available",
            diagnostics=["SYNTHETIC archived source diagnostic"],
        )
        checkpoint("original:outcome", dict(source=source_value))
        completed = dict(
            contract="source-acquisition-result/1",
            acquisition_record_ref=store.ref("request").model_dump(mode="json"),
            sources=[source_value],
            status="available",
            diagnostics=["SYNTHETIC archived aggregate diagnostic"],
        )
        checkpoint("result", completed)
    with repo.database.read_transaction() as tx:
        records = list(
            query(
                tx,
                "SELECT record_id,payload,reference_policy FROM runtime_records WHERE project_id=:p AND record_id=ANY(:ids) ORDER BY record_id",
                p=project,
                ids=[store.ref(key).record_id for key in keys],
            ).mappings()
        )
    assert all(row["reference_policy"] == "legacy/1" for row in records)
    return SimpleNamespace(
        request=request,
        source=source,
        representation=representation,
        extraction=extraction,
        records=records,
        completed=completed,
    )


@pytest.mark.parametrize(
    "stage", ["source", "extracted", "metadata_extracted", "representation", "completed"]
)
@pytest.mark.asyncio
async def test_legacy_prefix_resume_preserves_refs_payloads_and_reference_policy(
    repo, scope, producer, monkeypatch, stage
):
    saved = seed(repo, scope, producer, stage)
    monkeypatch.setattr(service, "_check_ssrf", lambda _: None)
    production = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")

    class NoFetch:
        async def fetch(self, *args, **kwargs):
            pytest.fail("legacy prefix already contains the received original")

    class Extractor:
        calls = 0

        async def extract(self, *args, **kwargs):
            self.calls += 1
            assert stage == "source", "legacy prefix already contains completed extraction"
            return saved.extraction

    extractor = Extractor()
    first = await service.acquire_source(
        production,
        scope[0],
        saved.request,
        fetcher=NoFetch(),
        extractors=extractor,
    )
    assert first.status == "available"
    if stage == "metadata_extracted":
        assert first.sources[0].source_revision_ref != saved.source.ref
        final_source = production.get(scope[0], first.sources[0].source_revision_ref).payload
        assert final_source["title"] == saved.extraction.title
        assert final_source["published_at"]["value"] == saved.extraction.published_date
    else:
        assert first.sources[0].source_revision_ref == saved.source.ref
    assert production.get(scope[0], saved.source.ref).payload == saved.source.payload
    if saved.representation is not None:
        assert first.sources[0].representation_refs == [saved.representation.ref]
    if saved.completed is not None:
        assert first.model_dump(mode="json") == saved.completed
    assert (
        await service.acquire_source(
            production,
            scope[0],
            saved.request,
            fetcher=NoFetch(),
            extractors=extractor,
        )
        == first
    )
    assert extractor.calls == (1 if stage == "source" else 0)
    with repo.database.read_transaction() as tx:
        after = list(
            query(
                tx,
                "SELECT record_id,payload,reference_policy FROM runtime_records WHERE project_id=:p AND record_id=ANY(:ids) ORDER BY record_id",
                p=scope[0],
                ids=[row["record_id"] for row in saved.records],
            ).mappings()
        )
        count = query(
            tx,
            "SELECT count(*) FROM artifact_revisions WHERE project_id=:p AND kind='SourceRevision'",
            p=scope[0],
        ).scalar_one()
    assert after == saved.records
    assert count == (2 if stage == "metadata_extracted" else 1)
