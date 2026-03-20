import uuid
from types import SimpleNamespace

import pytest

from app.pipeline.phase5_planning.integrator import (
    INTEGRATOR_SYSTEM,
    _build_integrator_prompt_payload,
    _missing_beat_sheet_acts,
    integrate_acts,
)


def test_build_integrator_prompt_payload_keeps_acts_structure() -> None:
    acts = [
        {
            "act_number": 1,
            "title": "Origin",
            "beat_sheet": [
                {"scene": 1, "type": "hook", "description": "Open"},
                {"scene": 2, "type": "takeaway", "description": "Land"},
            ],
        },
        {
            "act_number": 2,
            "title": "Mechanism",
            "beat_sheet": [
                {"scene": 1, "type": "explanation", "description": "Explain"},
            ],
        },
    ]

    payload = _build_integrator_prompt_payload(acts)

    assert payload == {"acts": acts}


def test_missing_beat_sheet_acts_reports_empty_or_missing_lists() -> None:
    acts = [
        {"act_number": 1, "beat_sheet": [{"scene": 1}]},
        {"act_number": 2, "beat_sheet": []},
        {"act_number": 3},
    ]

    assert _missing_beat_sheet_acts(acts) == [2, 3]


def test_integrator_prompt_demands_strict_json_and_compact_output() -> None:
    assert "Return strict JSON only. No markdown fences" in INTEGRATOR_SYSTEM
    assert "Keep `transition_note` compact" in INTEGRATOR_SYSTEM
    assert "Keep `issues_found` concise and high-signal." in INTEGRATOR_SYSTEM
    assert "If no important issue exists, return an empty `issues_found` list." in INTEGRATOR_SYSTEM


class _FakeResult:
    def __init__(self, scalar) -> None:
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeIntegratorDB:
    def __init__(self, manifest) -> None:
        self.manifest = manifest

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _statement):
        return _FakeResult(self.manifest)

    async def refresh(self, _obj) -> None:
        return None

    async def commit(self) -> None:
        return None


@pytest.mark.asyncio
async def test_integrator_raises_after_retry_exhaustion(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = SimpleNamespace(
        id=uuid.uuid4(),
        acts_json=[
            {
                "act_number": 1,
                "beat_sheet": [{"scene": 1, "type": "hook", "description": "Open"}],
            },
            {
                "act_number": 2,
                "beat_sheet": [{"scene": 1, "type": "takeaway", "description": "Land"}],
            },
        ],
    )
    db = _FakeIntegratorDB(manifest)
    dumps = []

    async def _fake_call_sonnet_structured(**_kwargs):
        return "```json\n{\"transitions\": ["

    monkeypatch.setattr("app.pipeline.phase5_planning.integrator.async_session_factory", lambda: db)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.integrator.call_sonnet_structured",
        _fake_call_sonnet_structured,
    )
    monkeypatch.setattr("app.pipeline.phase5_planning.integrator.publish_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.integrator._dump_integrator_debug",
        lambda **kwargs: dumps.append(kwargs),
    )

    with pytest.raises(RuntimeError, match="Integrator failed after 2 attempts"):
        await integrate_acts(
            {
                "generation_run_id": str(uuid.uuid4()),
                "manifest_id": str(manifest.id),
            }
        )

    assert len(dumps) == 1
    assert dumps[0]["status"] == "llm_or_parse_failed"
    assert dumps[0]["applied_transitions"] is None
