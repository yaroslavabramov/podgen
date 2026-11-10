"""Both actually persisted v1 forms replay; missing subsets and altered parameters never do."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from podgen.domain.errors import ContractError
from podgen.retrieval.config import COMPACT_PROFILE_V1, PROFILE, validate
from podgen.retrieval.hybrid import HybridSearch
from podgen.retrieval.memory import MemoryIndex
from podgen.runtime.evaluation.source_tool_contract import create, from_manifest
from podgen.writing.source_tools.contracts import Search
from podgen.writing.source_tools.corpus import Source

from tests.source_search_support import TinyModels

FIXED_FIELDS = (
    "embedding_dimensions",
    "pooling",
    "prefixes",
    "chunking",
    "fusion",
    "lexical",
    "max_model_tokens",
    "max_query_tokens",
    "rerank_batch_size",
)


def test_published_compact_form_replays_actual_search_without_rewriting_raw_contract():
    raw = create(SimpleNamespace(name="production/1"))
    raw["retrieval"] = deepcopy(COMPACT_PROFILE_V1)
    manifest = dict(behavior_profile="production/1", source_tool_contract=raw)
    frozen = deepcopy(manifest)
    contract = from_manifest(manifest)
    assert (
        contract == frozen["source_tool_contract"] and validate(contract["retrieval"]) == COMPACT_PROFILE_V1
    )
    document = Source("Primary source", "Original words about an experiment.", "a" * 64)
    args = Search(query="experiment", within="S1", offset=0)
    old = HybridSearch({"S1": document}, {}, contract["retrieval"], index=MemoryIndex(), network=TinyModels())
    new = HybridSearch({"S1": document}, {}, PROFILE, index=MemoryIndex(), network=TinyModels())
    assert old.search(args) == new.search(args)
    assert manifest == frozen


@pytest.mark.parametrize("field", FIXED_FIELDS)
def test_partial_explicit_settings_are_rejected(field):
    missing = deepcopy(PROFILE)
    missing.pop(field)
    with pytest.raises(ContractError):
        validate(missing)
    partial = deepcopy(COMPACT_PROFILE_V1)
    partial[field] = deepcopy(PROFILE[field])
    with pytest.raises(ContractError):
        validate(partial)


@pytest.mark.parametrize(
    "field,value",
    [
        ("candidate_pool", 36),
        ("page_hits", 7),
        ("max_response_bytes", 8000),
        ("chunk_tokens", 128),
        ("max_documents", 64.0),
    ],
)
def test_changed_compact_parameters_are_not_a_legacy_profile(field, value):
    raw = deepcopy(COMPACT_PROFILE_V1)
    raw[field] = value
    with pytest.raises(ContractError):
        validate(raw)
