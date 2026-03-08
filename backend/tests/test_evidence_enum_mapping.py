from sqlalchemy import Enum
from sqlalchemy.dialects import postgresql

from app.models.evidence import AssetType, EvidenceQuestionRelevance


def test_sqlalchemy_enum_binds_strenum_values_to_uppercase_labels() -> None:
    relevance_enum = Enum(EvidenceQuestionRelevance, native_enum=False)
    relevance_proc = relevance_enum.bind_processor(postgresql.dialect())
    assert relevance_proc is not None
    assert relevance_proc("direct") == "DIRECT"
    assert relevance_proc(EvidenceQuestionRelevance.CONTEXTUAL) == "CONTEXTUAL"

    asset_enum = Enum(AssetType, native_enum=False)
    asset_proc = asset_enum.bind_processor(postgresql.dialect())
    assert asset_proc is not None
    assert asset_proc("mechanism") == "MECHANISM"
