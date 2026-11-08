"""Full-episode hearing on the exact ordinary reader wire and its minimally changed control."""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from podgen.runtime.evaluation.script_revision import operation
from podgen.writing.duration_views import duration_context
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.prompts import load_reading_instructions
from podgen.writing.reader_schema import snapshot_model
from podgen.writing.roles import WriteOutput

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[4]
sys.path.insert(0, str(ROOT / "evals/operator/author-openings"))
from artifacts import read, save, send_once, sha
from conditions import response_text
from providers import Transport, credentials
from results import estimate

ORIGINAL = "По дням в ней указано две вещи: сколько человек умерло и сколько из тех, кто потом умер, заболело. Разница важна. Человек может заболеть в один день, а умереть через день или два. Если смотреть только на смерти, мы опаздываем. Так вот."
SHORT = "В таблице Сноу рядом с числом смертей по дням есть и другой ряд: когда заболели те, кто впоследствии умер."
RATES = read(ROOT / "evals/reports/2026-11-08-author-openings/pricing.json")[
    "rates_per_million_usd"
]


def script(payload):
    return {
        "utterances": [
            {"speaker": row["speaker"], "text": row["text"]}
            for row in payload["script"]["utterances"]
        ]
    }


def prepare(directory):
    base = read(directory / "podgen.json")
    assert sha(base["instructions"].encode()).startswith("beb6837039bc8")
    candidate = load_reading_instructions("reader-review")
    assert (HERE / "operation.md").read_text().strip() in candidate.body
    assignment = EditorialInput.model_validate(
        read(ROOT / "evals/editorial-inputs/current/snow-pump/shared.json")
    ).assignment
    receipt = read(directory / "receipt.json")
    prev = json.loads(response_text(receipt, "openai"))
    assert prev["recommendation"] == "keep" and prev["findings"] == []
    save(
        HERE / "previous.json",
        {
            "model": receipt["model"],
            "id": receipt["id"],
            "request_sha256": sha((directory / "podgen.json").read_bytes()),
            "output": prev,
        },
    )
    for key in ("one", "two"):
        body = deepcopy(base)
        body["instructions"] = candidate.body
        intext = body["input"][0]["content"][0]
        user = json.loads(intext["text"])
        assert duration_context(assignment, script(user)) == user["duration_context"]
        if key == "two":
            at = user["script"]["utterances"][5]
            assert at["text"].count(ORIGINAL) == 1
            at["text"] = at["text"].replace(ORIGINAL, SHORT)
            user["duration_context"] = duration_context(assignment, script(user))
            intext["text"] = json.dumps(user, ensure_ascii=False, separators=(",", ":"))
        else:
            assert body["input"] == base["input"]
        uid = uuid5(NAMESPACE_URL, "nov8-joined-explanation:" + key)
        body["metadata"].update(
            podgen_run_id=str(uid),
            podgen_invocation_id=str(uuid5(uid, "invocation:reader-review")),
        )
        save(HERE / key / "request.json", body)


def run(key, live):
    d = HERE / key
    req = read(d / "request.json")
    if live:
        assert (
            req["tools"] == []
            and req["reasoning"]["effort"] == "medium"
            and req["model"] == "gpt-6.1-terra"
        )
        resp = send_once(
            ROOT / "tmp/story-editing-controls/explanation-test" / key,
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
    out, receipt = read(d / "output.json"), read(d / "receipt.json")
    assert (
        sha((d / "request.json").read_bytes()) == receipt["request_sha256"]
        and sha((d / "output.json").read_bytes()) == receipt["output_sha256"]
    )
    words = script(json.loads(req["input"][0]["content"][0]["text"]))
    r = snapshot_model(out).model_validate(out)
    r.validate_against(WriteOutput.model_validate(words))
    if r.recommendation == "revise":
        selected = operation(
            words,
            {
                "reader": out,
                "fact": {"findings": [], "revision_scope": {"scale": "none"}},
            },
            0,
        )
        save(
            d / "permissions.json",
            {
                "mode": selected["mode"],
                "sequences": selected["sequences"],
                "table_targets": [
                    x for x in selected["targets"] if x.get("utterance_index") == 5
                ],
            },
        )
    return key, receipt["estimated_usd"], r.recommendation


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=("prepare", "live", "replay"))
    p.add_argument("--source", type=Path)
    args = p.parse_args()
    if args.mode == "prepare":
        prepare(args.source)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(
                pool.map(lambda key: run(key, args.mode == "live"), ("one", "two"))
            )
        print(rows, sum(Decimal(row[1]) for row in rows))
