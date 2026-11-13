# Runtime operation verification

Audit items 1, 2 and 4. No paid provider calls or continuation of the product run.

The baseline and final runs use an isolated PostgreSQL copy and immutable object-file copies of the saved 51-document research corpus. Providers use the real SDK over local HTTP mocks. One measurement covers context counting, one submitted mock response, actual source reading and durable publication of the tool result. A separate recovery measurement selects five events of one physical turn from 26 retained turns. Corpus setup, indexing/search and real provider latency are excluded; no semantic quality conclusion follows from this probe.

## Changes

- Frozen material descriptors remain run-owned immutable data. Each external boundary freshly authorizes shared input, fixed plan and scoped admissions, without reconstructing documents or repartitioning the report. Local source operations batch related checks; a cached text projection's length is immutable metadata. Content reads still reauthorize.
- ReadAuthorization owns bounded reachable facts: artifact frontiers, effective dependencies, privacy links, byte bindings, roles and typed runtime references. Privacy denial propagates through the complete reachable graph, including cycles; an intermediate visited shortcut is never treated as a negative proof. There is no process-wide access cache or project-wide payload cache.
- Runtime authorization returns one consistent short-header contract. Full bodies are fetched explicitly and remain outside authorization facts. Scope consumers, history restore and retrieval use typed references for closure checks. Scalar and batch blob checks share one decision function.
- Public read operations never expose a SQL connection. Private SQL phases prohibit object I/O while borrowed. Remote reads close metadata snapshots before waiting and freshly authorize before returning bytes. Provider calls reject an active read scope.
- Runtime input-reference validation has an explicit bounded pre-INSERT phase for the evaluator and public reader. Facts are discarded at publication/classification mutation.
- Exact native count receipts bind the canonical native count payload, provider configuration, successful response and durable intent. Submission adopts the same receipt by reference. Changed payload/configuration/evidence cannot reuse it; checkpoint candidates with different schemas still require distinct counts.
- Recovery selects indexed per-turn metadata before reading bodies. Migration 0023 backfills existing events and fills/validates the projection for legacy INSERT writers. Completed paid work is replayed from durable records; an unsent post-checkpoint count failure resumes the same window.

## Measurements

| Operation | Baseline | Final |
| --- | ---: | ---: |
| Step wall time | 32.5264 s | 1.6843 s |
| Step SQL | 324,255 | 1,345 |
| Native counts / mock submissions | 2 / 1 | 1 / 1 |
| Recovery SQL | 1,946,286 | 93 |
| Recovery wall time | 174.0356 s | 0.0982 s |
| Peak process RSS | 155,254,784 bytes | 191,152,128 bytes |

The exact numeric baseline, rejected intermediate, phase breakdown and final data are in the adjacent operation JSON files. The intermediate 155,923-SQL result was rejected: it still repeated the same graph for almost every event. The final measurement includes batched access facts and the separately implemented indexed privacy publication (0025).

The old storage API's `put_calls`/`put_bytes` count inputs passed to publication, not unique physical writes. The final API records staging, publication attempts and existing-object verification separately. These API-level counters are not directly equivalent; a missing old counter does not mean zero writes. File reads and streamed verification bytes are recorded. SQL bytes transferred are not measured.

Peak RSS is the process high-water mark, including corpus setup and imports. It increased relative to the 155,254,784-byte baseline; this is **not a memory improvement**. Payload-free authorization and bounded-frontier invariants are directly tested, but a larger-document-count memory scaling study remains unverified.

82 related PostgreSQL tests passed, including access withdrawal, fixed-plan access, complete cyclic privacy closure, per-turn selection, count replay and retrieval compatibility. The two final scalar-normalization/migration compatibility nodes also passed on a freshly migrated database. A separate cleanup regression passed: failed snapshot closure still clears transaction/facts and restores both operation ContextVars. Ruff passed. The provider and literary prompts, models and semantic stage order were unchanged.

## Reproduction

Use the repository's Python environment and PostgreSQL 17 binaries. Prepare a private state JSON containing `database_url`, `objects`, `project`, `actor` and `run_id` for a saved OpenAI planning invocation and its matching object store. Do not commit credentials or the source state.

From the repository root:

```sh
PYTHONPATH=backend/src python evals/reports/2026-11-13-engineering-audit/measure_operation.py \
  --state /path/to/private-state.json \
  --pg-bin /path/to/postgresql/bin \
  --out /path/to/operation-measurement.json \
  --breakdown
```

The procedure dumps the source read-only, creates a private PostgreSQL server without a TCP listener, clones immutable input files, applies current migrations and runs only a mock provider. It stops and deletes only its owned temporary server and files. The source server must already be available; the procedure never starts or stops it. The output contains counters and SQL templates, not user text, credentials or native request bodies. `--breakdown` adds profiling overhead.

A persistent integration clone was separately left available to the coordinator at `/tmp/podgen-runtime-integration-20261113/state.json`. Its owned data directory is `/tmp/podgen-runtime-integration-20261113/data` (Unix socket port 55465). Temporary probe/test servers were stopped by their runners. The original source PostgreSQL server was already running when this task began; this task did not start or stop it. The coordinator owns cleanup after final integration.
