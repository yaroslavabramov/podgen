"""Private SHA protection and withdrawal cross the existing generic graph once."""

import io
from types import SimpleNamespace
from uuid import uuid4

import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.database import json_text, query
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.execution.inputs import bind, refresh
from podgen.persistence.execution.live import authorize
from podgen.persistence.private_journal.access import entry_privacy
from podgen.persistence.private_journal.objects import prepare, upload
from podgen.persistence.private_journal.privacy import protect_private_owned
from podgen.persistence.repository import Repository
from tests.privacy_graph_cases import Graph
from tests.privacy_projection_support import seed_graph


@pytest.fixture
def private_scope(repo, scope, producer):
    project = scope[0]
    source_blob = repo.save_blob(project, io.BytesIO(b"SYNTHETIC borrowed source stays public"), "text/plain")
    source = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules=dict(blob=source_blob.model_dump(mode="json"))),
        producer=producer,
        operation_id=uuid4(),
    )
    manifest = RuntimeRef(record_kind="context_assembly", record_id=uuid4())
    repo.append_runtime(project, manifest, dict(synthetic=True), audience="evaluator")
    run_id = uuid4()
    with repo.database.transaction() as tx:
        query(
            tx,
            """INSERT INTO evaluation_writing_runs
            (project_id,run_id,actor_id,input_fingerprint,manifest_id,status,execution_storage)
            VALUES(:p,:r,:actor,'synthetic',:manifest,'prepared','private/3')""",
            p=project,
            r=run_id,
            actor=repo.actor_id,
            manifest=manifest.record_id,
        )
    row = bind(repo, project, run_id, [dict(purpose="generation", ref=source.ref.model_dump(mode="json"))])
    return SimpleNamespace(
        repo=repo,
        project=project,
        run_id=run_id,
        bound=row,
        source=source,
        source_blob=source_blob,
        producer=producer,
        revision=0,
    )


def _entry(case, body=None, *, parents=(), bound=None, abort=False):
    value = prepare(body, case.project) if body is not None else None
    if value is not None:
        upload(case.repo, case.project, [value])
    entry_id = uuid4()
    case.revision += 1
    with case.repo.database.transaction() as tx:
        case.repo._lock(tx, case.project)
        known = (
            value is not None
            and query(
                tx,
                "SELECT 1 FROM execution_objects WHERE project_id=:p AND sha256=:sha",
                p=case.project,
                sha=value.reference.sha256,
            ).first()
        )
        insert_rows(
            tx,
            "execution_entries",
            [
                dict(
                    project_id=case.project,
                    run_id=case.run_id,
                    entry_id=entry_id,
                    revision=case.revision,
                    commit_key=str(entry_id),
                    content_hash=digest(dict(synthetic=str(entry_id))),
                    checkpoint_kind="synthetic_privacy",
                    operation_id=uuid4(),
                    attempt_id=case.run_id,
                    input_set_id=(bound or case.bound)["set_id"],
                    data=json_text(dict(borrowed=case.source_blob.model_dump(mode="json"))),
                    owned_objects=json_text(
                        dict(body=value.reference.model_dump(mode="json")) if value else {}
                    ),
                    owned_hashes=[value.reference.sha256] if value else [],
                    parent_entries=list(parents),
                )
            ],
            json_columns=("data", "owned_objects"),
        )
        if value is not None:
            insert_rows(
                tx,
                "execution_objects",
                [
                    dict(
                        project_id=case.project,
                        **value.reference.model_dump(),
                        first_entry_id=entry_id,
                    )
                ],
                on_conflict=True,
            )
        protect_private_owned(
            tx, case.project, entry_id, {value.reference.sha256} if value and not known else set()
        )
        if abort:
            raise RuntimeError("SYNTHETIC private checkpoint rollback")
    return entry_id, value.reference if value else None


def _denied_entries(case):
    with case.repo.database.read_transaction() as tx:
        return set(
            query(
                tx, "SELECT entry_id FROM execution_entry_denials WHERE project_id=:p", p=case.project
            ).scalars()
        )


def test_first_private_sha_closes_preexisting_runtime_artifact_catalog_aliases(private_scope):
    case = private_scope
    body = b"SYNTHETIC first private alias root"
    own = prepare(body, case.project).reference
    derived, last = (prepare(raw, case.project).reference for raw in (b"derived", b"catalog descendant"))
    alias, child, catalog, artifact = (uuid4() for _ in range(4))
    graph = Graph(
        runtime_inputs={(child, alias)},
        runtime_blobs={(alias, own.sha256), (child, derived.sha256)},
        catalog_blobs={(catalog, derived.sha256)},
        original={(artifact, catalog)},
        artifact_blobs={(artifact, last.sha256)},
    )
    with case.repo.database.transaction() as tx:
        seed_graph(tx, case.project, graph)
    entry, _ = _entry(case, body)
    with case.repo.database.read_transaction() as tx:
        protected = set(
            query(
                tx, "SELECT sha256 FROM protected_byte_hashes WHERE project_id=:p", p=case.project
            ).scalars()
        )
        assert {own.sha256, derived.sha256, last.sha256} <= protected
        witnesses = list(
            query(
                tx,
                "SELECT sha256,execution_entry_id FROM evaluator_blob_classifications WHERE project_id=:p",
                p=case.project,
            ).mappings()
        )
        assert {row["sha256"] for row in witnesses} == {derived.sha256, last.sha256}
        assert all(row["execution_entry_id"] == entry for row in witnesses)
    public = Repository(case.repo.database, case.repo.objects, case.repo.actor_id, purpose="generation")
    for identity in (alias, child):
        with pytest.raises(AccessDenied):
            public.authorize_runtime(
                case.project, RuntimeRef(record_kind="tool_execution", record_id=identity)
            )


