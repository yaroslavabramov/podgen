"""Compile matched cold readings through Podgen and replay their actual editing permissions."""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "evals/operator/author-openings"))

from artifacts import read, save, send_once, sha
from conditions import response_text
from providers import Transport, credentials
from results import estimate
from podgen.domain.contracts.configuration import ModelProfile
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.role_request import compile_reading_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.runtime.evaluation.script_revision import operation
from podgen.writing.editorial_contract import EditorialDuration, EditorialInput
from podgen.writing.projection_versions import V9
from podgen.writing.prompts import load_reading_instructions
from podgen.writing.reader_schema import VERSION, snapshot_model
from podgen.writing.roles import WriteOutput

CASE_IDS = tuple("abcdefgh")
RATES = read(HERE.parents[1] / "2026-11-08-author-openings/pricing.json")[
    "rates_per_million_usd"
]


def story(key):
    all_texts = read(HERE / "stories.json")
    if key in "abc":
        v = all_texts["stairs"]
        parts = v["opening"] + v[key]
    elif key in "def":
        v = all_texts["reel"]
        parts = v["opening"] + v[key] + [v["action"]]
        if key == "f":
            parts.extend(v["amplification"])
        parts.extend(v["ending"])
    else:
        v = all_texts["station"]
        parts = v["opening"] + v[key] + v["ending"]
    return {"utterances": [{"speaker": "РАССКАЗЧИК", "text": s} for s in parts]}


def prepare():
    prompt = load_reading_instructions("reader-review")
    schema = response_schema(
        "reader-review",
        "editorial-role-packets/1",
        reader_schema_version=VERSION,
        projection_version=V9,
    )
    profile = ModelProfile.model_validate(read(HERE / "profile.json"))
    packet = EditorialInput.model_validate(
        read(ROOT / "evals/editorial-inputs/current/snow-pump/shared.json")
    )
    for key in CASE_IDS:
        uid = uuid5(
            NAMESPACE_URL, "podgen:cold-story-editing:" + key + ":" + prompt.version
        )
        assignment = packet.assignment.model_copy(
            update={
                "genre": "Короткий рассказ для радио",
                "listener_profile": "Взрослая аудитория, впервые слышит историю.",
                "duration": EditorialDuration(
                    min_seconds=35, target_seconds=125, max_seconds=210
                ),
            }
        )
        inputs = EditorialMaterials(
            packet=packet.model_copy(update={"assignment": assignment}),
            shared_ref=None,
            documents=(),
        )
        req = compile_reading_request(
            uid,
            "reader-review",
            profile,
            inputs,
            prompt.body,
            schema,
            script=story(key),
            projection_version=V9,
        )
        user = json.loads(req["input"][0]["content"][0]["text"])
        assert set(user) == {
            "script",
            "audience",
            "format",
            "genre",
            "output_language",
            "duration_context",
        }
        assert [x["text"] for x in user["script"]["utterances"]] == [
            x["text"] for x in story(key)["utterances"]
        ]
        save(HERE / "cases" / key / "request.json", req)


def run(key, live, group="cases"):
    d = HERE / group / key
    req = read(d / "request.json")
    if live:
        resp = send_once(
            ROOT / "tmp/story-editing-controls" / group / key
            if group != "cases"
            else ROOT / "tmp/story-editing-controls" / key,
            req,
            Transport("openai", credentials(ROOT)["openai"]),
        )
        assert resp["status"] == "completed"
        save(d / "output.json", json.loads(response_text(resp, "openai")))
        save(
            d / "receipt.json",
            {
                "id": resp["id"],
                "model": resp["model"],
                "usage": resp["usage"],
                "estimated_usd": estimate(resp, "openai", RATES),
                "request_sha256": sha((d / "request.json").read_bytes()),
                "output_sha256": sha((d / "output.json").read_bytes()),
            },
        )
    receipt, out = read(d / "receipt.json"), read(d / "output.json")
    assert receipt["request_sha256"] == sha(
        (d / "request.json").read_bytes()
    ) and receipt["output_sha256"] == sha((d / "output.json").read_bytes())
    review = snapshot_model(out).model_validate(out)
    review.validate_against(WriteOutput.model_validate(story(key)))
    if review.recommendation == "revise":
        readings = {
            "reader": out,
            "fact": {"findings": [], "revision_scope": {"scale": "none"}},
        }
        selection = operation(story(key), readings, 0)
        save(
            d / "permissions.json",
            {
                "mode": selection["mode"],
                "sequences": selection["sequences"],
                "targets": [t for t in selection["targets"] if t["kind"] == "text"],
                "insertions": [t for t in selection["targets"] if t["kind"] != "text"],
            },
        )
    return key, receipt["estimated_usd"], review.recommendation


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=["prepare", "live", "replay", "baseline-live", "baseline-replay"],
    )
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare()
    else:
        baseline = args.mode.startswith("baseline-")
        group = "baseline" if baseline else "cases"
        keys = ("b", "g") if baseline else CASE_IDS
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(
                pool.map(lambda k: run(k, args.mode.endswith("live"), group), keys)
            )
        print(results, sum(Decimal(cost) for _, cost, _ in results))
