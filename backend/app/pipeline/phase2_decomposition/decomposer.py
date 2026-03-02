"""Phase 2: Query Decomposition — Research Tree + Duration Fill Strategy."""

import json
import logging
import re
import time
import uuid
from typing import Any

from sqlalchemy import select

from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.content_map import ContentMap
from app.models.episode_manifest import EpisodeManifest
from app.models.show_bible import ShowBible
from app.pipeline.state import PipelineState
from app.pipeline.phase2_decomposition.duration_strategy import compute_fill_layers
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event, update_generation_progress
from app.realtime.events import phase_started, phase_completed, step_started, step_completed, progress

logger = logging.getLogger(__name__)

_ALLOWED_INTENTS = {"history", "explain", "compare", "debate", "case_study"}
_ALLOWED_QUALITY_NEED = {"VERIFIED", "OBSERVED", "ANY"}
_ALLOWED_EVIDENCE_TYPES = {
    "TIMELINE",
    "CASE",
    "EXPERT_QUOTE",
    "STATISTIC",
    "ANECDOTE",
    "DEFINITION",
    "MECHANISM",
    "CONTRAST",
}
_BRANCH_ID_RE = re.compile(r"^b\d+$")
_CHILD_ID_RE = re.compile(r"^b\d+\.\d+$")

DECOMPOSER_SYSTEM = """You are a research decomposer for a long-form podcast.
Given topic, content_map, audience_profile, adjacency_policy, duration_target_seconds, and language, build a research tree.

Return valid JSON:
{
  "root_question": "string",
  "branches": [
    {
      "id": "b1",
      "question": "string",
      "intent": "history|explain|compare|debate|case_study",
      "required_evidence": [
        {
          "type": "TIMELINE|CASE|EXPERT_QUOTE|STATISTIC|ANECDOTE|DEFINITION|MECHANISM|CONTRAST",
          "min_count": 1
        }
      ],
      "expected_evidence": ["TIMELINE","CASE","EXPERT_QUOTE","STATISTIC","ANECDOTE","DEFINITION","MECHANISM","CONTRAST"],
      "quality_need": "VERIFIED|OBSERVED|ANY",
      "priority": 1,
      "children": [
        {
          "id": "b1.1",
          "question": "string",
          "intent": "history|explain|compare|debate|case_study",
          "required_evidence": [
            {
              "type": "TIMELINE|CASE|EXPERT_QUOTE|STATISTIC|ANECDOTE|DEFINITION|MECHANISM|CONTRAST",
              "min_count": 1
            }
          ],
          "expected_evidence": ["TIMELINE","CASE","EXPERT_QUOTE","STATISTIC","ANECDOTE","DEFINITION","MECHANISM","CONTRAST"],
          "quality_need": "VERIFIED|OBSERVED|ANY",
          "priority": 1
        }
      ]
    }
  ]
}

Rules:
1) Language:
- All human-readable fields MUST be in language from the user payload.
- Do not mix languages inside one field.

2) Grounding and exploration:
- Use content_map as the primary grounding signal.
- Do NOT present unstated facts as true.
- If a useful angle is not explicitly confirmed in content_map, phrase it as a researchable check/question (exploratory framing), not as an asserted fact.
- Do not add specific names, dates, or numbers unless they are present in content_map.

3) Focus and anti-drift:
- Every branch must directly help answer root_question.
- Context/comparison branches are allowed only when they clearly return to the core topic.
- Avoid duplicate or near-duplicate branches.

4) Audience and adjacency:
- Prioritize branches using audience_profile.explain and audience_profile.level.
- Respect adjacency_policy: short, instrumental detours only.

5) Size adaptation by duration_target_seconds:
- <= 1500s: 4-5 top-level branches, 2-4 children each.
- 1501-3000s: 5-6 top-level branches, 3-5 children each.
- > 3000s: 6-7 top-level branches, 3-5 children each.

6) Priority semantics:
- 1 = must-have to answer root_question.
- 2 = important supporting context.
- 3 = optional but useful extension.
- 4+ = stretch context only.
- Keep branch priorities contiguous and meaningful.

7) quality_need semantics:
- VERIFIED for core factual/mechanistic/historical claims.
- OBSERVED for interpretive or less-certain claims that still need evidence.
- ANY only for speculative framing or open debate tails.

8) Evidence contract:
- required_evidence = strict minimum evidence assets needed for this question.
- expected_evidence = useful but non-blocking assets that improve storytelling.
- Use 1-3 required_evidence items; each min_count must be >= 1.
- Keep expected_evidence concise (0-4 items).
- Do not duplicate a type across required_evidence and expected_evidence.

9) Researchability:
- Each question must be answerable via retrievable evidence (corpus/web), not pure opinion prompts.
- Child questions must be concrete sub-questions of their parent.

10) IDs and contract:
- Use ids in the format b1..bn and b1.1..b1.k without gaps.
- Return ONLY the JSON object matching the schema above.

Failure behavior:
- If content_map is sparse/noisy, return a minimal valid tree with grounded core branches plus exploratory checks.
- Never return empty branches.
"""


