"""Authorized Terra-only independent reader calls; exclusive markers prevent automatic resubmission."""

import json
import os
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from dotenv import dotenv_values
from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing.reader_schema import VERSION, feedback_model
from podgen.writing.roles import WriteOutput
from pydantic import ValidationError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def extract(receipt):
    return json.loads(
        "".join(
            c["text"]
            for m in receipt["output"]
            if m["type"] == "message"
            for c in m["content"]
            if c["type"] == "output_text"
        )
    )


def estimate(receipt):
    usage = receipt["usage"]
    details = usage.get("input_tokens_details", {})
    cached = details.get("cached_tokens", 0)
    written = details.get("cache_write_tokens", 0)
    return str(
        (
            Decimal(usage["input_tokens"] - cached - written)
            + Decimal(".05") * cached
            + Decimal("1.25") * written
            + Decimal(5) * usage["output_tokens"]
        )
        / Decimal(1000000)
    )


def one(row):
    path = ROOT / row["path"]
    folder = path.parent
    body = read(path)
    case = row["case"]
    call = read(folder / "call.json")
    reused = call["reuse_archival"]
    assert body["model"] == "gpt-6.1-terra" and body["reasoning"] == {
        "effort": "medium"
    }
    assert (
        body["tools"] == [] and body["store"] is False and body["background"] is False
    )
    if reused:
        if not (folder / "receipt.json").exists():
            shutil.copyfile(HERE / "archive/receipt.json", folder / "receipt.json")
    elif not (folder / "receipt.json").exists():
        marker = folder / "submitted"
        with marker.open("x") as f:
            f.write(body["metadata"]["podgen_invocation_id"])
        print("Submitting", row["role"], case, flush=True)
        key = os.environ.get("OPENAI_API_KEY") or dotenv_values(ROOT / ".env").get(
            "OPENAI_API_KEY"
        )
        try:
            reply = OpenAIProvider(key).submit(
                body, client_request_id=body["metadata"]["podgen_invocation_id"]
            )
        except Exception as error:
            save(
                folder / "failure.json",
                {
                    "code": getattr(error, "code", type(error).__name__),
                    "automatic_retry": False,
                },
            )
            raise
        (folder / "receipt.json").write_bytes(reply.body)
    receipt = read(folder / "receipt.json")
    assert receipt["status"] == "completed", receipt.get("incomplete_details")
    assert receipt["model"] == body["model"]
    output = extract(receipt)
    save(folder / "raw.json", output)
    script = WriteOutput.model_validate(read(HERE / "texts" / f"{case}.json"))
    review = feedback_model(VERSION).model_validate(output)
    review.validate_against(script)
    save(folder / "validated.json", review.model_dump(mode="json"))
    summary = {
        "role": row["role"],
        "case": case,
        "reused": reused,
        "response_id": receipt["id"],
        "usage": receipt["usage"],
        "estimated_usd": estimate(receipt),
        "request_sha256": sha256(path.read_bytes()).hexdigest(),
        "recommendation": review.recommendation,
        "scale": review.revision_scope.scale,
    }
    save(folder / "summary.json", summary)
    print(
        "Completed",
        row["role"],
        case,
        summary["recommendation"],
        summary["scale"],
        flush=True,
    )
    return summary


def main():
    rows = read(HERE / "requests-v1.json")["requests"]
    assert sum(not (row["role"] == "old" and row["case"] == "01") for row in rows) == 9
    failures = []
    results = []
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(one, row): row for row in rows}
        for f in as_completed(futures):
            try:
                results.append(f.result())
            except (
                ProviderFault,
                ValidationError,
                ValueError,
                AssertionError,
                OSError,
                KeyError,
            ) as error:
                failures.append(
                    {
                        "role": futures[f]["role"],
                        "case": futures[f]["case"],
                        "code": getattr(error, "code", type(error).__name__),
                    }
                )
    report = {
        "results": sorted(results, key=lambda x: (x["case"], x["role"])),
        "failures": failures,
        "new_calls": sum(not x["reused"] for x in results),
        "new_estimated_usd": str(
            sum(Decimal(x["estimated_usd"]) for x in results if not x["reused"])
        ),
        "pricing_basis": "Same project estimate as the archived source: Terra $1 uncached input / $0.05 cached input / $1.25 cache write / $5 output per million tokens; not a billing receipt.",
    }
    save(HERE / "results-v1.json", report)
    print(
        json.dumps(
            {
                "completed": len(results),
                "failures": failures,
                "new_estimated_usd": report["new_estimated_usd"],
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())
