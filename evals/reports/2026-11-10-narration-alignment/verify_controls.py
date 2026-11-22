"""Review controls preserve saved speech and keep private judgments off the model wire."""

import hashlib
import json
from pathlib import Path

import pytest

from podgen.runtime.evaluation.request_payload import request_payload
from podgen.writing.roles import WriteOutput

ROOT = Path(__file__).resolve().parents[3]
CONTROLS = Path(__file__).resolve().parent / "controls"


def read(path):
    return json.loads(path.read_text())


@pytest.mark.parametrize("case", read(CONTROLS / "cases.json")["cases"], ids=lambda case: case["id"])
def test_saved_speech_and_prepared_cold_reader_request_are_exact(case):
    source = case["source"]
    raw = (ROOT / source["path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == source["sha256"]
    text = raw.decode()
    if "start" in source:
        text = text[source["start"] : source["end"]]
    script = read(CONTROLS / case["script"])
    WriteOutput.model_validate(script)
    assert [row["text"] for row in script["utterances"]] == text.strip().split("\n\n")

    prepared = read(CONTROLS / "prepared.json")
    row = next(row for row in prepared["requests"] if row["case_id"] == case["id"])
    request_bytes = (CONTROLS / row["request"]).read_bytes()
    assert hashlib.sha256(request_bytes).hexdigest() == row["sha256"]
    request = json.loads(request_bytes)
    assert hashlib.sha256(request["instructions"].encode()).hexdigest() == prepared["prompt_sha256"]
    payload = request_payload(request)
    assert set(payload) == {"output_language", "audience", "format", "genre", "script", "duration_context"}
    assert [row["text"] for row in payload["script"]["utterances"]] == [
        row["text"] for row in script["utterances"]
    ]
    assert not prepared["endpoint_called"]


def test_private_expectations_address_exact_existing_words():
    cases = {case["id"]: case for case in read(CONTROLS / "cases.json")["cases"]}
    for check in read(CONTROLS / "expectations.json")["checks"]:
        script = read(CONTROLS / cases[check["case_id"]]["script"])
        for anchor in check["focus"]:
            assert anchor["quote"] in script["utterances"][anchor["utterance_index"]]["text"]
