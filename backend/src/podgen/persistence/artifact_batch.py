"""The single artifact publication engine for scalar, package and document writes."""

from dataclasses import replace

from pydantic import TypeAdapter

from podgen.domain.canonical import canonical_bytes
from podgen.domain.contracts import validate_payload
from podgen.domain.contracts.common import ArtifactRef, ProducerRef, RuntimeRef
from podgen.domain.errors import AccessDenied, Conflict, ContractError
from podgen.domain.references import walk_values

from .batch_insert import insert_rows
from .database import bounded_rows, json_text, query
from .publication_identity import resolve
from .publication_inputs import BatchInputs
from .publication_rows import TABLES, compile_node, encoded
from .read_authorization import authorized_read, invalidate_publication_facts
from .repository import HIDDEN_KINDS


def publish(repo, tx, project, batch, *, identities=None):
    batch.validate_size()
    with authorized_read(repo, tx):
        role = repo._acl(tx, project, write=True)
        repo._lock(tx, project)
        batch.artifacts = tuple(
            replace(node, audience="evaluator") if node.kind in HIDDEN_KINDS else node
            for node in batch.artifacts
        )
        for node in (*batch.artifacts, *batch.runtimes):
            if node.audience not in ("project", "evaluator"):
                raise ContractError("invalid_audience")
            if node.audience == "evaluator" and (
                repo.purpose != "evaluation" or role not in ("owner", "evaluator", "operator")
            ):
                raise AccessDenied()
        old, runtime_old, refs, runtimes = resolve(tx, project, batch, identities)
        groups = {table: [] for table in TABLES}
        local = {}
        records = {}
        bodies, producers = {}, {}
        for node in batch.artifacts:
            body = batch.body(node.payload, refs, runtimes)
            validate_payload(node.kind, body)
            if not node.streaming and len(canonical_bytes(body)) > repo.inline_limit:
                raise ContractError(
                    "payload_too_large", "Use the full artifact document streaming import route."
                )
            producer = (
                node.producer.model_dump(mode="json")
                if hasattr(node.producer, "model_dump")
                else node.producer
            )
            TypeAdapter(ProducerRef).validate_python(producer)
            if producer["kind"] == "model" and producer["profile_ref"]["kind"] != "ModelProfile":
                raise ContractError("producer_profile_kind_mismatch")
            bodies[node.key], producers[node.key] = body, producer
            ref = refs[node.key]
            local[ref.revision_id] = dict(
                project_id=project,
                artifact_id=ref.artifact_id,
                revision_id=ref.revision_id,
                kind=node.kind,
                payload=body,
                audience=node.audience,
            )
        for node in batch.runtimes:
            body = batch.body(node.payload, refs, runtimes)
            canonical_bytes(body)
            bodies[node.key] = body
            records[node.ref.record_id] = dict(
                project_id=project,
                record_id=node.ref.record_id,
                record_kind=node.ref.record_kind,
                payload=body,
                audience=node.audience,
            )
            previous = runtime_old.get(node.ref.record_id)
            if previous and (
                not node.replay
                or any(
                    previous[key] != records[node.ref.record_id][key]
                    for key in ("record_kind", "payload", "audience")
                )
            ):
                raise Conflict("runtime_id_reused_with_different_input")
        from .acquisition_controls import prepare as prepare_controls
        from .acquisition_policy import VERSION, JournalBinding, fingerprint, projection, semantic_inputs

        for node in batch.runtimes:
            if node.journal is not None:
                if not isinstance(node.journal, JournalBinding) or node.audience != "project":
                    raise ContractError("acquisition_journal_binding_invalid")
                row = records[node.ref.record_id]
                row.update(
                    reference_policy=VERSION,
                    journal_binding=node.journal.wire(),
                    reference_manifest=projection(node.journal, node.ref, row["payload"]),
                    journal_fingerprint=fingerprint(node.journal, node.ref, row["payload"]),
                )
                previous = runtime_old.get(node.ref.record_id)
                if previous is not None and any(
                    previous.get(key) != row[key]
                    for key in ("reference_policy", "journal_binding", "journal_fingerprint")
                ):
                    raise Conflict("runtime_id_reused_with_different_input")
        journal_rows, controls, rights = prepare_controls(repo, tx, project, batch, bodies, records)
        inputs = BatchInputs(repo, tx, project, local, records, groups)
        occurrences, node_inputs = [], {}
        for node in batch.artifacts:
            declared = (*node.input_refs, *(refs[key] for key in node.input_keys))
            node_inputs[node.key] = inputs.node(
                bodies[node.key], declared, producers[node.key], node.audience
            )
            if node.key in controls:
                node_inputs[node.key].controls.update(controls[node.key])
            occurrences.append(
                (
                    node.audience,
                    [
                        ref
                        for _, _, ref in node_inputs[node.key].occurrences
                        if ref not in node_inputs[node.key].controls
                    ],
                )
            )
        for node in batch.runtimes:
            references = (
                semantic_inputs(node.journal, bodies[node.key])
                if node.journal is not None
                else [ref for _, _, ref in walk_values(bodies[node.key])]
            )
            occurrences.append((node.audience, references))
        if rights:
            # Rights belong to the project-audience journal/source contract,
            # independent of the ordering or audience of unrelated batch nodes.
            occurrences.append(("project", rights))
        _acyclic(local, records, occurrences[: len(batch.artifacts) + len(batch.runtimes)])
        existing_refs = {"project": [], "evaluator": []}
        for node in batch.artifacts:
            if node.operation_id in old:
                existing_refs[node.audience].append(refs[node.key])
        for node in batch.runtimes:
            if node.ref.record_id in runtime_old:
                existing_refs[node.audience].append(node.ref)
        with inputs.authorize(occurrences, existing_refs):
            kinds = (
                {
                    row["artifact_id"]: row["kind"]
                    for row in bounded_rows(
                        tx,
                        "SELECT artifact_id,kind FROM artifacts WHERE project_id=:p AND artifact_id=ANY(:ids)",
                        [ref.artifact_id for key, ref in refs.items() if key in bodies],
                        p=project,
                    )
                }
                if batch.artifacts
                else {}
            )
            results = {}
            for node in _ordered_artifacts(batch.artifacts, node_inputs, refs):
                ref = refs[node.key]
                if ref.artifact_id in kinds and kinds[ref.artifact_id] != node.kind:
                    raise Conflict("artifact_kind_is_immutable")
                if ref.artifact_id not in kinds:
                    groups["artifacts"].append(
                        dict(project_id=project, artifact_id=ref.artifact_id, kind=node.kind)
                    )
                    kinds[ref.artifact_id] = node.kind
                identity_node = (
                    replace(node, artifact_id=refs[node.identity_of].artifact_id)
                    if node.identity_of
                    else node
                )
                row = compile_node(
                    repo,
                    tx,
                    project,
                    identity_node,
                    ref,
                    bodies[node.key],
                    producers[node.key],
                    node_inputs[node.key],
                    batch.prepared,
                    old.get(node.operation_id),
                    groups,
                )
                local[ref.revision_id] = row
                results[node.key] = row
            pending_runtime = [row for key, row in records.items() if key not in runtime_old]
            insert_rows(tx, "artifacts", groups["artifacts"])
            rows, columns = encoded("artifact_revisions", groups["artifact_revisions"])
            inserted = insert_rows(tx, "artifact_revisions", rows, json_columns=columns, returning=True) or []
            for policy in (False, True):
                runtime_group = [
                    dict(
                        row,
                        **{
                            key: json_text(row[key])
                            for key in (
                                ("payload", "reference_manifest", "journal_binding")
                                if policy
                                else ("payload",)
                            )
                        },
                    )
                    for row in pending_runtime
                    if ("reference_policy" in row) == policy
                ]
                insert_rows(
                    tx,
                    "runtime_records",
                    runtime_group,
                    json_columns=("payload", "reference_manifest", "journal_binding"),
                )
            for table in TABLES[2:]:
                rows, columns = encoded(table, groups[table])
                insert_rows(tx, table, _unique(rows), json_columns=columns)
            if inserted:
                query(
                    tx,
                    "UPDATE projects SET validity_version=validity_version+:n WHERE project_id=:p",
                    p=project,
                    n=len(inserted),
                )
            from .publication_scope import Append

            invalidate_publication_facts(
                repo,
                tx,
                effect=Append(
                    artifacts=tuple(row["revision_id"] for row in inserted),
                    runtimes=tuple((project, row["record_id"]) for row in pending_runtime),
                ),
            )
            from .classification import classify_evaluator, classify_evaluator_batch

            for row in inserted:
                if row["audience"] == "evaluator":
                    classify_evaluator(tx, project, artifact_id=row["revision_id"])
            classified = [row["record_id"] for row in pending_runtime if row["audience"] == "evaluator"]
            if classified:
                classify_evaluator_batch(tx, project, classified)
            by_id = {row["revision_id"]: row for row in inserted}
            from .publication_plan import PublicationResult

            return PublicationResult(
                {
                    node.key: repo._envelope(by_id.get(refs[node.key].revision_id, results[node.key]))
                    for node in batch.artifacts
                }
                | {node.key: node.ref for node in batch.runtimes},
                {node.key: bodies[node.key] for node in batch.runtimes},
            )


