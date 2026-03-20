"""Phase 5.3: Integrator — check transitions, anti-duplication, complexity flow."""

import copy
import json
import logging
import time
import uuid

from sqlalchemy import select

from app.config import settings
from app.workers.worker_db import worker_session_factory as async_session_factory
from app.models.episode_manifest import EpisodeManifest
from app.pipeline.debug_dump import safe_filename_token, write_json_debug_dump
from app.pipeline.state import PipelineState
from app.pipeline.phase5_planning.act_planner import plan_act
from app.services.llm import call_sonnet_structured
from app.pipeline.llm_json import parse_llm_json
from app.realtime.publisher import publish_event
from app.realtime.events import step_started, step_completed, phase_completed, progress

logger = logging.getLogger(__name__)
INTEGRATOR_RETRIES = 2

INTEGRATOR_SYSTEM = """You are a podcast episode integrator checking the flow between acts.
Given all beat sheets, check:
1. Smooth transitions between acts
2. No duplicated claims across acts
3. Complexity builds gradually
4. Pacing alternates between dense facts and lighter stories

Return JSON:
{
  "transitions": [
    {"from_act": 1, "to_act": 2, "transition_note": "Short bridge from history to mechanics"}
  ],
  "issues_found": [
    {"type": "duplicate_claim|pacing|complexity", "description": "...", "suggestion": "..."}
  ]
}

Rules:
- Return strict JSON only. No markdown fences, no prose before or after the JSON.
- Keep `transition_note` compact: usually 1-2 sentences, enough to bridge acts without rewriting them.
- Keep `issues_found` concise and high-signal. Do not write essay-length diagnostics.
- If no important issue exists, return an empty `issues_found` list.
- Prefer fewer stronger notes over many repetitive ones."""


