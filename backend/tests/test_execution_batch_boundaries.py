"""Bounded batches and committed descriptors preserve fresh authorization boundaries."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.blob_batch import save_blobs
from podgen.persistence.database import query
from podgen.persistence.execution.descriptors import DescriptorCache
from podgen.persistence.execution.text import authorize_text
from podgen.persistence.source_reading import read_sources
from podgen.persistence.text_source import verified_text
from tests.test_execution_parent_privacy import exchange as exchange
from tests.test_model_step_publication import setup


def test_descriptor_cache_is_bounded_and_does_not_publish_rolled_back_overlay():
    cache = DescriptorCache(max_entries=3, max_bytes=512)
    failed = cache.view()
    failed.put(("parent", "uncommitted"), dict(contract="test"))
    assert cache.get(("parent", "uncommitted")) is None
    for index in range(8):
        view = cache.view()
        view.put(("parent", index), dict(contract="test"))
        view.commit()
    assert len(cache.entries) <= 3 and cache.byte_length <= 512
    assert cache.get(("parent", 0)) is None
    assert cache.get(("parent", 7)) == dict(contract="test")
    cache.remember(("oversized", "x" * 1000), True)
    assert len(cache.entries) <= 3


def test_publication_descriptor_rollback_and_warm_checkpoint_query_bound(repo, scope, monkeypatch):
    journal, root, tj, turn, phase, *_ = setup(repo, scope)
    phase.commit()
    before = dict(journal.execution.descriptors.entries)
    phase = tj.publication(turn)
    ref = phase.event("failure", data=dict(code="synthetic"))

    def rollback(tx):
        raise RuntimeError("synthetic rollback")

    phase.writes.append(rollback)
    with pytest.raises(RuntimeError, match="synthetic rollback"):
        phase.commit()
    assert dict(journal.execution.descriptors.entries) == before
    phase.writes.remove(rollback)
    phase.commit()
    assert journal.execution.descriptors.get(("parent", scope[0], root["run_id"], ref.record_id)) is not None

    def forbidden(*args, **kwargs):
        pytest.fail("warm checkpoint reloaded immutable control metadata")

    monkeypatch.setattr("podgen.persistence.execution.control_bundle.query", forbidden)
    tj.event(turn, "failure", data=dict(code="still synthetic"))
    with repo.database.transaction() as tx:
        query(
            tx, "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a", p=scope[0], a=repo.actor_id
        )
    with pytest.raises(AccessDenied):
        tj.event(turn, "failure", data=dict(code="must deny"))


def test_cached_physical_parent_still_checks_withdrawal(exchange):
    from podgen.runtime.evaluation.model_turns import TurnJournal

    journal = exchange.journal
    root = journal.invocation(exchange.run_id, "draft")
    tj = TurnJournal(journal, root, 0)
    with journal.repo.database.transaction() as tx:
        turn = tj._row(tx)
    tj.event(turn, "failure", data=dict(code="synthetic warmup"))
    assert journal.execution.descriptors.get(
        ("parent", exchange.project, exchange.run_id, exchange.request.record_id)
    )
    exchange.withdraw_request_alias()
    with pytest.raises(AccessDenied):
        tj.event(turn, "failure", data=dict(code="must deny"))


def test_text_bindings_batch_and_scalar_helper_recheck_fresh_membership(repo, scope):
    journal, root, *_ = setup(repo, scope)
    values = [(f"SYNTHETIC source text {i} {uuid4()}".encode(), "text/plain") for i in range(64)]
    blobs = save_blobs(repo, scope[0], values)
    source = RuntimeRef(record_kind="user_input", record_id=uuid4())
    repo.append_runtime(scope[0], source, dict(blobs=[blob.model_dump(mode="json") for blob in blobs]))
    journal.execution.prepare(root["run_id"], generation=[source])
    documents = []
    for blob in blobs:
        text = verified_text(repo, scope[0], source, blob)
        text.bind_execution(journal.execution, root["run_id"])
        text.verify()
        documents.append(SimpleNamespace(text=text))
    lookups = []

    def capture(conn, cursor, sql, params, context, many):
        if "SELECT identity,binding,owner_project_id" in sql:
            lookups.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        with read_sources(documents):
            for _ in range(5):
                for doc in documents:
                    assert doc.text[:9] == "SYNTHETIC"
        assert len(lookups) == 1
        # The scalar helper owns its own snapshot and returns that check's owner.
        assert authorize_text(repo, journal.execution, root["run_id"], scope[0], source, blobs[0]) == scope[0]
        assert len(lookups) == 2
        with repo.database.transaction() as tx:
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                p=scope[0],
                a=repo.actor_id,
            )
        with pytest.raises(AccessDenied):
            authorize_text(repo, journal.execution, root["run_id"], scope[0], source, blobs[0])
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)


def test_mass_read_batches_parent_privacy_in_current_snapshot(repo, scope):
    journal, root, _, _, initial, *_ = setup(repo, scope)
    initial.commit()
    phase = journal.publication(root)
    refs = [phase.record(dict(full=f"SYNTHETIC record {index}")) for index in range(64)]
    phase.commit()
    checks = []

    def capture(conn, cursor, sql, params, context, many):
        if sql.startswith("WITH nodes(record_id)"):
            checks.append(sql)

    event.listen(repo.database.engine, "before_cursor_execute", capture)
    try:
        with journal.read_operation():
            assert len(repo.get_runtime_many(scope[0], refs)) == 64
            repo.authorize_runtime_many(scope[0], refs)
        assert len(checks) == 1
        with repo.database.transaction() as tx:
            query(
                tx,
                "DELETE FROM project_members WHERE project_id=:p AND actor_id=:a",
                p=scope[0],
                a=repo.actor_id,
            )
        with pytest.raises(AccessDenied):
            repo.get_runtime_many(scope[0], refs)
    finally:
        event.remove(repo.database.engine, "before_cursor_execute", capture)


def test_bound_corpus_uses_prepared_action_scope_and_denies_source_withdrawal(
    repo, scope, producer, monkeypatch
):
    from podgen.domain.contracts.common import BlobRef
    from podgen.persistence.editorial_inputs import generation_repo, load_editorial_materials
    from podgen.writing.source_tools import SourceCorpus
    from tests.authorization_helpers import withdraw_blob
    from tests.research_material_cases import saved

    reference, _, acquisition, article = saved(repo, scope, producer, monkeypatch)
    materials = load_editorial_materials(generation_repo(repo), scope[0], reference, case_id="hosted-case")
    journal, root, *_ = setup(repo, scope)
    journal.execution.prepare(root["run_id"], generation=[materials.shared_ref])
    corpus = SourceCorpus(materials)
    corpus.bind_execution(journal.execution, root["run_id"])
    representation = repo.get(scope[0], acquisition.sources[0].representation_refs[0]).payload

    def forbidden(*args, **kwargs):
        pytest.fail("prepared corpus reconstructed provenance at a tool boundary")

    with monkeypatch.context() as current:
        current.setattr("podgen.persistence.scope.ScopeAccess.resolve", forbidden)
        current.setattr("podgen.persistence.scope.ScopeAccess.runtime", forbidden)
        result = corpus.execute("read_source", dict(source_id="S11", start=0, end=100), read_id="E1")
        assert result["passage"]["text"] == article[:100]
    withdraw_blob(repo, scope[0], BlobRef.model_validate(representation["canonical_text_blob"]))
    with pytest.raises(AccessDenied):
        corpus.execute("read_source", dict(source_id="S11", start=0, end=100), read_id="E2")
