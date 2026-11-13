"""Stage a bounded group of complete byte values for the common blob publisher."""

import io
from contextlib import ExitStack

from podgen.domain.errors import ContractError

from .blob_publication import publish_staged


def save_blobs(repo, project, values):
    if not values or len(values) > 64:
        raise ContractError("invalid_blob_publication_size")
    with repo.database.transaction() as tx:
        repo._acl(tx, project, write=True)
    with ExitStack() as stack:
        staged = [
            stack.enter_context(repo.objects.stage(project, io.BytesIO(data), media))
            for data, media in values
        ]
        return publish_staged(repo, project, staged)
