import base64
import json
from copy import deepcopy

import httpx
import pytest
from pydantic import ValidationError

from podgen.audio.fidelity.adjudication.contracts import WindowResolution
from podgen.audio.fidelity.adjudication.gemini import parse, prepare
from podgen.audio.fidelity.adjudication.windows import group
from podgen.audio.http import RawAudioTransport
from podgen.runtime.evaluation.audio.adjudication.contracts import ReviewerProfile
from tests.audio_cases import signal


def decision(case="C71", status="dismissed", reason="asr_error"):
    return dict(
        case_id=case,
        status=status,
        reason_code=reason,
        heard_spans=[dict(start_seconds=0.1, end_seconds=2, words="тысяча девятьсот семидесятый")],
        explanation="Слышно 1970.",
    )


def response(results):
    return json.dumps(
        dict(
            status="completed",
            id="review-fixture",
            steps=[
                dict(type="model_output", content=[dict(type="text", text=json.dumps(dict(results=results)))])
            ],
            usage=dict(total_input_tokens=100, total_output_tokens=100),
        )
    ).encode()


def test_gemini_port_preserves_prompt_real_audio_short_ids_and_structured_output():
    prompt = "Exact approved system prompt.\n"
    payload = dict(
        output_language="ru",
        cases=[
            dict(case_id="C71", expected_words=["1970"], asr_transcript_excerpt="1971"),
            dict(case_id="C82", expected_words=["1972"], asr_transcript_excerpt="1971"),
        ],
    )
    request, wire = prepare(prompt, payload, signal(), ReviewerProfile())
    body = json.loads(wire)
    assert body["model"] == "gemini-3.8-flash" and body["system_instruction"] == prompt
    assert (
        body["generation_config"]["max_output_tokens"] == 8192
        and "temperature" not in body["generation_config"]
    )
    assert request["headers"]["Api-Revision"] == "2026-05-20"
    assert json.loads(body["input"][0]["text"]) == payload
    assert base64.b64decode(body["input"][1]["data"]) == signal()
    assert body["response_format"]["mime_type"] == "application/json"
    parsed, usage, request_id = parse(
        response([decision(), decision("C82", "confirmed", "audible_mismatch")]), ["C71", "C82"], 5
    )
    assert [r.case_id for r in parsed.results] == ["C71", "C82"] and usage["total_input_tokens"] == 100
    assert request_id == "review-fixture"


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong-id",
        "duplicate",
        "missing",
        "status-reason",
        "outside",
        "empty-confirmed",
        "empty-dismissed",
        "backwards",
        "blank-words",
    ],
)
def test_review_structural_contract_rejects_unsupported_resolutions(mutation):
    rows = [decision(), decision("C82", "confirmed", "audible_mismatch")]
    if mutation == "wrong-id":
        rows[0]["case_id"] = "C81"
    elif mutation == "duplicate":
        rows[1]["case_id"] = "C71"
    elif mutation == "missing":
        rows = rows[:1]
    elif mutation == "status-reason":
        rows[0]["reason_code"] = "audible_mismatch"
    elif mutation == "outside":
        rows[0]["heard_spans"][0]["end_seconds"] = 6
    elif mutation == "empty-confirmed":
        rows[1]["heard_spans"] = []
    elif mutation == "empty-dismissed":
        rows[0]["heard_spans"] = []
    elif mutation == "backwards":
        rows[0]["heard_spans"][0]["end_seconds"] = 0.05
    else:
        rows[0]["heard_spans"][0]["words"] = " "
    with pytest.raises((ValueError, ValidationError)):
        parse(response(rows), ["C71", "C82"], 5)


def test_inconclusive_does_not_require_invented_words_or_positive_evidence():
    row = decision(status="inconclusive", reason="unclear_speech")
    row["heard_spans"] = []
    parsed, _, _ = parse(response([row]), ["C71"], 5)
    assert parsed.results[0].status == "inconclusive"


def test_overlapping_cases_group_without_invented_time_or_oversized_windows():
    def case(name, start, end):
        return dict(
            candidate_id=name, audio_range=dict(start_frame=start, end_frame=end), clip_status="extracted"
        )

    cases = [case("A", 0, 30), case("B", 20, 40), case("C", 100, 140), case("D", 200, 1000)]
    windows, excluded = group(cases, 10, 45, 8)
    assert [(w["start_frame"], w["end_frame"]) for w in windows] == [(0, 40), (100, 140)]
    assert [c["candidate_id"] for c in windows[0]["cases"]] == ["A", "B"]
    assert excluded[0]["case_id"] == "D"
    original = deepcopy(cases)
    group(cases, 10, 45, 1)
    assert cases == original


def test_google_key_header_is_in_memory_only_and_wire_is_archived_body():
    request, wire = prepare(
        "Prompt", dict(output_language="ru", cases=[dict(case_id="C71")]), signal(), ReviewerProfile()
    )
    calls = []

    def handler(req):
        calls.append(req)
        assert req.headers["x-goog-api-key"] == "secret" and "authorization" not in req.headers
        assert req.content == wire
        return httpx.Response(429, json={"error": "quota"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        receipt = RawAudioTransport(client=client, auth_header="x-goog-api-key", auth_prefix="").submit(
            request, wire, "secret"
        )
    assert len(calls) == 1 and receipt.status_code == 429 and receipt.complete
    assert "secret" not in repr(request) and b"secret" not in wire
    assert (
        WindowResolution.model_json_schema()["$defs"]["CaseResolution"]["properties"]["case_id"]["type"]
        == "string"
    )