async def decompose(state: PipelineState) -> dict:
    """LangGraph node: build research tree and duration fill strategy."""
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, phase_started("phase2_decomposition", "Decomposing topic into research tree"))
    update_generation_progress(run_id, "phase2_decomposition", "decomposer", 10.0)

    async with async_session_factory() as db:
        # Load ContentMap
        content_map_data = {}
        if state.get("content_map_id"):
            result = await db.execute(
                select(ContentMap).where(ContentMap.id == uuid.UUID(state["content_map_id"]))
            )
            cm = result.scalar_one_or_none()
            if cm:
                content_map_data = cm.map_json

        # Load ShowBible
        bible_data = {}
        if state.get("show_bible_id"):
            result = await db.execute(
                select(ShowBible).where(ShowBible.id == uuid.UUID(state["show_bible_id"]))
            )
            bible = result.scalar_one_or_none()
            if bible:
                bible_data = {
                    "audience_profile": bible.audience_profile,
                    "adjacency_policy": bible.adjacency_policy,
                    "show_style": bible.show_style,
                }

        # Build research tree
        publish_event(run_id, step_started("phase2_decomposition", "build_tree", "Building research tree"))

        prompt_payload = {
            "topic": state["user_prompt"],
            "duration_target_seconds": state["duration_target"],
            "language": state["language"],
            "content_map": _compact_content_map(content_map_data),
            "audience_profile": bible_data.get("audience_profile", {}),
            "adjacency_policy": bible_data.get("adjacency_policy", ""),
            "show_style": bible_data.get("show_style", "Conversational"),
        }
        prompt = json.dumps(prompt_payload, ensure_ascii=False)

        raw = await call_sonnet_structured(
            system=DECOMPOSER_SYSTEM,
            prompt=prompt,
            temperature=0.6,
        )
        research_tree = parse_llm_json(raw)
        research_tree = _sanitize_and_validate_research_tree(research_tree)

        publish_event(run_id, step_completed("phase2_decomposition", "build_tree"))

        # Compute duration fill layers
        publish_event(run_id, step_started("phase2_decomposition", "duration_strategy", "Computing duration fill layers"))
        duration_minutes = state["duration_target"] / 60
        branch_count = len(research_tree.get("branches", []))
        fill_layers = compute_fill_layers(duration_minutes, branch_count, bible_data.get("audience_profile", {}))
        publish_event(run_id, step_completed("phase2_decomposition", "duration_strategy"))

        # Create or update EpisodeManifest for this run (idempotent resume/retry).
        run_uuid = uuid.UUID(run_id)
        existing_result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.generation_run_id == run_uuid)
        )
        manifest = existing_result.scalar_one_or_none()

        if manifest is None:
            manifest = EpisodeManifest(
                id=uuid.uuid4(),
                generation_run_id=run_uuid,
                research_tree_json=research_tree,
                duration_fill_layers=fill_layers,
            )
            db.add(manifest)
        else:
            manifest.research_tree_json = research_tree
            manifest.duration_fill_layers = fill_layers
            # Reset downstream planning fields when decomposition reruns.
            manifest.act_count = None
            manifest.acts_json = None

        await db.commit()
        manifest_id = str(manifest.id)

    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase2_decomposition", duration_ms))
    update_generation_progress(run_id, "phase2_decomposition", "complete", 15.0)

    branch_total = sum(1 + len(b.get("children", [])) for b in research_tree.get("branches", []))
    publish_event(run_id, progress(15.0, f"Research tree: {len(research_tree.get('branches', []))} angles, {branch_total} drill-downs"))

    return {"manifest_id": manifest_id, "current_phase": "phase2_decomposition"}


