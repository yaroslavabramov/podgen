"""Frozen live-reader controls, exact quotation contracts and historical schema pins."""

import importlib.util
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from podgen.writing.reader_schema import feedback_model
from podgen.writing.roles import WriteOutput
from pydantic import ValidationError

REPORT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text())


def speech_model():
    spec = importlib.util.spec_from_file_location("speech_contract", REPORT / "speech_contract.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SpeechProofread


def test_frozen_factorial_changes_only_the_opening_and_the_case_form():
    frozen = read(REPORT / "frozen.json")
    scripts = {case: read(REPORT / "texts" / f"{case}.json") for case in frozen["case_sha256"]}
    for case, expected in frozen["case_sha256"].items():
        assert sha256((REPORT / "texts" / f"{case}.json").read_bytes()).hexdigest() == expected
    for case in ("02", "03", "04"):
        assert scripts[case]["utterances"][4:] == scripts["01"]["utterances"][4:]
    for bad, repaired in (("01", "02"), ("03", "04")):
        expected = deepcopy(scripts[bad])
        expected["utterances"][2]["text"] = expected["utterances"][2]["text"].replace(
            "Из восемьдесят девяти", "Из восьмидесяти девяти"
        )
        assert expected == scripts[repaired]
    for case in ("03", "04"):
        assert all(
            left != right
            for left, right in zip(
                scripts["01"]["utterances"][:4],
                scripts[case]["utterances"][:4],
                strict=True,
            )
        )
    assert sha256((REPORT / "criteria.json").read_bytes()).hexdigest() == frozen["criteria_sha256"]


def test_prompt_comparison_keeps_full_payload_and_provider_settings_identical():
    for case in ("01", "02", "03", "04", "05"):
        old = read(REPORT / "runs/old" / case / "request.json")
        new = read(REPORT / "runs/new-v1" / case / "request.json")
        assert old.pop("instructions") == (REPORT / "old-reader.md").read_text()
        assert new.pop("instructions") == (REPORT / "new-reader-v1.md").read_text()
        old.pop("metadata")
        new.pop("metadata")
        assert old == new
        payload = json.loads(new["input"][0]["content"][0]["text"])
        assert set(payload) == {
            "script",
            "audience",
            "format",
            "genre",
            "output_language",
            "duration_context",
        }
        expected = read(REPORT / "texts" / f"{case}.json")["utterances"]
        assert [
            {k: v for k, v in row.items() if k != "index"} for row in payload["script"]["utterances"]
        ] == expected


def test_all_saved_full_reader_results_validate_without_locator_repair():
    for variant in ("old", "new-v1", "wording-first"):
        paths = list((REPORT / "runs" / variant).glob("*/validated.json"))
        assert {path.parent.name for path in paths} == (
            {"01", "04", "05"} if variant == "wording-first" else {"01", "02", "03", "04", "05"}
        )
        for path in paths:
            script = WriteOutput.model_validate(read(REPORT / "texts" / f"{path.parent.name}.json"))
            review = feedback_model("listener-experience/4").model_validate(read(path))
            review.validate_against(script)
            assert review.model_dump(mode="json") == read(path)


def test_speech_requests_are_frozen_and_contain_only_consecutive_spoken_text():
    criteria = read(REPORT / "speech-criteria.json")
    for row in criteria["requests"]:
        case = row["case"]
        path = REPORT / "runs/speech-v1" / case / "request.json"
        assert sha256(path.read_bytes()).hexdigest() == row["request_sha256"]
        body = read(path)
        assert sha256(body["instructions"].encode()).hexdigest() == criteria["prompt_sha256"]
        assert body["text"]["format"]["schema"] == criteria["schema"]
        payload = json.loads(body["input"][0]["content"][0]["text"])
        assert set(payload) == {"output_language", "utterances"}
        expected = read(REPORT / "texts" / f"{case}.json")["utterances"]
        expected = expected if case == "05" else expected[:4]
        assert payload["utterances"] == [
            dict(index=index, **utterance) for index, utterance in enumerate(expected)
        ]
        result = path.parent / "validated.json"
        if result.exists():
            speech_model().model_validate(read(result)).validate_against(payload["utterances"])


def test_final_director_contrast_changes_only_the_instruction():
    criteria = read(REPORT / "director-criteria.json")
    assert {row["case"] for row in criteria["requests"]} == {"01", "04", "05"}
    for row in criteria["requests"]:
        path = REPORT / "runs/speech-v2" / row["case"] / "request.json"
        assert sha256(path.read_bytes()).hexdigest() == row["request_sha256"]
        new = read(path)
        prior = read(REPORT / "runs/speech-v1" / row["case"] / "request.json")
        payload = json.loads(new["input"][0]["content"][0]["text"])
        assert sha256(new["instructions"].encode()).hexdigest() == criteria["prompt_sha256"]
        for body in (prior, new):
            body.pop("instructions")
            body.pop("metadata")
        assert prior == new
        result = path.parent / "validated.json"
        if result.exists():
            speech_model().model_validate(read(result)).validate_against(payload["utterances"])


def test_excerpt_contract_allows_empty_findings_and_rejects_invalid_addresses():
    model = speech_model()
    model.model_validate({"findings": []}).validate_against([])
    finding = {
        "kind": "grammar",
        "required": True,
        "anchors": [{"utterance_index": 7, "quote": "Actual words"}],
        "observation": "An addressed problem.",
        "requested_change": "A concrete repair.",
        "preserve": ["The transition."],
    }
    excerpt = [{"index": 7, "text": "Actual words remain intact."}]
    model.model_validate({"findings": [finding]}).validate_against(excerpt)
    for anchor in (
        {"utterance_index": 0, "quote": "Actual words"},
        {"utterance_index": 7, "quote": "invented"},
    ):
        invalid = deepcopy(finding)
        invalid["anchors"] = [anchor]
        with pytest.raises(ValueError, match="invalid_speech_quote_anchor"):
            model.model_validate({"findings": [invalid]}).validate_against(excerpt)
    with pytest.raises(ValidationError):
        model.model_validate({"findings": [dict(finding, expected="operator-only label")]})
    long_finding = dict(finding, observation="Full semantic detail. " * 1000)
    assert (
        model.model_validate({"findings": [long_finding]}).findings[0].observation
        == long_finding["observation"]
    )
