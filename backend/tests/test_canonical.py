import json
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import pytest

from podgen.domain.canonical import canonical_bytes, content_hash
from podgen.domain.contracts.common import ArtifactRef, TextSelector, select_text
from podgen.domain.contracts.registry import REGISTRY
from podgen.schema_export import export

ROOT = Path(__file__).resolve().parents[2]


def test_frozen_python_golden_and_typescript():
    fixture = json.loads((ROOT / "contracts/golden.json").read_text())
    for case in fixture["cases"]:
        doc = case["document"]
        assert canonical_bytes(doc).decode() == case["canonical"]
        assert (
            content_hash(
                doc["kind"],
                doc["schema_version"],
                doc["payload"],
                [ArtifactRef.model_validate(r) for r in doc["input_refs"]],
                doc["blob_hashes"],
            )
            == case["content_hash"]
        )
    for item in fixture["selectors"]:
        assert (
            select_text(
                item["text"],
                TextSelector(
                    kind="text",
                    block_ids=[],
                    char_start=item["start"],
                    char_end=item["end"],
                    exact=item["exact"],
                    prefix="",
                    suffix="",
                ),
            )
            == item["exact"]
        )
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js 22+ required for independent TypeScript hash check")
    subprocess.run(
        [node, "--experimental-strip-types", str(ROOT / "contracts/check-golden.ts")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_inputs_are_sets_but_payload_order_is_preserved():
    one = ArtifactRef(kind="ResearchNote", artifact_id=UUID(int=1), revision_id=UUID(int=2))
    two = ArtifactRef(kind="ResearchNote", artifact_id=UUID(int=3), revision_id=UUID(int=4))
    assert content_hash("ResearchNote", "1.0", {"ordered": ["a", "b"]}, [one, two, one], []) == content_hash(
        "ResearchNote", "1.0", {"ordered": ["a", "b"]}, [two, one], []
    )
    assert content_hash("ResearchNote", "1.0", {"ordered": ["a", "b"]}, [one], []) != content_hash(
        "ResearchNote", "1.0", {"ordered": ["b", "a"]}, [one], []
    )
    assert content_hash("ResearchNote", "1.0", {"x": None}, [], []) != content_hash(
        "ResearchNote", "1.0", {}, [], []
    )


def test_generated_schemas_match_canonical_owners(tmp_path):
    export(tmp_path)
    expected = ROOT / "contracts/generated"
    assert {p.name for p in tmp_path.iterdir()} == {p.name for p in expected.iterdir()}
    for actual in tmp_path.iterdir():
        assert actual.read_bytes() == (expected / actual.name).read_bytes(), f"Regenerate {actual.name}"
    assert len(REGISTRY) == 44
