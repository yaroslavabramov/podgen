"""The loaded privacy component has complete reachability before deny decisions."""

from uuid import uuid4

from podgen.persistence.access_facts import access_facts
from podgen.persistence.dependencies import DependencyLedger
from podgen.persistence.read_authorization import _read_connection, read_operation


def test_cycle_does_not_cache_an_incomplete_negative_privacy_result(repo, scope, producer):
    project, _ = scope
    a, b, c = [
        repo.create(
            project,
            "Policy",
            dict(policy_kind="data", rules={"node": name}),
            producer=producer,
            operation_id=uuid4(),
        )
        for name in ("a", "b", "c")
    ]
    with repo.database.transaction() as tx:
        repo._edge(tx, project, a.ref.revision_id, b.ref.revision_id, "uses_content")
        repo._edge(tx, project, b.ref.revision_id, a.ref.revision_id, "uses_content")
        repo._edge(tx, project, a.ref.revision_id, c.ref.revision_id, "uses_content")
    with read_operation(repo), _read_connection(repo) as tx:
        facts = access_facts(repo, tx)
        assert facts.artifacts.invalid(facts.artifacts.row(a.ref)) is False
        assert facts.artifacts.invalid(facts.artifacts.row(b.ref)) is False
    DependencyLedger(repo).invalidate(project, c.ref, reason="Test descendant withdrawal", category="access")
    with read_operation(repo), _read_connection(repo) as tx:
        facts = access_facts(repo, tx)
        assert facts.artifacts.invalid(facts.artifacts.row(b.ref)) is True
        assert facts.artifacts.invalid(facts.artifacts.row(a.ref)) is True
        assert set(facts.artifacts.rows) == {a.ref.revision_id, b.ref.revision_id, c.ref.revision_id}
