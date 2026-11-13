# Read-only diagnosis of longitude runtime overhead

2026-11-13. No production edits, heavy tests, paid calls or live-process interruption. One 1-second native sample, small read-only SQL snapshots, and one authorized metadata read measured on a separate connection.

## Measured evidence

- Live Python PID 20684: 11m48s elapsed / 9m30s CPU, ~84% CPU; native sample footprint 1.5 GB. The sample's main thread was executing Python/psycopg/libpq work. ONNX worker was waiting in that sample. This is a short sample, not a complete CPU profile.
- One `Repository.get_runtime()` of the latest `source_context_count`: **0.888 seconds, 8,248 SQL statements**. Breakdown: artifact_revisions 2273; runtime_records 132; project_members 397; blob_access_revocations 1025; blobs 356; revalidation_privacy_links 669; artifact_dependencies 927; artifact_blobs 258; other 2211. All queries were reads in the repository's read-only authorization transaction.
- Live at the later SQL snapshot: 12 completed physical responses/counts/context plans; 11 completed tools: read_material 7, read_source 2, list_research_results 1, search_sources 1. Therefore repeated retrieval reranking cannot explain all inter-turn delays.
- PostgreSQL activity snapshots showed artifact_revisions/artifact_dependencies reads and ClientRead, not a lock wait. pg_stat_statements is not installed; it was not enabled.

## Concrete cause

`ResearchHistory.RecordView` retains references rather than full results, but re-reads even small immutable operational metadata via `EvaluationJournal.read()` → `Repository.get_runtime()`. Every call starts a fresh authorization memo/snapshot and recursively traverses request_ref → original request → manifest/frozen research provenance. Successful-node memoization works within one transaction, not between these calls.

The new runtime repeatedly scans those views:

1. `ResearchContext.request()` searches all earlier context plans by turn index.
2. `ResearchContext.measured()` scans earlier counts by request hash, and is called twice for an ordinary request.
3. `_account()` → `model_turns.charged_rows()` scans all prior submit_intent records.

This is approximately **4T metadata graph reads per turn**, plus fixed work. At T=12 and the measured 0.888 s/read, this alone predicts ~43 s, matching the observed ~40 s local delay. With a roughly fixed provenance graph G, this component is O(T·G) per turn / O(T²·G) over the role. No evidence here establishes cubic growth. Checkpoint rotation does not clear those history scans, so it does not remove this cost.

Additional fixed overhead: `service.guard()` reloads `_materials()` several times per turn. `load_materials()` individually reads each retained SourceRepresentation, and later reads it again while checking ResearchSource bindings; those public reads each create their own authorization snapshot. `journal.request()` also authorizes a request once for its record and again for its blob. VerifiedText range/length operations reauthorize the individual source. Search additionally performs many individually authorized candidate range/length accesses.

## Narrow opportunities after the live run

1. Keep tiny metadata indexes in ResearchHistory: counts keyed by request hash, context decisions keyed by turn, submitted-turn IDs. Rehydrate them durably once, and update on appended events. Keep full results/texts referenced and loaded on demand. This removes the growing scans without retaining a corpus or transcript in RAM.
2. Batch immutable metadata reads within one bounded read-only authorization operation, reusing its existing memo. Fresh authorization must still apply at provider/network/tool/public-read boundaries; do not hold one snapshot across model/network waits.
3. Separate the cheap time check from complete material reconstruction; validate frozen descriptors in a bounded batch rather than independently reloading every document at every guard. Preserve withdrawal/ACL checks.

A projection, not a timing guarantee: at 50 turns the identified scans alone would approach 3 minutes per turn / ~75 minutes cumulative per role if the measured per-read cost stays similar. A full run can therefore become impractical or hit its elapsed budget despite sufficient model-token headroom. The first fix should target metadata indexing, not weaken authorization or change literary behavior.

Native sample: /tmp/podgen-live-sample-20261113.txt
