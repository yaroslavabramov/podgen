"""Exact prepared source bindings batched within the existing read snapshot."""

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import query
from podgen.persistence.read_authorization import _read_connection, authorization_state, checked_node

from .live import authorize
from .reads import current_inputs


def _key(project, set_id, reference, blob):
    return ("execution_text_binding", project, set_id, reference, blob)


@current_inputs
def authorize_texts(repo, store, run_id, project, bindings):
    row = store.sets[run_id]
    with _read_connection(repo) as tx:
        authorize(repo, tx, project, row["set_id"])
        state = authorization_state(repo, tx)
        bindings = tuple(bindings)
        pending = {
            digest(dict(ref=reference.model_dump(mode="json"), blob=blob.model_dump(mode="json"))): (
                reference,
                blob,
            )
            for reference, blob in bindings
            if state is None or _key(project, row["set_id"], reference, blob) not in state.completed
        }
        identities = list(pending)
        for offset in range(0, len(identities), 64):
            selected = identities[offset : offset + 64]
            found = {
                binding["identity"]: binding
                for binding in query(
                    tx,
                    """SELECT identity,binding,owner_project_id
                FROM execution_input_resources WHERE project_id=:p AND set_id=:s AND purpose='evaluation'
                AND resource_kind='binding' AND identity=ANY(CAST(:ids AS text[])) AND read_required""",
                    p=project,
                    s=row["set_id"],
                    ids=selected,
                ).mappings()
            }
            for identity in selected:
                reference, blob = pending[identity]
                binding = found.get(identity)
                if binding is None or binding["binding"] != dict(
                    ref=reference.model_dump(mode="json"), blob=blob.model_dump(mode="json")
                ):
                    raise AccessDenied()
                checked_node(
                    repo,
                    tx,
                    _key(project, row["set_id"], reference, blob),
                    lambda binding=binding: binding["owner_project_id"],
                )

        def missing():
            raise AccessDenied()

        return {
            (reference, blob): checked_node(repo, tx, _key(project, row["set_id"], reference, blob), missing)
            for reference, blob in bindings
        }


@current_inputs
def authorize_text(repo, store, run_id, project, reference, blob):
    return authorize_texts(repo, store, run_id, project, [(reference, blob)])[reference, blob]