def _unique(rows):
    return list({tuple(row.items()): row for row in rows}.values())


def _acyclic(artifacts, runtimes, occurrences):
    keys = {("a", key) for key in artifacts} | {("r", key) for key in runtimes}
    graph = {}
    for key, (_, refs) in zip(
        (*[("a", key) for key in artifacts], *[("r", key) for key in runtimes]), occurrences, strict=True
    ):
        graph[key] = {
            ("a", ref.revision_id) if isinstance(ref, ArtifactRef) else ("r", ref.record_id)
            for ref in refs
            if isinstance(ref, (ArtifactRef, RuntimeRef))
        } & keys
    done = set()
    while len(done) != len(graph):
        ready = {key for key, deps in graph.items() if key not in done and deps <= done}
        if not ready:
            raise ContractError("cyclic_publication_references")
        done.update(ready)


def _ordered_artifacts(nodes, inputs, refs):
    pending, done, identities = list(nodes), set(), {refs[node.key].revision_id for node in nodes}
    while pending:
        ready = [
            node
            for node in pending
            if {ref.revision_id for _, _, ref in inputs[node.key].occurrences if isinstance(ref, ArtifactRef)}
            & identities
            <= done
        ]
        if not ready:
            raise ContractError("cyclic_publication_references")
        for node in ready:
            yield node
            done.add(refs[node.key].revision_id)
            pending.remove(node)
