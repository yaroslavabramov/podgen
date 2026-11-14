"""Adversarial reference-policy tests through the shared publisher, with no external work."""

from copy import deepcopy
from dataclasses import asdict
from io import BytesIO
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.acquisition_policy import SOURCE, JournalBinding
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.artifact_batch import publish
from podgen.persistence.artifact_preparation import prepare
from podgen.persistence.database import query
from podgen.persistence.publication_plan import ArtifactNode, PublicationBatch, RuntimeNode
from podgen.persistence.repository import Repository
from podgen.sources.acquisition.contracts import ReExtractionRequest
from podgen.sources.acquisition.source_payload import received
from podgen.sources.acquisition.store import AcquisitionStore
from tests.source_acquisition_support import document, setup


def publish_batch(repo, project, batch):
    return archive_write(repo, project, lambda tx: publish(repo, tx, project, batch))


def case(repo, scope, producer, *, request=None):
    request = request or setup(repo, scope, producer)[0]
    store = AcquisitionStore(repo, scope[0], request.operation_id)
    store.put("request", request.model_dump(mode="json"))
    fetched = document(f"SYNTHETIC saved document {uuid4()}")
    blob = repo.save_blob(scope[0], BytesIO(fetched.body), "text/plain")
    original = dict(
        contract="source-received-original/1",
        request_ref=store.ref("request").model_dump(mode="json"),
        role="original",
        original_blob=blob.model_dump(mode="json"),
        **{key: value for key, value in asdict(fetched).items() if key != "body"},
    )
    return SimpleNamespace(request=request, store=store, document=fetched, blob=blob, original=original)


def extracted(value):
    return dict(
        request_ref=value.store.ref("request").model_dump(mode="json"),
        original_ref=value.store.ref("original:original").model_dump(mode="json"),
        text_blob=value.blob.model_dump(mode="json"),
        metadata=dict(name="synthetic-reference-test", version="test-only/1"),
    )


@pytest.mark.parametrize(
    "stage,missing",
    [
        ("original", "request_ref"),
        ("original", "original_blob"),
        ("extracted", "request_ref"),
        ("extracted", "original_ref"),
        ("extracted", "text_blob"),
    ],
)
def test_required_parent_and_owned_blob_fields_cannot_be_omitted(repo, scope, producer, stage, missing):
    value = case(repo, scope, producer)
    if stage == "extracted":
        value.store.put("original:original", value.original)
    payload = deepcopy(value.original if stage == "original" else extracted(value))
    del payload[missing]
    with pytest.raises(ContractError):
        value.store.put("original:" + stage, payload)
    assert value.store.get("original:" + stage) is None


def test_nested_reference_cannot_be_disguised_as_extractor_metadata(repo, scope, producer):
    value = case(repo, scope, producer)
    value.store.put("original:original", value.original)
    public = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    repo.append_runtime(scope[0], public, dict(synthetic="otherwise readable nested reference"))
    payload = extracted(value)
    payload["metadata"]["blocks"] = [dict(nested=dict(representation_refs=[public.model_dump(mode="json")]))]
    with pytest.raises(ContractError):
        value.store.put("original:extracted", payload)
    assert value.store.get("original:extracted") is None


def test_original_parent_requires_exact_runtime_kind_as_well_as_id(repo, scope, producer):
    value = case(repo, scope, producer)
    payload = deepcopy(value.original)
    payload["request_ref"]["record_kind"] = "tool_execution"
    with pytest.raises(ContractError):
        value.store.put("original:original", payload)


