from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from uuid import UUID

from podgen.domain.canonical import content_hash
from podgen.domain.contracts import ArtifactRef, ArtifactRevision
from podgen.domain.errors import AccessDenied, ContractError

from .database import Database, one, query
from .execution.reads import current_inputs
from .read_authorization import _read_connection, authorized_read, checked_node

CONFIG_KINDS = {
    "PolicyBundle",
    "Policy",
    "PromptDefinition",
    "ToolsetDefinition",
    "ModelProfile",
    "ProcessingProfile",
    "VoiceProfile",
    "PronunciationLexicon",
}
HIDDEN_KINDS = {"EvaluationCase", "EvaluationAnnotation", "EvaluationResult", "HumanAssessment"}
WRITE_ROLES = {"owner", "editor", "evaluator", "operator"}


def timestamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Repository:
    """One authenticated principal per instance; all operations recheck live ACLs."""

    def __init__(
        self,
        database: Database,
        object_store,
        actor_id: UUID,
        *,
        purpose="generation",
        inline_limit=16 * 1024 * 1024,
    ):
        if purpose not in ("generation", "evaluation"):
            raise ValueError("invalid_access_purpose")
        self.database, self.objects, self.actor_id = database, object_store, actor_id
        self.purpose, self.inline_limit = purpose, inline_limit
        self._read_authorization = ContextVar("podgen_read_authorization", default=None)
        self._runtime_graph = ContextVar("podgen_runtime_graph", default=None)
        self._read_path = ContextVar("podgen_read_path", default=frozenset())

    @contextmanager
    def _reading(self, kind, identity):
        key = (kind, identity)
        path = self._read_path.get()
        if key in path:
            raise AccessDenied()
        token = self._read_path.set(path | {key})
        try:
            yield
        finally:
            self._read_path.reset(token)

    def _acl(self, tx, project_id, *, write=False, operator=False):
        from .access_facts import access_facts

        facts = access_facts(self, tx)
        if facts is not None:
            member = facts.role(project_id)
        else:
            member = one(
                tx,
                "SELECT role FROM project_members WHERE project_id=:p AND actor_id=:a",
                p=project_id,
                a=self.actor_id,
            )
        return self._require_role(member["role"] if member else None, write=write, operator=operator)

    @staticmethod
    def _require_role(role, *, write=False, operator=False):
        if role is None or (write and role not in WRITE_ROLES) or (operator and role != "operator"):
            raise AccessDenied()
        return role

    def _lock(self, tx, project_id):
        state = self._read_authorization.get()
        if state is not None and state.transaction is tx and project_id in state.write_projects:
            return
        # A write fence, not a topology change: waiting RR writers must retry
        # rather than validate against a snapshot predating first SHA protection.
        query(tx, "UPDATE projects SET validity_version=validity_version WHERE project_id=:p", p=project_id)
        if state is not None and state.transaction is tx:
            state.write_projects.add(project_id)

    def _validity(self, tx, project_id):
        return one(tx, "SELECT validity_version FROM projects WHERE project_id=:p", p=project_id)[
            "validity_version"
        ]

    def _bump(self, tx, project_id):
        query(tx, "UPDATE projects SET validity_version=validity_version+1 WHERE project_id=:p", p=project_id)

    def _row(self, tx, ref):
        from .scope import ScopeAccess

        return ScopeAccess(self).row(tx, ref)

    def _publication(self, tx, consumer_project, row):
        from .scope import ScopeAccess

        return ScopeAccess(self).publication(tx, consumer_project, row)

    def _access_invalid(self, tx, row):
        from .scope import ScopeAccess

        return ScopeAccess(self).access_invalid(tx, row)

    def _authorize_artifact(self, tx, project_id, ref, seen=None):
        from .scope import ScopeAccess

        with authorized_read(self, tx), self._reading("artifact", (project_id, ref.revision_id)):
            key = ("artifact", project_id, ref.kind, ref.artifact_id, ref.revision_id)
            return checked_node(self, tx, key, lambda: ScopeAccess(self).resolve(tx, project_id, ref, seen))

    def _resolve(self, tx, project_id, ref, seen=None):
        self._authorize_artifact(tx, project_id, ref, seen)
        return self._row(tx, ref)

    def _envelope(self, row):
        return ArtifactRevision(
            ref=ArtifactRef(artifact_id=row["artifact_id"], revision_id=row["revision_id"], kind=row["kind"]),
            schema_version=row["schema_version"],
            project_id=row["project_id"],
            episode_id=row["episode_id"],
            input_refs=row["input_refs"],
            payload=row["payload"],
            content_hash=row["content_hash"],
            producer=row["producer"],
            created_at=row["created_at"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        )

    def _verify_hash(self, tx, row):
        from .access_facts import access_facts

        facts = access_facts(self, tx)
        if facts is not None:
            facts.artifacts.load([row["revision_id"]])
            blob_hashes = facts.artifacts.integrity_blobs[row["revision_id"]]
        else:
            blobs = query(
                tx,
                """SELECT a.purpose,b.sha256 FROM artifact_blobs a JOIN blobs b USING(project_id,storage_key)
                                WHERE a.project_id=:p AND a.revision_id=:r""",
                p=row["project_id"],
                r=row["revision_id"],
            ).mappings()
            blob_hashes = [dict(purpose=b["purpose"], hash=b["sha256"]) for b in blobs]
        actual = content_hash(
            row["kind"],
            row["schema_version"],
            row["payload"],
            [ArtifactRef.model_validate(v) for v in row["input_refs"]],
            blob_hashes,
        )
        if actual != row["content_hash"]:
            raise ContractError("artifact_integrity_error")

    @current_inputs
    def get(self, project_id, ref):
        with _read_connection(self) as tx:
            row = self._resolve(tx, project_id, ref)
            self._verify_hash(tx, row)
            return self._envelope(row)

    def save_blob(self, project_id, stream, media_type, **kwargs):
        from .blob_publication import save_blob

        return save_blob(self, project_id, stream, media_type, **kwargs)

    def _check_blob(self, tx, project_id, ref, *, public_only=False):
        from .access_facts import access_facts
        from .blob_publication import check_identity

        facts = access_facts(self, tx)
        if facts is not None:
            facts.blobs.check(project_id, ref, public_only=public_only)
            return
        check_identity(self, tx, project_id, ref.sha256, public_only=public_only)

        row = one(
            tx, "SELECT * FROM blobs WHERE project_id=:p AND storage_key=:k", p=project_id, k=ref.storage_key
        )
        from .blob_access import validate_binding

        validate_binding(ref, row)

    @current_inputs
    def read_blob(self, project_id, artifact_ref, blob_ref):
        from .read_authorization import object_io
        from .text_source import authorized_blob

        with authorized_blob(self, project_id, artifact_ref, blob_ref):
            with object_io(self):
                value = self.objects.read(project_id, blob_ref)
        return value

    def create(
        self,
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
    ):
        from .artifact_preparation import prepare

        prepared = prepare(self, project_id, kind, payload, audience=audience)
        with self.database.transaction() as tx:
            return self._create(
                tx,
                project_id,
                kind,
                payload,
                prepared=prepared,
                producer=producer,
                operation_id=operation_id,
                input_refs=input_refs,
                artifact_id=artifact_id,
                episode_id=episode_id,
                audience=audience,
            )

    def create_from_stream(self, project_id, kind, stream, **kwargs):
        """Full JSON document route; the inline transport limit never truncates meaning."""
        import json
        import tempfile

        maximum = 256 * 1024 * 1024
        with tempfile.TemporaryFile() as staged:
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > maximum:
                    raise ContractError(
                        "payload_too_large", "Artifact document exceeds the 256 MiB document limit."
                    )
                staged.write(chunk)
            staged.seek(0)

            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ContractError("duplicate_json_key")
                    result[key] = value
                return result

            payload = json.load(staged, object_pairs_hook=unique)
        from .artifact_preparation import prepare

        prepared = prepare(self, project_id, kind, payload, audience=kwargs.get("audience", "project"))
        with self.database.transaction() as tx:
            return self._create(tx, project_id, kind, payload, streaming=True, prepared=prepared, **kwargs)

    def _create(self, tx, project_id, kind, payload, **kwargs):
        from .artifact_writer import create_revision

        return create_revision(self, tx, project_id, kind, payload, **kwargs)

    def _edge(self, tx, project_id, consumer_id, input_id, relation):
        query(
            tx,
            """INSERT INTO artifact_dependencies VALUES(:p,:c,:i,:rel,'{}'::jsonb) ON CONFLICT DO NOTHING""",
            p=project_id,
            c=consumer_id,
            i=input_id,
            rel=relation,
        )

    def _runtime(self, tx, project_id, ref):
        """Authorize a runtime closure and return only its short immutable header."""
        from .scope import ScopeAccess

        with authorized_read(self, tx), self._reading("runtime", (project_id, ref.record_id)):
            key = ("runtime", project_id, ref.record_kind, ref.record_id)
            return checked_node(self, tx, key, lambda: ScopeAccess(self).runtime(tx, project_id, ref))

    def append_runtime(self, project_id, ref, payload, audience="project"):
        from .runtime_records import append_runtime

        with self.database.transaction() as tx:
            return append_runtime(self, tx, project_id, ref, payload, audience)

    def authorize_runtime(self, project_id, ref):
        """Freshly authorize a frozen reference without fetching its semantic body."""
        self.authorize_runtime_many(project_id, [ref])

    @current_inputs
    def authorize_runtime_many(self, project_id, references):
        from .access_facts import access_facts

        with _read_connection(self) as tx:
            facts = access_facts(self, tx)
            facts.runtimes.prefetch(project_id, references)
            from .execution.parents import authorize_parents

            authorize_parents(
                self,
                tx,
                project_id,
                [
                    ref.record_id
                    for ref in references
                    if facts.runtimes.rows.get((project_id, ref.record_id), {}).get("execution_input_set_id")
                    is not None
                ],
            )
            for reference in references:
                self._runtime(tx, project_id, reference)

    @current_inputs
    def get_runtime(self, project_id, ref):
        with _read_connection(self) as tx:
            self._runtime(tx, project_id, ref)
            from .access_facts import access_facts

            facts = access_facts(self, tx)
            return facts.runtimes.payload(project_id, ref)

    @current_inputs
    def get_runtime_many(self, project_id, references):
        """Read at most 64 requested bodies in order, preserving duplicate references.

        Authorize every node before fetching any body. Facts contain only graph
        metadata; returned payloads belong to this caller, not the snapshot memo.
        This local operation exposes neither its connection nor a blob-I/O lease.
        """
        from .access_facts import access_facts

        references = tuple(references)
        if len(references) > 64:
            raise ContractError("runtime_read_batch_too_large")
        if not references:
            return []
        with _read_connection(self) as tx:
            facts = access_facts(self, tx)
            facts.runtimes.prefetch(project_id, references)
            from .execution.parents import authorize_parents

            authorize_parents(
                self,
                tx,
                project_id,
                [
                    ref.record_id
                    for ref in references
                    if facts.runtimes.rows.get((project_id, ref.record_id), {}).get("execution_input_set_id")
                    is not None
                ],
            )
            for reference in references:
                self._runtime(tx, project_id, reference)
            payloads = {
                row["record_id"]: row["payload"]
                for row in query(
                    tx,
                    "SELECT record_id,payload FROM runtime_records WHERE project_id=:p AND record_id=ANY(:ids)",
                    p=project_id,
                    ids=list({reference.record_id for reference in references}),
                ).mappings()
            }
            return [payloads[reference.record_id] for reference in references]

    @current_inputs
    def read_runtime_blob(self, project_id, runtime_ref, blob_ref):
        from .read_authorization import object_io
        from .text_source import authorized_blob

        with authorized_blob(self, project_id, runtime_ref, blob_ref):
            with object_io(self):
                value = self.objects.read(project_id, blob_ref)
        return value

    # Structural binding validation is independent of SQL writes.
    def _validate_bindings(self, tx, project_id, kind, payload, refs, *, inputs=None, prepared=None):
        from .validation import validate_bindings

        validate_bindings(self, tx, project_id, kind, payload, refs, inputs=inputs, prepared=prepared)

    def _effective_edges(self, tx, project_id, revision_id):
        from .dependencies import effective_edges

        return effective_edges(tx, project_id, revision_id)
