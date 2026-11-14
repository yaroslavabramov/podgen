"""Trusted input binder. Roots are immutable; their flat expansion is replaceable metadata."""

from typing import Literal
from uuid import uuid4

from pydantic import ValidationError

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import ArtifactRef, BlobRef, Contract, RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.batch_insert import insert_rows
from podgen.persistence.database import json_text, one, query

from .live import StaleInputs, authorize
from .resources import compile_resources


class InputRoot(Contract):
    purpose: Literal["evaluation", "generation"]
    ref: ArtifactRef | RuntimeRef | BlobRef


def normalize(roots):
    try:
        values = [InputRoot.model_validate(root).model_dump(mode="json") for root in roots]
    except ValidationError as error:
        raise ContractError("invalid_execution_input_root") from error
    if not values:
        raise ContractError("empty_execution_input_roots")
    found = {digest(root): root for root in values}
    return [found[key] for key in sorted(found)]


def get(tx, project, set_id):
    row = one(tx, "SELECT * FROM execution_input_sets WHERE project_id=:p AND set_id=:s", p=project, s=set_id)
    if row is None:
        raise AccessDenied()
    return dict(row)


def replace_expansion(repo, tx, row):
    resources, versions = compile_resources(repo, tx, row["project_id"], normalize(row["roots"]))
    query(
        tx,
        "DELETE FROM execution_input_resources WHERE project_id=:p AND set_id=:s",
        p=row["project_id"],
        s=row["set_id"],
    )
    for offset in range(0, len(resources), 128):
        insert_rows(
            tx,
            "execution_input_resources",
            [
                dict(
                    resource,
                    project_id=row["project_id"],
                    run_id=row["run_id"],
                    set_id=row["set_id"],
                    binding=json_text(resource["binding"]),
                )
                for resource in resources[offset : offset + 128]
            ],
            json_columns=("binding",),
        )
    query(
        tx,
        """INSERT INTO execution_input_expansions VALUES(:p,:r,:s,CAST(:versions AS jsonb),:hash)
          ON CONFLICT(project_id,run_id,set_id) DO UPDATE
          SET owner_versions=EXCLUDED.owner_versions,resource_hash=EXCLUDED.resource_hash""",
        p=row["project_id"],
        r=row["run_id"],
        s=row["set_id"],
        versions=json_text(versions),
        hash=digest(
            [{**resource, "owner_project_id": str(resource["owner_project_id"])} for resource in resources]
        ),
    )


def bind(repo, project, run_id, roots):
    """Only input/admission owners call this; publication never adds unknown refs."""

    def write(tx):
        repo._acl(tx, project, write=True)
        run = one(
            tx,
            "SELECT actor_id FROM evaluation_writing_runs WHERE project_id=:p AND run_id=:r",
            p=project,
            r=run_id,
        )
        if run is None or run["actor_id"] != repo.actor_id or repo.purpose != "evaluation":
            raise AccessDenied()
        previous = one(
            tx,
            """SELECT * FROM execution_input_sets WHERE project_id=:p AND run_id=:r
                    ORDER BY generation DESC LIMIT 1""",
            p=project,
            r=run_id,
        )
        combined = normalize([*(previous["roots"] if previous else []), *roots])
        identity = digest(combined)
        if previous and previous["identity"] == identity:
            row = dict(previous)
            try:
                authorize(repo, tx, project, row["set_id"], action=True)
            except StaleInputs:
                replace_expansion(repo, tx, row)
            return row
        row = dict(
            project_id=project,
            run_id=run_id,
            set_id=uuid4(),
            generation=previous["generation"] + 1 if previous else 0,
            identity=identity,
            roots=combined,
        )
        insert_rows(
            tx, "execution_input_sets", [dict(row, roots=json_text(combined))], json_columns=("roots",)
        )
        replace_expansion(repo, tx, row)
        return row

    return _publish(repo, project, write)


def _publish(repo, project, write):
    retries = {}
    while True:
        try:
            return archive_write(repo, project, write)
        except StaleInputs as stale:
            key = (stale.project, stale.set_id)
            retries[key] = retries.get(key, 0) + 1
            if retries[key] > 3:
                raise
            refresh(repo, stale.project, stale.set_id)


def refresh(repo, project, set_id):
    def write(tx):
        repo._acl(tx, project)
        row = get(tx, project, set_id)
        # Metadata is derived under current complete rules; no original payload,
        # root identity or input generation is modified.
        replace_expansion(repo, tx, row)

    return _publish(repo, project, write)
