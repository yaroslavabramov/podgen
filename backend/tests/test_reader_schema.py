"""Cold-reader semantics change descriptions only; old wire and other roles remain frozen."""

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest

from podgen.domain.errors import ContractError
from podgen.runtime.evaluation.preview import preview_request
from podgen.runtime.evaluation.role_schema import response_schema
from podgen.writing.projection_versions import V1, V2, V3, V4, V5, V6, V7
from podgen.writing.reader_schema import FIELDS, FINDING_FIELDS, VERSION
from podgen.writing.reader_schema import V1 as READER_V1
from tests.test_planning_schema_versions import without_descriptions
from tests.test_script_handoff import wire_hash
from tests.test_v5_role_wire import fixture

CONTRACT = "editorial-role-packets/1"
VERSIONS = (V1, V2, V3, V4, V5, V6, V7)
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("contract", [CONTRACT, "legacy-author-materials/1"])
@pytest.mark.parametrize("stage", ["reader-review", "reader-review-1", "reader-review-2"])
def test_reader_schema_preserves_types_and_legacy_bytes_in_both_contracts(contract, stage):
    expected = json.loads((ROOT / "backend/tests/fixtures/role-projection-v6.hashes.json").read_text())
    for version in VERSIONS:
        old = response_schema(stage, contract, projection_version=version)
        assert wire_hash(old) == expected["schemas"]["reader-review"]
        new = response_schema(stage, contract, projection_version=version, reader_schema_version=READER_V1)
        assert without_descriptions(new) == without_descriptions(old)
        for field, description in FIELDS.items():
            assert new["properties"][field]["description"] == description
        for field, description in FINDING_FIELDS.items():
            assert new["$defs"]["EditorialFinding"]["properties"][field]["description"] == description
        assert response_schema(stage, contract, projection_version=version) == old


def test_only_reader_descriptions_change_and_preview_uses_the_pinned_contract():
    materials, _, _, profile, script, *_ = fixture()
    for stage in ("plan", "plan-review", "draft", "review", "fact-review", "revise-1"):
        assert response_schema(stage, CONTRACT, reader_schema_version=VERSION) == response_schema(
            stage, CONTRACT
        )
    for stage in ("reader-review", "reader-review-1", "reader-review-2"):
        body = preview_request(uuid4(), stage, profile, materials, draft=deepcopy(script))
        assert body["text"]["format"]["schema"] == response_schema(
            stage, CONTRACT, reader_schema_version=VERSION
        )
    with pytest.raises(ContractError, match="unknown_reader_review_schema_version"):
        response_schema("reader-review", CONTRACT, reader_schema_version="unknown")
