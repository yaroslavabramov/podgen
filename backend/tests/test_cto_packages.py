from uuid import uuid4, uuid5

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.domain.packages import ArtifactPackage
from podgen.persistence.database import json_text, one, query
from podgen.persistence.packages import PackageImporter
from podgen.persistence.repository import Repository


def package(nodes=(), runtimes=(), **kwargs):
    return ArtifactPackage.model_validate(
        dict(
            format="podgen-fixture-v1",
            package_id=str(uuid4()),
            test_data=True,
            description="SYNTHETIC CTO regression",
            artifacts=list(nodes),
            runtime_records=list(runtimes),
            blobs=[],
            external_refs={},
            **kwargs,
        )
    )


def runtime(key, payload, audience="project"):
    return dict(key=key, record_kind="tool_execution", payload=payload, audience=audience)


def artifact(key, payload):
    return dict(
        key=key,
        kind="Policy",
        payload=dict(policy_kind="data", rules=payload),
        input_keys=[],
        audience="project",
        identity_of=None,
    )


def counts(repo, project):
    with repo.database.transaction() as tx:
        return tuple(
            one(tx, f"SELECT count(*) AS n FROM {table} WHERE project_id=:p", p=project)["n"]
            for table in ["artifact_revisions", "runtime_records", "operational_records"]
        )


@pytest.mark.parametrize(
    "variant", ["self", "mutual", "mixed", "missing", "duplicate-runtime", "duplicate-artifact"]
)
def test_import_rejects_all_invalid_graphs_before_publication(repo, scope, producer, tmp_path, variant):
    nodes = []
    records = []
    if variant == "self":
        records = [runtime("r", {"ref": {"$runtime": "r"}})]
    if variant == "mutual":
        records = [runtime("r", {"ref": {"$runtime": "s"}}), runtime("s", {"ref": {"$runtime": "r"}})]
    if variant == "mixed":
        nodes = [artifact("a", {"ref": {"$runtime": "r"}})]
        records = [runtime("r", {"ref": {"$artifact": "a"}})]
    if variant == "missing":
        records = [runtime("r", {"ref": {"$runtime": "absent"}})]
    if variant == "duplicate-runtime":
        records = [runtime("r", {"value": 1}), runtime("r", {"value": 2})]
    if variant == "duplicate-artifact":
        nodes = [artifact("a", {"value": 1}), artifact("a", {"value": 2})]
    before = counts(repo, scope[0])
    with pytest.raises(ContractError):
        PackageImporter(repo).import_package(*scope, package(nodes, records), tmp_path, producer=producer)
    assert counts(repo, scope[0]) == before


def test_legal_mixed_dag_is_topologically_imported_and_repeated(repo, scope, producer, tmp_path):
    p = package(
        [artifact("consumer", {"ref": {"$runtime": "r"}}), artifact("origin", {"value": "full text"})],
        [runtime("r", {"origin": {"$artifact": "origin"}})],
    )
    importer = PackageImporter(repo)
    result = importer.import_package(*scope, p, tmp_path, producer=producer)
    assert len(result) == 2
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid5(p.package_id, "runtime:r"))
    assert repo.get_runtime(scope[0], ref)["origin"] == result[0].ref.model_dump(mode="json")
    before = counts(repo, scope[0])
    assert importer.import_package(*scope, p, tmp_path, producer=producer) == result
    assert counts(repo, scope[0]) == before


@pytest.mark.parametrize("variant", ["missing", "foreign", "hidden", "hidden-wrapper"])
def test_external_runtime_access_fails_closed_without_partial_publication(
    repo, scope, producer, tmp_path, variant
):
    foreign = uuid4()
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    with repo.database.transaction() as tx:
        query(tx, "INSERT INTO projects(project_id,namespace_kind) VALUES(:p,'tenant')", p=foreign)
        query(tx, "INSERT INTO project_members VALUES(:p,:a,'owner')", p=foreign, a=repo.actor_id)
    if variant == "foreign":
        repo.append_runtime(foreign, ref, {"private": "foreign"})
    if variant in ("hidden", "hidden-wrapper"):
        repo.append_runtime(scope[0], ref, {"gold": "private evaluator annotation"}, audience="evaluator")
    if variant == "hidden-wrapper":
        wrapper = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        repo.append_runtime(scope[0], wrapper, {"ref": ref.model_dump(mode="json")}, audience="evaluator")
        ref = wrapper
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    p = package(
        [artifact("first", {"innocent": True}), artifact("blocked", {"ref": {"$runtime": "external"}})],
        external_runtime_refs={"external": ref.model_dump(mode="json")},
    )
    before = counts(repo, scope[0])
    with pytest.raises(AccessDenied):
        PackageImporter(generation).import_package(*scope, p, tmp_path, producer=producer)
    assert counts(repo, scope[0]) == before


def test_reads_of_legacy_runtime_cycles_fail_closed(repo, scope):
    ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    with repo.database.transaction() as tx:
        query(
            tx,
            "INSERT INTO runtime_records VALUES(:p,:r,'tool_execution',CAST(:body AS json),'project')",
            p=scope[0],
            r=ref.record_id,
            body=json_text({"ref": ref.model_dump(mode="json")}),
        )
    with pytest.raises(AccessDenied):
        repo.get_runtime(scope[0], ref)
