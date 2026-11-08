"""Explicit selected excerpt calls; markers prevent retrying a submitted request."""

import argparse
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from hashlib import sha256

from dotenv import dotenv_values
from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.openai_provider import OpenAIProvider
from pydantic import ValidationError
from run_readings import HERE, ROOT, estimate, extract, read, save
from speech_contract import SpeechProofread


def one(case, variant):
    folder = HERE / "runs" / variant / case
    path = folder / "request.json"
    body = read(path)
    frozen = read(
        HERE
        / (
            "speech-criteria.json"
            if variant == "speech-v1"
            else "director-criteria.json"
        )
    )
    expected = next(row for row in frozen["requests"] if row["case"] == case)
    assert sha256(path.read_bytes()).hexdigest() == expected["request_sha256"]
    assert body["model"] == "gpt-6.1-terra" and body["reasoning"] == {
        "effort": "medium"
    }
    assert (
        body["tools"] == [] and body["store"] is False and body["background"] is False
    )
    if not (folder / "receipt.json").exists():
        with (folder / "submitted").open("x") as marker:
            marker.write(body["metadata"]["podgen_invocation_id"])
        key = os.environ.get("OPENAI_API_KEY") or dotenv_values(ROOT / ".env").get(
            "OPENAI_API_KEY"
        )
        print("Submitting speech", case, flush=True)
        reply = OpenAIProvider(key).submit(
            body, client_request_id=body["metadata"]["podgen_invocation_id"]
        )
        (folder / "receipt.json").write_bytes(reply.body)
    receipt = read(folder / "receipt.json")
    assert receipt["status"] == "completed", receipt.get("incomplete_details")
    assert receipt["model"] == body["model"]
    output = extract(receipt)
    save(folder / "raw.json", output)
    review = SpeechProofread.model_validate(output)
    payload = json.loads(body["input"][0]["content"][0]["text"])
    review.validate_against(payload["utterances"])
    save(folder / "validated.json", review.model_dump(mode="json"))
    result = {
        "case": case,
        "response_id": receipt["id"],
        "usage": receipt["usage"],
        "estimated_usd": estimate(receipt),
        "required": sum(finding.required for finding in review.findings),
        "request_sha256": expected["request_sha256"],
    }
    save(folder / "summary.json", result)
    print("Completed speech", case, result["required"], flush=True)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="+", choices=["01", "02", "03", "04", "05"])
    parser.add_argument(
        "--variant", choices=["speech-v1", "speech-v2"], default="speech-v1"
    )
    args = parser.parse_args()
    cases = args.cases
    assert len(set(cases)) == len(cases)
    if args.variant == "speech-v2":
        assert set(cases) <= {"01", "04", "05"}
    if any(case in ("02", "03") for case in cases):
        assert read(HERE / "speech-initial-assessment.json")["initial_pass"] is True
    results = []
    failures = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(one, case, args.variant): case for case in cases}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except (
                ProviderFault,
                ValidationError,
                ValueError,
                AssertionError,
                OSError,
                KeyError,
            ) as error:
                failure = {
                    "case": futures[future],
                    "code": getattr(error, "code", type(error).__name__),
                }
                save(
                    HERE / "runs" / args.variant / futures[future] / "failure.json",
                    failure,
                )
                failures.append(failure)
    result = {
        "variant": args.variant,
        "cases": cases,
        "results": sorted(results, key=lambda row: row["case"]),
        "failures": failures,
        "new_estimated_usd": str(sum(Decimal(row["estimated_usd"]) for row in results)),
    }
    save(HERE / ("results-" + args.variant + "-" + "-".join(cases) + ".json"), result)
    print(json.dumps(result), flush=True)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
