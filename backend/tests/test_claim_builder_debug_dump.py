import json

from app.pipeline.phase4_claims.claim_builder import _dump_claim_builder_batch_debug


def test_dump_claim_builder_batch_debug_writes_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.settings.web_extractor_debug_dump_dir",
        str(tmp_path),
        raising=False,
    )

    _dump_claim_builder_batch_debug(
        run_id="run-42",
        batch_number=2,
        status="ok",
        input_payload={
            "target_language": "ru",
            "instructions": "Build claims from these evidence spans. Group related evidence.",
            "evidence_spans": [
                {
                    "id": "e1",
                    "verbatim_quote": "Quote one",
                    "asset_type": "case",
                    "source_id": "s1",
                }
            ],
        },
        llm_raw_output='[{"claim_type":"FACT","statement":"Statement"}]',
        parsed_claims=[
            {
                "claim_type": "FACT",
                "statement": "Statement",
                "supporting_evidence_ids": ["e1"],
                "illustrative_evidence_ids": [],
            }
        ],
        saved_claims=[
            {
                "claim_id": "c1",
                "claim_type": "fact",
                "statement": "Statement",
                "supporting_evidence_ids": ["e1"],
                "illustrative_evidence_ids": [],
            }
        ],
    )

    files = list((tmp_path / "claims").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["run_id"] == "run-42"
    assert payload["batch_number"] == 2
    assert payload["status"] == "ok"
    assert payload["input"]["evidence_count"] == 1
    assert payload["persisted"]["saved_claim_count"] == 1


def test_dump_claim_builder_batch_debug_skips_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.phase4_claims.claim_builder.settings.web_extractor_debug_dump_dir",
        "",
        raising=False,
    )

    _dump_claim_builder_batch_debug(
        run_id="run-42",
        batch_number=1,
        status="ok",
        input_payload={"target_language": "en", "instructions": "x", "evidence_spans": []},
        llm_raw_output="[]",
        parsed_claims=[],
        saved_claims=[],
    )

    assert list(tmp_path.glob("**/*.json")) == []
