"""Paper-edit control on the complete production reader wire; no production edits until judged."""

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
from podgen.writing.duration_views import duration_context
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projections import indexed_draft
from podgen.writing.reader_schema import snapshot_model
from podgen.writing.roles import WriteOutput
from providers import Transport, credentials
from results import estimate

SOURCE = HERE.parent.parent / "2026-11-08-snow-full-authors"
RATES = read(HERE.parent.parent / "2026-11-08-author-openings/pricing.json")[
    "rates_per_million_usd"
]
ORDINARY = HERE.parent / "runs/A/request.json"
PLAYER = "РАССКАЗЧИК"


def snapshot(name):
    if name == "cassette":
        body = (HERE / "cassette.md").read_text().strip().split("\n\n")
        return {"utterances": [{"speaker": PLAYER, "text": t} for t in body]}
    return read(SOURCE / "sealed" / name / "output.json")


def prepare():
    base = read(ORDINARY)
    old = base["instructions"].split("\n\n")
    start = next(
        i
        for i, v in enumerate(old)
        if v.startswith("Follow interest through the whole episode.")
    )
    old[start] = (HERE / "instruction.md").read_text().strip()
    base["instructions"] = "\n\n".join(old)
    episode = EditorialInput.model_validate(
        read(ROOT / "evals/editorial-inputs/current/snow-pump/shared.json")
    ).assignment
    for name in ("A", "B", "cassette"):
        body = json.loads(json.dumps(base))
        user = json.loads(body["input"][0]["content"][0]["text"])
        script = snapshot(name)
        assignment = episode
        if name == "cassette":
            user.update(
                genre="Короткий художественный рассказ",
                audience="Взрослый слушатель, впервые слышит эту историю.",
            )
            assignment = episode.model_copy(
                update={
                    "duration": episode.duration.__class__(
                        min_seconds=65, target_seconds=90, max_seconds=120
                    )
                }
            )
        user["script"] = indexed_draft(script)
        user["duration_context"] = duration_context(assignment, script)
        body["input"][0]["content"][0]["text"] = json.dumps(
            user, ensure_ascii=False, separators=(",", ":")
        )
        if name == "A":
            assert body["input"] == base["input"]
        uid = uuid5(NAMESPACE_URL, "nov8:paper-edit:" + name)
        body["metadata"].update(
            podgen_run_id=str(uid),
            podgen_invocation_id=str(uuid5(uid, "invocation:reader-review-2")),
        )
        save(HERE / "runs" / name / "request.json", body)


def run(name, live):
    d = HERE / "runs" / name
    q = ROOT / "tmp/narrative-controls-nov8/paper-edit" / name
    if live:
        req = read(d / "request.json")
        assert (
            req["model"] == "gpt-6.1-terra"
            and req["reasoning"] == {"effort": "medium"}
            and req["tools"] == []
        )
        res = send_once(q, req, Transport("openai", credentials(ROOT)["openai"]))
        assert res["status"] == "completed" and res["model"] == req["model"]
        output = json.loads(response_text(res, "openai"))
        save(d / "output.json", output)
        save(
            d / "receipt.json",
            {
                "id": res["id"],
                "model": res["model"],
                "usage": res["usage"],
                "estimated_usd": estimate(res, "openai", RATES),
                "request_sha256": sha((d / "request.json").read_bytes()),
                "output_sha256": sha((d / "output.json").read_bytes()),
            },
        )
    receipt = read(d / "receipt.json")
    assert receipt["request_sha256"] == sha((d / "request.json").read_bytes())
    assert receipt["output_sha256"] == sha((d / "output.json").read_bytes())
    raw = read(d / "output.json")
    v = snapshot_model(raw).model_validate(raw)
    v.validate_against(WriteOutput.model_validate(snapshot(name)))
    assert v.reading_mode == "script_only"
    return Decimal(receipt["estimated_usd"])


def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("mode", choices=("prepare", "first", "second", "replay"))
    mode = cli.parse_args().mode
    if mode == "prepare":
        prepare()
        return
    if mode == "first":
        names = ("A", "cassette")
    elif mode == "second":
        assert read(HERE / "first-decision.json")["release_B"] is True
        names = ("B",)
    else:
        names = tuple(
            k
            for k in ("A", "cassette", "B")
            if (HERE / "runs" / k / "receipt.json").exists()
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        costs = list(pool.map(lambda n: run(n, mode != "replay"), names))
    print(dict(zip(names, map(str, costs), strict=True)), sum(costs))


if __name__ == "__main__":
    main()