def test_private_hash_blocks_late_public_aliases_and_protects_late_evaluator_derivative(
    private_scope, monkeypatch
):
    case = private_scope
    body = b"SYNTHETIC already-private bytes"

    def forbidden(*args, **kwargs):
        pytest.fail("Private bytes without generic aliases entered a graph walk")

    with monkeypatch.context() as patch:
        patch.setattr("podgen.persistence.private_journal.privacy.protected_alias_origins", forbidden)
        _entry(case, body)
    with case.repo.database.read_transaction() as tx:
        assert (
            query(
                tx, "SELECT count(*) FROM evaluator_blob_classifications WHERE project_id=:p", p=case.project
            ).scalar_one()
            == 0
        )
    alias_blob = case.repo.save_blob(case.project, io.BytesIO(body), "text/plain")
    alias = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    with pytest.raises(AccessDenied):
        case.repo.append_runtime(case.project, alias, dict(blob=alias_blob.model_dump(mode="json")))
    derivative = case.repo.save_blob(case.project, io.BytesIO(b"SYNTHETIC late derivative"), "text/plain")
    case.repo.append_runtime(
        case.project,
        alias,
        dict(blob=alias_blob.model_dump(mode="json"), derivative=derivative.model_dump(mode="json")),
        audience="evaluator",
    )
    public = Repository(case.repo.database, case.repo.objects, case.repo.actor_id, purpose="generation")
    with pytest.raises(AccessDenied):
        public.save_blob(case.project, io.BytesIO(body), "application/octet-stream")
    with pytest.raises(AccessDenied), public.database.read_transaction() as tx:
        public._check_blob(tx, case.project, derivative)


def test_alias_withdrawal_closes_parent_child_and_semantic_results_without_tainting_borrowed_source(
    private_scope,
):
    case = private_scope
    body = b"SYNTHETIC private receipt for parent propagation"
    parent, owned = _entry(case, body)
    child, _ = _entry(case, parents=(parent,))
    unrelated, _ = _entry(case, b"SYNTHETIC independent receipt")
    alias = case.repo.save_blob(case.project, io.BytesIO(body), "text/plain")
    alias_owner = case.repo.create(
        case.project,
        "Policy",
        dict(policy_kind="data", rules=dict(synthetic_private_alias=alias.model_dump(mode="json"))),
        producer=case.producer,
        operation_id=uuid4(),
        audience="evaluator",
    )
    output_blob = case.repo.save_blob(
        case.project, io.BytesIO(b"SYNTHETIC semantic result bytes"), "text/plain"
    )
    output = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    case.repo.append_runtime(
        case.project, output, dict(blob=output_blob.model_dump(mode="json")), audience="evaluator"
    )
    artifact = case.repo.create(
        case.project,
        "Policy",
        dict(policy_kind="data", rules=dict(semantic="full result")),
        producer=case.producer,
        operation_id=uuid4(),
        audience="evaluator",
    )
    with case.repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO semantic_execution_links(project_id,runtime_record_id,entry_id) VALUES(:p,:r,:e)",
            p=case.project,
            r=output.record_id,
            e=parent,
        )
        query(
            tx,
            "INSERT INTO semantic_execution_links(project_id,artifact_revision_id,entry_id) VALUES(:p,:r,:e)",
            p=case.project,
            r=artifact.ref.revision_id,
            e=child,
        )
    DependencyLedger(case.repo).invalidate(
        case.project, alias_owner.ref, reason="SYNTHETIC private alias withdrawal", category="access"
    )
    assert _denied_entries(case) == {parent, child}
    with case.repo.database.read_transaction() as tx:
        revoked = set(
            query(
                tx, "SELECT sha256 FROM blob_access_revocations WHERE project_id=:p", p=case.project
            ).scalars()
        )
        assert {owned.sha256, output_blob.sha256} <= revoked
        assert case.source_blob.sha256 not in revoked
        entry_privacy(tx, case.project, [unrelated])
        with pytest.raises(AccessDenied):
            entry_privacy(tx, case.project, [child])
    assert case.repo.read_blob(case.project, case.source.ref, case.source_blob)
    with pytest.raises(AccessDenied):
        case.repo.get(case.project, artifact.ref)


