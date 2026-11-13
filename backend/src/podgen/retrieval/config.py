"""Published immutable retrieval profile. New weights or preprocessing require a new profile."""

import json
from copy import deepcopy

from podgen.domain.errors import ContractError

ENCODER = dict(
    repository="Xenova/multilingual-e5-small",
    revision="761b726dd34fb83930e26aab4e9ac3899aa1fa78",
    license="MIT",
    files={
        "onnx/model_quantized.onnx": "f80102d3f2a1229f387d3c81909990d8945513e347b0eab049f7de3c6f98c193",
        "tokenizer.json": "0b44a9d7b51c3c62626640cda0e2c2f70fdacdc25bbbd68038369d14ebdf4c39",
    },
)
RERANKER = dict(
    repository="cross-encoder/mmarco-mMiniLMv2-L12-H384-v1",
    revision="1427fd652930e4ba29e8149678df786c240d8825",
    license="Apache-2.0",
    files={
        "onnx/model_qint8_arm64.onnx": "1825907d6c1a9001ff78124780bbde20a614a8c3df3b63409cf3c72c6fe5c8b4",
        "tokenizer.json": "62c24cdc13d4c9952d63718d6c9fa4c287974249e16b7ade6d5a85e7bbb75626",
    },
)
PROFILE = dict(
    version="hybrid-local/1",
    embedding_dimensions=384,
    pooling="attention-mean-l2/1",
    prefixes={"query": "query: ", "passage": "passage: "},
    chunking="overlapping-token-window/1",
    fusion="rrf-k60-union/1",
    lexical="postgres-fts-language/1",
    max_model_tokens=512,
    max_query_tokens=128,
    rerank_batch_size=8,
    encoder=ENCODER,
    reranker=RERANKER,
    chunk_tokens=224,
    overlap_tokens=32,
    candidate_pool=96,
    page_hits=6,
    preview_characters=280,
    max_response_bytes=6144,
    max_documents=64,
    max_characters=2000000,
    max_chunks=12000,
)


# This exact compact form was persisted before the implementation's nine fixed settings
# were made explicit. It uses the same weights and behavior; partial forms are not profiles.
COMPACT_PROFILE_V1 = dict(
    version="hybrid-local/1",
    encoder=ENCODER,
    reranker=RERANKER,
    chunk_tokens=224,
    overlap_tokens=32,
    candidate_pool=96,
    page_hits=6,
    preview_characters=280,
    max_response_bytes=6144,
    max_documents=64,
    max_characters=2000000,
    max_chunks=12000,
)


STREAMING_PROFILE = {
    **{
        key: value
        for key, value in PROFILE.items()
        if key not in {"max_documents", "max_characters", "max_chunks"}
    },
    "version": "hybrid-local/2",
    "chunking": "bounded-character-token-windows/2",
    "tokenization_characters": 16384,
    "tokenization_overlap_characters": 2048,
    "index_batch_size": 32,
}


def streaming_profile():
    return deepcopy(STREAMING_PROFILE)


def profile():
    return deepcopy(PROFILE)


def validate(value):
    try:
        identity = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        published = [
            json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
            for item in (PROFILE, COMPACT_PROFILE_V1, STREAMING_PROFILE)
        ]
    except (ValueError, TypeError):
        raise ContractError("unsupported_source_retrieval_profile") from None
    if not isinstance(value, dict) or identity not in published:
        raise ContractError("unsupported_source_retrieval_profile")
    # Preserve the archived representation; validation must never rewrite a manifest or request.
    return deepcopy(value)
