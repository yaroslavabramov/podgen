"""Whole-document product boundaries through acquisition; local doubles never use the network."""

from dataclasses import replace
from uuid import uuid4, uuid5

import pytest

from podgen.domain.contracts.common import ArtifactRef, BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.domain.packages import ArtifactPackage
from podgen.persistence.database import query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.packages import PackageImporter
from podgen.persistence.repository import Repository
from podgen.sources.acquisition import service
from podgen.sources.acquisition.store import AcquisitionStore
from podgen.sources.extraction.contracts import ExtractedDocument
from tests.source_acquisition_support import ARTICLE, Fetcher, document, setup


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    monkeypatch.setattr(service, "_check_ssrf", lambda _: None)


def production(repo):
    return Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")


def revisions(repo, project, kind):
    with repo.database.read_transaction() as tx:
        return list(
            query(
                tx,
                "SELECT revision_id,payload FROM artifact_revisions WHERE project_id=:p AND kind=:kind",
                p=project,
                kind=kind,
            ).mappings()
        )


class Extractor:
    def __init__(self, before=None):
        self.calls, self.before = 0, before

    async def extract(self, fetched, profile, *, role):
        self.calls += 1
        if self.before is not None:
            self.before(fetched)
        return ExtractedDocument(
            ARTICLE,
            "synthetic-whole-document-extractor",
            "test-only/1",
            title="Synthetic retained document title",
            published_date="2026-01-02",
            coverage="complete",
        )


@pytest.mark.asyncio
async def test_source_is_published_once_with_final_metadata_after_durable_original(repo, scope, producer):
    request, _ = setup(repo, scope, producer)
    current, project = production(repo), scope[0]
    observed = []

    def inspect(fetched):
        assert not revisions(repo, project, "SourceRevision")
        assert not revisions(repo, project, "SourceRepresentation")
        with repo.database.read_transaction() as tx:
            receipts = list(
                query(
                    tx,
                    "SELECT record_id,record_kind,payload FROM runtime_records WHERE project_id=:p AND record_kind='acquisition_record'",
                    p=project,
                ).mappings()
            )
        originals = [row for row in receipts if row["payload"].get("original_blob")]
        assert len(originals) == 1
        row = originals[0]
        ref = RuntimeRef(record_kind=row["record_kind"], record_id=row["record_id"])
        saved = BlobRef.model_validate(row["payload"]["original_blob"])
        assert current.read_runtime_blob(project, ref, saved) == fetched.body
        observed.append(ref)

    fetcher, extractor = Fetcher(), Extractor(inspect)
    result = await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert result.status == "available" and len(observed) == 1
    assert len(fetcher.calls) == extractor.calls == 1
    source = result.sources[0]
    assert len(revisions(repo, project, "SourceRevision")) == 1
    assert len(revisions(repo, project, "SourceRepresentation")) == 1
    payload = current.get(project, source.source_revision_ref).payload
    assert payload["title"] == "Synthetic retained document title"
    assert payload["published_at"]["value"] == "2026-01-02"
    assert payload["source_metadata_evidence"]
    representation = current.get(project, source.representation_refs[0]).payload
    assert representation["source_revision_ref"] == source.source_revision_ref.model_dump(mode="json")
    text = BlobRef.model_validate(representation["canonical_text_blob"])
    assert current.read_blob(project, source.representation_refs[0], text).decode() == ARTICLE


@pytest.mark.asyncio
async def test_pending_saved_request_reauthorizes_rights_before_fetch(repo, scope, producer):
    request, _ = setup(repo, scope, producer)
    current, project = production(repo), scope[0]
    store = AcquisitionStore(current, project, request.operation_id)
    store.put("request", request.model_dump(mode="json"))
    assert store.get("original:original") is None
    DependencyLedger(repo).invalidate(
        project,
        request.rights.storage_policy_ref,
        category="access",
        reason="SYNTHETIC rights withdrawal before resuming a saved pending request",
    )
    fetcher, extractor = Fetcher(), Extractor()
    with pytest.raises(AccessDenied):
        await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert fetcher.calls == []
    assert extractor.calls == 0
    assert not revisions(repo, project, "SourceRevision")
    assert not revisions(repo, project, "SourceRepresentation")


@pytest.mark.asyncio
async def test_rights_withdrawn_during_fetch_cannot_be_bypassed_by_a_new_unrevoked_sha(repo, scope, producer):
    request, _ = setup(repo, scope, producer)
    current, project = production(repo), scope[0]

    class WithdrawDuringFetch(Fetcher):
        async def fetch(self, *args, **kwargs):
            fetched = await super().fetch(*args, **kwargs)
            # No original blob/receipt exists yet for the privacy closure to mark.
            DependencyLedger(repo).invalidate(
                project,
                request.rights.storage_policy_ref,
                category="access",
                reason="SYNTHETIC rights withdrawal while HTTP response is in flight",
            )
            return fetched

    fetcher, extractor = WithdrawDuringFetch(), Extractor()
    with pytest.raises(AccessDenied):
        await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert len(fetcher.calls) == 1 and extractor.calls == 0
    assert not revisions(repo, project, "SourceRevision")
    assert not revisions(repo, project, "SourceRepresentation")


