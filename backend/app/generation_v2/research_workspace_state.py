from __future__ import annotations

from typing import Any


WORKSPACE_SCHEMA_VERSION = "research_workspace.v2"


def normalize_workspace(workspace: dict[str, Any]) -> dict[str, Any]:
    workspace = dict(workspace or {})
    workspace.setdefault("schema_version", WORKSPACE_SCHEMA_VERSION)
    workspace.setdefault("source_order", [])
    workspace.setdefault("research_questions", [])
    workspace.setdefault("investigation_jobs", [])
    workspace.setdefault("processed_jobs", [])
    workspace.setdefault("covered_source_refs", [])
    workspace.setdefault("selected_evidence_ids", [])
    workspace.setdefault("atom_drafts", [])
    workspace.setdefault("open_questions", [])
    workspace.setdefault("research_gaps", [])
    workspace.setdefault("risk_register", [])
    workspace.setdefault("memo_fields", {})
    return workspace
