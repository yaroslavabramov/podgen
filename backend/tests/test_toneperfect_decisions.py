import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen import toneperfect_qualification_cli as cli
from podgen.audio.toneperfect import parse
from podgen.runtime.evaluation.toneperfect.contracts import Trial
from podgen.runtime.evaluation.toneperfect.decisions import summarize
from podgen.runtime.evaluation.toneperfect.export import export
from podgen.runtime.evaluation.toneperfect.service import run
from podgen.runtime.evaluation.toneperfect.store import Store
from tests.test_toneperfect_qualification import Transport, fixture, reply

ROOT = Path(__file__).resolve().parents[2]


def test_projection_separates_every_decision_without_using_score_thresholds():
    long_word = "дословно" * 1000
    raw_decision = dict(future="unrecognized decision")
    response = dict(
        assessable=True,
        scores=dict(overall=99.9),
        words=[
            dict(word="проверить", decision="review", score=100),
            dict(word="услышал", decision="uncertain", score=99.5),
            dict(word="сервис", decision="no_correction", score=0),
            dict(word=long_word, decision=raw_decision, score=100),
            dict(word="новое", decision="accepted_future_value", score=100),
            dict(word="без решения", score=100),
            dict(word="не оценено", decision="no_correction", assessed=False, score=100),
            dict(word="нет оценки", decision="no_correction", score=None),
            dict(text="compact", decision="unassessable"),
        ],
    )
    original = deepcopy(response)
    result = summarize(response)
    assert result["counts"] == dict(review=1, uncertain=1, no_correction=1, other=3, unassessed=3)
    assert result["groups"]["uncertain"][0] == dict(
        index=1, word="услышал", decision="uncertain", score=99.5, assessed=None, omitted=None
    )
    assert result["groups"]["other"][0]["word"] == long_word
    assert result["groups"]["other"][0]["decision"] == raw_decision
    assert response == original
    assert not result["production_ready"] and result["qualification_status"] == "not_evaluated"


def test_no_response_and_unassessable_response_do_not_become_acceptance():
    assert not summarize(None)["available"]
    assert summarize(None)["counts"] == dict(review=0, uncertain=0, no_correction=0, other=0, unassessed=0)
    result = summarize(
        dict(assessable=False, words=[dict(word="не слышно", decision="no_correction", score=100)])
    )
    assert result["counts"]["unassessed"] == 1 and result["counts"]["no_correction"] == 0


@pytest.mark.parametrize(
    "name,target,decision,index",
    [("ordinary-district", "обычной", "review", 1), ("heard-verb", "услышал", "uncertain", 2)],
)
def test_real_errors_parser_and_export_preserve_word_decisions_and_human_evidence(
    tmp_path, name, target, decision, index
):
    source = ROOT / "evals/reports/2026-11-12-toneperfect-real-errors"
    config = Trial.model_validate_json((source / "trial.json").read_bytes())
    raw = (source / name / "response.json").read_bytes()
    parsed = parse(raw, {}, 1)
    original = json.loads(raw)
    assert parsed["provider_response"] == original
    store = Store(tmp_path / name)
    cases = [
        dict(case_id=c.case_id, idempotency_key="saved-" + c.case_id, reservation_credits=1)
        for c in config.cases
    ]
    store.put("manifest.json", dict(configuration=config.model_dump(mode="json"), cases=cases))
    key = f"cases/{name}/attempt-1"
    store.put(key + "/submit.json", dict(idempotency_key="saved-" + name))
    store.put(key + "/result.json", parsed)
    store.blob(key + "/response.bin", raw)
    frozen_manifest = store.bytes("manifest.json")
    frozen_result = store.bytes(key + "/result.json")
    report = export(store, config)
    case = next(c for c in report["cases"] if c["case_id"] == name)
    row = next(w for w in case["provider_word_decisions"]["groups"][decision] if w["word"] == target)
    assert row["index"] == index and row["decision"] == decision
    assert case["provider_word_decisions"]["counts"][decision] == 1
    assert case["operator_evaluation"] == next(c.evaluation for c in config.cases if c.case_id == name)
    assert case["provider_response"] == original
    assert report["word_decision_counts"][decision] == 1
    assert report["qualification_status"] == "not_evaluated" and not report["production_ready"]
    if name == "heard-verb":
        assert row["score"] == 99.5 and original["scores"]["overall"] == 99.9
        assert all(w["index"] != index for w in case["provider_word_decisions"]["groups"]["no_correction"])
    assert store.bytes("manifest.json") == frozen_manifest
    assert store.bytes(key + "/result.json") == frozen_result
    assert store.bytes(key + "/response.bin") == raw


def test_cli_prints_service_decision_counts_separately_from_delivery_status(tmp_path, monkeypatch, capsys):
    config, store = fixture(tmp_path, monkeypatch)
    transport = Transport(reply(words=[dict(word="услышал", score=99.5, decision="uncertain")]))
    run(store, config, transport)
    path = tmp_path / "trial.json"
    path.write_text(config.model_dump_json())
    assert cli.main(["export", "--config", str(path), "--output-dir", str(store.root)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "responses_saved"
    assert summary["word_decision_counts"]["uncertain"] == len(config.cases)
    assert summary["word_decision_counts"]["no_correction"] == 0
    assert summary["qualification_status"] == "not_evaluated" and not summary["production_ready"]
