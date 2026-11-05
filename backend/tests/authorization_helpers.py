"""Use the normal WP-01 invalidation ledger to withdraw exact bytes in engineering tests."""

from uuid import uuid4

from podgen.domain.contracts.common import CodeProducer
from podgen.persistence.dependencies import DependencyLedger


def withdraw_blob(repo, project, blob):
    owner = repo.create(
        project,
        "Policy",
        dict(policy_kind="data", rules={"synthetic_owned_blob": blob.model_dump(mode="json")}),
        producer=CodeProducer(
            kind="code", component="synthetic-access-test", build_id="test-only", operation_id=uuid4()
        ),
        operation_id=uuid4(),
    )
    return DependencyLedger(repo).invalidate(
        project, owner.ref, reason="SYNTHETIC source withdrawal", category="access"
    )