def _compact_content_map(content_map_data: Any) -> dict[str, Any]:
    """Keep content map signal while avoiding nested JSON strings and giant payloads."""
    if not isinstance(content_map_data, dict):
        return {}

    def _clip_text(value: Any, limit: int) -> str:
        return str(value or "")[:limit]

    compact_topics: list[dict[str, Any]] = []
    for raw in content_map_data.get("topics", [])[:8]:
        if not isinstance(raw, dict):
            continue
        compact_topics.append(
            {
                "name": _clip_text(raw.get("name"), 120),
                "description": _clip_text(raw.get("description"), 240),
                "chunk_count": raw.get("chunk_count"),
            }
        )

    compact_entities: list[dict[str, Any]] = []
    for raw in content_map_data.get("entities", [])[:12]:
        if not isinstance(raw, dict):
            continue
        compact_entities.append(
            {
                "name": _clip_text(raw.get("name"), 120),
                "type": _clip_text(raw.get("type"), 80),
                "mentions": raw.get("mentions"),
            }
        )

    compact_terms: list[dict[str, Any]] = []
    for raw in content_map_data.get("key_terms", [])[:12]:
        if not isinstance(raw, dict):
            continue
        compact_terms.append(
            {
                "term": _clip_text(raw.get("term"), 120),
                "context": _clip_text(raw.get("context"), 180),
            }
        )

    compact_gaps = [
        _clip_text(gap, 180)
        for gap in (content_map_data.get("gaps", []) if isinstance(content_map_data.get("gaps"), list) else [])
        if str(gap).strip()
    ][:8]

    compact: dict[str, Any] = {
        "summary": _clip_text(content_map_data.get("summary"), 1200),
        "topics": compact_topics,
        "entities": compact_entities,
        "key_terms": compact_terms,
        "gaps": compact_gaps,
    }

    if content_map_data.get("_raw_response"):
        compact["parse_warning"] = "content_map_parse_failed"

    return compact


def _sanitize_and_validate_research_tree(research_tree: Any) -> dict[str, Any]:
    """Sanitize legacy fields and fail-fast on schema violations."""
    if not isinstance(research_tree, dict):
        raise ValueError("Decomposer output must be a JSON object")

    root_question = research_tree.get("root_question")
    if not isinstance(root_question, str) or not root_question.strip():
        raise ValueError("Decomposer output must contain non-empty root_question")

    raw_branches = research_tree.get("branches")
    if not isinstance(raw_branches, list) or not raw_branches:
        raise ValueError("Decomposer output must contain non-empty branches array")

    branch_ids: set[str] = set()
    child_ids: set[str] = set()
    normalized_branches: list[dict[str, Any]] = []

    for idx, raw_branch in enumerate(raw_branches, start=1):
        branch = _validate_node(raw_branch, path=f"branches[{idx - 1}]", is_child=False)
        branch_id = branch["id"]
        if branch_id in branch_ids:
            raise ValueError(f"Duplicate branch id: {branch_id}")
        branch_ids.add(branch_id)

        raw_children = raw_branch.get("children")
        if not isinstance(raw_children, list):
            raise ValueError(f"{idx}: children must be a list")

        normalized_children: list[dict[str, Any]] = []
        for cidx, raw_child in enumerate(raw_children, start=1):
            child = _validate_node(raw_child, path=f"branches[{idx - 1}].children[{cidx - 1}]", is_child=True)
            child_id = child["id"]
            if child_id in child_ids:
                raise ValueError(f"Duplicate child id: {child_id}")
            child_ids.add(child_id)
            normalized_children.append(child)

        branch["children"] = normalized_children
        normalized_branches.append(branch)

    return {
        "root_question": root_question.strip(),
        "branches": normalized_branches,
    }


