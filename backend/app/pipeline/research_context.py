"""Helpers for flattening research-tree question context."""

from __future__ import annotations


def build_question_context_map(research_tree_json: dict | None) -> dict[str, dict]:
    if not isinstance(research_tree_json, dict):
        return {}

    contexts: dict[str, dict] = {}
    for branch in research_tree_json.get("branches", []):
        if not isinstance(branch, dict):
            continue
        branch_id = str(branch.get("id", "")).strip()
        if not branch_id:
            continue
        contexts[branch_id] = {
            "question_id": branch_id,
            "branch_id": branch_id,
            "question": branch.get("question", ""),
            "intent": branch.get("intent", ""),
            "priority": branch.get("priority", 5),
            "required_evidence": branch.get("required_evidence", []),
            "expected_evidence": branch.get("expected_evidence", []),
        }
        for child in branch.get("children", []):
            if not isinstance(child, dict):
                continue
            question_id = str(child.get("id", "")).strip()
            if not question_id:
                continue
            contexts[question_id] = {
                "question_id": question_id,
                "branch_id": branch_id,
                "question": child.get("question", ""),
                "intent": child.get("intent", branch.get("intent", "")),
                "priority": child.get("priority", branch.get("priority", 5)),
                "required_evidence": child.get("required_evidence", []),
                "expected_evidence": child.get("expected_evidence", []),
            }
    return contexts
