from __future__ import annotations

from typing import Any


ANSWER_POINT_STATUSES = {"covered", "partial", "conflicted"}


def _answer_points(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    points: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        point = str(item.get("point") or "").strip()
        finding_ids = [str(value) for value in item.get("finding_ids") or [] if str(value)]
        if point or finding_ids:
            points.append({"point": point, "finding_ids": finding_ids})
    return points


def _joined_answer_points(points: list[dict[str, Any]]) -> str:
    return "\n".join(str(item.get("point") or "").strip() for item in points if str(item.get("point") or "").strip())


def _answer_point_finding_ids(points: list[dict[str, Any]]) -> list[str]:
    return list(
        dict.fromkeys(
            str(finding_id)
            for point in points
            for finding_id in point.get("finding_ids") or []
            if str(finding_id)
        )
    )


def question_resolution_shape_error(args: dict[str, Any]) -> dict[str, Any] | None:
    for index, result in enumerate(args.get("requirement_results") or []):
        if not isinstance(result, dict):
            return {
                "accepted": False,
                "error": "question_resolution_requirement_must_be_an_object",
                "requirement_index": index,
            }
        if "established_answer" in result:
            return {
                "accepted": False,
                "error": "question_resolution_established_answer_is_deprecated",
                "requirement_id": str(result.get("requirement_id") or ""),
                "next_action": (
                    "Move every established proposition into answer_points[].point with supporting finding_ids. "
                    "The question outcome is assembled from answer_points only."
                ),
            }
        status = str(result.get("status") or "")
        raw_answer_points = result.get("answer_points")
        answer_points = _answer_points(raw_answer_points)
        finding_ids = _answer_point_finding_ids(answer_points)
        answer = _joined_answer_points(answer_points)
        criterion = str(result.get("criterion_match_reason") or "").strip()
        remainder = str(result.get("unresolved_remainder") or "").strip()
        conflict = str(result.get("conflict_summary") or "").strip()
        deferred = str(result.get("deferred_reason") or "").strip()
        if not isinstance(raw_answer_points, list):
            return {
                "accepted": False,
                "error": "question_resolution_answer_points_must_be_an_array",
                "requirement_id": str(result.get("requirement_id") or ""),
                "next_action": (
                    "Express each established answer proposition as answer_points[].point and attach the "
                    "finding_ids that support that exact point. If the proposition is not already in a saved "
                    "finding, update the source analysis first."
                ),
            }
        if status in ANSWER_POINT_STATUSES and not answer_points:
            return {
                "accepted": False,
                "error": "question_resolution_requires_answer_points_for_supported_status",
                "requirement_id": str(result.get("requirement_id") or ""),
                "status": status,
                "next_action": (
                    "Add at least one answer point linked to saved findings, or use missing/deferred when no "
                    "saved finding establishes an answer fragment."
                ),
            }
        if status not in ANSWER_POINT_STATUSES and answer_points:
            return {
                "accepted": False,
                "error": "question_resolution_answer_points_contradict_status",
                "requirement_id": str(result.get("requirement_id") or ""),
                "status": status,
                "next_action": "Use answer_points only for covered, partial, or conflicted requirements.",
            }
        valid = {
            "covered": bool(finding_ids and answer and criterion and not remainder and not conflict and not deferred),
            "partial": bool(finding_ids and answer and criterion and remainder and not conflict and not deferred),
            "missing": bool(not finding_ids and not answer and not criterion and remainder and not conflict and not deferred),
            "conflicted": bool(finding_ids and conflict and not deferred),
            "deferred": bool(
                not finding_ids
                and not answer
                and not criterion
                and not remainder
                and not conflict
                and deferred
            ),
        }
        if not valid.get(status, False):
            return {
                "accepted": False,
                "error": "question_resolution_fields_contradict_status",
                "requirement_id": str(result.get("requirement_id") or ""),
                "status": status,
                "next_action": (
                    "covered needs findings, an established answer, a criterion match, and no remainder; partial "
                    "also needs a concrete remainder; missing needs only a remainder; conflicted needs findings and "
                    "a conflict summary; deferred needs only a representation-blocked reason."
                ),
            }
    return None


def normalize_question_resolution_args(args: dict[str, Any]) -> dict[str, Any]:
    normalized_results: list[dict[str, Any]] = []
    statuses: list[str] = []
    for raw in args.get("requirement_results") or []:
        if not isinstance(raw, dict):
            continue
        status = str(raw.get("status") or "")
        statuses.append(status)
        remainder = str(raw.get("unresolved_remainder") or "").strip()
        answer_points = _answer_points(raw.get("answer_points"))
        finding_ids = _answer_point_finding_ids(answer_points)
        normalized_results.append(
            {
                "requirement_id": str(raw.get("requirement_id") or ""),
                "status": status,
                "finding_ids": finding_ids,
                "answer_points": answer_points,
                "summary": _joined_answer_points(answer_points),
                "closure_basis": str(raw.get("criterion_match_reason") or "").strip(),
                "remaining_uncertainty": remainder,
                "missing_detail": remainder if status in {"partial", "missing"} else "",
                "conflict_summary": str(raw.get("conflict_summary") or "").strip(),
                "deferred_reason": str(raw.get("deferred_reason") or "").strip(),
                "confidence": 0.5,
                "next_action": "research",
            }
        )
    overall_status = _overall_status(statuses)
    return {
        "set_id": str(args.get("set_id") or ""),
        "question_id": str(args.get("question_id") or ""),
        "status": overall_status,
        "requirement_results": normalized_results,
        "orientation_summary": _joined(
            normalized_results,
            "summary",
            for_statuses={"covered", "partial", "conflicted"},
        ),
        "missing_detail": (
            _joined(
                normalized_results,
                "missing_detail",
                for_statuses={"partial", "missing"},
            )
            if overall_status in {"partial", "missing"}
            else ""
        ),
        "conflict_summary": _joined(
            normalized_results,
            "conflict_summary",
            for_statuses={"conflicted"},
        ),
        "deferred_reason": (
            _joined(
                normalized_results,
                "deferred_reason",
                for_statuses={"deferred"},
            )
            if overall_status == "deferred"
            else ""
        ),
        "confidence": 0.5,
        "next_action": "research",
    }


def apply_question_resolution_links(
    *,
    findings_by_id: dict[str, dict[str, Any]],
    set_id: str,
    question: dict[str, Any],
    requirement_results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    question_id = str(question.get("question_id") or "")
    known_requirement_ids = {
        str(item.get("requirement_id") or "")
        for item in question.get("answer_requirements") or []
        if isinstance(item, dict)
    }
    links_by_finding: dict[str, list[str]] = {}
    rationale_by_finding: dict[str, list[str]] = {}
    for result in requirement_results:
        requirement_id = str(result.get("requirement_id") or "")
        if requirement_id not in known_requirement_ids:
            return {
                "accepted": False,
                "error": "question_resolution_references_unknown_requirement",
                "question_id": question_id,
                "requirement_id": requirement_id,
            }
        for finding_id in result.get("finding_ids") or []:
            finding_id = str(finding_id)
            finding = findings_by_id.get(finding_id)
            if finding is None or str(finding.get("set_id") or "") != set_id:
                return {
                    "accepted": False,
                    "error": "question_resolution_references_unknown_or_wrong_set_finding",
                    "finding_id": finding_id,
                    "question_id": question_id,
                }
            links_by_finding.setdefault(finding_id, []).append(requirement_id)
            rationale = str(result.get("closure_basis") or "").strip()
            if rationale:
                rationale_by_finding.setdefault(finding_id, []).append(rationale)
    for finding_id, finding in findings_by_id.items():
        if str(finding.get("set_id") or "") != set_id:
            continue
        links = [
            link
            for link in finding.get("question_links") or []
            if isinstance(link, dict) and str(link.get("question_id") or "") != question_id
        ]
        requirement_ids = list(dict.fromkeys(links_by_finding.get(finding_id, [])))
        if requirement_ids:
            links.append(
                {
                    "question_id": question_id,
                    "requirement_ids": requirement_ids,
                    "rationale": "\n".join(
                        dict.fromkeys(rationale_by_finding.get(finding_id, []))
                    ),
                }
            )
        finding["question_links"] = links
    return None


def _overall_status(statuses: list[str]) -> str:
    status_set = set(statuses)
    if "conflicted" in status_set:
        return "conflicted"
    if status_set == {"covered"}:
        return "covered"
    if status_set == {"missing"}:
        return "missing"
    if status_set and "deferred" in status_set and status_set.issubset({"missing", "deferred"}):
        return "deferred"
    return "partial"


def _joined(
    results: list[dict[str, Any]],
    field: str,
    *,
    for_statuses: set[str],
) -> str:
    values = [
        str(item.get(field) or "").strip()
        for item in results
        if str(item.get("status") or "") in for_statuses
        and str(item.get(field) or "").strip()
    ]
    return "\n".join(dict.fromkeys(values))


__all__ = [
    "apply_question_resolution_links",
    "normalize_question_resolution_args",
    "question_resolution_shape_error",
]
