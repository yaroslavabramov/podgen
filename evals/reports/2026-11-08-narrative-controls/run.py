"""Freeze four model requests, or explicitly submit the frozen requests once."""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "evals/operator/author-openings"))

from artifacts import encoded, read, save, save_bytes, send_once, sha, verified_response
from conditions import author_request, payload, response_text, spoken
from podgen.domain.contracts.common import RuntimeRef
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.validation import RoleOutputError, validate_output
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projection_versions import V8
from podgen.writing.prompts import load_reading_instructions, role_instructions
from podgen.writing.reader_schema import snapshot_model
from podgen.writing.reading_views import reading_payload
from podgen.writing.roles import WriteOutput
from providers import Transport, credentials
from results import estimate

OPENINGS = HERE.parent / "2026-11-08-author-openings"
FULL = HERE.parent / "2026-11-08-snow-full-authors"
READER = HERE.parent / "2026-11-08-voice-comparison/release/01/request.json"
CASES = ("01", "02", "A", "author")
RATES = read(OPENINGS / "pricing.json")["rates_per_million_usd"]
STAGING = ROOT / "tmp/narrative-controls-nov8"


def snapshot(case):
    return read(
        FULL / "sealed/A/output.json" if case == "A" else HERE / (case + ".json")
    )


def prepare():
    # Retain the previously used full native reader schema and provider settings.
    # Rebuild the allowlisted user content with the production cold-input projector.
    original = read(OPENINGS / "source/request.json")
    author = author_request(original, payload(original), "anthropic", "claude-opus-5-5")
    author["max_tokens"] = original["max_output_tokens"]
    previous = next(
        c
        for c in read(FULL / "sealed/usage.json")["calls"]
        if c["model"] == author["model"] and c["accepted"]
    )
    assert sha(encoded(author)) == previous["request_sha256"]
    author["system"] = role_instructions("draft", "solo")
    save(HERE / "runs/author/request.json", author)

    packet = EditorialInput.model_validate(
        read(ROOT / "evals/editorial-inputs/current/snow-pump/shared.json")
    )
    reader = read(READER)
    for case in CASES[:-1]:
        selected = packet
        if case != "A":
            assignment = packet.assignment
            n = assignment.model_copy(
                update={
                    "genre": "Короткий художественный рассказ",
                    "listener_profile": "Взрослый слушатель, впервые слышит эту историю и не знаком с этой мастерской.",
                    "duration": assignment.duration.__class__(
                        min_seconds=60, target_seconds=90, max_seconds=120
                    ),
                }
            )
            selected = packet.model_copy(update={"assignment": n})
        uid = uuid5(NAMESPACE_URL, "podgen-2026-11-08-narrative-control-" + case)
        materials = EditorialMaterials(
            selected, RuntimeRef(record_kind="user_input", record_id=uid), ()
        )
        obj = snapshot(case)
        user = reading_payload(
            "reader-review", materials, script=obj, projection_version=V8
        )
        assert set(user) == {
            "script",
            "audience",
            "format",
            "genre",
            "output_language",
            "duration_context",
        }
        assert [r["text"] for r in obj["utterances"]] == [
            r["text"] for r in user["script"]["utterances"]
        ]
        req = deepcopy(reader)
        req["instructions"] = load_reading_instructions("reader-review").body
        req["input"][0]["content"][0]["text"] = json.dumps(
            user, ensure_ascii=False, separators=(",", ":")
        )
        req["metadata"].update(
            podgen_run_id=str(uid),
            podgen_invocation_id=str(uuid5(uid, "invocation:reader-review-2")),
        )
        assert req["model"] == "gpt-6.1-terra" and req["reasoning"] == {
            "effort": "medium"
        }
        save(HERE / "runs" / case / "request.json", req)


def collect(case, live):
    provider = "anthropic" if case == "author" else "openai"
    dest = HERE / "runs" / case
    folder = STAGING / case
    if live:
        response = send_once(
            folder,
            read(dest / "request.json"),
            Transport(provider, credentials(ROOT)[provider]),
        )
        assert read(folder / "transport.json")["status"] == 200
        assert response["model"] == read(dest / "request.json")["model"]
        assert (
            response.get("status") == "completed"
            if provider == "openai"
            else response.get("stop_reason") == "end_turn"
        )
        save(dest / "output.json", json.loads(response_text(response, provider)))
        save(
            dest / "receipt.json",
            {
                "id": response["id"],
                "model": response["model"],
                "usage": response["usage"],
                "estimated_usd": estimate(response, provider, RATES),
                "request_sha256": sha((dest / "request.json").read_bytes()),
                "output_sha256": sha((dest / "output.json").read_bytes()),
            },
        )
        assert verified_response(folder) == response
    receipt = read(dest / "receipt.json")
    assert sha((dest / "request.json").read_bytes()) == receipt["request_sha256"]
    assert sha((dest / "output.json").read_bytes()) == receipt["output_sha256"]
    raw = read(dest / "output.json")
    if case == "author":
        WriteOutput.model_validate(raw)
        try:
            validate_output("draft", json.dumps(raw), speaker_labels=["РАССКАЗЧИК"])
        except RoleOutputError as error:
            if error.details != [{"code": "speaker_labels_mismatch"}]:
                raise
            validation = {
                "accepted": False,
                "errors": error.details,
                "unexpected_labels": [
                    {"utterance_index": i, "received": row["speaker"]}
                    for i, row in enumerate(raw["utterances"])
                    if row["speaker"] != "РАССКАЗЧИК"
                ],
            }
        else:
            validation = {"accepted": True, "errors": []}
        save(dest / "validation.json", validation)
        # Plain-text listening copy preserves the exact words, including any speaker-bound draft rejection.
        save_bytes(HERE / "author.md", spoken(raw).encode())
    else:
        result = snapshot_model(raw).model_validate(raw)
        result.validate_against(WriteOutput.model_validate(snapshot(case)))
        assert result.reading_mode == "script_only"
    return Decimal(receipt["estimated_usd"])


def main():
    action = argparse.ArgumentParser()
    action.add_argument("mode", choices=("prepare", "live", "replay"))
    args = action.parse_args()
    if args.mode == "prepare":
        prepare()
        return
    with ThreadPoolExecutor(max_workers=4) as pool:
        costs = dict(
            zip(
                CASES,
                pool.map(lambda c: collect(c, args.mode == "live"), CASES),
                strict=True,
            )
        )
    total = sum(costs.values())
    save(
        HERE / "usage.json",
        {
            "calls": {k: str(v) for k, v in costs.items()},
            "total_estimated_usd": str(total),
        },
    )
    print("Replayed", len(costs), "responses; estimated USD", total)


if __name__ == "__main__":
    main()
