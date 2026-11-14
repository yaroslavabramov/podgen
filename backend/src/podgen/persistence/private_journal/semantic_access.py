"""Fresh semantic provenance checks without exposing private execution bytes."""

from podgen.domain.errors import AccessDenied
from podgen.persistence.execution.live import authorize


def authorize_sources(repo, tx, project, sources):
    # Source metadata and denial flags belong to RuntimeFacts' current SQL snapshot.
    # They must never enter the cross-snapshot RuntimeGraph metadata cache.
    if any(source["denied"] for source in sources):
        raise AccessDenied()
    for set_id in {source["input_set_id"] for source in sources}:
        authorize(repo, tx, project, set_id)
