import json

from app.pipeline.phase4_claims.verifier import _dump_verifier_debug
from app.pipeline.phase5_planning.act_planner import _dump_act_planner_debug
from app.pipeline.phase5_planning.showrunner import _dump_showrunner_debug


def test_dump_verifier_debug_writes_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.settings.web_extractor_debug_dump_dir",
        str(tmp_path),
        raising=False,
    )

    _dump_verifier_debug(
        run_id="run-100",
        dump_type="summary",
        payload={"run_id": "run-100", "claim_count": 3, "research_gaps": []},
    )

    files = list((tmp_path / "verifier").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-100"
    assert payload["claim_count"] == 3


def test_dump_showrunner_debug_writes_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.showrunner.settings.web_extractor_debug_dump_dir",
        str(tmp_path),
        raising=False,
    )

    _dump_showrunner_debug(
        run_id="run-200",
        status="ok",
        input_payload={"duration_target_seconds": 1200, "claims": []},
        llm_raw_output='{"acts":[]}',
        parsed_plan={"acts": []},
        normalized_plan={"act_count": 0, "acts": []},
    )

    files = list((tmp_path / "planning_showrunner").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-200"
    assert payload["status"] == "ok"
    assert payload["normalized_plan"]["act_count"] == 0


def test_dump_act_planner_debug_writes_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase5_planning.act_planner.settings.web_extractor_debug_dump_dir",
        str(tmp_path),
        raising=False,
    )

    _dump_act_planner_debug(
        run_id="run-300",
        act_number=2,
        status="ok",
        base_prompt={"act_number": 2, "claims": [], "evidence": []},
        attempts=[{"attempt": 1, "status": "ok"}],
        beat_sheet=[{"scene": 1, "type": "hook", "description": "Open"}],
        gap_fill_result={"new_evidence_ids": [], "new_evidence_count": 0},
        quality_issues=[],
    )

    files = list((tmp_path / "planning_acts").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-300"
    assert payload["act_number"] == 2
    assert payload["normalized_beat_sheet"][0]["scene"] == 1


def test_dump_verifier_debug_skips_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.verifier.settings.web_extractor_debug_dump_dir",
        "",
        raising=False,
    )

    _dump_verifier_debug(
        run_id="run-100",
        dump_type="summary",
        payload={"run_id": "run-100"},
    )

    assert list(tmp_path.glob("**/*.json")) == []
