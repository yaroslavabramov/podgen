"""Offline replay, anonymous rendering and receipt-based cost estimates."""

import json
from decimal import Decimal
from pathlib import Path

from artifacts import read, save, save_bytes, sha, verified_response
from conditions import TASK, author_request, payload, script, spoken, words


def estimate(response, provider, rates):
    usage = response.get("usage")
    if usage is None or response.get("model") not in rates:
        return None
    rate = {key: Decimal(str(value)) for key, value in rates[response["model"]].items()}
    if provider == "openai":
        detail = usage.get("input_tokens_details", {})
        hit, write = detail.get("cached_tokens", 0), detail.get("cache_write_tokens", 0)
        amount = (
            rate["input"] * (usage["input_tokens"] - hit - write)
            + rate["cached"] * hit
            + rate["write"] * write
            + rate["output"] * usage["output_tokens"]
        )
    else:
        amount = (
            rate["input"] * usage["input_tokens"]
            + rate["cached"] * usage.get("cache_read_input_tokens", 0)
            + rate["write"] * usage.get("cache_creation_input_tokens", 0)
            + rate["output"] * usage["output_tokens"]
        )
    return str(amount / 1000000)


def replay(directory):
    directory = Path(directory)
    common = read(directory / "common/payload.json")
    instructions = (directory / "common/instructions.txt").read_text()
    original = read(directory / "source/request.json")
    frozen = read(directory / "conditions.json")
    assert frozen["source_request_sha256"] == sha((directory / "source/request.json").read_bytes())
    assert frozen["source_response_sha256"] == sha((directory / "source/response.json").read_bytes())
    assert frozen["instructions_sha256"] == sha(instructions.encode())
    assert common == dict(payload(original), opening_task=TASK)
    assert frozen["story_sha256"] == sha(
        json.dumps(common["story"], ensure_ascii=False, sort_keys=True).encode()
    )
    speakers = [row["label"] for row in common["assignment"]["speakers"]]
    identity = read(directory / "sealed/identity.json")
    rows = []
    for label, entry in identity.items():
        folder = directory / "sealed/candidates" / entry["slot"]
        request = read(folder / "request.json")
        provider = entry["provider"]
        if provider == "openai":
            assert payload(request) == common
            assert request["instructions"] == instructions
        else:
            assert json.loads(request["messages"][0]["content"][0]["text"]) == common
            assert request["system"] == instructions
        assert request == author_request(original, common, provider, entry["model"])
        assert read(folder / "submitted.json")["request_sha256"] == sha(
            (folder / "request.json").read_bytes()
        )
        assert read(folder / "transport.json")["response_sha256"] == sha(
            (folder / "response.bin").read_bytes()
        )
        response = verified_response(folder)
        assert response == json.loads((folder / "response.bin").read_bytes())
        assert response["model"] == entry["model"]
        assert read(folder / "transport.json")["status"] == 200
        output = script(response, provider, speakers)
        save(folder / "validated.json", output)
        text = spoken(output)
        save(directory / "blind" / (label + ".json"), output)
        save_bytes(directory / "blind" / (label + ".md"), text.encode())
        count = words(text)
        rows.append(
            dict(
                label=label,
                word_count=count,
                within_guidance=220 <= count <= 270,
                output_sha256=sha((directory / "blind" / (label + ".json")).read_bytes()),
            )
        )
    rates = read(directory / "pricing.json")["rates_per_million_usd"]
    costs, total = [], Decimal(0)
    for path in sorted((directory / "sealed").glob("**/response.json")):
        folder = path.parent
        request = read(folder / "request.json")
        provider = "anthropic" if request["model"].startswith("claude-") else "openai"
        response = verified_response(folder)
        usd = estimate(response, provider, rates)
        if usd is not None:
            total += Decimal(usd)
        costs.append(
            dict(
                path=str(path.relative_to(directory)),
                response_id=response.get("id"),
                usage=response.get("usage"),
                estimated_usd=usd,
                http_status=read(folder / "transport.json")["status"],
            )
        )
    save(
        directory / "sealed/usage.json",
        dict(
            calls=costs,
            estimated_total_usd=str(total),
            all_generated_usage_priced=all(
                row["estimated_usd"] is not None or row["http_status"] in (400, 401, 403, 404)
                for row in costs
            ),
        ),
    )
    # Identity and per-author accounting deliberately remain outside the reading packet.
    save(directory / "blind/checks.json", dict(samples=rows, editorial_assessment_performed=False))
    return dict(samples=len(rows), estimated_total_usd=str(total), physical_generation_attempts=len(costs))
