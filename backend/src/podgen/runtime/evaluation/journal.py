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

from .contracts import command_payload


class EvaluationJournal:
    def __init__(self, repo, project):
        self.repo, self.project = repo, project

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

    def record(self, payload, *, record_id=None, kind="tool_execution"):
        ref = RuntimeRef(record_kind=kind, record_id=record_id or uuid4())

        def write(tx):
            self.authorize(tx)
            append_runtime(self.repo, tx, self.project, ref, payload, "evaluator", replay=True)

        archive_write(self.repo, self.project, write)
        return ref

    def read(self, ref):
        return self.repo.get_runtime(self.project, ref)

    def read_blob(self, ref, blob):
        return self.repo.read_runtime_blob(self.project, ref, blob)

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
                """INSERT INTO evaluation_writing_runs(project_id,run_id,actor_id,input_fingerprint,manifest_id,status)
                        VALUES(:p,:r,:a,:fp,:m,'prepared')""",
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
        self.run(run_id)
        with self.repo.database.transaction() as tx:
            self.authorize(tx)
            rows = [
                dict(row)
                for row in query(
                    tx,
                    "SELECT * FROM evaluation_model_invocations WHERE project_id=:p AND run_id=:r AND stage=:s ORDER BY attempt_index",
                    p=self.project,
                    r=run_id,
                    s=stage,
                ).mappings()
            ]
            originals = {row["invocation_id"] for row in rows if row["attempt_index"] == 0}
            if any(
                row["attempt_index"] == 1
                and (stage != "review" or row["parent_invocation_id"] not in originals)
                for row in rows
            ):
                raise Conflict("invalid_review_repair_parent")
            return rows

    def invocation(self, run_id, stage, attempt_index=None):
        attempts = self.attempts(run_id, stage)
        return (
            next((row for row in attempts if row["attempt_index"] == attempt_index), None)
            if attempt_index is not None
            else (attempts[-1] if attempts else None)
        )

    def prepare(self, run_id, stage, body, context, *, attempt_index=0, parent_invocation_id=None):
        existing = self.invocation(run_id, stage, attempt_index)
        if existing:
            return existing
        if attempt_index:
            original = self.invocation(run_id, stage, 0)
            if (
                attempt_index != 1
                or stage != "review"
                or original is None
                or parent_invocation_id != original["invocation_id"]
                or original["status"] != "validation_failed"
            ):
                raise Conflict("invalid_review_repair_parent")
        invocation_id = uuid5(run_id, "invocation:" + stage + (":anchor-repair:1" if attempt_index else ""))
        blob = self.blob(
            json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        )
        ref = RuntimeRef(record_kind="tool_execution", record_id=uuid5(invocation_id, "request"))
        record = dict(
            contract="model-request-v1",
            invocation_id=str(invocation_id),
            run_id=str(run_id),
            stage=stage,
            request_blob=blob.model_dump(mode="json"),
            request_hash=blob.sha256,
            context=context,
        )

        def write(tx):
            self.authorize(tx)
            append_runtime(self.repo, tx, self.project, ref, record, "evaluator", replay=True)
            query(
                tx,
                """INSERT INTO evaluation_model_invocations(project_id,run_id,invocation_id,stage,status,request_record_id,attempt_index,parent_invocation_id)
                        VALUES(:p,:r,:i,:s,'prepared',:request,:attempt,:parent)""",
                p=self.project,
                r=run_id,
                i=invocation_id,
                s=stage,
                request=ref.record_id,
                attempt=attempt_index,
                parent=parent_invocation_id,
            )

        archive_write(self.repo, self.project, write)
        return self.invocation(run_id, stage, attempt_index)

    def request(self, invocation):
        ref = RuntimeRef(record_kind="tool_execution", record_id=invocation["request_record_id"])
        record = self.read(ref)
        return json.loads(self.read_blob(ref, BlobRef.model_validate(record["request_blob"])))

    def event(self, invocation, kind, *, reply=None, data=None):
        payload = dict(
            contract="model-invocation-event-v1",
            invocation_id=str(invocation["invocation_id"]),
            event_kind=kind,
            request_ref=RuntimeRef(
                record_kind="tool_execution", record_id=invocation["request_record_id"]
            ).model_dump(mode="json"),
            data=data or {},
        )
        if reply:
            blob = self.blob(reply.body)
            payload.update(
                response_blob=blob.model_dump(mode="json"),
                provider_http_status=reply.status_code,
                provider_request_id=reply.request_id,
                secret_redacted=reply.redacted,
            )
            if getattr(reply, "request_body", None) is not None:
                wire = self.blob(reply.request_body)
                payload["wire_request_blob"] = wire.model_dump(mode="json")
        ref = RuntimeRef(record_kind="tool_execution", record_id=uuid4())

        def write(tx):
            self.authorize(tx)
            append_runtime(self.repo, tx, self.project, ref, payload, "evaluator")
            query(
                tx,
                "INSERT INTO evaluation_invocation_events(project_id,invocation_id,event_id,record_id,event_kind) VALUES(:p,:i,:e,:r,:k)",
                p=self.project,
                i=invocation["invocation_id"],
                e=uuid4(),
                r=ref.record_id,
                k=kind,
            )
            if data and data.get("provider_response_id"):
                query(
                    tx,
                    "UPDATE evaluation_model_invocations SET provider_response_id=:id WHERE project_id=:p AND invocation_id=:i",
                    p=self.project,
                    i=invocation["invocation_id"],
                    id=data["provider_response_id"],
                )

        archive_write(self.repo, self.project, write)
        return ref

    def last_response(self, invocation):
        for event in reversed(self.events(invocation)):
            if event["event_kind"] == "provider_response":
                ref = RuntimeRef(record_kind="tool_execution", record_id=event["record_id"])
                record = self.read(ref)
                return self.read_blob(ref, BlobRef.model_validate(record["response_blob"]))
        return None

    def transition(
        self, invocation, status, *, provider_id=None, input_tokens=None, output_tokens=None, output_ref=None
    ):
        with self.repo.database.transaction() as tx:
            self.authorize(tx)
            query(
                tx,
                """UPDATE evaluation_model_invocations SET status=:s,provider_response_id=COALESCE(:provider,provider_response_id),
                         input_tokens=COALESCE(:input,input_tokens),output_tokens=COALESCE(:output,output_tokens),output_record_id=COALESCE(:ref,output_record_id)
                         WHERE project_id=:p AND invocation_id=:i""",
                p=self.project,
                i=invocation["invocation_id"],
                s=status,
                provider=provider_id,
                input=input_tokens,
                output=output_tokens,
                ref=output_ref.record_id if output_ref else None,
            )
            return self.invocation_after(tx, invocation)

    def invocation_after(self, tx, invocation):
        return dict(
            one(
                tx,
                "SELECT * FROM evaluation_model_invocations WHERE project_id=:p AND invocation_id=:i",
                p=self.project,
                i=invocation["invocation_id"],
            )
        )

    def output(self, invocation):
        if not invocation["output_record_id"]:
            raise AccessDenied()
        return self.read(RuntimeRef(record_kind="tool_execution", record_id=invocation["output_record_id"]))

    def events(self, invocation):
        with self.repo.database.transaction() as tx:
            self.authorize(tx)
            rows = query(
                tx,
                "SELECT * FROM evaluation_invocation_events WHERE project_id=:p AND invocation_id=:i ORDER BY sequence",
                p=self.project,
                i=invocation["invocation_id"],
            ).mappings()
            return [dict(row) for row in rows]
