from __future__ import annotations

from typing import Any

from app.generation_v2.model_authored_language import model_authored_language_error


def source_analysis_language_error(
    args: dict[str, Any],
    *,
    output_language: str,
) -> dict[str, Any] | None:
    findings = [item for item in args.get("findings") or [] if isinstance(item, dict)]
    return model_authored_language_error(
        output_language=output_language,
        fields={
            "source_summary": args.get("source_summary"),
            "representation_reason": args.get("representation_reason"),
            "authority_reason": args.get("authority_reason"),
            "uniqueness_reason": args.get("uniqueness_reason"),
            "limitations": args.get("limitations"),
            "no_findings_reason": args.get("no_findings_reason"),
            "reading_basis": args.get("reading_basis"),
            "findings": [
                {
                    "statement": item.get("statement"),
                    "support_explanation": item.get("support_explanation"),
                    "scope": item.get("scope"),
                    "limitations": item.get("limitations"),
                }
                for item in findings
            ],
        },
    )


def question_resolution_language_error(
    args: dict[str, Any],
    *,
    output_language: str,
) -> dict[str, Any] | None:
    results = [item for item in args.get("requirement_results") or [] if isinstance(item, dict)]
    return model_authored_language_error(
        output_language=output_language,
        fields={
            "requirement_results": [
                {
                    "established_answer": item.get("established_answer", item.get("summary")),
                    "criterion_match_reason": item.get(
                        "criterion_match_reason",
                        item.get("closure_basis"),
                    ),
                    "unresolved_remainder": item.get(
                        "unresolved_remainder",
                        item.get("remaining_uncertainty"),
                    ),
                    "missing_detail": item.get("missing_detail"),
                    "conflict_summary": item.get("conflict_summary"),
                    "deferred_reason": item.get("deferred_reason"),
                }
                for item in results
            ],
            "orientation_summary": args.get("orientation_summary"),
            "missing_detail": args.get("missing_detail"),
            "conflict_summary": args.get("conflict_summary"),
            "deferred_reason": args.get("deferred_reason"),
        },
    )


def reading_note_language_error(
    args: dict[str, Any],
    *,
    output_language: str,
) -> dict[str, Any] | None:
    links = [item for item in args.get("question_links") or [] if isinstance(item, dict)]
    return model_authored_language_error(
        output_language=output_language,
        fields={
            "what_was_read": args.get("what_was_read"),
            "what_was_established": args.get("what_was_established"),
            "boundaries": args.get("boundaries"),
            "unread_regions": args.get("unread_regions"),
            "unread_risk_assessment": args.get(
                "unread_risk_assessment",
                args.get("why_unread_is_safe"),
            ),
            "question_links": [item.get("connection") for item in links],
            "doubts": args.get("doubts"),
            "next_checks": args.get("next_checks"),
        },
    )


__all__ = [
    "question_resolution_language_error",
    "reading_note_language_error",
    "source_analysis_language_error",
]