@pytest.mark.asyncio
async def test_rights_withdrawn_during_extraction_prevent_a_half_ready_document(repo, scope, producer):
    request, _ = setup(repo, scope, producer)
    current, project = production(repo), scope[0]

    def withdraw(_):
        DependencyLedger(repo).invalidate(
            project,
            request.rights.publication_policy_ref,
            category="access",
            reason="SYNTHETIC rights withdrawal after receiving original bytes",
        )

    fetcher, extractor = Fetcher(), Extractor(withdraw)
    with pytest.raises(AccessDenied):
        await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert len(fetcher.calls) == extractor.calls == 1
    assert not revisions(repo, project, "SourceRevision")
    assert not revisions(repo, project, "SourceRepresentation")


@pytest.mark.parametrize("different_parent_profile", [False, True])
@pytest.mark.asyncio
async def test_extractor_profile_identity_reuses_equal_payloads_and_keeps_distinct_parent_refs(
    repo, scope, producer, different_parent_profile
):
    first, _ = setup(repo, scope, producer)
    current, project = production(repo), scope[0]
    parent = first.profile_ref
    if different_parent_profile:
        parent = repo.create(
            project,
            "ProcessingProfile",
            repo.get(project, parent).payload,
            producer=producer,
            operation_id=uuid4(),
        ).ref
    second = first.model_copy(
        update=dict(
            operation_id=uuid4(), source_id=uuid4(), url="https://example.com/second", profile_ref=parent
        )
    )
    fetcher = Fetcher({first.url: document(), second.url: document(url=second.url)})
    extractor, results = Extractor(), []
    for request in (first, second):
        results.append(
            await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
        )
    profiles = []
    for request, result in zip((first, second), results, strict=True):
        source = result.sources[0]
        representation = current.get(project, source.representation_refs[0]).payload
        profile = representation["extractor"]["profile_ref"]
        profiles.append(profile)
        saved = current.get(project, ArtifactRef.model_validate(profile)).payload
        assert saved["input_profile_refs"] == [request.profile_ref.model_dump(mode="json")]
    assert (profiles[0] != profiles[1]) is different_parent_profile
    assert len(revisions(repo, project, "SourceRevision")) == 2
    assert len(fetcher.calls) == extractor.calls == 2


@pytest.mark.asyncio
async def test_completed_resume_reauthorizes_outputs_without_fetch_or_extraction(repo, scope, producer):
    request, _ = setup(repo, scope, producer)
    current, project = production(repo), scope[0]
    fetcher, extractor = Fetcher(), Extractor()
    result = await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    DependencyLedger(repo).invalidate(
        project,
        result.sources[0].source_revision_ref,
        category="access",
        reason="SYNTHETIC source withdrawal after successful publication",
    )
    with pytest.raises(AccessDenied):
        await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert len(fetcher.calls) == extractor.calls == 1


@pytest.mark.parametrize("error_type", [RuntimeError, OSError])
@pytest.mark.asyncio
async def test_failed_final_publication_reuses_durable_fetch_and_extraction(
    repo, scope, producer, monkeypatch, error_type
):
    from podgen.persistence import artifact_batch

    request, _ = setup(repo, scope, producer)
    current, project = production(repo), scope[0]
    fetcher, extractor = Fetcher(), Extractor()
    insert, interrupted = artifact_batch.insert_rows, []

    def crash_after_revisions(tx, table, rows, **options):
        returned = insert(tx, table, rows, **options)
        if table == "artifact_revisions" and any(row["kind"] == "SourceRepresentation" for row in rows):
            interrupted.append(True)
            raise error_type("SYNTHETIC final publication failure")
        return returned

    with monkeypatch.context() as patch:
        patch.setattr(artifact_batch, "insert_rows", crash_after_revisions)
        with pytest.raises(error_type, match="final publication failure"):
            await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert interrupted == [True]
    assert not revisions(repo, project, "SourceRevision")
    assert not revisions(repo, project, "SourceRepresentation")
    assert len(fetcher.calls) == extractor.calls == 1
    result = await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert result.status == "available"
    assert len(fetcher.calls) == extractor.calls == 1
    assert len(revisions(repo, project, "SourceRevision")) == 1
    assert len(revisions(repo, project, "SourceRepresentation")) == 1


