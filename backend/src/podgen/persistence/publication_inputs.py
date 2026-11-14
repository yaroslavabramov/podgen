"""Authorize only external nodes; internal bindings use the complete validated graph."""

from contextlib import ExitStack

from podgen.domain.contracts.common import ArtifactRef, RuntimeRef
from podgen.domain.errors import AccessDenied, ContractError
from podgen.domain.references import walk_values

from .access_facts import access_facts
from .artifact_inputs import ArtifactInputs
from .read_authorization import authorized_read
from .runtime_records import _validate_inputs


class BatchInputs:
    def __init__(self, repo, tx, project, local_artifacts, local_runtimes, groups):
        self.repo, self.tx, self.project = repo, tx, project
        self.locals, self.runtime_rows, self.groups = local_artifacts, local_runtimes, groups
        from .repository import Repository

        public = (
            repo
            if repo.purpose == "generation"
            else Repository(repo.database, repo.objects, repo.actor_id, purpose="generation")
        )
        self.external = {
            "project": ArtifactInputs(repo, tx, project, {}, (), {"kind": "code"}, public),
            "evaluator": ArtifactInputs(repo, tx, project, {}, (), {"kind": "code"}, None),
        }
        self.body_refs = {"project": set(), "evaluator": set()}

    def authorize(self, occurrences, existing):
        """Public and evaluator dependency unions remain distinct."""
        stack = ExitStack()
        stack.enter_context(authorized_read(self.repo, self.tx))
        try:
            external = {"project": {}, "evaluator": {}}
            for audience, references in occurrences:
                for ref in references:
                    key = (
                        ref.revision_id
                        if isinstance(ref, ArtifactRef)
                        else ref.record_id
                        if isinstance(ref, RuntimeRef)
                        else None
                    )
                    local = (
                        self.locals.get(key) if isinstance(ref, ArtifactRef) else self.runtime_rows.get(key)
                    )
                    if local is not None:
                        if (
                            isinstance(ref, ArtifactRef)
                            and (local["artifact_id"] != ref.artifact_id or local["kind"] != ref.kind)
                            or isinstance(ref, RuntimeRef)
                            and local["record_kind"] != ref.record_kind
                        ):
                            raise ContractError("publication_reference_identity_mismatch")
                        if audience == "project" and local["audience"] != "project":
                            raise AccessDenied()
                        continue
                    external[audience][ref] = None
            for audience, refs in existing.items():
                external[audience].update(dict.fromkeys(refs))
            for audience, refs in external.items():
                if refs:
                    _validate_inputs(
                        self.repo,
                        self.tx,
                        self.project,
                        [ref.model_dump(mode="json") for ref in refs],
                        audience,
                    )
            for audience, refs in self.body_refs.items():
                self.external[audience].load(ref for ref in refs if ref.revision_id not in self.locals)
            return stack
        except BaseException:
            stack.close()
            raise

    def node(self, payload, input_refs, producer, audience):
        value = NodeInputs(self, payload, input_refs, producer, audience)
        self.body_refs[audience].update(
            ref for _, _, ref in value.occurrences if isinstance(ref, ArtifactRef)
        )
        return value

    def resolve(self, ref, audience):
        local = self.locals.get(ref.revision_id)
        if local is not None:
            if audience == "project" and local["audience"] != "project":
                raise AccessDenied()
            return local
        return self.external[audience].resolve(ref)

    def edges(self, ref):
        local = [row for row in self.groups["artifact_dependencies"] if row["consumer_id"] == ref.revision_id]
        if local:
            return local
        return access_facts(self.repo, self.tx).artifacts.edges.get(ref.revision_id, ())


class NodeInputs:
    def __init__(self, owner, payload, input_refs, producer, audience):
        self.owner, self.audience = owner, audience
        self.occurrences = list(walk_values(payload)) + [("input_refs", "", ref) for ref in input_refs]
        if producer["kind"] == "model":
            self.occurrences.append(
                ("model_profile_ref", "", ArtifactRef.model_validate(producer["profile_ref"]))
            )
        self.runtimes = tuple(ref for _, _, ref in self.occurrences if isinstance(ref, RuntimeRef))
        self.controls = set()

    def resolve(self, ref):
        return self.owner.resolve(ref, self.audience)

    def edges(self, ref):
        return self.owner.edges(ref)
