"""Prepare immutable blind wires, run the two independent reading tasks, or replay their public outputs."""

import argparse
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from itertools import chain
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from pair_schema import Comparison
from podgen.domain.canonical import digest
from podgen.domain.contracts.configuration import ModelProfile
from podgen.runtime.evaluation.compiler import response_request
from podgen.runtime.evaluation.role_request import compile_reading_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.editorial_contract import Assignment
from podgen.writing.model_references import ModelReferences
from podgen.writing.projection_versions import V9
from podgen.writing.prompts import load_reading_instructions
from podgen.writing.reader_schema import VERSION, snapshot_model
from podgen.writing.roles import WriteOutput

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[3]
BLIND = HERE.parent / "blind"
sys.path.insert(0, str(ROOT / "evals/operator/author-openings"))
from artifacts import read, save, send_once, sha
from conditions import response_text
from providers import Transport, credentials
from results import estimate

LABELS = "ABCD"
PIN = "reader-review/sha256:beb6837039bc8ae18e494044121e0572a9d43747b847c813dc5b5b02e2013018"
PROFILE = ModelProfile.model_validate(
    read(ROOT / "evals/reports/2026-11-08-story-editing/controls/profile.json")
)
RATES = read(ROOT / "evals/reports/2026-11-08-author-openings/pricing.json")[
    "rates_per_million_usd"
]


def speech(label):
    return WriteOutput.model_validate(read(BLIND / f"{label}.json")).model_dump(
        mode="json"
    )


def boxes():
    edges = [tuple(edge) for edge in read(BLIND / "pairs.json")]
    assert len(edges) == len({frozenset(edge) for edge in edges}) == 4
    assert all(len(edge) == 2 and edge[0] != edge[1] for edge in edges)
    assert Counter(chain.from_iterable(edges)) == Counter({k: 2 for k in LABELS})
    return [(a, b) for edge in edges for a, b in (edge, edge[::-1])]


def prepare():
    for k in LABELS:
        assert (BLIND / f"{k}.md").is_file()
    assignment = Assignment.model_validate(read(BLIND / "assignment.json"))
    prompt = load_reading_instructions("reader-review")
    assert prompt.version == PIN
    schema = response_schema(
        "reader-review",
        "editorial-role-packets/1",
        reader_schema_version=VERSION,
        projection_version=V9,
    )
    # The cold reader sees no dossier and has no fields for evidence or plan references.
    refs = ModelReferences(digest(assignment.model_dump(mode="json")), ())
    materials = SimpleNamespace(
        packet=SimpleNamespace(assignment=assignment),
        format=assignment.format,
        output_language=assignment.output_language,
    )
    for label in LABELS:
        utterances = speech(label)
        uid = uuid5(
            NAMESPACE_URL, f"podgen:nov8:composition:cold:{label}:{digest(utterances)}"
        )
        wire = compile_reading_request(
            uid,
            "reader-review",
            PROFILE,
            materials,
            prompt.body,
            schema,
            script=utterances,
            projection_version=V9,
            reference_catalog=refs,
        )
        visible = json.loads(wire["input"][0]["content"][0]["text"])
        assert (
            set(visible)
            == {
                "script",
                "format",
                "genre",
                "audience",
                "duration_context",
                "output_language",
            }
            and wire["text"]["format"]["schema"] == schema
        )
        save(HERE / "requests/cold" / f"{label}.json", wire)
    for first, second in boxes():
        episodes = {
            "first": [row["text"] for row in speech(first)["utterances"]],
            "second": [row["text"] for row in speech(second)["utterances"]],
        }
        user = {
            "assignment": assignment.question,
            "audience": assignment.listener_profile or assignment.listener,
            "genre": assignment.genre,
            "requested_seconds": assignment.duration.model_dump(mode="json"),
            "output_language": assignment.output_language,
            "episodes": episodes,
        }
        uid = uuid5(NAMESPACE_URL, f"podgen:nov8:composition:pair:{digest(user)}")
        wire = response_request(
            uid,
            "review",
            PROFILE,
            user,
            (HERE / "rules/pair.md").read_text(),
            Comparison.model_json_schema(),
        )
        save(HERE / "requests/pairs" / f"{first}{second}.json", wire)
    prepared_checksums = {
        str(p.relative_to(HERE)): sha(p.read_bytes())
        for p in sorted((HERE / "requests").glob("*/*.json"))
    }
    assert len(prepared_checksums) == 12
    save(HERE / "rules/request-checksums.json", prepared_checksums)


def read_one(entry, live):
    kind, label = entry
    source = HERE / "requests" / kind / f"{label}.json"
    target = HERE / "results" / kind / label
    wire = read(source)
    source_hash = sha(source.read_bytes())
    assert (
        read(HERE / "rules/request-checksums.json")[str(source.relative_to(HERE))]
        == source_hash
    )
    assert (
        wire["model"] == "gpt-6.1-terra"
        and wire["reasoning"]["effort"] == "medium"
        and not wire["tools"]
    )
    if live:
        response = send_once(
            ROOT / "tmp/composition-blind-reading" / kind / label,
            wire,
            Transport("openai", credentials(ROOT)["openai"]),
        )
        amount = estimate(response, "openai", RATES)
        save(
            target / "receipt.json",
            {
                "id": response["id"],
                "model": response["model"],
                "status": response["status"],
                "usage": response["usage"],
                "estimated_usd": amount,
                "request_sha256": source_hash,
            },
        )
        if response["status"] != "completed":
            raise ValueError("Incomplete physical response for " + str(entry))
        save(target / "output.json", json.loads(response_text(response, "openai")))
    out = read(target / "output.json")
    receipt = read(target / "receipt.json")
    assert receipt["request_sha256"] == source_hash
    if kind == "cold":
        model = snapshot_model(out).model_validate(out)
        model.validate_against(WriteOutput.model_validate(speech(label)))
    else:
        pair = Comparison.model_validate(out)
        for member, guest in ((pair.first, label[0]), (pair.second, label[1])):
            texts = [row["text"] for row in speech(guest)["utterances"]]
            for note in (member.decisive_passage, member.ending):
                if not any(note.quote in text for text in texts):
                    raise ValueError(
                        f"Quotation does not appear in {label}/{guest}: "
                        + repr(note.quote)
                    )
    return label, receipt


def evaluate(live):
    entries = [("cold", label) for label in LABELS] + [
        ("pairs", a + b) for a, b in boxes()
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(lambda e: read_one(e, live), entries))
    assert len({r["id"] for _, r in rows}) == 12
    for kind in ("cold", "pairs"):
        group = [
            (k, r)
            for (scope, _), (k, r) in zip(entries, rows, strict=True)
            if scope == kind
        ]
        total = sum(Decimal(row["estimated_usd"]) for _, row in group)
        save(
            HERE / "cost" / f"{kind}.json",
            {
                "estimated_total_usd": str(total),
                "calls": [dict(case=k, **r) for k, r in group],
            },
        )
    print("Received and validated", len(rows), "independent responses.")
    print("Total estimated USD:", sum(Decimal(r["estimated_usd"]) for _, r in rows))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["prepare", "live", "replay"])
    mode = parser.parse_args().mode
    if mode == "prepare":
        prepare()
    else:
        evaluate(mode == "live")
