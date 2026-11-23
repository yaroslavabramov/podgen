# Podgen backend

The backend runs the podcast workflow and records the material, model calls and audio behind each episode. PostgreSQL stores identities, revisions, permissions and execution state. Original documents, exported scripts and media live in object storage. The episode CLI coordinates research, source acquisition, planning, writing, two independent readings, revision and audio assembly.

## Local setup

From the repository root, install Python 3.12 or newer and the locked dependencies with uv. The examples below use the development PostgreSQL and MinIO services bundled in [compose.yaml](compose.yaml).

```sh
uv sync --directory backend --locked
docker compose -p podgen-dev -f backend/compose.yaml up -d --build
export PODGEN_DATABASE_URL='postgresql+psycopg://podgen:podgen-test-only-password@127.0.0.1:55440/podgen_wp01'
uv run --directory backend alembic upgrade head
```

Wait for PostgreSQL to become healthy before applying migrations. The Compose stack uses PostgreSQL 17 with pgvector and binds both services to loopback. Use a dedicated database for tests. Local source retrieval also needs the pinned encoder and reranker. Install those separately with `uv run --directory backend python -m podgen.retrieval prepare`. Audio decoding and assembly require FFmpeg. See [source search](../docs/podcast-production/source-search.md) and [episode production](../docs/podcast-production/episode-production.md) for additional prerequisites.

## Try the storage and editorial contracts

This example stages two frozen editorial cases into a new test project. It uses the local database and filesystem object store and makes no model calls. Run it from the repository root after setup.

```sh
export PODGEN_DEMO_DIR="$(pwd)/tmp/editorial-demo"
mkdir -p "$PODGEN_DEMO_DIR"
uv run --directory backend python -m podgen.staging_cli \
  ../evals/cases/development/wp02-batch-01 --new-test-project \
  --objects "$PODGEN_DEMO_DIR/objects" --out "$PODGEN_DEMO_DIR/staging.json"
```

Staging checks the package against `inventory.sha256.json` before publication. It verifies the exact bytes, script order and Unicode offsets, repeats the import to check idempotency, and tests generation access to evaluator-only material. The report contains the project ID, actor ID and per-file references needed for the [writing walkthrough](WRITING-EVALUATION.md). A staged draft or reference answer retains its original provenance and review status.

A smaller synthetic GPS walkthrough exercises canonical artifacts and their relationships:

```sh
uv run --directory backend python -m tests.demo \
  --out "$PODGEN_DEMO_DIR/storage-demo.json" --objects "$PODGEN_DEMO_DIR/storage-objects"
```

## Development checks

The contract tests and TypeScript vectors run without a provider account. The TypeScript check requires a Node release that supports `--experimental-strip-types` (Node 22.6 or later).

```sh
uv run --directory backend pytest -q tests/test_canonical.py
node --experimental-strip-types contracts/check-golden.ts
```

For the integration suite, set `PODGEN_TEST_DATABASE_URL` to a migrated test database and `PODGEN_TEST_S3_ENDPOINT` to the local MinIO endpoint. Migration tests require permission to create and remove isolated test databases. Missing integration services produce explicit skips. Run a single heavy suite at a time.

```sh
export PODGEN_TEST_DATABASE_URL="$PODGEN_DATABASE_URL"
export PODGEN_TEST_S3_ENDPOINT='http://127.0.0.1:59091'
export PODGEN_TEST_MIGRATIONS=1
uv run --directory backend pytest -q
uv run --directory backend ruff check src tests migrations
```

## Interfaces

| Task | Entry point |
| --- | --- |
| Prepare or run one episode from a topic | [Episode production](../docs/podcast-production/episode-production.md) |
| Import editorial inputs, preview a role or run an isolated writing cycle | [Writing evaluation](WRITING-EVALUATION.md) |
| Validate an interchange package and regenerate schemas | [Canonical contracts](../contracts/README.md) |
| Inspect editorial wire contracts and revision addressing | [Writing schemas](schemas/README.md) |

## Persistence and recovery

Canonical artifact revisions are immutable. Runtime records have separate identities and permissions. An import checks references, access, selectors and dependency cycles in one database transaction. Blob uploads can precede this transaction; a failed import may leave unreferenced bytes. Oversized semantic inputs fail explicitly.

Reading a record requires current project authorization. Evaluator-only content follows its bytes through aliases and derived records; ordinary generation cannot retrieve it through another reference. Quality dispositions and privacy restrictions have independent lifetimes. A task run binds its own manifest, model, prompts and tools.

The execution journal records a submission intent before every paid action. A known response can be retrieved or revalidated; ambiguous submissions remain visible for recovery. A completed stage keeps its paid result across a retry. See [technical recovery](../docs/podcast-production/technical-recovery.md) and [execution journal](../docs/podcast-production/execution-journal.md).
