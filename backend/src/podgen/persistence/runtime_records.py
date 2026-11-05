"""One append boundary for immutable runtime records, including package and staging imports."""

from podgen.domain.canonical import canonical_bytes
from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.references import walk_values

from .database import json_text, one, query


def append_runtime(repo, tx, project, ref, payload, audience="project", *, replay=False):
    repo._acl(tx, project, write=True)
    if audience not in ("project", "evaluator"):
        raise ContractError("invalid_audience")
    if audience == "evaluator" and (
        repo.purpose != "evaluation" or repo._acl(tx, project) not in ("owner", "evaluator", "operator")
    ):
        raise AccessDenied()
    canonical_bytes(payload)
    existing = one(
        tx, "SELECT * FROM runtime_records WHERE project_id=:p AND record_id=:r", p=project, r=ref.record_id
    )
    if existing:
        if (
            not replay
            or existing["record_kind"] != ref.record_kind
            or existing["payload"] != payload
            or existing["audience"] != audience
        ):
            raise Conflict("runtime_id_reused_with_different_input")
        repo._runtime(tx, project, ref)
        return ref
    from .repository import Repository

    public_reader = (
        Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
        if audience == "project"
        else None
    )
    for _, _, linked in walk_values(payload):
        if isinstance(linked, ArtifactRef):
            repo._resolve(tx, project, linked)
            if public_reader:
                public_reader._resolve(tx, project, linked)
        elif isinstance(linked, RuntimeRef):
            repo._runtime(tx, project, linked)
            if public_reader:
                public_reader._runtime(tx, project, linked)
        else:
            repo._check_blob(tx, project, linked, public_only=audience == "project")
    query(
        tx,
        "INSERT INTO runtime_records VALUES(:p,:r,:k,CAST(:body AS json),:a)",
        p=project,
        r=ref.record_id,
        k=ref.record_kind,
        body=json_text(payload),
        a=audience,
    )
    if audience == "evaluator":
        from .classification import classify_evaluator

        classify_evaluator(tx, project, runtime_id=ref.record_id)
    return ref
