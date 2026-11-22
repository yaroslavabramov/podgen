"""V1 native requests stay frozen while V2 adds only adjacent signed primary evidence."""

import hashlib
import json
from copy import deepcopy
from pathlib import Path
from uuid import UUID

import pytest

from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.preview_snapshot import PreviewSnapshot
from podgen.runtime.evaluation.request_payload import request_payload
from podgen.writing import fact_context
from podgen.writing.source_signatures import signature_evidence
from tests.signature_cases import materials_for
from tests.source_excerpt_cases import release_before_reading_audit


@pytest.mark.parametrize("case", ["snow-pump", "rain-soil"])
def test_v1_bytes_stay_identical_and_v2_supports_only_the_cited_cards(monkeypatch, case):
    materials, profile = materials_for(case)
    _, _, _, current = release_before_reading_audit(monkeypatch, materials, profile)
    assert current[fact_context.KEY] == fact_context.SIGNATURE_VERSION
    # Reconstruct the released pre-tools pair. Hash fixtures represent those exact historical requests.
    from podgen.runtime.evaluation.role_schema import response_schema
    from podgen.writing.model_references import bind_schema, run_references

    current.pop("source_tool_contract")
    current["fact_review_schema_version"] = fact_context.SCHEMA_VERSION
    for stage in ("fact-review", "fact-review-1", "fact-review-2"):
        current["response_schemas"][stage] = bind_schema(
            response_schema(
                stage, "editorial-role-packets/1", fact_schema_version=fact_context.SCHEMA_VERSION
            ),
            run_references(materials, current),
        )
    old = deepcopy(current)
    old[fact_context.KEY] = fact_context.VERSION
    script = dict(utterances=[dict(speaker=materials.speaker_labels[0], text="A frozen factual statement.")])
    common = dict(draft=script)
    run_id = UUID("00000000-0000-4000-8000-000000000001")
    historical = preview_request(
        run_id,
        "fact-review",
        profile("fact-review"),
        materials,
        snapshot=PreviewSnapshot(old, "Frozen fact instructions."),
        **common,
    )
    expected = json.loads((Path(__file__).parent / "fixtures/fact-context-v1.hashes.json").read_text())
    assert (
        hashlib.sha256(json.dumps(historical, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        == expected[case]
    )
    additional = signature_evidence(materials)
    if case == "snow-pump":
        signed = next(row for row in additional if "HENRY WHITEHEAD, M.A." in row["text"])
        assert signed["supports"] == ["C23d72f102d80"]
        assert signed["text"] == "HENRY WHITEHEAD, M.A.\n"
    else:
        assert additional == []
    prefix = None
    for stage in ("fact-review", "fact-review-1", "fact-review-2"):
        a = preview_request(
            run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(current, "Frozen fact instructions."),
            **common,
        )
        b = preview_request(
            run_id,
            stage,
            profile(stage),
            materials,
            snapshot=PreviewSnapshot(old, "Frozen fact instructions."),
            **common,
        )
        av, bv = request_payload(a), request_payload(b)
        assert av["evidence"][: len(bv["evidence"])] == bv["evidence"]
        extras = av["evidence"][len(bv["evidence"]) :]
        parts = a["input"][0]["content"]
        cached = parts[: 1 + max(i for i, part in enumerate(parts) if "prompt_cache_breakpoint" in part)]
        if prefix is not None:
            assert prefix == cached
        prefix = cached
        assert all(label.startswith("M") for row in extras for label in row["supports"])
        av["evidence"] = bv["evidence"]
        assert av == bv and a["text"] == b["text"] and a["instructions"] == b["instructions"]
        if case == "snow-pump":
            assert len(extras) == len(additional) > 0
            assert "HENRY WHITEHEAD, M.A." in json.dumps(cached)
        else:
            assert a == b
    assert (
        preview_request(
            run_id,
            "fact-review",
            profile("fact-review"),
            materials,
            snapshot=PreviewSnapshot(old, "Frozen fact instructions."),
            **common,
        )
        == historical
    )
