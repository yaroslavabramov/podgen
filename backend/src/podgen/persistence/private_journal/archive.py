"""Versioned private projections. Runtime-shaped aliases are never generic read grants."""

from copy import deepcopy
from uuid import UUID

from podgen.domain.contracts.common import BlobRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.domain.references import walk_values
from podgen.persistence.database import query
from podgen.persistence.execution.descriptors import DescriptorCache
from podgen.persistence.execution.reads import current_inputs
from podgen.persistence.read_authorization import _read_connection, checked_node, object_io, read_operation

from . import access


class ExecutionArchiveAdapter:
    def __init__(self, repo, project):
        self.repo, self.project = repo, project

    def _private_many(self, references, *, include_payload=True):
        return _private_many(self.repo, self.project, references, include_payload=include_payload)

    def _load(self, references, *, include_payload=True):
        if len(references) > 64:
            raise ContractError("private_archive_batch_too_large")
        if not references:
            return {}
        columns = "*" if include_payload else "entry_id,run_id,input_set_id,projection_version,archive_views"
        with _read_connection(self.repo) as tx:
            # These grants belong only to this exact authorization snapshot.
            # Remote object I/O and publication clear it through ReadOperation.
            cache = self._cache(tx)
            bodies = self._cache(tx, bodies=True)
            result, pending = {}, []
            for ref in references:
                key = (ref.record_id, ref.record_kind, include_payload)
                saved = bodies.get(key)
                if saved is None and not include_payload:
                    saved = bodies.get((ref.record_id, ref.record_kind, True))
                    if saved is None and cache.get((ref.record_id, ref.record_kind, "authorized")):
                        saved = ({}, None)
                if saved is None:
                    pending.append(ref)
                else:
                    result[ref.record_id] = deepcopy(saved)
            if not pending:
                return result
            entries = list(
                query(
                    tx,
                    f"""SELECT {columns} FROM execution_entries
                WHERE project_id=:p AND archive_ids && CAST(:ids AS uuid[])""",
                    p=self.project,
                    ids=list({ref.record_id for ref in pending}),
                ).mappings()
            )
            if not entries:
                return result
            for run_id, set_id in {(row["run_id"], row["input_set_id"]) for row in entries}:
                access.inputs(self.repo, tx, self.project, run_id, set_id)
            access.entry_privacy(tx, self.project, [row["entry_id"] for row in entries])
            requested = {}
            for ref in references:
                if ref.record_id in requested and requested[ref.record_id] != ref.record_kind:
                    raise AccessDenied()
                requested[ref.record_id] = ref.record_kind
            for row in entries:
                if row["projection_version"] != 3:
                    raise ContractError("unsupported_private_archive_projection")
                payloads = row["archive_payloads"] if include_payload else [None] * len(row["archive_views"])
                for view, payload in zip(row["archive_views"], payloads, strict=True):
                    identity = UUID(view["record_id"])
                    if identity in requested:
                        if view["record_kind"] != requested[identity]:
                            raise AccessDenied()
                        if include_payload:
                            from .projections import render

                            payload = render(self, view, payload)
                        result[identity] = ({"owned_objects": row.get("owned_objects", {})}, payload)
                        cache.remember((identity, view["record_kind"], "authorized"), True)
                        if include_payload:
                            owned = {BlobRef.model_validate(value) for value in row["owned_objects"].values()}
                            grants = [
                                ref.model_dump(mode="json")
                                for _, _, ref in walk_values(payload)
                                if isinstance(ref, BlobRef) and ref in owned
                            ]
                            cache.remember((identity, view["record_kind"], "blob_bindings"), grants)
                        bodies.remember((identity, view["record_kind"], include_payload), result[identity])
            return result

    def read_many(self, references):
        """Resolve private aliases and historical generic rows under one bounded owner."""
        references = list(references)
        if len(references) > 64:
            raise ContractError("private_archive_batch_too_large")
        with read_operation(self.repo):
            found = {}
            for offset in range(0, len(references), 64):
                batch = references[offset : offset + 64]
                found.update(self._private_many(batch))
            legacy = [ref for ref in references if ref.record_id not in found]
            old = dict(
                zip(
                    (ref.record_id for ref in legacy),
                    self.repo.get_runtime_many(self.project, legacy) if legacy else (),
                    strict=True,
                )
            )
            from .semantic import restore_locators

            # A literal legacy marker is ordinary historical content. Only a
            # real v3 publication link authorizes decoding this projection.
            with _read_connection(self.repo) as tx:
                semantic_ids = (
                    set(
                        query(
                            tx,
                            "SELECT DISTINCT runtime_record_id FROM semantic_execution_links WHERE project_id=:p AND runtime_record_id=ANY(CAST(:ids AS uuid[]))",
                            p=self.project,
                            ids=list(old),
                        ).scalars()
                    )
                    if old
                    else set()
                )

            return [
                found[ref.record_id][1]
                if ref.record_id in found
                else (
                    restore_locators(old[ref.record_id])
                    if ref.record_id in semantic_ids
                    else old[ref.record_id]
                )
                for ref in references
            ]

    def read(self, reference):
        return self.read_many([reference])[0]

    def authorize_many(self, references):
        """Recheck a complete batch after I/O without loading its semantic bodies."""
        references = list(references)
        with read_operation(self.repo):
            for offset in range(0, len(references), 64):
                batch = references[offset : offset + 64]
                found = self._private_many(batch, include_payload=False)
                legacy = [ref for ref in batch if ref.record_id not in found]
                if legacy:
                    self.repo.authorize_runtime_many(self.project, legacy)

    def read_blob(self, reference, blob):
        with read_operation(self.repo):
            if not self._private_binding(reference, blob):
                return self.repo.read_runtime_blob(self.project, reference, blob)
            with object_io(self.repo):
                value = self.repo.objects.read(self.project, blob)
            # Remote storage releases the old snapshot. Recheck current ACL,
            # input generation, parent withdrawal, SHA revocation and exact binding.
            if not self._private_binding(reference, blob):
                raise AccessDenied()
            return value

    def _cache(self, tx, *, bodies=False):
        return checked_node(
            self.repo,
            tx,
            ("private_archive_snapshot_bodies" if bodies else "private_archive_snapshot", self.project),
            lambda: (
                DescriptorCache(max_entries=64, max_entry_bytes=2 * 1024 * 1024)
                if bodies
                else DescriptorCache(max_entries=256)
            ),
        )

    def _private_binding(self, reference, blob):
        with _read_connection(self.repo) as tx:
            grants = self._cache(tx).get((reference.record_id, reference.record_kind, "blob_bindings"))
            if grants is not None:
                if blob.model_dump(mode="json") not in grants:
                    raise AccessDenied()
                return True
        found = self._private_many([reference])
        if reference.record_id not in found:
            return False
        self._binding(found[reference.record_id], blob)
        return True

    @staticmethod
    def _binding(entry_payload, blob):
        row, payload = entry_payload
        if not any(isinstance(ref, BlobRef) and ref == blob for _, _, ref in walk_values(payload)):
            raise AccessDenied()
        owned = row["owned_objects"]
        if not any(BlobRef.model_validate(ref) == blob for ref in owned.values()):
            # Borrowed source bytes are accessed by their source owner, never by
            # piggybacking a locator on an execution event.
            raise AccessDenied()


@current_inputs
def _private_many(repo, project, references, *, include_payload=True):
    return ExecutionArchiveAdapter(repo, project)._load(references, include_payload=include_payload)
