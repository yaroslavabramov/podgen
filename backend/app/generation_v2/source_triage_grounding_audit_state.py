from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

from app.generation_v2.source_triage_finalizer_evidence import finalizer_has_full_evidence


GROUNDING_AUDITS_FIELD = "source_finding_grounding_audits"


def finding_grounding_signature(finding: dict[str, Any]) -> str:
    semantic = {
        key: finding.get(key)
        for key in (
            "finding_id",
            "statement",
            "support_explanation",
            "evidence_ids",
            "scope",
            "limitations",
        )
    }
    return sha256(
        json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def current_grounding_audit_violations(workspace: dict[str, Any]) -> list[dict[str, Any]]:
    audits = workspace.get(GROUNDING_AUDITS_FIELD)
    audits = audits if isinstance(audits, dict) else {}
    missing = [
        str(finding.get("finding_id") or "")
        for finding in workspace.get("source_findings") or []
        if isinstance(finding, dict)
        and str(finding.get("finding_id") or "")
        and (
            not isinstance(audits.get(str(finding.get("finding_id") or "")), dict)
            or str(audits[str(finding.get("finding_id") or "")].get("signature") or "")
            != finding_grounding_signature(finding)
        )
    ]
    if not missing:
        return []
    return [
        {
            "code": "source_finding_grounding_audit_incomplete",
            "message": "Every current source finding needs a finalizer grounding audit against its exact evidence.",
            "missing_finding_count": len(missing),
            "finding_ids_preview": sorted(missing)[:20],
            "omitted_finding_id_count": max(0, len(missing) - 20),
            "next_action": (
                "Page source_findings; each returned record part carries grounding_audit_status. "
                "Audit every pending finding, then refresh stale outcomes."
            ),
        }
    ]


def annotate_finding_page_grounding_state(
    workspace: dict[str, Any],
    page: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    audits = workspace.get(GROUNDING_AUDITS_FIELD)
    audits = audits if isinstance(audits, dict) else {}
    current_ids: set[str] = set()
    pending_ids: set[str] = set()
    finding_id_by_owner_ref: dict[str, str] = {}
    for finding in workspace.get("source_findings") or []:
        if not isinstance(finding, dict):
            continue
        finding_id = str(finding.get("finding_id") or "")
        if not finding_id:
            continue
        owner_ref = finding_id if finding_id.startswith("finding:") else f"source_finding:{finding_id}"
        finding_id_by_owner_ref[owner_ref] = finding_id
        audit = audits.get(finding_id)
        if (
            isinstance(audit, dict)
            and str(audit.get("signature") or "") == finding_grounding_signature(finding)
        ):
            current_ids.add(finding_id)
        else:
            pending_ids.add(finding_id)
    annotated: list[dict[str, Any]] = []
    for raw_part in page:
        part = dict(raw_part)
        finding_id = finding_id_by_owner_ref.get(str(part.get("owner_ref") or ""))
        if finding_id is not None:
            part["grounding_audit_status"] = "current" if finding_id in current_ids else "pending"
        annotated.append(part)
    return annotated, {
        "current_count": len(current_ids),
        "pending_count": len(pending_ids),
    }


def grounding_audit_payload_error(
    args: dict[str, Any],
    *,
    verdict: str,
) -> dict[str, Any] | None:
    revised_scope = args.get("revised_scope")
    revised_scope = revised_scope if isinstance(revised_scope, dict) else {}
    has_semantic_revision = bool(
        str(args.get("revised_statement") or "").strip()
        or str(args.get("revised_support_explanation") or "").strip()
        or list(args.get("revised_limitations") or [])
        or any(
            str(revised_scope.get(field) or "").strip()
            for field in ("actor", "period", "geography")
        )
        or list(revised_scope.get("qualifiers") or [])
    )
    revised_evidence_ids = [str(value) for value in args.get("revised_evidence_ids") or [] if str(value)]
    has_no_findings_reason = bool(str(args.get("source_no_findings_reason") or "").strip())
    if verdict == "narrowed" and has_no_findings_reason:
        return {
            "accepted": False,
            "error": "source_no_findings_reason is only valid when removal leaves no findings",
        }
    if verdict != "narrowed" and has_semantic_revision:
        return {
            "accepted": False,
            "error": f"{verdict} audit must leave all revised finding fields empty",
        }
    if verdict == "narrowed" and not revised_evidence_ids:
        return {
            "accepted": False,
            "error": "narrowed audit requires a complete non-empty revised_evidence_ids set",
        }
    if verdict == "removed" and revised_evidence_ids:
        return {
            "accepted": False,
            "error": "removed audit must leave revised_evidence_ids empty",
        }
    if verdict == "supported" and has_no_findings_reason:
        return {
            "accepted": False,
            "error": "supported audit cannot set source_no_findings_reason",
        }
    return None


def evidence_requiring_full_finalizer_read(
    workspace: dict[str, Any],
    finding: dict[str, Any],
) -> list[str]:
    previews = {
        str(item.get("evidence_id") or ""): item
        for item in finding.get("evidence_previews") or []
        if isinstance(item, dict) and str(item.get("evidence_id") or "")
    }
    return [
        evidence_id
        for evidence_id in [str(value) for value in finding.get("evidence_ids") or []]
        if (
            evidence_id not in previews
            or bool(previews[evidence_id].get("snippet_truncated"))
        )
        and not finalizer_has_full_evidence(workspace, evidence_id=evidence_id)
    ]


__all__ = [
    "GROUNDING_AUDITS_FIELD",
    "annotate_finding_page_grounding_state",
    "current_grounding_audit_violations",
    "evidence_requiring_full_finalizer_read",
    "finding_grounding_signature",
    "grounding_audit_payload_error",
]
