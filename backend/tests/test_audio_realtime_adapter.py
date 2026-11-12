import base64
import json
from copy import deepcopy
from pathlib import Path

import pytest

from podgen.audio.fidelity.adjudication.realtime import parse, prepare
from podgen.audio.media.review_projection import pcm
from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.audio.adjudication.contracts import RealtimeReviewerProfile
from tests.audio_cases import signal
from tests.audio_realtime_cases import done


def transcript(response):
    return json.dumps(dict(events=[dict(direction="server", raw=json.dumps(response))])).encode()


def test_frozen_realtime_wire_preserves_role_payload_audio_and_forces_only_review_tool():
    payload = dict(output_language="ru", cases=[dict(case_id="C1")])
    request, wire = prepare("Exact prompt.\n", payload, signal(), RealtimeReviewerProfile())
    events = json.loads(wire)
    session = events[0]["session"]
    assert session["instructions"] == "Exact prompt.\n\n\nReturn the results by calling submit_audio_review."
    assert session["reasoning"] == dict(effort="medium") and session["max_output_tokens"] == 4096
    assert session["audio"]["input"]["turn_detection"] is None
    assert session["output_modalities"] == ["text"]
    assert [tool["name"] for tool in session["tools"]] == ["submit_audio_review"]
    assert session["tool_choice"] == dict(type="function", name="submit_audio_review")
    assert json.loads(events[1]["item"]["content"][0]["text"]) == payload
    assert base64.b64decode(events[2]["audio"]) == pcm(signal())[0]
    assert "Authorization" not in wire.decode() and request["transport"] == "websocket"


@pytest.mark.parametrize("name,case", [("altered", "C2"), ("swapped", "C1")])
def test_real_completed_controls_allow_commentary_but_use_only_function(name, case):
    response = json.loads(
        (Path(__file__).parent / f"fixtures/audio/realtime-{name}-response.json").read_text()
    )
    assert any(item["type"] == "message" for item in response["output"])
    result, usage, request_id = parse(transcript(dict(type="response.done", response=response)), [case], 12)
    assert result.results[0].status == "confirmed" and request_id == response["id"]
    assert usage == response["usage"]
    words = " ".join(s.words for s in result.results[0].heard_spans)
    assert ("1970" in words) if name == "altered" else ("Лондон" in words)


@pytest.mark.parametrize(
    "mutation", ["foreign", "duplicate", "incomplete", "no-call", "bad-json", "wrong-id", "message-only"]
)
def test_realtime_rejects_invalid_or_ambiguous_result(mutation):
    event = done(dict(cases=[dict(case_id="C1")]))
    response, call = event["response"], event["response"]["output"][-1]
    if mutation == "foreign":
        call["name"] = "other_tool"
    elif mutation == "duplicate":
        response["output"].append(deepcopy(call))
    elif mutation == "incomplete":
        response["status"] = "incomplete"
    elif mutation in ("no-call", "message-only"):
        response["output"] = response["output"][:1]
    elif mutation == "bad-json":
        call["arguments"] = "bad JSON"
    else:
        call["arguments"] = call["arguments"].replace("C1", "C2")
    with pytest.raises((ContractError, ValueError)):
        parse(transcript(event), ["C1"], 5)
