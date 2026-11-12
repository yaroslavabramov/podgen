"""Narrow saved-audio experiment. Each named request can be submitted only once."""

import argparse
import asyncio
import copy
import hashlib
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from podgen.audio.realtime_transport import RealtimeTransport
from podgen.domain.canonical import canonical_bytes
from text_transport import submit_text

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
BASE = ROOT / "tmp/audio-blind-comparison-2026-11-12"
SOURCE = ROOT / "tmp/audio-review-semantics-2026-11-12"
CONTROLS = ("feminine-exact", "feminine-altered", "date-equivalent", "word-boundary")


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_comparison(folder, text):
    candidate = text.strip()
    fenced = candidate.startswith("```json\n") and candidate.endswith("\n```")
    if fenced:
        candidate = candidate[len("```json\n"):-len("\n```")]
    result = json.loads(candidate)
    payload = read(folder / "payload.json")
    assert set(result) == {"case_id", "verdict", "transcript_quote", "expected_words", "explanation"}
    assert result["case_id"] == payload["case_id"]
    assert result["expected_words"] == payload["expected_words"]
    assert result["verdict"] in ("matches", "differs", "unresolved")
    assert result["transcript_quote"] in payload["transcript"]
    write(folder / "parse.json", {"outer_json_fence_removed": fenced, "original_output_preserved": True})
    write(folder / "result.json", result)


def prepare(stage, name):
    source_name = "feminine-exact" if name == "feminine-altered" else name
    original = read(SOURCE / source_name / "wire.json")
    events = copy.deepcopy(original)
    session = events[0]["session"]
    session.update(instructions=(HERE / f"{stage}.md").read_text(), tools=[], tool_choice="auto")
    provenance = {"stage": stage, "control": name, "model": session["model"]}
    if stage == "transcribe":
        if name == "feminine-altered":
            raise ValueError("Reuse the exact same frozen transcription, do not transcribe again")
        payload = {"output_language": "ru", "task": "Transcribe the supplied audio."}
        assert events[2:] == original[2:]
        provenance["source_wire_sha256"] = digest((SOURCE / name / "wire.json").read_bytes())
        provenance["audio_projection"] = read(SOURCE / name / "request.json")["audio_projection"]
    else:
        transcript_path = BASE / "transcribe" / source_name / "transcript.txt"
        transcript = transcript_path.read_text()
        source_result = read(BASE / "transcribe" / source_name / "response.json")
        case = read(SOURCE / name / "payload.json")["cases"][0]
        payload = {"output_language": "ru", "transcript": transcript,
                   **{key: case[key] for key in ("case_id", "expected_words", "expected_context")}}
        if name == "date-equivalent":
            # Exercise numerical equivalence after ASR is removed from the task.
            payload["expected_words"] = "1"
            provenance["reference_transform"] = {"original": case["expected_words"], "test": "1"}
        provenance.update(transcript_sha256=digest(transcript.encode()),
                          transcription_response_id=source_result["id"])
        events = events[:2] + events[4:]
    events[1]["item"]["content"][0]["text"] = json.dumps(payload, ensure_ascii=False)
    folder = BASE / stage / name
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "submit-intent.json").exists():
        raise RuntimeError("Existing intent: inspect artifacts; never automatically repeat")
    wire = canonical_bytes(events)
    (folder / "wire.json").write_bytes(wire)
    write(folder / "payload.json", payload)
    provenance["wire_sha256"] = digest(wire)
    write(folder / "manifest.json", provenance)
    return folder, events, wire, read(SOURCE / source_name / "request.json")


def run(stage, name, execute):
    folder, events, wire, request = prepare(stage, name)
    if not execute:
        print(folder)
        return
    load_dotenv(ROOT / ".env", override=True)
    credential = os.environ["OPENAI_API_KEY"]
    # Written before network: ambiguous or failed requests are not retried.
    write(folder / "submit-intent.json", {"time": time.time(), "wire_sha256": digest(wire)})
    receipt = (RealtimeTransport(timeout_seconds=180).submit(request, wire, credential)
               if stage == "transcribe" else asyncio.run(submit_text(events, credential)))
    (folder / "events.json").write_bytes(receipt.body)
    write(folder / "receipt.json", {k: v for k, v in receipt.__dict__.items() if k != "body"})
    if receipt.error or not receipt.response_done:
        raise RuntimeError("Incomplete exchange; inspect saved receipt, no automatic retry")
    responses = [e["response"] for row in json.loads(receipt.body)["events"]
                 if row["direction"] == "server"
                 and (e := json.loads(row["raw"])).get("type") == "response.done"]
    if len(responses) != 1:
        raise ValueError("Expected one complete response")
    response = responses[0]
    write(folder / "response.json", response)
    if response["status"] != "completed":
        raise ValueError("Response incomplete")
    texts = [c["text"] for item in response["output"] if item["type"] == "message"
             for c in item["content"] if c["type"] == "output_text"]
    if len(texts) != 1:
        raise ValueError("Expected exactly one text output")
    text = texts[0]
    (folder / ("transcript.txt" if stage == "transcribe" else "comparison.txt")).write_text(text)
    if stage == "compare":
        parse_comparison(folder, text)
    print(json.dumps({"stage": stage, "control": name, "text": text,
                      "usage": response["usage"]}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("transcribe", "compare"))
    parser.add_argument("name", choices=CONTROLS)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    run(args.stage, args.name, args.execute)
