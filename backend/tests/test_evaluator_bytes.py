import io
from uuid import uuid4

import pytest

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import AccessDenied
from podgen.persistence.repository import Repository
from tests.factories import Factory


@pytest.mark.parametrize("closed_owner", ["runtime", "artifact"])
def test_gold_bytes_stay_evaluator_only_through_existing_new_wrappers_and_dedup(
    repo, scope, producer, closed_owner
):
    f = Factory(repo, *scope, producer).setup()
    author, author_blob = f.source(
        "SYNTHETIC allowed author source. Gold may cite this source without hiding it."
    )
    raw = b"SYNTHETIC evaluator-only annotation or script bytes"
    gold = repo.save_blob(f.project, io.BytesIO(raw), "text/plain")
    # These public owners predate the closed classification and must also become inaccessible to generation.
    old_wrapper = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    repo.append_runtime(f.project, old_wrapper, {"original_blob": gold.model_dump(mode="json")})
    alias = f.make(
        "SourceRevision",
        author.payload | {"source_id": str(uuid4()), "original_blob": gold.model_dump(mode="json")},
    )
    derived_blob = repo.save_blob(
        f.project, io.BytesIO(b"SYNTHETIC derivative through former public alias"), "text/plain"
    )
    f.make(
        "Policy",
        dict(
            policy_kind="data",
            rules={"input": alias.ref.model_dump(mode="json"), "blob": derived_blob.model_dump(mode="json")},
        ),
    )
    derived_alias = f.make(
        "SourceRevision",
        author.payload | {"source_id": str(uuid4()), "original_blob": derived_blob.model_dump(mode="json")},
    )
    if closed_owner == "runtime":
        root = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
        repo.append_runtime(
            f.project,
            root,
            {
                "original_blob": gold.model_dump(mode="json"),
                "public_basis": author.ref.model_dump(mode="json"),
            },
            audience="evaluator",
        )
        assert repo.read_runtime_blob(f.project, root, gold) == raw
    else:
        root = f.make(
            "SourceRevision",
            author.payload | {"source_id": str(uuid4()), "original_blob": gold.model_dump(mode="json")},
            audience="evaluator",
        )
        assert repo.read_blob(f.project, root.ref, gold) == raw
    generation = Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
    with pytest.raises(AccessDenied):
        generation.get_runtime(f.project, old_wrapper)
    with pytest.raises(AccessDenied):
        generation.read_runtime_blob(f.project, old_wrapper, gold)
    for artifact, blob in [(alias, gold), (derived_alias, derived_blob)]:
        with pytest.raises(AccessDenied):
            generation.get(f.project, artifact.ref)
        with pytest.raises(AccessDenied):
            generation.read_blob(f.project, artifact.ref, blob)
    for writer in (repo, generation):
        with pytest.raises(AccessDenied):
            writer.append_runtime(
                f.project,
                RuntimeRef(record_kind="tool_execution", record_id=uuid4()),
                {"blob": gold.model_dump(mode="json")},
            )
        with pytest.raises(AccessDenied):
            writer.create(
                f.project,
                "SourceRevision",
                author.payload | {"source_id": str(uuid4()), "original_blob": gold.model_dump(mode="json")},
                producer=producer,
                operation_id=uuid4(),
            )
    with pytest.raises(AccessDenied):
        generation.save_blob(f.project, io.BytesIO(raw), "application/octet-stream")
    # Evaluation can retain/reuse these bytes, but cannot lower their classification.
    same = repo.save_blob(f.project, io.BytesIO(raw), "application/octet-stream")
    private = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    repo.append_runtime(f.project, private, {"blob": same.model_dump(mode="json")}, audience="evaluator")
    assert repo.read_runtime_blob(f.project, private, same) == raw
    with pytest.raises(AccessDenied):
        generation.read_runtime_blob(f.project, private, same)
    # Public source is an INPUT to the hidden record, not owned gold output bytes.
    assert generation.read_blob(f.project, author.ref, author_blob).startswith(
        b"SYNTHETIC allowed author source"
    )
    public_wrapper = RuntimeRef(record_kind="tool_execution", record_id=uuid4())
    generation.append_runtime(f.project, public_wrapper, {"blob": author_blob.model_dump(mode="json")})
    assert generation.read_runtime_blob(f.project, public_wrapper, author_blob)