def test_later_generation_withdrawal_preserves_earlier_entry(private_scope):
    case = private_scope
    early, early_blob = _entry(case, b"SYNTHETIC earlier generation receipt")
    late_source = case.repo.create(
        case.project,
        "Policy",
        dict(policy_kind="data", rules=dict(extra="late source")),
        producer=case.producer,
        operation_id=uuid4(),
    )
    late_bound = bind(
        case.repo,
        case.project,
        case.run_id,
        [dict(purpose="generation", ref=late_source.ref.model_dump(mode="json"))],
    )
    late, late_blob = _entry(case, b"SYNTHETIC later generation receipt", bound=late_bound)
    DependencyLedger(case.repo).invalidate(
        case.project, late_source.ref, reason="SYNTHETIC late withdrawal", category="access"
    )
    assert _denied_entries(case) == {late}
    refresh(case.repo, case.project, case.bound["set_id"])
    with case.repo.database.read_transaction() as tx:
        authorize(case.repo, tx, case.project, case.bound["set_id"], action=True)
        entry_privacy(tx, case.project, [early])
        revoked = set(
            query(
                tx, "SELECT sha256 FROM blob_access_revocations WHERE project_id=:p", p=case.project
            ).scalars()
        )
        assert late_blob.sha256 in revoked and early_blob.sha256 not in revoked


def test_reused_private_sha_performs_no_alias_query_or_walk(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Reused private bytes entered first-ownership alias classification")

    monkeypatch.setattr("podgen.persistence.private_journal.privacy.query", forbidden)
    monkeypatch.setattr("podgen.persistence.private_journal.privacy.protected_alias_origins", forbidden)
    protect_private_owned(object(), uuid4(), uuid4(), set())


def test_private_first_protection_rollback_leaves_generic_aliases_readable(private_scope):
    case = private_scope
    body = b"SYNTHETIC rolled-back private bytes"
    alias_blob = case.repo.save_blob(case.project, io.BytesIO(body), "text/plain")
    derived = case.repo.save_blob(case.project, io.BytesIO(b"SYNTHETIC public derivative"), "text/plain")
    alias, child = (RuntimeRef(record_kind="tool_execution", record_id=uuid4()) for _ in range(2))
    case.repo.append_runtime(case.project, alias, dict(blob=alias_blob.model_dump(mode="json")))
    case.repo.append_runtime(
        case.project, child, dict(parent=alias.model_dump(mode="json"), blob=derived.model_dump(mode="json"))
    )
    public = Repository(case.repo.database, case.repo.objects, case.repo.actor_id, purpose="generation")
    public.authorize_runtime(case.project, child)
    with pytest.raises(RuntimeError, match="private checkpoint rollback"):
        _entry(case, body, abort=True)
    public.authorize_runtime(case.project, child)
    with case.repo.database.read_transaction() as tx:
        assert (
            query(
                tx, "SELECT count(*) FROM execution_objects WHERE project_id=:p", p=case.project
            ).scalar_one()
            == 0
        )
        assert (
            query(
                tx, "SELECT count(*) FROM execution_entries WHERE project_id=:p", p=case.project
            ).scalar_one()
            == 0
        )
        assert (
            query(
                tx, "SELECT count(*) FROM evaluator_blob_classifications WHERE project_id=:p", p=case.project
            ).scalar_one()
            == 0
        )


@pytest.mark.parametrize("denial", ("entry", "sha"))
def test_flat_semantic_runtime_checks_live_private_denial_without_generic_bytes(private_scope, denial):
    case = private_scope
    entry, owned = _entry(case, b"SYNTHETIC semantic source receipt")
    result = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    case.repo.append_runtime(
        case.project, result, dict(output="SYNTHETIC full semantic result"), audience="evaluator"
    )
    with case.repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO semantic_execution_links(project_id,runtime_record_id,entry_id) VALUES(:p,:r,:e)",
            p=case.project,
            r=result.record_id,
            e=entry,
        )
    bound = bind(
        case.repo, case.project, case.run_id, [dict(purpose="evaluation", ref=result.model_dump(mode="json"))]
    )
    event = DependencyLedger(case.repo).invalidate(
        case.project,
        case.source.ref,
        reason="SYNTHETIC independent quality event for live-flag fixture",
        category="quality",
    )
    refresh(case.repo, case.project, bound["set_id"])
    with case.repo.database.read_transaction() as tx:
        authorize(case.repo, tx, case.project, bound["set_id"])
    with case.repo.database.transaction() as tx:
        if denial == "entry":
            query(
                tx,
                "INSERT INTO execution_entry_denials VALUES(:p,:entry,:event,'SYNTHETIC live entry denial')",
                p=case.project,
                entry=entry,
                event=event,
            )
        else:
            query(
                tx,
                "INSERT INTO blob_access_revocations VALUES(:p,:sha,:event,'SYNTHETIC live SHA denial')",
                p=case.project,
                sha=owned.sha256,
                event=event,
            )
    # No validity bump: stale metadata must not mask the live semantic-link check.
    with pytest.raises(AccessDenied), case.repo.database.read_transaction() as tx:
        authorize(case.repo, tx, case.project, bound["set_id"])
