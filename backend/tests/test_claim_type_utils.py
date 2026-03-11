import uuid

import pytest

from app.pipeline.phase4_claims.claim_type_utils import normalize_claim_type
from app.pipeline.phase4_claims.dedupe import _repair_invalid_claim_types


@pytest.mark.parametrize(
    ("raw_claim_type", "attribution", "expected"),
    [
        ("FACT", None, "fact"),
        ("case", None, "fact"),
        ("CASE", None, "fact"),
        ("expert_quote", None, "fact"),
        ("weird_label", {"who": "HKMA"}, "report"),
        ("weird_label", None, "fact"),
    ],
)
def test_normalize_claim_type_maps_invalid_labels(raw_claim_type, attribution, expected) -> None:
    normalized, _ = normalize_claim_type(raw_claim_type, attribution)
    assert normalized == expected


class _FakeMappingResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _FakeRepairDB:
    def __init__(self) -> None:
        self.update_statements = []
        self.flushed = False

    async def execute(self, statement, params=None):
        sql = str(statement)
        if "SELECT id, claim_type, attribution" in sql:
            return _FakeMappingResult(
                [
                    {
                        "id": uuid.uuid4(),
                        "claim_type": "case",
                        "attribution": None,
                    }
                ]
            )
        self.update_statements.append(statement)
        return None

    async def flush(self) -> None:
        self.flushed = True


@pytest.mark.asyncio
async def test_repair_invalid_claim_types_coerces_case_rows_before_orm_load() -> None:
    db = _FakeRepairDB()

    await _repair_invalid_claim_types(db, [uuid.uuid4()])

    assert db.flushed is True
    assert len(db.update_statements) == 1
    compiled = db.update_statements[0].compile()
    assert compiled.params["claim_type"] == "fact"