@pytest.mark.asyncio
async def test_unavailable_promoted_pdf_does_not_finish_before_browser_fallback(
    repo, scope, producer, monkeypatch
):
    from podgen.sources.acquisition.fallbacks import jina_request

    request, profile = setup(
        repo,
        scope,
        producer,
        enable_jina=True,
        enable_browser=True,
        prefer_mediawiki=False,
        min_text_chars=10,
    )
    current, project = production(repo), scope[0]
    monkeypatch.setenv(profile.jina_credential_env, "synthetic-no-network-key")
    jina_url, _ = jina_request(request.url, profile)
    pdf_url = "https://example.com/paper.pdf"
    blocked = (
        "Cookie settings Accept only necessary cookies Accept all cookies Manage your cookie preferences"
    )
    portal = (
        "Title: Publisher\nAccess denied request blocked\n###### Topics\n###### Countries\n###### Series\n"
        + "\n".join(f"[N{index}](/nav/{index})" for index in range(35))
        + f"\n[Download PDF]({pdf_url})"
    )
    from podgen.sources.acquisition.fallbacks import promoted_pdf
    from podgen.sources.extraction.quality import assess_web_fetch_health

    assert assess_web_fetch_health(portal, min_chars=profile.min_text_chars).state == "blocked"
    assert promoted_pdf(portal, request.url) == pdf_url
    fetcher = Fetcher(
        {
            request.url: document(blocked, status=403),
            jina_url: document(portal, url=jina_url, content_type="text/markdown"),
            pdf_url: document(blocked, url=pdf_url),
        }
    )
    browser = Fetcher({request.url: replace(document(), acquisition_kind="browser_snapshot")})

    class FallbackExtractor:
        calls = []

        async def extract(self, fetched, profile, *, role):
            self.calls.append(role)
            text = ARTICLE if role == "browser" else portal if role == "jina" else blocked
            return ExtractedDocument(text, "synthetic-fallback-" + role, "test-only/1")

    extractor = FallbackExtractor()
    first = await service.acquire_source(
        current,
        project,
        request,
        fetcher=fetcher,
        extractors=extractor,
        browser_fetcher=browser,
    )
    assert first.status == "available"
    assert [source.role for source in first.sources] == ["original", "jina", "promoted_pdf", "browser"]
    assert first.sources[-1].status == "available"
    assert [url for url, _ in fetcher.calls] == [request.url, jina_url, pdf_url]
    assert len(browser.calls) == 1
    assert extractor.calls == ["original", "jina", "promoted_pdf", "browser"]
    assert (
        await service.acquire_source(
            current,
            project,
            request,
            fetcher=fetcher,
            extractors=extractor,
            browser_fetcher=browser,
        )
        == first
    )
    assert len(fetcher.calls) == 3 and len(browser.calls) == 1 and len(extractor.calls) == 4


@pytest.mark.asyncio
async def test_missing_jina_credential_is_in_the_first_durable_terminal_result(
    repo, scope, producer, monkeypatch
):
    request, profile = setup(
        repo,
        scope,
        producer,
        enable_jina=True,
        enable_browser=False,
        prefer_mediawiki=False,
    )
    monkeypatch.delenv(profile.jina_credential_env, raising=False)
    current, project = production(repo), scope[0]
    blocked = (
        "Cookie settings Accept only necessary cookies Accept all cookies Manage your cookie preferences"
    )
    fetcher = Fetcher({request.url: document(blocked, status=403)})

    class BlockedExtractor:
        calls = 0

        async def extract(self, *args, **kwargs):
            self.calls += 1
            return ExtractedDocument(blocked, "synthetic-consent-only", "test-only/1")

    extractor = BlockedExtractor()
    first = await service.acquire_source(current, project, request, fetcher=fetcher, extractors=extractor)
    assert first.status == "unavailable"
    assert "jina_credential_missing" in first.diagnostics
    assert (
        await service.acquire_source(
            current,
            project,
            request,
            fetcher=fetcher,
            extractors=extractor,
        )
        == first
    )
    assert len(fetcher.calls) == extractor.calls == 1


def test_partial_existing_batch_resolves_real_refs_before_dependent_fingerprints(
    repo, scope, producer, tmp_path
):
    project, episode = scope
    package_id = uuid4()
    existing_payload = dict(policy_kind="data", rules=dict(synthetic="existing parent"))
    existing = repo.create(
        project,
        "Policy",
        existing_payload,
        producer=producer,
        operation_id=uuid5(package_id, "operation:parent"),
        episode_id=episode,
    )
    package = ArtifactPackage.model_validate(
        dict(
            format="podgen-fixture-v1",
            package_id=package_id,
            test_data=True,
            description="SYNTHETIC retry with a previously committed parent and a new dependent",
            artifacts=[
                dict(
                    key="parent",
                    kind="Policy",
                    payload=existing_payload,
                    input_keys=[],
                    audience="project",
                    identity_of=None,
                ),
                dict(
                    key="child",
                    kind="Policy",
                    payload=dict(policy_kind="data", rules=dict(parent={"$artifact": "parent"})),
                    input_keys=["parent"],
                    audience="project",
                    identity_of=None,
                ),
            ],
            runtime_records=[],
            blobs=[],
            external_refs={},
        )
    )
    importer = PackageImporter(repo)
    first = importer.import_package(project, episode, package, tmp_path, producer=producer)
    second = importer.import_package(project, episode, package, tmp_path, producer=producer)
    assert first[0].ref == existing.ref
    assert [row.ref for row in first] == [row.ref for row in second]
    child = repo.get(project, first[1].ref)
    assert child.payload["rules"]["parent"] == existing.ref.model_dump(mode="json")
    assert child.input_refs == [existing.ref]
    assert len(revisions(repo, project, "Policy")) == 2