def test_reextraction_parent_must_be_the_requested_sources_original(repo, scope, producer):
    first, second = case(repo, scope, producer), case(repo, scope, producer)
    first.store.put("original:original", first.original)
    second.store.put("original:original", second.original)
    payload = received(
        first.request, "original", first.document, first.blob, first.store.ref("original:original")
    )
    source = repo.create(scope[0], "SourceRevision", payload, producer=producer, operation_id=uuid4())
    request = ReExtractionRequest(
        operation_id=uuid4(),
        source_revision_ref=source.ref,
        profile_ref=first.request.profile_ref,
        reason="SYNTHETIC explicit local re-extraction",
    )
    store = AcquisitionStore(repo, scope[0], request.operation_id)
    store.put("request", request.model_dump(mode="json"))
    wrong = extracted(second)
    wrong["request_ref"] = store.ref("request").model_dump(mode="json")
    with pytest.raises(ContractError):
        store.put("original:extracted", wrong)
    assert store.get("original:extracted") is None
    correct = extracted(first)
    correct["request_ref"] = store.ref("request").model_dump(mode="json")
    store.put("original:extracted", correct)
    assert store.get("original:extracted") == correct


def test_evaluator_first_node_cannot_change_a_public_receipts_rights_view(repo, scope, producer):
    project = scope[0]
    request, _ = setup(repo, scope, producer)
    secret = repo.save_blob(
        project, BytesIO(b"SYNTHETIC policy bytes later classified private"), "text/plain"
    )
    policy = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(blob=secret.model_dump(mode="json"))),
        producer=producer,
        operation_id=uuid4(),
    )
    request = request.model_copy(
        update=dict(
            rights=request.rights.model_copy(update=dict(storage_policy_ref=policy.ref)),
        )
    )
    value = case(repo, scope, producer, request=request)
    repo.append_runtime(
        project,
        RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
        dict(blob=secret.model_dump(mode="json")),
        audience="evaluator",
    )
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    with pytest.raises(AccessDenied):
        generation.get(project, policy.ref)
    private = ArtifactNode(
        "private-first",
        "Policy",
        dict(policy_kind="data", rules=dict(synthetic=True)),
        producer,
        uuid4(),
        audience="evaluator",
    )
    original = value.store.node("original:original", value.original)
    with pytest.raises(AccessDenied):
        publish_batch(repo, project, PublicationBatch(artifacts=(private,), runtimes=(original,)))
    assert value.store.get("original:original") is None


@pytest.mark.parametrize("audience", ["project", "evaluator"])
def test_source_metadata_runtime_refs_keep_ordinary_authorization(repo, scope, producer, audience):
    value = case(repo, scope, producer)
    value.store.put("original:original", value.original)
    other = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    repo.append_runtime(scope[0], other, dict(synthetic="ordinary metadata dependency"), audience=audience)
    payload = received(
        value.request, "original", value.document, value.blob, value.store.ref("original:original")
    )
    payload["source_metadata_evidence"].append(dict(nested=dict(other_ref=other.model_dump(mode="json"))))
    prepared = prepare(repo, scope[0], "SourceRevision", payload)
    node = ArtifactNode(
        "source",
        "SourceRevision",
        payload,
        producer,
        uuid4(),
        reference_policy=SOURCE,
    )
    batch = PublicationBatch(artifacts=(node,), prepared=prepared)
    if audience == "evaluator":
        with pytest.raises(AccessDenied):
            publish_batch(repo, scope[0], batch)
        return
    published = publish_batch(repo, scope[0], batch)["source"]
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    assert generation.get(scope[0], published.ref).ref == published.ref
    with repo.database.read_transaction() as tx:
        assert (
            query(
                tx,
                "SELECT read_required FROM runtime_links WHERE project_id=:p AND revision_id=:r AND record_id=:other",
                p=scope[0],
                r=published.ref.revision_id,
                other=other.record_id,
            ).scalar_one()
            is True
        )


