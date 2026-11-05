"""Append-only acceptance and revocation; model tools cannot write approvals."""

from podgen.domain.contracts.runtime import AcceptanceRecord, RevocationRecord
from podgen.domain.errors import AccessDenied, ContractError

from .database import json_text, one, query

REPORT_RULES = {
    ("ScriptRevision", "factual"): ("VerificationReport", "script_ref"),
    ("ScriptRevision", "editorial"): ("EditorialReview", "script_ref"),
    ("ShowNotes", "show_notes"): ("ShowNotesReview", "show_notes_ref"),
    ("AudioSegment", "audio"): ("AudioQAReport", "audio_segment_ref"),
    ("AudioMaster", "audio"): ("AudioQAReport", "audio_master_ref"),
}


class AcceptanceLedger:
    def __init__(self, repository):
        self.repo = repository

    def _reviewer(self, tx, project_id, producer):
        repo = self.repo
        role = repo._acl(tx, project_id)
        if role not in ("owner", "evaluator", "operator"):
            raise AccessDenied()
        # Qualification of model reviewers belongs to WP-05. Until then only authenticated humans.
        if producer.kind != "human_review" or producer.rater_id != repo.actor_id:
            raise AccessDenied()

    def append(self, project_id, record: AcceptanceRecord):
        repo = self.repo
        with repo.database.transaction() as tx:
            self._reviewer(tx, project_id, record.reviewer_ref)
            repo._lock(tx, project_id)
            subject = repo._resolve(tx, project_id, record.subject_ref)
            if subject["project_id"] != project_id:
                raise AccessDenied()
            if record.policy_ref.kind != "PolicyBundle":
                raise ContractError("acceptance_requires_policy_bundle")
            repo._resolve(tx, project_id, record.policy_ref)
            reports = [repo._resolve(tx, project_id, r) for r in record.reviewed_input_refs]
            for finding in record.findings:
                report = repo._resolve(tx, project_id, finding.report_ref)

                def find(value, target=finding.finding_id):
                    if isinstance(value, dict):
                        return value.get("finding_id") == str(target) or any(find(v) for v in value.values())
                    return isinstance(value, list) and any(find(v) for v in value)

                if not find(report["payload"]):
                    raise ContractError("unknown_finding")
            if record.check_kind == "profile_qualification":
                if record.subject_ref.kind not in (
                    "ModelProfile",
                    "ProcessingProfile",
                    "VoiceProfile",
                    "PronunciationLexicon",
                ):
                    raise ContractError("qualification_subject_kind")
                matching = [
                    r
                    for r in reports
                    if (
                        r["kind"] == "CapabilityReport"
                        and r["payload"]["profile_ref"] == record.subject_ref.model_dump(mode="json")
                    )
                    or (
                        r["kind"] == "EvaluationResult"
                        and record.subject_ref.model_dump(mode="json")
                        in r["payload"]["candidate_profile_refs"]
                    )
                ]
                if not matching or not record.assessment_scope.supported_cells:
                    raise ContractError("qualification_requires_scoped_assessment")
                if record.decision == "accepted":
                    from .qualification import validate_qualification

                    validate_qualification(repo, tx, project_id, record, reports)

            else:
                rule = REPORT_RULES.get((record.subject_ref.kind, record.check_kind))
                if rule is None:
                    raise ContractError("unsupported_acceptance_subject")
                report_kind, subject_field = rule
                matching = [
                    r
                    for r in reports
                    if r["kind"] == report_kind
                    and r["payload"][subject_field] == record.subject_ref.model_dump(mode="json")
                ]
                if not matching:
                    raise ContractError("acceptance_requires_exact_owner_report")
                for report in matching:
                    if report["payload"]["policy_ref"] != record.policy_ref.model_dump(mode="json"):
                        raise ContractError("acceptance_policy_mismatch")
                    recommendation = report["payload"].get("recommendation", report["payload"].get("result"))
                    if record.decision == "accepted" and recommendation not in ("accepted", "acceptable"):
                        raise ContractError("acceptance_contradicts_report")
            query(
                tx,
                """INSERT INTO acceptance_records VALUES(:p,:id,:s,:kind,:decision,CAST(:body AS json))""",
                p=project_id,
                id=record.record_id,
                s=record.subject_ref.revision_id,
                kind=record.check_kind,
                decision=record.decision,
                body=json_text(record),
            )
            repo._bump(tx, project_id)
        return record

    def revoke(self, project_id, record: RevocationRecord):
        repo = self.repo
        with repo.database.transaction() as tx:
            self._reviewer(tx, project_id, record.reviewer_ref)
            repo._lock(tx, project_id)
            if not one(
                tx,
                "SELECT 1 FROM acceptance_records WHERE project_id=:p AND record_id=:id",
                p=project_id,
                id=record.acceptance_record_id,
            ):
                raise AccessDenied()
            for ref in record.basis_refs:
                repo._resolve(tx, project_id, ref)
            query(
                tx,
                "INSERT INTO revocations VALUES(:p,:id,:a,CAST(:body AS json))",
                p=project_id,
                id=record.revocation_id,
                a=record.acceptance_record_id,
                body=json_text(record),
            )
            repo._bump(tx, project_id)

    def current(self, project_id, subject_ref, check_kind):
        repo = self.repo
        with repo.database.transaction() as tx:
            repo._resolve(tx, project_id, subject_ref)
            return [
                AcceptanceRecord.model_validate(r["payload"])
                for r in query(
                    tx,
                    """
                SELECT a.payload FROM acceptance_records a WHERE a.project_id=:p AND a.subject_id=:s
                AND a.check_kind=:k AND a.decision='accepted'
                AND NOT EXISTS(SELECT 1 FROM revocations v WHERE v.project_id=a.project_id AND v.acceptance_record_id=a.record_id)
            """,
                    p=project_id,
                    s=subject_ref.revision_id,
                    k=check_kind,
                ).mappings()
            ]
