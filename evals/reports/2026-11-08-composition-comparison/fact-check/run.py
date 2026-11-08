"""Blind Snow fact checks. 'prepare' is entirely offline. 'run' requires blind/COMPLETE."""

import argparse
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from dotenv import load_dotenv
from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.contracts.configuration import ModelProfile
from podgen.persistence.editorial_inputs import EditorialMaterials
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.repair_projection import anchor_payload, replace_input
from podgen.runtime.evaluation.validation import load_unique_json
from podgen.runtime.models.contracts import ProviderFault
from podgen.runtime.models.openai_provider import OpenAIProvider
from podgen.writing.anchor_selection import AnchorChoices, AnchorRepairPlan
from podgen.writing.editorial_contract import EditorialInput
from podgen.writing.fact_review import feedback_model
from podgen.writing.model_references import ModelReferences
from podgen.writing.prompts import load_anchor_repair_instructions
from podgen.writing.revision.contracts import FACT_VERSION
from podgen.writing.roles import WriteOutput

OUT = Path(__file__).resolve().parent
ROOT = OUT.parents[3]
BLIND = OUT.parent / "blind"
SHARED = ROOT / "evals/editorial-inputs/current/snow-pump/shared.json"
SOURCES = ROOT / "evals/cases/development/wp02-batch-01/sources/snow"
PROFILE = ROOT / "evals/reports/2026-11-08-story-editing/controls/profile.json"
NS = uuid5(NAMESPACE_URL, "podgen/2026-11-08/blind-snow/fact-check")
PROFILE_MODEL = ModelProfile.model_validate_json(PROFILE.read_bytes())
PACKET = EditorialInput.model_validate_json(SHARED.read_bytes())
MATERIALS = EditorialMaterials(PACKET, RuntimeRef(record_kind="user_input", record_id=NS), ())
REFS = ModelReferences.create(PACKET)
MODEL = feedback_model(FACT_VERSION)