def test_source_extraction_metadata_cannot_borrow_another_originals_receipt(repo, scope, producer):
    first, second = case(repo, scope, producer), case(repo, scope, producer)
    first.store.put("original:original", first.original)
    second.store.put("original:original", second.original)
    second.store.put("original:extracted", extracted(second))
    payload = received(
        first.request, "original", first.document, first.blob, first.store.ref("original:original")
    )
    payload["title"] = "SYNTHETIC title claiming the wrong extraction provenance"
    payload["source_metadata_evidence"].append(
        dict(
            extractor="synthetic-reference-test",
            extraction_record_ref=second.store.ref("original:extracted").model_dump(mode="json"),
            title=payload["title"],
        )
    )
    prepared = prepare(repo, scope[0], "SourceRevision", payload)
    node = ArtifactNode("source", "SourceRevision", payload, producer, uuid4(), reference_policy=SOURCE)
    with pytest.raises(ContractError):
        publish_batch(repo, scope[0], PublicationBatch(artifacts=(node,), prepared=prepared))


def test_source_receipt_reference_requires_the_actual_runtime_kind(repo, scope, producer):
    value = case(repo, scope, producer)
    value.store.put("original:original", value.original)
    payload = received(
        value.request, "original", value.document, value.blob, value.store.ref("original:original")
    )
    payload["acquisition_record_ref"]["record_kind"] = "tool_execution"
    prepared = prepare(repo, scope[0], "SourceRevision", payload)
    node = ArtifactNode(
        "source",
        "SourceRevision",
        payload,
        producer,
        uuid4(),
        reference_policy=SOURCE,
    )
    with pytest.raises(ContractError):
        publish_batch(repo, scope[0], PublicationBatch(artifacts=(node,), prepared=prepared))


@pytest.mark.parametrize("field", ["source_revision_ref", "representation_refs"])
def test_document_outcome_requires_the_declared_output_artifact_kinds(repo, scope, producer, field):
    value = case(repo, scope, producer)
    value.store.put("original:original", value.original)
    source = repo.create(
        scope[0],
        "SourceRevision",
        received(value.request, "original", value.document, value.blob, value.store.ref("original:original")),
        producer=producer,
        operation_id=uuid4(),
    )
    payload = dict(
        source=dict(
            source_revision_ref=source.ref.model_dump(mode="json"),
            representation_refs=[],
            role="original",
            acquisition_record_ref=value.store.ref("original:original").model_dump(mode="json"),
            status="extraction_failed",
            diagnostics=["SYNTHETIC wrong output artifact kind"],
        )
    )
    wrong_ref = value.request.rights.storage_policy_ref.model_dump(mode="json")
    payload["source"][field] = wrong_ref if field == "source_revision_ref" else [wrong_ref]
    with pytest.raises(ContractError):
        value.store.put("original:outcome", payload)
    assert value.store.get("original:outcome") is None


@pytest.mark.parametrize("artifact_root", [False, True])
def test_project_nodes_cannot_hide_evaluator_inputs_in_a_mixed_dag(repo, scope, producer, artifact_root):
    secret = ArtifactNode(
        "secret",
        "Policy",
        dict(policy_kind="data", rules=dict(synthetic="evaluator input")),
        producer,
        uuid4(),
        audience="evaluator",
    )
    runtime = RuntimeNode(
        "middle",
        RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
        dict(input={"$artifact": "secret"}),
    )
    root = ArtifactNode(
        "root",
        "Policy",
        dict(policy_kind="data", rules=dict(input={"$runtime": "middle"})),
        producer,
        uuid4(),
    )
    batch = PublicationBatch(
        artifacts=(secret, root) if artifact_root else (secret,),
        runtimes=(runtime,),
    )
    with pytest.raises(AccessDenied):
        publish_batch(repo, scope[0], batch)


def test_user_contract_string_does_not_select_the_technical_reference_policy(repo, scope):
    secret = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    repo.append_runtime(scope[0], secret, dict(synthetic="private configuration"), audience="evaluator")
    binding = JournalBinding(uuid4(), "original", "original")
    node = RuntimeNode(
        "forged",
        binding.reference(),
        dict(contract="acquisition-journal/2", request_ref=secret.model_dump(mode="json")),
    )
    with pytest.raises(AccessDenied):
        publish_batch(repo, scope[0], PublicationBatch(runtimes=(node,)))
