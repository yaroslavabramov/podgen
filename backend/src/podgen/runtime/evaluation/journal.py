"""SQL projection + immutable, evaluator-only full-payload records in WP-01 storage."""

import io
import json
from contextlib import contextmanager
from uuid import uuid4, uuid5

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict
from podgen.persistence.archive_authorization import archive_write
from podgen.persistence.database import one, query
from podgen.persistence.runtime_records import append_runtime

from .attempts import validate_lineage
from .contracts import command_payload


class EvaluationJournal:
    def __init__(self, repo, project):
        self.repo, self.project = repo, project
        from .execution_session import ExecutionSession

        self.execution = ExecutionSession(repo, project)
        from podgen.persistence.private_journal.archive import ExecutionArchiveAdapter

        self.archive = ExecutionArchiveAdapter(repo, project)

    def runtime_graph(self):
        from podgen.persistence.runtime_graph import runtime_graph

        return runtime_graph(self.repo)

    def read_operation(self):
        from podgen.persistence.read_authorization import read_operation

        return read_operation(self.repo)

    def authorize(self, tx):
        if self.repo.purpose != "evaluation" or self.repo._acl(tx, self.project) not in (
            "owner",
            "evaluator",
            "operator",
        ):
            raise AccessDenied()

    @contextmanager
    def lock(self, run_id):
        with self.repo.database.engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as connection:
            self.authorize(connection)
            key = f"writing:{self.project}:{run_id}"
            held = one(connection, "SELECT pg_try_advisory_lock(hashtextextended(:key,0)) AS held", key=key)[
                "held"
            ]
            if not held:
                raise Conflict("evaluation_run_busy")
            try:
                yield
            finally:
                query(connection, "SELECT pg_advisory_unlock(hashtextextended(:key,0))", key=key)

    def blob(self, data, media="application/json"):
        return self.repo.save_blob(self.project, io.BytesIO(data), media)

    def blob_many(self, values):
        from podgen.persistence.blob_batch import save_blobs

        return save_blobs(self.repo, self.project, values)

    def record(self, payload, *, record_id=None, kind="tool_execution"):
        ref = RuntimeRef(record_kind=kind, record_id=record_id or uuid4())

        if payload.get("contract") in (
            "writing-evaluation-output-v1",
            "independent-reading-output/1",
            "writer-material-request/1",
        ):
            from .journal_completion import complete

            complete(self, payload, ref)
        else:

            def write(tx):
                self.authorize(tx)
                append_runtime(self.repo, tx, self.project, ref, payload, "evaluator", replay=True)

            archive_write(self.repo, self.project, write)
        return ref

    def read(self, ref):
        return self.archive.read(ref)

    def authorize_many(self, references):
        return self.archive.authorize_many(references)

    def read_many(self, references):
        return self.archive.read_many(references)

    def event_metadata(self, roots):
        from .history_metadata import stream

        return stream(self, roots)

    def read_blob(self, ref, blob):
        return self.archive.read_blob(ref, blob)

    def text_blob(self, ref, blob):
        from podgen.persistence.text_source import verified_text

        return verified_text(self.repo, self.project, ref, blob)

    def run(self, run_id):
        with self.repo.database.transaction() as tx:
            self.authorize(tx)
            row = one(
                tx,
                "SELECT * FROM evaluation_writing_runs WHERE project_id=:p AND run_id=:r",
                p=self.project,
                r=run_id,
            )
            if row and row["actor_id"] != self.repo.actor_id:
                raise AccessDenied()
        return dict(row) if row else None

    def manifest(self, run_id):
        row = self.run(run_id)
        if row is None:
            raise AccessDenied()
        return self.read(RuntimeRef(record_kind="context_assembly", record_id=row["manifest_id"]))

    def create_run(self, command, manifest):
        fingerprint = digest(command_payload(command))
        previous = self.run(command.run_id)
        if previous:
            if previous["input_fingerprint"] != fingerprint:
                raise Conflict("run_id_reused_with_different_input")
            return self.manifest(command.run_id)
        ref = RuntimeRef(record_kind="context_assembly", record_id=uuid5(command.run_id, "manifest"))

        def write(tx):
            self.authorize(tx)
            append_runtime(self.repo, tx, self.project, ref, manifest, "evaluator", replay=True)
            query(
                tx,
                """INSERT INTO evaluation_writing_runs(project_id,run_id,actor_id,input_fingerprint,manifest_id,status,execution_storage)
                        VALUES(:p,:r,:a,:fp,:m,'prepared','private/3')""",
                p=self.project,
                r=command.run_id,
                a=self.repo.actor_id,
                fp=fingerprint,
                m=ref.record_id,
            )

        archive_write(self.repo, self.project, write)
        return manifest

    def status(self, run_id, status, failure=None):
        with self.repo.database.transaction() as tx:
            self.authorize(tx)
            query(
                tx,
                "UPDATE evaluation_writing_runs SET status=:s,failure_code=:f WHERE project_id=:p AND run_id=:r",
                p=self.project,
                r=run_id,
                s=status,
                f=failure,
            )

    def attempts(self, run_id, stage):
        from podgen.persistence.private_journal.state import attempts

        self.run(run_id)
        rows = attempts(self, run_id, stage)
        validate_lineage(rows, stage)
        return rows

    def invocation(self, run_id, stage, attempt_index=None):
        attempts = self.attempts(run_id, stage)
        return (
            next((row for row in attempts if row["attempt_index"] == attempt_index), None)
            if attempt_index is not None
            else (attempts[-1] if attempts else None)
        )

    def prepare(self, run_id, stage, body, context, **options):
        from .journal_attempts import prepare

        return prepare(self, run_id, stage, body, context, **options)

    def request(self, invocation):
        ref = RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
        from podgen.persistence.read_authorization import read_operation

        with read_operation(self.repo):
            record = self.read(ref)
            return json.loads(self.read_blob(ref, BlobRef.model_validate(record["request_blob"])))

    def publication(self, invocation):
        from .publication import Publication

        return Publication(self, invocation)

    def event_binding(self, invocation, data):
        return invocation, dict(data or {})

    def event(self, invocation, kind, *, reply=None, data=None):
        phase = self.publication(invocation)
        ref = phase.event(kind, reply=reply, data=data)
        phase.commit()
        return ref

    def last_response(self, invocation):
        for event in reversed(self.events(invocation)):
            if event["event_kind"] in ("provider_response", "source_tools_final_response"):
                ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
                record = self.read(ref)
                return self.read_blob(ref, BlobRef.model_validate(record["response_blob"]))
        return None

    def transition(self, invocation, status, **fields):
        if (
            status == invocation["status"]
            and all(
                invocation.get({"provider_id": "provider_response_id"}.get(key, key)) == value
                for key, value in fields.items()
                if key != "output_ref"
            )
            and "output_ref" not in fields
        ):
            return dict(invocation)
        if status == "completed" and fields.get("output_ref") is not None:
            current = self.invocation(
                invocation["run_id"], invocation["stage"], invocation.get("attempt_index", 0)
            )
            if (
                current
                and current["status"] == "completed"
                and current["output_record_id"] == fields["output_ref"].record_id
            ):
                return current
        phase = self.publication(invocation)
        phase.transition(status, **fields)
        return phase.commit()

    def output(self, invocation):
        if not invocation["output_record_id"]:
            raise AccessDenied()
        return self.read(RuntimeRef(record_kind="tool_execution", record_id=invocation["output_record_id"]))

    def events(self, invocation):
        from podgen.persistence.private_journal.state import events

        return events(self, invocation)

    def events_for_turn(self, invocation, turn_index):
        from podgen.persistence.private_journal.state import events

        return events(self, invocation, turn_index=turn_index)