async def integrate_acts(state: PipelineState) -> dict:
    """LangGraph node: integrate and polish act transitions."""
    run_id = state["generation_run_id"]
    t0 = time.time()
    publish_event(run_id, step_started("phase5_planning", "integrate", "Integrating acts"))

    async with async_session_factory() as db:
        result = await db.execute(
            select(EpisodeManifest).where(EpisodeManifest.id == uuid.UUID(state["manifest_id"]))
        )
        manifest = result.scalar_one_or_none()
        if not manifest or not manifest.acts_json:
            return {"manifest_id": state["manifest_id"]}

        acts = manifest.acts_json
        if not isinstance(acts, list):
            raise RuntimeError("Integrator expected acts_json list in manifest")
        acts = copy.deepcopy(acts)

        missing_beat_sheet_acts = _missing_beat_sheet_acts(acts)
        if missing_beat_sheet_acts:
            logger.warning(
                "Integrator detected empty beat sheets for acts %s; re-running act planner for recovery",
                missing_beat_sheet_acts,
            )
            await plan_act(state)
            await db.refresh(manifest)
            acts = manifest.acts_json
            if not isinstance(acts, list):
                raise RuntimeError("Integrator expected acts_json list in manifest")
            acts = copy.deepcopy(acts)
            missing_beat_sheet_acts = _missing_beat_sheet_acts(acts)
            if missing_beat_sheet_acts:
                raise RuntimeError(
                    "Integrator requires non-empty beat_sheet for acts "
                    + ", ".join(str(a) for a in missing_beat_sheet_acts)
                )

        base_prompt_payload = _build_integrator_prompt_payload(acts)
        raw = ""
        parsed = None
        attempt_debugs: list[dict] = []
        integration = None
        last_error = ""

        for attempt in range(1, INTEGRATOR_RETRIES + 1):
            prompt_payload = dict(base_prompt_payload)
            if attempt > 1:
                prompt_payload["strict_output_contract"] = (
                    "Return valid JSON only with top-level keys transitions and issues_found. No markdown fences."
                )
                prompt_payload["previous_error"] = last_error
            prompt = json.dumps(prompt_payload, ensure_ascii=False)

            try:
                raw = await call_sonnet_structured(
                    system=INTEGRATOR_SYSTEM,
                    prompt=prompt,
                    max_tokens=4096,
                    temperature=0.3,
                )
                parsed = parse_llm_json(raw)
                if not isinstance(parsed, dict):
                    raise ValueError(f"integrator output must be a JSON object, got {type(parsed).__name__}")
                integration = parsed
                attempt_debugs.append(
                    {
                        "attempt": attempt,
                        "status": "ok",
                        "prompt_payload": prompt_payload,
                        "llm_output": {
                            "raw_chars": len(raw or ""),
                            "raw": raw,
                            "parsed_type": type(parsed).__name__ if parsed is not None else None,
                            "parsed": parsed,
                        },
                    }
                )
                break
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                attempt_debugs.append(
                    {
                        "attempt": attempt,
                        "status": "invalid_output",
                        "prompt_payload": prompt_payload,
                        "llm_output": {
                            "raw_chars": len(raw or ""),
                            "raw": raw,
                            "parsed_type": type(parsed).__name__ if parsed is not None else None,
                            "parsed": parsed,
                        },
                        "error": last_error,
                    }
                )
                logger.warning(
                    "Integration output invalid (attempt %s/%s): %s",
                    attempt,
                    INTEGRATOR_RETRIES,
                    last_error,
                    exc_info=True,
                )
                raw = ""
                parsed = None

        if integration is None:
            _dump_integrator_debug(
                run_id=run_id,
                status="llm_or_parse_failed",
                input_payload=base_prompt_payload,
                attempts=attempt_debugs,
                applied_transitions=None,
            )
            logger.warning("Integration step failed after retries")
        else:
            applied_transitions = []
            for tr in integration.get("transitions", []):
                if not isinstance(tr, dict):
                    continue
                try:
                    from_idx = int(tr.get("from_act", 0)) - 1
                except (TypeError, ValueError):
                    continue
                if 0 <= from_idx < len(acts):
                    beat_sheet = acts[from_idx].get("beat_sheet", [])
                    if beat_sheet:
                        transition_note = str(tr.get("transition_note") or "").strip()
                        beat_sheet[-1]["transition_note"] = transition_note
                        applied_transitions.append(
                            {
                                "from_act": from_idx + 1,
                                "to_act": tr.get("to_act"),
                                "transition_note": transition_note,
                            }
                        )

            manifest.acts_json = acts
            await db.commit()
            _dump_integrator_debug(
                run_id=run_id,
                status="ok",
                input_payload=base_prompt_payload,
                attempts=attempt_debugs,
                applied_transitions=applied_transitions,
            )

    publish_event(run_id, step_completed("phase5_planning", "integrate"))
    duration_ms = int((time.time() - t0) * 1000)
    publish_event(run_id, phase_completed("phase5_planning", duration_ms))
    publish_event(run_id, progress(60.0, "Episode structure finalized"))

    return {"manifest_id": state["manifest_id"], "current_phase": "phase5_planning"}


def _missing_beat_sheet_acts(acts: list) -> list[int]:
    missing: list[int] = []
    for i, act in enumerate(acts, start=1):
        act_num = act.get("act_number", i) if isinstance(act, dict) else i
        beat_sheet = act.get("beat_sheet") if isinstance(act, dict) else None
        if not isinstance(beat_sheet, list) or not beat_sheet:
            missing.append(act_num)
    return missing


def _build_integrator_prompt_payload(acts: list[dict]) -> dict:
    return {"acts": acts}


def _dump_integrator_debug(
    *,
    run_id: str,
    status: str,
    input_payload: dict,
    attempts: list[dict],
    applied_transitions: list[dict] | None,
) -> None:
    dump_dir = (settings.web_extractor_debug_dump_dir or "").strip()
    if not dump_dir:
        return
    write_json_debug_dump(
        raw_dir=dump_dir,
        subdir="planning_integrator",
        filename_stem=f"run-{safe_filename_token(run_id)}_{safe_filename_token(status)}",
        payload={
            "run_id": run_id,
            "status": status,
            "input": input_payload,
            "attempts": attempts,
            "applied_transitions": applied_transitions,
        },
    )
