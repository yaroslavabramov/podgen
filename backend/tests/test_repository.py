import copy
import io
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import DBAPIError, IntegrityError

from podgen.domain.contracts.common import ArtifactRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.spoken import spoken_view
from podgen.persistence.database import query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.repository import Repository
from tests.factories import ANSWER, Factory


@pytest.fixture
def factory(repo, scope, producer):
    return Factory(repo, *scope, producer).setup()


def test_ac01_full_long_roundtrip_streaming_and_limits(factory):
    f = factory
    text = ("Тест 🛰️ е\u0301 𐀀\0\n" * 25000) + "\nПОСЛЕДНИЙ АБЗАЦ: ничего не потеряно."
    note = f.note(text)
    loaded = f.repo.get(f.project, note.ref)
    assert loaded.payload["answer_markdown"] == text
    assert loaded.content_hash == note.content_hash
    small = Repository(
        f.repo.database, f.repo.objects, f.repo.actor_id, purpose="evaluation", inline_limit=1024
    )
    with pytest.raises(ContractError, match="full artifact document streaming"):
        small.create(f.project, "ResearchNote", note.payload, producer=f.producer, operation_id=uuid4())
    from podgen.persistence.database import json_text

    streamed = small.create_from_stream(
        f.project,
        "ResearchNote",
        io.BytesIO(json_text(note.payload).encode()),
        producer=f.producer,
        operation_id=uuid4(),
    )
    assert streamed.payload == note.payload
    with pytest.raises(ContractError, match="duplicate_json_key"):
        small.create_from_stream(
            f.project,
            "ResearchNote",
            io.BytesIO(b'{"title":"a","title":"b"}'),
            producer=f.producer,
            operation_id=uuid4(),
        )


def test_full_normal_repository_path_and_empty_listener_prefix(factory):
    f = factory
    note = f.note()
    dossier = f.dossier(note)
    plan = f.plan(dossier)
    script = f.script(dossier, plan)
    view = spoken_view(
        script.ref,
        __import__(
            "podgen.domain.contracts.writing", fromlist=["ScriptRevision"]
        ).ScriptRevision.model_validate(script.payload),
    )
    assert len(view["utterances"]) == 2
    assert view["utterances"][-1]["text"] == ANSWER
    assert f.repo.get(f.project, script.ref) == script
    empty = spoken_view(
        script.ref,
        __import__(
            "podgen.domain.contracts.writing", fromlist=["ScriptRevision"]
        ).ScriptRevision.model_validate(script.payload),
        0,
    )
    state = f.make(
        "ListenerState",
        dict(
            script_ref=script.ref.model_dump(mode="json"),
            covered_prefix=dict(
                through_item_id=None, ordered_item_ids=[], content_and_order_hash=empty["view_hash"]
            ),
            audience_assumptions=[],
            introduced_entities=[],
            explained_concepts=[],
            established_connections=[],
            questions_and_promises=[],
            comprehension_risks=[],
            extractor_profile_ref=f.model.ref.model_dump(mode="json"),
        ),
    )
    assert f.repo.get(f.project, state.ref).payload["covered_prefix"]["ordered_item_ids"] == []
    broken = copy.deepcopy(state.payload)
    broken["covered_prefix"]["content_and_order_hash"] = "wrong"
    with pytest.raises(ContractError, match="listener_prefix_hash_mismatch"):
        f.make("ListenerState", broken)


def test_immutable_history_and_scoped_database_foreign_keys(factory):
    f = factory
    old = f.note("Первый полный текст")
    payload = old.payload | {"answer_markdown": "Исправленный полный текст"}
    new = f.make("ResearchNote", payload, artifact_id=old.ref.artifact_id)
    assert old.ref.artifact_id == new.ref.artifact_id
    assert old.ref.revision_id != new.ref.revision_id
    assert f.repo.get(f.project, old.ref).payload["answer_markdown"] == "Первый полный текст"
    for table, key, value in [
        ("artifact_revisions", "revision_id", old.ref.revision_id),
        ("outbox", "project_id", f.project),
    ]:
        with pytest.raises(DBAPIError, match="immutable_history"):
            with f.repo.database.transaction() as tx:
                query(tx, f"DELETE FROM {table} WHERE {key}=:id", id=value)
    foreign = uuid4()
    with f.repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=foreign)
    with pytest.raises(IntegrityError):
        with f.repo.database.transaction() as tx:
            query(
                tx,
                "INSERT INTO revision_links VALUES(:p,:c,:i)",
                p=foreign,
                c=old.ref.revision_id,
                i=new.ref.revision_id,
            )