def fingerprint(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(path, value):
    data = (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if path.exists() and path.read_bytes() != data:
        raise ValueError(f"immutable artifact changed: {path}")
    if not path.exists():
        path.write_bytes(data)


def request(script):
    return preview_request(uuid5(NS, digest(script)), "fact-review", PROFILE_MODEL, MATERIALS, draft=script)


def common(body):
    payload = json.loads(body["input"][0]["content"][0]["text"])
    assert set(payload) == {
        "script",
        "factual_remit",
        "output_language",
        "support",
        "evidence",
        "necessary_limits",
    }
    del payload["script"]
    return {k: v for k, v in body.items() if k not in {"metadata", "input"}} | {"payload": payload}


def prepare():
    packet = PACKET.model_dump(mode="json")
    documents = {}
    archived = {}
    for spec in packet["documents"]:
        path = (
            SOURCES / Path(spec["source_path"]).name
            if spec["purpose"] == "primary"
            else ROOT / "evals" / spec["source_path"]
        )
        assert fingerprint(path) == spec["sha256"]
        archived[spec["key"]] = path.read_text()
        documents[spec["key"]] = {
            "path": str(path.relative_to(ROOT)),
            "sha256": spec["sha256"],
        }
    frozen = common(request({"utterances": [{"speaker": MATERIALS.speaker_labels[0], "text": "Текст."}]}))
    facts = frozen["payload"]
    assert facts["factual_remit"]["question"] == PACKET.assignment.question
    locators = json.loads((SOURCES / "evidence.json").read_text())
    additional_snow_pages = {
        19: "Preface to the second edition",
        675: "printed 15",
        694: "printed 15",
        942: "printed 22–23",
        1599: "printed 40–41",
    }
    rows = []
    for index, card in enumerate(packet["dossier"]["knowledge_cards"]):
        alias = REFS.aliases[index]
        assert facts["support"][index]["card_id"] == alias
        for e in card["excerpts"]:
            key, start, end = e["document"], e["start"], e["end"]
            source = documents[key]["path"]
            assert e["text"] == archived[key][start:end]
            matches = [r for r in facts["evidence"] if alias in r["supports"] and r["text"] == e["text"]]
            assert len(matches) == 1
            line = 1 + archived[key].count("\n", 0, start)
            covering = [
                r
                for r in locators
                if r["representation"] == Path(source).name
                and r["start_codepoint"] <= start < end <= r["end_codepoint"]
            ]
            locator = (
                min(covering, key=lambda r: r["end_codepoint"] - r["start_codepoint"]) if covering else {}
            )
            page = locator["locator"] if locator else additional_snow_pages[line]
            rows.append(
                dict(
                    label=alias,
                    card=card["label"],
                    document=key,
                    start=start,
                    end=end,
                    line=line,
                    passage_id=locator.get("label"),
                    locator=page,
                    excerpt_sha256=hashlib.sha256(e["text"].encode()).hexdigest(),
                )
            )
    assert len(facts["support"]) == 17 and len(facts["evidence"]) == len(rows) == 46
    freeze(OUT / "evidence-index.json", dict(documents=documents, entries=rows))
    freeze(OUT / "common.json", frozen)
    freeze(
        OUT / "preflight.json",
        dict(
            shared_file_sha256=fingerprint(SHARED),
            common_digest=digest(frozen),
            fact_schema_version=FACT_VERSION,
            schema_digest=digest(frozen["text"]["format"]["schema"]),
            prompt_sha256=hashlib.sha256(frozen["instructions"].encode()).hexdigest(),
            profile_source=str(PROFILE.relative_to(ROOT)),
            profile_file_sha256=fingerprint(PROFILE),
            card_and_excerpt_counts=[17, 46],
            stable_references=REFS.as_payload(),
        ),
    )
    return frozen


def submit(directory, body):
    directory.mkdir(exist_ok=True)
    freeze(directory / "request.json", body)
    raw_path = directory / "response.json"
    if not raw_path.exists():
        provider = OpenAIProvider(os.environ["OPENAI_API_KEY"])
        with (directory / "submission.json").open("x") as lock:
            json.dump(
                {
                    "request_digest": digest(body),
                    "invocation_id": body["metadata"]["podgen_invocation_id"],
                },
                lock,
            )
        try:
            reply = provider.submit(body, client_request_id=body["metadata"]["podgen_invocation_id"])
        except ProviderFault as exc:
            if exc.reply:
                (directory / "http-error-body").write_bytes(exc.reply.body)
            freeze(directory / "error.json", {"code": exc.code, "ambiguous": exc.ambiguous})
            raise
        raw_path.write_bytes(reply.body)
        if reply.request_body:
            (directory / "wire-request.json").write_bytes(reply.request_body)
        freeze(
            directory / "receipt.json",
            dict(http_status=reply.status_code, request_id=reply.request_id),
        )
    raw = load_unique_json(raw_path.read_bytes())
    freeze(directory / "usage.json", raw.get("usage"))
    if raw["status"] != "completed" or raw["model"] != body["model"] or raw["metadata"] != body["metadata"]:
        raise ValueError("Incomplete or unbound provider reply")
    parts = []
    for item in raw["output"]:
        if item["type"] == "reasoning":
            continue
        assert item["type"] == "message" and item["role"] == "assistant" and item["status"] == "completed"
        for part in item["content"]:
            assert part["type"] == "output_text"
            parts.append(part["text"])
    parsed = load_unique_json("".join(parts))
    freeze(directory / "output.json", parsed)
    return parsed


def repair(directory, plan, parent):
    from copy import deepcopy

    body = deepcopy(parent)
    parent_id = body["metadata"]["podgen_invocation_id"]
    body["metadata"].update(
        podgen_parent_invocation_id=parent_id,
        podgen_attempt_index="2",
        podgen_invocation_id=str(uuid5(NS, "anchor-repair:" + parent_id)),
    )
    body["text"]["format"].update(name="anchor_choices", schema=AnchorChoices.model_json_schema())
    offered = anchor_payload(plan)
    replace_input(body, load_anchor_repair_instructions().body, offered)
    choices = AnchorChoices.model_validate(submit(directory / "anchor-repair", body))
    allowed = {item["anchor_id"]: item["selector_ids"] for item in offered["invalid_anchors"]}
    for choice in choices.choices:
        if choice.selector_id is not None and choice.selector_id not in allowed[choice.anchor_id]:
            raise ValueError("unoffered_anchor_selector")
    return plan.apply(choices)


def verify_one(row):
    letter, script, body, path, before = row
    directory = OUT / letter
    directory.mkdir(exist_ok=True)
    wire = MODEL.model_validate(submit(directory / "fact-review", body))
    script_model = WriteOutput.model_validate(script)
    plan = AnchorRepairPlan.build(script_model, wire)
    if plan.broken:
        wire = repair(directory, plan, body)
    wire.validate_against(script_model)
    assert wire.reading_mode == "informed"
    assert wire.recommendation != "needs_material" or wire.research_requests
    stable = MODEL.model_validate(REFS.translate(wire.model_dump(mode="json"), inbound=True))
    stable.validate_against(script_model)
    assert all(set(f.support_labels) <= set(MATERIALS.material_catalog) for f in stable.findings)
    assert fingerprint(path) == before
    freeze(directory / "validated-wire.json", wire.model_dump(mode="json"))
    freeze(directory / "validated-stable.json", stable.model_dump(mode="json"))
    group_counts = {
        key: sum(len(entry.anchors) for entry in group) for key, group in wire.anchored_groups().items()
    }
    freeze(
        directory / "binding.json",
        dict(
            blind_source=str(path.relative_to(ROOT)),
            script_file_sha256=before,
            canonical_script_digest=digest(script),
            common_digest=digest(common(body)),
            anchor_count=group_counts,
            repaired_anchor_count=len(plan.broken),
            all_support_labels_resolved=True,
        ),
    )


def summarize():
    from decimal import Decimal

    pricing = ROOT / "evals/reports/2026-11-08-author-openings/pricing.json"
    rates = json.loads(pricing.read_text())["rates_per_million_usd"][PROFILE_MODEL.model_id]
    rates = {key: Decimal(rate) for key, rate in rates.items()}
    calls = []
    for letter in "ABCD":
        for usage_path in sorted((OUT / letter).glob("*/usage.json")):
            usage = json.loads(usage_path.read_text())
            cached = usage["input_tokens_details"]["cached_tokens"]
            written = usage["input_tokens_details"]["cache_write_tokens"]
            regular = usage["input_tokens"] - cached - written
            assert regular >= 0
            usd = (
                regular * rates["input"]
                + cached * rates["cached"]
                + written * rates["write"]
                + usage["output_tokens"] * rates["output"]
            ) / Decimal(1_000_000)
            calls.append(
                dict(letter=letter, stage=usage_path.parent.name, usage=usage, estimated_usd=str(usd))
            )
    freeze(
        OUT / "usage.json",
        dict(
            calls=calls,
            total_input_tokens=sum(c["usage"]["input_tokens"] for c in calls),
            total_output_tokens=sum(c["usage"]["output_tokens"] for c in calls),
            estimated_total_usd=str(sum(Decimal(c["estimated_usd"]) for c in calls)),
            pricing_source=str(pricing.relative_to(ROOT)),
            pricing_source_sha256=fingerprint(pricing),
        ),
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run"))
    args = parser.parse_args()
    frozen = prepare()
    if args.action == "prepare":
        return
    if not (BLIND / "COMPLETE").is_file():
        raise SystemExit("Awaiting blind/COMPLETE")
    complete = load_unique_json((BLIND / "COMPLETE").read_bytes())
    assert set(complete) == set("ABCD")
    rows = []
    for letter in "ABCD":
        candidates = [p for p in (BLIND / f"{letter}.json", BLIND / letter / "script.json") if p.is_file()]
        if len(candidates) != 1:
            raise ValueError(f"Expected one blind script file for {letter}")
        path = candidates[0]
        script = WriteOutput.model_validate_json(path.read_bytes()).model_dump(mode="json")
        assert digest(script) == complete[letter]["script"]
        assert fingerprint(BLIND / f"{letter}.md") == complete[letter]["text"]
        body = request(script)
        assert common(body) == frozen
        rows.append((letter, script, body, path, fingerprint(path)))
    assert len({digest(row[1]) for row in rows}) == 4
    load_dotenv(ROOT / ".env", override=False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(verify_one, row) for row in rows]
        errors = []
        for row, future in zip(rows, futures, strict=True):
            try:
                future.result()
            except Exception as exc:
                errors.append(f"{row[0]}: {type(exc).__name__}: {exc}")
        if errors:
            raise RuntimeError(errors)
    summarize()


if __name__ == "__main__":
    main()
