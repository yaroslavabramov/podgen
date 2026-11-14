from uuid import uuid4

from podgen.domain.contracts.common import ArtifactRef, DependencyDisposition
from podgen.domain.contracts.runtime import AcceptanceRecord, DependencyRevalidationEvent
from podgen.domain.errors import AccessDenied, Conflict, ContractError

from .database import json_text, one, query

QUALITY_RELATIONS = {"uses_content", "uses_order", "renders", "verifies"}


def effective_edges(tx, project_id, revision_id):
    edges = {
        (r["input_id"], r["relation"]): dict(r)
        for r in query(
            tx,
            "SELECT * FROM artifact_dependencies WHERE project_id=:p AND consumer_id=:r",
            p=project_id,
            r=revision_id,
        ).mappings()
    }
    events = query(
        tx,
        """SELECT d.payload FROM dependency_revalidations d WHERE project_id=:p AND subject_id=:r
                         AND (d.acceptance_record_id IS NULL OR NOT EXISTS(
                         SELECT 1 FROM revocations v WHERE v.project_id=d.project_id AND v.acceptance_record_id=d.acceptance_record_id))
                         ORDER BY (payload->>'expected_validity_version')::bigint""",
        p=project_id,
        r=revision_id,
    ).mappings()
    return apply_dispositions(edges.values(), events)


def apply_dispositions(rows, events):
    """One definition of effective provenance for scalar and batched readers."""
    edges = {(r["input_id"], r["relation"]): dict(r) for r in rows}
    for event in events:
        for disposition in event["payload"]["dispositions"]:
            old = ArtifactRef.model_validate(disposition["prior_input_ref"])
            if disposition["disposition"] == "retained":
                for key, edge in edges.items():
                    if key[0] == old.revision_id and key[1] in QUALITY_RELATIONS:
                        edge["reviewed_through"] = event["payload"]["expected_validity_version"]
            if disposition["disposition"] != "retained":
                edges = {
                    k: v
                    for k, v in edges.items()
                    if not (k[0] == old.revision_id and k[1] in QUALITY_RELATIONS)
                }
            if disposition["disposition"] == "replaced":
                for value in disposition["current_input_refs"]:
                    ref = ArtifactRef.model_validate(value)
                    edges[(ref.revision_id, "uses_content")] = dict(
                        input_id=ref.revision_id, relation="uses_content"
                    )
    return list(edges.values())


