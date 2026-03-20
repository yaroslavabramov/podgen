from app.pipeline.phase5_planning.integrator import (
    INTEGRATOR_SYSTEM,
    _build_integrator_prompt_payload,
    _missing_beat_sheet_acts,
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
