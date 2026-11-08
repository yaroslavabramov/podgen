"""Run a frozen same-wire listening comparison; replay never calls a provider."""

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "evals/operator/author-openings"))
from artifacts import read, save, send_once, sha
from conditions import response_text
from podgen.domain.canonical import digest
from podgen.writing.duration_views import duration_context
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.projections import indexed_draft
from podgen.writing.reader_schema import snapshot_model
from podgen.writing.roles import WriteOutput
from providers import Transport, credentials
from results import estimate

RATES = read(HERE.parents[1] / "2026-11-08-author-openings/pricing.json")["rates_per_million_usd"]
SNOW_DIGEST = "09d28f35cd0d0279f9c4104dc0de0b7a5b9665c9fd2dde56ce165c76f8bf4956"


def user(request):
    return json.loads(request["input"][0]["content"][0]["text"])


def script(request):
    return {"utterances": [{"speaker": r["speaker"], "text": r["text"]} for r in user(request)["script"]["utterances"]]}


def prepare(path):
    base = read(path / "podgen.json")
    assert digest(script(base)) == SNOW_DIGEST
    original = base["instructions"]
    assert "Make two paper edits" in original and "42da" not in original
    marker = "Report a decision to change in the ordinary findings"
    new_task = (HERE / "instruction.md").read_text().strip()
    variant = original.replace("Make two paper edits using only the supplied speech.", "Use the supplied speech for your paper edits.")
    variant = variant.replace(marker, new_task + "\n\n" + marker)
    for name in ("snow", "club"):
        req = deepcopy(base)
        req["instructions"] = variant
        uid = uuid5(NAMESPACE_URL, "nov8-recordable-explanation:" + name)
        req["metadata"].update(podgen_run_id=str(uid), podgen_invocation_id=str(uuid5(uid, "read")))
        if name == "club":
            u = user(req)
            rows = (HERE / "club.md").read_text().strip().split("\n\n")
            draft = {"utterances": [{"speaker": "РАССКАЗЧИК", "text": t} for t in rows]}
            assignment = EditorialInput.model_validate(read(ROOT / "evals/editorial-inputs/current/snow-pump/shared.json")).assignment
            assignment = assignment.model_copy(update={"duration": assignment.duration.__class__(min_seconds=60, target_seconds=90, max_seconds=120)})
            u.update(genre="Короткий художественный рассказ", audience="Взрослый слушатель, впервые слышит эту историю.", script=indexed_draft(draft), duration_context=duration_context(assignment, draft))
            req["input"][0]["content"][0]["text"] = json.dumps(u, ensure_ascii=False, separators=(",", ":"))
        else:
            assert req["input"] == base["input"]
        save(HERE / name / "request.json", req)
    save(HERE / "base" / "output.json", json.loads(response_text(read(path / "receipt.json"), "openai")))
    save(HERE / "base" / "metadata.json", {"request_sha256": sha((path / "podgen.json").read_bytes()), "response_id": read(path / "receipt.json")["id"], "prompt": "reader-review/sha256:42da84b6f021ee7985c384ea117c7a2098bc5eb845617ab3974b79b4abbaae3a", "script_digest": SNOW_DIGEST})


def run(name, live):
    d = HERE / name
    request = read(d / "request.json")
    if live:
        assert request["tools"] == [] and request["model"] == "gpt-6.1-terra" and request["reasoning"]["effort"] == "medium"
        result = send_once(ROOT / "tmp/narrative-controls-nov8/explanation-edit" / name, request, Transport("openai", credentials(ROOT)["openai"]))
        assert result["status"] == "completed"
        save(d / "output.json", json.loads(response_text(result, "openai")))
        save(d / "receipt.json", {"id": result["id"], "model": result["model"], "usage": result["usage"], "estimated_usd": estimate(result, "openai", RATES), "request_sha256": sha((d / "request.json").read_bytes()), "output_sha256": sha((d / "output.json").read_bytes())})
    receipt, out = read(d / "receipt.json"), read(d / "output.json")
    assert sha((d / "request.json").read_bytes()) == receipt["request_sha256"] and sha((d / "output.json").read_bytes()) == receipt["output_sha256"]
    review = snapshot_model(out).model_validate(out)
    review.validate_against(WriteOutput.model_validate(script(request)))
    return name, receipt["estimated_usd"], review.recommendation


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "live", "replay"])
    parser.add_argument("--original", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.original)
    else:
        with ThreadPoolExecutor(max_workers=2) as pool:
            print(list(pool.map(lambda name: run(name, args.mode == "live"), ("snow", "club"))))
