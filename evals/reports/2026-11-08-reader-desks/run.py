"""Frozen complete-text diagnostic; explicit live phases, immutable requests and no retries."""

# ruff: noqa: E402

import argparse
import copy
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(ROOT / "evals/operator/author-openings"))

from artifacts import read, save, save_bytes, send_once, sha, verified_response
from conditions import response_text
from contracts import CONTRACT, ReaderDesks, response_schema
from dotenv import dotenv_values
from results import estimate

from podgen.runtime.evaluation.validation import load_unique_json
from podgen.runtime.evaluation.wire_budget import check_tokens, check_visible, visible_sizes
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing.roles import WriteOutput

SOURCE = HERE.parent / "2026-11-08-reader-voice"
INITIAL = ("01", "04", "05")
FOLLOWUP = ("02", "03")


def prepare():
    source_frozen = read(SOURCE / "frozen.json")
    save_bytes(HERE / "criteria.json", (SOURCE / "criteria.json").read_bytes())
    base = (SOURCE / "new-reader-v1.md").read_text()
    save_bytes(HERE / "listener-instructions.md", base.encode())
    instructions = (HERE / "desk-instructions.md").read_text() + "\n\n# Listener assessment rules\n\n" + base
    save_bytes(HERE / "instructions.txt", instructions.encode())
    schema = response_schema()
    rows = []
    for case in (*INITIAL, *FOLLOWUP):
        script_path = SOURCE / "texts" / (case + ".json")
        assert sha(script_path.read_bytes()) == source_frozen["case_sha256"][case]
        save_bytes(HERE / "texts" / (case + ".json"), script_path.read_bytes())
        original = read(SOURCE / "runs/new-v1" / case / "request.json")
        body = copy.deepcopy(original)
        body["instructions"] = instructions
        body["text"]["format"] = dict(type="json_schema", name="reader_desks", schema=schema, strict=True)
        body["max_output_tokens"] = 8192
        identifier = uuid5(NAMESPACE_URL, "podgen-reader-desks:" + CONTRACT + ":" + case)
        body["metadata"]["podgen_run_id"] = str(identifier)
        body["metadata"]["podgen_invocation_id"] = str(uuid5(identifier, "invocation:reader-review"))
        assert body["model"] == "gpt-6.1-terra" and body["reasoning"] == {"effort": "medium"}
        assert body["tools"] == [] and body["store"] is False and body["background"] is False
        assert body["input"] == original["input"]
        payload = json.loads(body["input"][0]["content"][0]["text"])
        script = read(script_path)
        assert [
            {k: v for k, v in row.items() if k != "index"} for row in payload["script"]["utterances"]
        ] == script["utterances"]
        assert not {
            "criteria",
            "case",
            "expected",
            "assignment",
            "story",
            "support",
            "evidence",
            "prior_reviews",
        } & set(payload)
        check_visible(body, "editorial-role-packets/1")
        save(HERE / "runs" / case / "request.json", body)
        rows.append(
            dict(
                case=case,
                script_sha256=sha(script_path.read_bytes()),
                request_sha256=sha((HERE / "runs" / case / "request.json").read_bytes()),
                visible_bytes=visible_sizes(body),
            )
        )
    save(
        HERE / "frozen.json",
        dict(
            contract=CONTRACT,
            initial=list(INITIAL),
            conditional=list(FOLLOWUP),
            criteria_sha256=sha((HERE / "criteria.json").read_bytes()),
            instructions_sha256=sha(instructions.encode()),
            source="2026-11-08-reader-voice: exact full script_only payloads from new-v1",
            cases=rows,
            intervention="A separate language_review section before the unchanged reader-review shape; original reader fields retain their order.",
            conditions="One independent call per case, no hints about case contents, no retries or editorial feedback; 02/03 only after the initial criteria pass.",
        ),
    )


def replay_case(case):
    folder = HERE / "runs" / case
    request, response = read(folder / "request.json"), verified_response(folder)
    assert response["status"] == "completed" and response["model"] == request["model"]
    assert response["metadata"] == request["metadata"]
    raw = load_unique_json(response_text(response, "openai"))
    save(folder / "raw.json", raw)
    review = ReaderDesks.model_validate(raw)
    review.validate_against(WriteOutput.model_validate(read(HERE / "texts" / (case + ".json"))))
    save(folder / "validated.json", review.model_dump(mode="json"))
    rates = read(ROOT / "evals/reports/2026-11-08-author-openings/pricing.json")["rates_per_million_usd"]
    summary = dict(
        case=case,
        response_id=response["id"],
        model=response["model"],
        usage=response["usage"],
        estimated_usd=estimate(response, "openai", rates),
        language_findings=len(review.language_review.findings),
        reader_findings=len(review.reader_review.findings),
        recommendation=review.reader_review.recommendation,
        revision_scope=review.reader_review.revision_scope.scale,
        language_recommendation_consistent=review.consistency(),
    )
    save(folder / "summary.json", summary)
    return summary


def live_case(case):
    folder = HERE / "runs" / case
    body = read(folder / "request.json")
    if not (folder / "response.json").exists():
        key = os.getenv("OPENAI_API_KEY") or dotenv_values(ROOT / ".env").get("OPENAI_API_KEY")
        provider = OpenAIProvider(key)
        if not (folder / "count.json").exists():
            count = provider.count(body)
            save_bytes(folder / "count.json", count.body)
        check_tokens(read(folder / "count.json")["input_tokens"], "editorial-role-packets/1")

        def transport(request, client_id):
            reply = provider.submit(request, client_request_id=client_id)
            return reply.status_code, reply.request_id, reply.body

        send_once(folder, body, transport)
    return replay_case(case)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "initial", "followup", "replay"])
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
        print("Frozen 5 full-text requests; only 3 initial calls authorized unconditionally.")
        return
    cases = INITIAL
    if args.action == "followup":
        assert read(HERE / "initial-assessment.json")["eligible_for_followup"] is True
        cases = FOLLOWUP
    if args.action == "replay":
        results = [
            replay_case(case)
            for case in (*INITIAL, *FOLLOWUP)
            if (HERE / "runs" / case / "response.json").exists()
        ]
    else:
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(live_case, cases))
    results = sorted(results, key=lambda row: row["case"])
    report = dict(
        contract=CONTRACT,
        diagnostic_only=True,
        runtime_changed=False,
        results=results,
        estimated_total_usd=str(sum(Decimal(row["estimated_usd"]) for row in results)),
    )
    save(HERE / ("results-" + args.action + ".json"), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
