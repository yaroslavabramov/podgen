"""Even a hash-bound package alias cannot re-admit bytes of an excluded fixed plan."""

from uuid import NAMESPACE_URL, uuid5

import pytest

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.editorial_staging import STAGING_IMPORTER_VERSION
from podgen.persistence.repository import Repository
from podgen.persistence.writing_sources import own_plan_sources
from tests.test_own_plan import own_dataset as own_dataset


def test_fixed_plan_byte_alias_under_new_package_allowed_path_is_rejected_without_body_read(
    own_dataset, monkeypatch
):
    repo, project, report, _, _ = own_dataset
    fixed = repo.get_runtime(
        project, RuntimeRef.model_validate(report["file_refs"]["author/gps-clock/plan.md"])
    )
    path = "author/gps-clock/dossier.md"
    package_hash = digest({path: fixed["original_blob"]["sha256"]})
    identity = uuid5(
        NAMESPACE_URL, f"podgen-editorial-staging-import-{STAGING_IMPORTER_VERSION}:" + package_hash
    )
    alias = RuntimeRef(record_kind="user_input", record_id=uuid5(identity, path))
    root = RuntimeRef(record_kind="user_input", record_id=uuid5(identity, "author:gps-clock"))
    index = RuntimeRef(record_kind="user_input", record_id=uuid5(identity, "evaluator"))
    repo.append_runtime(project, alias, dict(fixed, source_path=path))
    repo.append_runtime(
        project,
        root,
        dict(
            contract="editorial-staging-author-v1",
            case_id="gps-clock",
            technical_fixture=True,
            semantic_acceptance=False,
            input_refs=[alias.model_dump(mode="json")],
        ),
    )
    repo.append_runtime(
        project,
        index,
        dict(
            contract="editorial-staging-evaluator-v1",
            package_hash=package_hash,
            files={path: alias.model_dump(mode="json")},
        ),
        audience="evaluator",
    )
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    monkeypatch.setattr(
        generation, "get_runtime", lambda *args: pytest.fail("selector must not open excluded/alias bodies")
    )
    with pytest.raises(AccessDenied):
        own_plan_sources(generation, project, root, "gps-clock")