def test_idempotency_same_operation_and_concurrent_retry(factory):
    f = factory
    note = f.note()
    operation = uuid4()

    def save():
        return f.repo.create(
            f.project, "ResearchNote", note.payload, producer=f.producer, operation_id=operation
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: save(), range(2)))
    assert first.ref == second.ref
    with pytest.raises(Conflict, match="operation_id_reused"):
        f.repo.create(
            f.project,
            "ResearchNote",
            note.payload | {"title": "Другое"},
            producer=f.producer,
            operation_id=operation,
        )


def test_ac02_foreign_refs_forged_kind_and_live_acl(factory):
    f = factory
    note = f.note()
    other = uuid4()
    with f.repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=other)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=other, a=f.repo.actor_id)
    for ref in [
        note.ref,
        note.ref.model_copy(update={"kind": "ModelProfile"}),
        ArtifactRef(artifact_id=uuid4(), revision_id=uuid4(), kind="ResearchNote"),
    ]:
        with pytest.raises(AccessDenied, match="reference_unavailable"):
            f.repo.get(other, ref)
    with pytest.raises(AccessDenied):
        f.repo.create(
            other,
            "ResearchNote",
            note.payload,
            producer=f.producer,
            operation_id=uuid4(),
            input_refs=[note.ref],
        )
    voice = f.voice.ref
    with pytest.raises(AccessDenied):
        f.repo.get(other, voice)
    with f.repo.database.transaction() as tx:
        query(
            tx,
            "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
            p=f.project,
            a=f.repo.actor_id,
        )
    with pytest.raises(AccessDenied):
        f.repo.get(f.project, note.ref)


def test_blob_full_bytes_checksum_scope_and_revoked_ancestor(factory):
    f = factory
    text = "оригинал 🛰️\0 е\u0301\n" * 10000
    source, blob = f.source(text)
    assert f.repo.read_blob(f.project, source.ref, blob) == text.encode()
    forged = blob.model_copy(update={"sha256": "0" * 64})
    with pytest.raises(AccessDenied):
        f.repo.read_blob(f.project, source.ref, forged)
    with pytest.raises(ContractError, match="blob_checksum_mismatch"):
        f.repo.save_blob(
            f.project, io.BytesIO(b"bytes"), "application/octet-stream", expected_sha256="0" * 64
        )
    note = f.make("ResearchNote", f.note().payload, input_refs=[source.ref])
    DependencyLedger(f.repo).invalidate(
        f.project, source.ref, category="access", reason="Test privacy revocation"
    )
    with pytest.raises(AccessDenied):
        f.repo.get(f.project, note.ref)
    with pytest.raises(AccessDenied):
        f.repo.read_blob(f.project, source.ref, blob)


def test_invalid_required_fields_kinds_aliases_and_unsafe_json(factory):
    f = factory
    payload = f.note().payload
    for bad in [
        payload | {"unknown": "field"},
        {k: v for k, v in payload.items() if k != "citations"},
        payload | {"title": ""},
        payload | {"question_refs": ["q1@r1"]},
    ]:
        with pytest.raises((ValueError, ValidationError)):
            f.make("ResearchNote", bad)
    with pytest.raises(ValueError, match="unsupported_kind"):
        f.make("UnknownArtifact", {})
    for value in [float("nan"), float("inf"), 2**60, "\ud800"]:
        with pytest.raises((ValueError, UnicodeError)):
            f.make("Policy", dict(policy_kind="data", rules={"bad": value}))
