# Searching pinned source documents

The original `hybrid-local/1` profile shipped with `frozen-corpus-tools/4`. Earlier tool versions use their original literal search. Later tool profiles add larger corpora, pagination and continuation. The manifest pins the search contract, model revisions and checksums for a particular run. [Current corpus runtime](research-corpus-runtime.md)

## Installation

From the repository root, configure `PODGEN_DATABASE_URL` for the database described in the backend guide. [Local setup](../../backend/README.md)

```sh
uv sync --directory backend --locked
docker compose -p podgen-wp01 -f backend/compose.yaml up -d --build
uv run --directory backend alembic upgrade head
uv run --directory backend python -m podgen.retrieval prepare
uv run --directory backend python -m podgen.retrieval check
```

The bundled PostgreSQL 17 service includes pgvector. An external database needs that extension installed and permission to enable it. Migrations 0019 and 0020 introduced the projection and its withdrawal rules. The derived index can be rebuilt from retained source data.

`prepare` downloads roughly 270 MB of pinned models and tokenizers. It verifies each SHA-256 before atomic publication. Set `PODGEN_SEARCH_CACHE` to override the cache at `~/.cache/podgen/source-search/models`. The filesystem cache holds model assets. Source text and vectors stay in project-scoped storage. Local inference uses the E5 multilingual small encoder and the mMARCO multilingual MiniLM reranker. [Encoder](https://huggingface.co/intfloat/multilingual-e5-small) · [ONNX conversion](https://huggingface.co/Xenova/multilingual-e5-small) · [Reranker](https://huggingface.co/cross-encoder/mmarco-mMiniLMv2-L12-H384-v1)

## Run, preview and resume

An ordinary run with this contract checks project access and model installation, then prepares the index before submitting to the writing provider. Preview shows the tool declarations without downloading models or building the index. Resume reuses the original manifest and completed tool results. Missing dependencies or an incomplete index produce a technical error. After the cause is resolved, resume continues the unfinished operation.

The projection key includes project, shared input record, profile SHA-256, source ID and source text digest. PostgreSQL holds FTS data, exact tokens, vectors, offsets and blob dependencies. Each source publishes atomically. Blob withdrawal cascades to index rows and blocks republication. The project lock serializes publication and reading against withdrawal. Model computation takes place outside that lock.

## Search contract

1. `query` accepts a question in the episode language, including a Russian question about an English source. The hybrid profile limits a query to 1,000 characters and 128 model tokens. An oversized query returns an error.
2. `within=S…` restricts the search to one source. `within=M…` searches the original documents behind the card, including material outside its excerpts. With no scope, the tool searches the admitted primary documents. Scope applies before dense retrieval, full-text ranking and reranking.
3. Three branches retrieve up to 96 candidates each. PostgreSQL performs language-appropriate FTS (`english`, `russian` or `simple`), a literal branch preserves exact names and numbers, and dense search measures distance within the bounded corpus. Reciprocal rank fusion combines candidates, then a multilingual cross-encoder reranks their union.
4. A page returns up to six results within 6,144 UTF-8 bytes. A result has a source alias, title preview, a verbatim preview up to 280 characters, its precise preview offsets and the full ranked window's offsets. All offsets count Unicode code points in the source. Follow the continuation to read more. `read_source` opens the original range and issues an E receipt. A search hit supplies a location for reading.
5. Scores and component ranks stay in `source_search_diagnostics` and the stage's `source-search.json` export. Even a question with no answer in the corpus may have a nearest neighbor. The reviewer establishes support from the passage.

The original bounded profile admits at most 64 primary documents, two million source characters and 12,000 overlapping 224-token chunks. It fails explicitly on overflow. The streaming profile has a separate contract. The in-memory adapter is available for explicitly configured offline evaluations.

## Duplicate passages and text variants

`passage-diversity/1` groups exact windows by text digest before a branch applies its candidate limit. Every original `(source_id, start, end)` remains addressable. Nearby versions may form a navigation group after ranking. The policy requires at least 20 tokens and 65% shared seven-token sequences, with at least 85% token agreement for close variants. Only whitespace may differ for an expanded range to count as identical. Migration 0030 records window text digests and backfills them in bounded batches.

`overlapping_text_variants` exposes both verbatim variants with source ID, range and surrounding context. An absent insertion has an empty range. Differences that cannot be shown within 1,024 bytes remain separate results. The tool displays two alias selectors up front. Follow `next_alias_page` with `aliases_of` to reach the rest. Each reading rechecks access. Multiple aliases for the same wording provide navigation across copies, without establishing independent corroboration.

## Verification

Database integration tests cover PostgreSQL/pgvector, scope across all branches, Russian FTS, project isolation, atomic publication, withdrawal and concurrent indexing. Offline tests exercise exact previews, byte limits, pagination, interrupted installations and resume of an unfinished tool without resubmitting to the model. Search quality requires separate queries against real source material and negative controls. [Integration tests](../../backend/tests/test_source_search_index.py) · [Offline search tests](../../backend/tests/test_hybrid_source_search.py) · [Tool runtime](../../backend/tests/test_source_tool_runtime.py)
