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
    _validate_inputs(repo, tx, project, payload, audience)
    query(
        tx,
        "INSERT INTO runtime_records VALUES(:p,:r,:k,CAST(:body AS json),:a)",
        p=project,
        r=ref.record_id,
        k=ref.record_kind,
        body=json_text(payload),
        a=audience,
    )
    # No authorization facts may survive publication/classification mutations.
    state = repo._read_authorization.get()
    if state is not None and state.transaction is tx:
        state.completed.clear()
    if audience == "evaluator":
        from .classification import classify_evaluator

        classify_evaluator(tx, project, runtime_id=ref.record_id)
    return ref


def _validate_inputs(repo, tx, project, payload, audience):
    """A complete input graph per principal, scoped strictly before the INSERT."""
    from contextlib import ExitStack

    from .access_facts import access_facts
    from .read_authorization import authorized_read
    from .repository import Repository

    readers = [repo]
    if audience == "project":
        readers.append(Repository(repo.database, repo.objects, repo.actor_id, purpose="generation"))
    references = tuple(dict.fromkeys(ref for _, _, ref in walk_values(payload)))
    with ExitStack() as stack:
        for reader in readers:
            stack.enter_context(authorized_read(reader, tx))
            facts = access_facts(reader, tx)
            facts.artifacts.expand([ref.revision_id for ref in references if isinstance(ref, ArtifactRef)])
            facts.runtimes.prefetch(project, [ref for ref in references if isinstance(ref, RuntimeRef)])
            facts.blobs.load(
                project,
                [ref.storage_key for ref in references if not isinstance(ref, (ArtifactRef, RuntimeRef))],
            )
            for linked in references:
                if isinstance(linked, ArtifactRef):
                    reader._resolve(tx, project, linked)
                elif isinstance(linked, RuntimeRef):
                    reader._runtime(tx, project, linked)
                else:
                    facts.blobs.check(project, linked, public_only=audience == "project")
