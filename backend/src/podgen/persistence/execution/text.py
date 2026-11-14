"""Exact prepared parent/blob binding for source text, with live conditions."""

from podgen.domain.canonical import digest
from podgen.domain.errors import AccessDenied
from podgen.persistence.database import one
from podgen.persistence.read_authorization import _read_connection

from .live import authorize
from .reads import current_inputs


@current_inputs
def authorize_text(repo, store, run_id, project, reference, blob):
    row = store.sets[run_id]
    with _read_connection(repo) as tx:
        authorize(repo, tx, project, row["set_id"])
        binding = one(
            tx,
            """SELECT owner_project_id FROM execution_input_resources
            WHERE project_id=:p AND set_id=:s AND purpose='evaluation'
            AND resource_kind='binding' AND identity=:identity AND read_required""",
            p=project,
            s=row["set_id"],
            identity=digest(dict(ref=reference.model_dump(mode="json"), blob=blob.model_dump(mode="json"))),
        )
        if binding is None:
            raise AccessDenied()
        return binding["owner_project_id"]
