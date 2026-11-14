"""Scalar compatibility adapter to the common bounded artifact publisher."""

from uuid import uuid4


def create_revision(
    repo,
    tx,
    project_id,
    kind,
    payload,
    *,
    producer,
    operation_id,
    input_refs=(),
    artifact_id=None,
    episode_id=None,
    audience="project",
    streaming=False,
    prepared=None,
):
    from .artifact_batch import publish
    from .publication_plan import ArtifactNode, PublicationBatch

    node = ArtifactNode(
        "artifact",
        kind,
        payload,
        producer,
        operation_id,
        input_refs=tuple(input_refs),
        artifact_id=artifact_id,
        episode_id=episode_id,
        audience=audience,
        streaming=streaming,
        revision_id=uuid4(),
    )
    return publish(repo, tx, project_id, PublicationBatch(artifacts=(node,), prepared=prepared))[node.key]
