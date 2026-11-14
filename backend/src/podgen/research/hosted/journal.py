"""Project-visible immutable research records in the production repository."""

import io
from contextlib import contextmanager
from copy import deepcopy
from uuid import uuid5

from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import Conflict
from podgen.persistence.database import json_text, one, query
from podgen.persistence.runtime_records import append_runtime


class ResearchJournal:
    def __init__(self, repo, project, run_id):
        self.repo, self.project, self.run_id = repo, project, run_id

    def ref(self, key):
        return RuntimeRef(
            record_kind="tool_execution", record_id=uuid5(self.run_id, "hosted-research:" + key)
        )

    def get(self, key):
        ref = self.ref(key)
        from podgen.persistence.read_authorization import _read_connection, read_operation

        with read_operation(self.repo):
            with _read_connection(self.repo) as tx:
                self.repo._acl(tx, self.project)
                found = one(
                    tx,
                    "SELECT record_id FROM runtime_records WHERE project_id=:p AND record_id=:r",
                    p=self.project,
                    r=ref.record_id,
                )
            return self.repo.get_runtime(self.project, ref) if found else None

    def put(self, key, value):
        with self.repo.database.transaction() as tx:
            append_runtime(self.repo, tx, self.project, self.ref(key), value, "project", replay=True)
        return self.ref(key)

    def create_manifest(self, value):
        from .projection import empty

        with self.repo.database.transaction() as tx:
            append_runtime(self.repo, tx, self.project, self.ref("manifest"), value, "project", replay=True)
            self._insert_head(tx, empty())
        return self.ref("manifest")

    def _insert_head(self, tx, value):
        query(
            tx,
            """INSERT INTO hosted_research_heads(project_id,run_id,manifest_record_id,revision,projection)
            VALUES(:p,:r,:manifest,:revision,CAST(:body AS jsonb)) ON CONFLICT DO NOTHING""",
            p=self.project,
            r=self.run_id,
            manifest=self.ref("manifest").record_id,
            revision=value["revision"],
            body=json_text(value),
        )

    def blob(self, body, media="application/json"):
        return self.repo.save_blob(self.project, io.BytesIO(body), media).model_dump(mode="json")

    def bytes(self, key, blob):
        return self.repo.read_runtime_blob(self.project, self.ref(key), BlobRef.model_validate(blob))

    def head(self):
        from podgen.persistence.read_authorization import _read_connection, read_operation

        from .projection import CONTRACT

        with read_operation(self.repo):
            with _read_connection(self.repo) as tx:
                self.repo._acl(tx, self.project)
                row = one(
                    tx,
                    "SELECT manifest_record_id,projection FROM hosted_research_heads WHERE project_id=:p AND run_id=:r",
                    p=self.project,
                    r=self.run_id,
                )
            if row and (
                row["manifest_record_id"] != self.ref("manifest").record_id
                or row["projection"].get("contract") != CONTRACT
            ):
                raise Conflict("hosted_research_head_binding_changed")
            if row is None:
                return None
            value = row["projection"]
            keys = {"manifest"}
            if value["current"]:
                keys.add(value["current"]["key"] + ":receipt")
            if value["last_exchange"]:
                last = value["last_exchange"]
                keys.add(last["key"] + (":receipt" if last["receipt"] else ":intent"))
            # Fresh, bounded closures, without fetching any previous HTTP body.
            self.repo.authorize_runtime_many(self.project, [self.ref(key) for key in sorted(keys)])
            return value

    def adopt_head(self, value):
        # Called once, under the run lock, after replaying an older archive.
        with self.repo.database.transaction() as tx:
            # Rebuilding a derived projection does not authorize new research.
            # Readers retain status/export access to older project-visible runs.
            self.repo._acl(tx, self.project)
            self._insert_head(tx, value)
        return self.head()

    def exchange(self, key, value, previous, updated):
        """The immutable exchange and its bounded projection commit together."""
        with self.repo.database.transaction() as tx:
            append_runtime(self.repo, tx, self.project, self.ref(key), value, "project", replay=True)
            if updated["job_recorded"] and not previous["job_recorded"]:
                self._append_job(tx, updated)
            self._update_head(tx, previous, updated)
        return self.ref(key)

    def _append_job(self, tx, value):
        append_runtime(
            self.repo,
            tx,
            self.project,
            self.ref("job"),
            dict(
                provider_job_id=value["current"]["id"],
                manifest_ref=self.ref("manifest").model_dump(mode="json"),
            ),
            "project",
            replay=True,
        )

    def _update_head(self, tx, previous, updated):
        row = one(
            tx,
            """UPDATE hosted_research_heads SET revision=:next,projection=CAST(:body AS jsonb)
                WHERE project_id=:p AND run_id=:r AND revision=:previous RETURNING revision""",
            p=self.project,
            r=self.run_id,
            previous=previous["revision"],
            next=updated["revision"],
            body=json_text(updated),
        )
        if row is None:
            raise Conflict("hosted_research_head_changed")

    def ensure_job(self, previous):
        if not previous["current"] or previous["job_recorded"] or previous.get("integrity_error"):
            return previous
        updated = deepcopy(previous)
        updated.update(revision=previous["revision"] + 1, job_recorded=True)
        with self.repo.database.transaction() as tx:
            self._append_job(tx, updated)
            self._update_head(tx, previous, updated)
        return updated

    @contextmanager
    def lock(self, *, write=True):
        with self.repo.database.engine.connect().execution_options(isolation_level="AUTOCOMMIT") as tx:
            self.repo._acl(tx, self.project, write=write)
            key = f"hosted-research:{self.project}:{self.run_id}"
            if not one(tx, "SELECT pg_try_advisory_lock(hashtextextended(:key,0)) AS held", key=key)["held"]:
                raise Conflict("hosted_research_run_busy")
            try:
                yield
            finally:
                query(tx, "SELECT pg_advisory_unlock(hashtextextended(:key,0))", key=key)