class DependencyLedger:
    def __init__(self, repository):
        self.repo = repository

    def validity_version(self, project_id):
        with self.repo.database.transaction() as tx:
            self.repo._acl(tx, project_id)
            return self.repo._validity(tx, project_id)

    def invalidate(self, project_id, ref, *, reason, category):
        if category not in ("quality", "access") or not reason:
            raise ContractError("invalid_invalidation")
        repo = self.repo
        with repo.database.transaction() as tx:
            if repo._acl(tx, project_id) not in ("owner", "evaluator", "operator"):
                raise AccessDenied()
            repo._lock(tx, project_id)
            row = repo._resolve(tx, project_id, ref)
            if row["project_id"] != project_id:
                raise AccessDenied()
            event_id = uuid4()
            query(
                tx,
                "INSERT INTO invalidations VALUES(:p,:id,:r,:why,:category,:version)",
                p=project_id,
                id=event_id,
                r=ref.revision_id,
                why=reason,
                category=category,
                version=repo._validity(tx, project_id) + 1,
            )
            if category == "access":
                from .privacy import revoke_content

                revoke_content(tx, project_id, ref.revision_id, event_id, reason)
            repo._bump(tx, project_id)
            return event_id

    def quality_blockers(self, project_id, ref):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._resolve(tx, project_id, ref)
            visited, blocked = set(), []

            def visit(revision_id, reviewed_through=-1):
                if (revision_id, reviewed_through) in visited:
                    return
                visited.add((revision_id, reviewed_through))
                for record in query(
                    tx,
                    "SELECT reason FROM invalidations WHERE project_id=:p AND revision_id=:r AND category='quality' AND validity_version>:reviewed",
                    p=project_id,
                    r=revision_id,
                    reviewed=reviewed_through,
                ).mappings():
                    blocked.append(dict(revision_id=str(revision_id), reason=record["reason"]))
                for edge in effective_edges(tx, project_id, revision_id):
                    if edge["relation"] != "lineage":
                        visit(edge["input_id"], max(reviewed_through, edge.get("reviewed_through", -1)))

            visit(ref.revision_id)
            return blocked

    def revalidate(self, project_id, event: DependencyRevalidationEvent):
        repo = self.repo
        with repo.database.transaction() as tx:
            if repo._acl(tx, project_id) not in ("owner", "editor", "evaluator", "operator"):
                raise AccessDenied()
            repo._lock(tx, project_id)
            subject = repo._resolve(tx, project_id, event.subject_ref)
            assessment = repo._resolve(tx, project_id, event.assessment_ref)
            repo._resolve(tx, project_id, event.policy_ref)
            if subject["project_id"] != project_id or assessment["project_id"] != project_id:
                raise AccessDenied()
            previous = one(
                tx,
                "SELECT payload FROM dependency_revalidations WHERE project_id=:p AND event_id=:id",
                p=project_id,
                id=event.event_id,
            )
            if previous:
                if previous["payload"] != event.model_dump(mode="json"):
                    raise Conflict("event_id_reused_with_different_input")
                self._authorize(tx, project_id, subject, assessment, event)
                return DependencyRevalidationEvent.model_validate(previous["payload"])
            if repo._validity(tx, project_id) != event.expected_validity_version:
                raise Conflict("validity_version_conflict")
            normalized = self._authorize(tx, project_id, subject, assessment, event)
            if [v.model_dump(mode="json") for v in event.dispositions] != normalized:
                raise ContractError("dispositions_do_not_match_owner_assessment")
            edges = effective_edges(tx, project_id, event.subject_ref.revision_id)
            for disposition in event.dispositions:
                if not any(
                    e["input_id"] == disposition.prior_input_ref.revision_id
                    and e["relation"] in QUALITY_RELATIONS
                    for e in edges
                ):
                    raise ContractError("no_active_quality_dependency")
                for ref in disposition.current_input_refs:
                    row = repo._resolve(tx, project_id, ref)
                    if row["project_id"] != project_id:
                        raise AccessDenied()
                    visited = set()

                    def depends_on_subject(current, visited=visited):
                        if current["revision_id"] == event.subject_ref.revision_id:
                            return True
                        if current["revision_id"] in visited:
                            return False
                        visited.add(current["revision_id"])
                        children = [
                            repo._row(tx, ArtifactRef.model_validate(r)) for r in current["input_refs"]
                        ]
                        children += [
                            one(
                                tx,
                                "SELECT * FROM artifact_revisions WHERE project_id=:p AND revision_id=:r",
                                p=current["project_id"],
                                r=e["input_id"],
                            )
                            for e in effective_edges(tx, current["project_id"], current["revision_id"])
                        ]
                        return any(child is not None and depends_on_subject(child) for child in children)

                    if depends_on_subject(row):
                        raise ContractError("cyclic_active_dependency")
                if not disposition.subject_selectors:
                    raise ContractError("revalidation_requires_exact_subject_selectors")
                for selector in disposition.subject_selectors:
                    owner_ref = getattr(
                        selector,
                        "artifact_ref",
                        getattr(
                            selector,
                            "script_ref",
                            getattr(
                                selector,
                                "note_ref",
                                getattr(selector, "show_notes_ref", getattr(selector, "audio_ref", None)),
                            ),
                        ),
                    )
                    if owner_ref != event.subject_ref:
                        raise ContractError("revalidation_selector_subject_mismatch")
                if disposition.prior_input_ref.kind in ("Policy", "PolicyBundle"):
                    raise ContractError("cannot_remove_policy_dependency")
            for disposition in event.dispositions:
                for ref in disposition.current_input_refs:
                    query(
                        tx,
                        "INSERT INTO revalidation_privacy_links VALUES(:p,:s,:i) ON CONFLICT DO NOTHING",
                        p=project_id,
                        s=event.subject_ref.revision_id,
                        i=ref.revision_id,
                    )
            acceptance_id = event.authority.record_id if event.authority.kind == "acceptance_record" else None
            integration_id = (
                event.authority.record_id if event.authority.kind == "integration_event" else None
            )
            query(
                tx,
                """INSERT INTO dependency_revalidations VALUES(:p,:id,:s,:a,:authority,:integration,CAST(:body AS json))""",
                p=project_id,
                id=event.event_id,
                s=event.subject_ref.revision_id,
                a=event.assessment_ref.revision_id,
                authority=acceptance_id,
                integration=integration_id,
                body=json_text(event),
            )
            from .publication_scope import AccessChange, notify

            notify(tx, AccessChange())
            repo._bump(tx, project_id)
            return event

    def _authorize(self, tx, project_id, subject, assessment, event):
        body = assessment["payload"]
        if event.authority.kind == "acceptance_record":
            kinds = {
                "ScriptRevision": ("VerificationReport", "factual", "script_ref"),
                "ShowNotes": ("ShowNotesReview", "show_notes", "show_notes_ref"),
                "AudioMaster": ("AudioQAReport", "audio", "audio_master_ref"),
                "AudioSegment": ("AudioQAReport", "audio", "audio_segment_ref"),
            }
            if subject["kind"] not in kinds:
                raise ContractError("unsupported_revalidation_owner")
            report_kind, check, field = kinds[subject["kind"]]
            row = one(
                tx,
                """SELECT a.payload FROM acceptance_records a WHERE project_id=:p AND record_id=:id
                              AND NOT EXISTS(SELECT 1 FROM revocations v WHERE v.project_id=a.project_id AND v.acceptance_record_id=a.record_id)""",
                p=project_id,
                id=event.authority.record_id,
            )
            if row is None:
                raise AccessDenied()
            acceptance = AcceptanceRecord.model_validate(row["payload"])
            if (
                assessment["kind"] != report_kind
                or acceptance.check_kind != check
                or acceptance.decision != "accepted"
                or acceptance.subject_ref != event.subject_ref
                or acceptance.policy_ref != event.policy_ref
                or event.assessment_ref not in acceptance.reviewed_input_refs
                or body[field] != event.subject_ref.model_dump(mode="json")
            ):
                raise ContractError("revalidation_authority_mismatch")
            if subject["kind"] == "ScriptRevision":
                normalized = []
                for disposition in body["dependency_dispositions"]:
                    if disposition["checked_against_script_ref"] != event.subject_ref.model_dump(mode="json"):
                        raise ContractError("checked_script_mismatch")
                    normalized.append(
                        DependencyDisposition(
                            prior_input_ref=disposition["prior_input_ref"],
                            current_input_refs=disposition["current_input_refs"],
                            disposition=disposition["disposition"],
                            subject_selectors=disposition["affected_selectors"],
                            basis=disposition["basis"],
                        ).model_dump(mode="json")
                    )
                return normalized
            return body["dependency_dispositions"]
        row = one(
            tx,
            "SELECT * FROM integration_events WHERE project_id=:p AND event_id=:id",
            p=project_id,
            id=event.authority.record_id,
        )
        if (
            row is None
            or assessment["kind"] != "EditorialDecision"
            or row["decision_id"] != assessment["revision_id"]
            or subject["kind"] not in ("ResearchNote", "StoryPlan")
            or event.subject_ref.model_dump(mode="json") not in body["reviewed_output_refs"]
        ):
            raise ContractError("integration_authority_mismatch")
        fence = row["payload"]
        if "branch_id" in fence:
            from uuid import UUID

            head = one(
                tx,
                "SELECT generation_epoch FROM branch_heads WHERE project_id=:p AND episode_id=:e AND branch_id=:b",
                p=project_id,
                e=UUID(fence["episode_id"]),
                b=UUID(fence["branch_id"]),
            )
            if head is None or head["generation_epoch"] != fence["generation_epoch"]:
                raise Conflict("integration_generation_epoch_changed")
        matches = [
            d
            for d in body["dependency_dispositions"]
            if d["subject_ref"] == event.subject_ref.model_dump(mode="json")
        ]
        if row["payload"]["policy_ref"] != event.policy_ref.model_dump(mode="json"):
            raise ContractError("integration_policy_mismatch")
        if len(matches) != 1:
            raise ContractError("missing_subject_dispositions")
        return matches[0]["dispositions"]