def _validate_node(raw: Any, *, path: str, is_child: bool) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be an object")

    node_id = raw.get("id")
    if not isinstance(node_id, str) or not node_id.strip():
        raise ValueError(f"{path}.id must be a non-empty string")
    node_id = node_id.strip()

    expected_re = _CHILD_ID_RE if is_child else _BRANCH_ID_RE
    if not expected_re.match(node_id):
        expected = "bN.M" if is_child else "bN"
        raise ValueError(f"{path}.id must match {expected}")

    question = raw.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"{path}.question must be a non-empty string")

    intent = raw.get("intent")
    if not isinstance(intent, str):
        raise ValueError(f"{path}.intent must be a string")
    intent_normalized = intent.strip().lower()
    if intent_normalized not in _ALLOWED_INTENTS:
        raise ValueError(f"{path}.intent is invalid: {intent}")

    required_evidence = raw.get("required_evidence")
    expected_evidence = raw.get("expected_evidence")
    legacy_expected_evidence = raw.get("expected_evidence_type")

    if required_evidence is None and expected_evidence is None and legacy_expected_evidence is not None:
        # Backward compatibility for older manifests/prompts.
        legacy_normalized = _normalize_evidence_type_list(
            legacy_expected_evidence,
            path=f"{path}.expected_evidence_type",
            require_non_empty=True,
        )
        required_evidence = [{"type": asset_type, "min_count": 1} for asset_type in legacy_normalized]
        expected_evidence = []

    normalized_required = _normalize_required_evidence(required_evidence, path=f"{path}.required_evidence")

    if expected_evidence is None:
        normalized_expected: list[str] = []
    else:
        normalized_expected = _normalize_evidence_type_list(
            expected_evidence,
            path=f"{path}.expected_evidence",
            require_non_empty=False,
        )

    required_types = {item["type"] for item in normalized_required}
    normalized_expected = [item for item in normalized_expected if item not in required_types]

    quality_need = raw.get("quality_need")
    if not isinstance(quality_need, str):
        raise ValueError(f"{path}.quality_need must be a string")
    quality_need_normalized = quality_need.strip().upper()
    if quality_need_normalized not in _ALLOWED_QUALITY_NEED:
        raise ValueError(f"{path}.quality_need is invalid: {quality_need}")

    priority = raw.get("priority")
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 1:
        raise ValueError(f"{path}.priority must be an integer >= 1")

    return {
        "id": node_id,
        "question": question.strip(),
        "intent": intent_normalized,
        "required_evidence": normalized_required,
        "expected_evidence": normalized_expected,
        "quality_need": quality_need_normalized,
        "priority": priority,
    }


def _normalize_evidence_type_list(
    raw_value: Any,
    *,
    path: str,
    require_non_empty: bool,
) -> list[str]:
    if not isinstance(raw_value, list):
        raise ValueError(f"{path} must be a list")
    if require_non_empty and not raw_value:
        raise ValueError(f"{path} must be a non-empty list")

    normalized: list[str] = []
    seen: set[str] = set()
    for item in raw_value:
        if not isinstance(item, str):
            raise ValueError(f"{path} items must be strings")
        evidence = item.strip().upper()
        if evidence not in _ALLOWED_EVIDENCE_TYPES:
            raise ValueError(f"{path} contains invalid value: {item}")
        if evidence in seen:
            continue
        seen.add(evidence)
        normalized.append(evidence)
    return normalized


def _normalize_required_evidence(raw_value: Any, *, path: str) -> list[dict[str, int | str]]:
    if not isinstance(raw_value, list) or not raw_value:
        raise ValueError(f"{path} must be a non-empty list")

    dedup: dict[str, int] = {}
    order: list[str] = []
    for idx, item in enumerate(raw_value):
        item_path = f"{path}[{idx}]"
        if not isinstance(item, dict):
            raise ValueError(f"{item_path} must be an object")

        evidence_type_raw = item.get("type")
        if not isinstance(evidence_type_raw, str):
            raise ValueError(f"{item_path}.type must be a string")
        evidence_type = evidence_type_raw.strip().upper()
        if evidence_type not in _ALLOWED_EVIDENCE_TYPES:
            raise ValueError(f"{item_path}.type contains invalid value: {evidence_type_raw}")

        min_count = item.get("min_count")
        if isinstance(min_count, bool) or not isinstance(min_count, int) or min_count < 1:
            raise ValueError(f"{item_path}.min_count must be an integer >= 1")

        if evidence_type not in dedup:
            dedup[evidence_type] = min_count
            order.append(evidence_type)
        else:
            dedup[evidence_type] = max(dedup[evidence_type], min_count)

    return [{"type": evidence_type, "min_count": dedup[evidence_type]} for evidence_type in order]
