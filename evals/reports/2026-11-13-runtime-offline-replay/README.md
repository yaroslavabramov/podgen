# Full offline fact-review replay — verified changes and remaining costs

This records verified engineering changes, **not acceptance of the complete runtime cost**.
The initial intermediate receipts are retained unchanged; `post-metadata-*.json` records
the reviewed `305ecac5` implementation in a new isolated replay.
It replays the saved report-off fact review: 43 provider responses and 42 actual tool
operations, with PostgreSQL persistence and the complete export. No external provider
or source API calls are made. The real OpenAI SDK uses an HTTP MockTransport oracle.

## Reproduction

Use the project Python environment, prepared local retrieval models, PostgreSQL 17
binaries, a private state file (`project`, `actor`, `run_id`, `objects`) and the saved
pre-experiment `source.dump`. The input must retain the original ready legacy corpus:
51 documents / 1,227 chunks. The runner asserts this before execution.

From the repository root, with the Python environment activated:

```sh
python backend/benchmarks/offline_fact_replay/run.py \
  --state /path/to/private-state.json --dump /path/to/source.dump \
  --trace evals/reports/2026-11-13-fact-review-source-only/live/trace.tar.gz \
  --out /path/to/private-output --pg-bin /path/to/postgresql/bin
```

The runner clones immutable objects and restores the dump into its own temporary
PostgreSQL server (Unix socket only), migrates it and stops/deletes only that server.
Original database servers and original object files are not modified. Initial restore,
migrations, imports, input validation and final oracle verification are setup/checking
costs outside the product-operation phase timers.

`first` is a new full run over the ready legacy corpus. `repeat` is another new full
run, reusing document projections. `completed` resumes the first completed run and
must perform zero count/submit/tool operations. These are separate measurements.
The historical first baseline matches the previously recorded 212,684 SQL exactly.

## Absolute measurements

| Operation | Baseline first | Post-metadata first | New repeat | Completed resume |
| --- | ---: | ---: | ---: | ---: |
| Startup SQL | 100,150 | 1,178 | 641 | 613 |
| Stage SQL | 65,375 | 14,468 | 14,726 | 1,119 |
| Export SQL | 47,155 | 478 | 478 | 478 |
| Whole phase SQL | 212,684 | 16,128 | 15,849 | 2,214 |
| Whole phase seconds | 394.58 | 187.22 | 186.56 | 3.99 |
| Count / submit mock requests | 43 / 43 | 43 / 43 | 43 / 43 | 0 / 0 |
| Real tool results verified | 42 | 42 | 42 | 0 |

SQL includes literal transaction-SET statements and logical INSERT
attempts, including ON CONFLICT. Each whole phase excludes three advisory-lock/ACL
coordination statements; `sql_including_coordination` includes those three. The
baseline templates likewise total 212,687 including coordination.

A normal first physical turn costs 1,081 SQL before and 279 after this patch; ordinary
later turns generally cost 297 after it. The new repeat starts at 285 SQL/turn.
Last-turn attribution also includes final publication and audit work, and is reported
separately in each JSON's `turn_sql`. The post-metadata first whole operation still
spends **38.39 seconds outside its
148.82 seconds of retrieval search** (35.20 seconds within the fact stage itself).
The fact stage still issues 14,468 SQL; 4,078 artifact-frontier reads now contain
metadata, with no artifact body. This remaining cost is not declared optimal or
resolved by the overall percentage improvement. Both new full passes perform zero
passage-embedding calls. Repeated new-run query ranking remains real CPU inference,
not a replayed tool answer.

Byte counters cover raw object reads, staging and publication API calls. First raw
reads remain 90,798,027 bytes; no semantic bytes were removed. The instrumented
Unicode projection counters describe logical UTF-32 returned/created bytes, not
unique physical disk I/O. Staging/publication counts are logical operations, not
physical writes. SQL wire bytes are not measured.

## Exactness and limits

Every actual native request JSON is checked against its saved counterpart, including
all model-facing inputs and schema/tool fields. Every actual tool result is compared
with the saved result. The only routing changes for the second run are run/invocation
identities and the matching response metadata; outputs, tool calls and usage remain
unchanged. The 43 saved count response bodies are served without alteration.

Every export contains 370 files. The first post-metadata pass and completed replay
verify 322 files byte-for-byte and 48 as JSON after explicit technical substitutions.
The new repeat verifies 146 byte-for-byte and 224 after the same allowed routing
substitutions. Initial baseline/intermediate export counts are retained separately.
Substitutions pair event IDs by ordered event identity, derived run/request/output
IDs, the newly created prompt reference and derived hashes. Changed blob references
are allowed only after their actual exported content matches. Only the manifest's
creation time and technical event-envelope HTTP request IDs are normalized. Files
without an actual allowed substitution must retain bytes, not just equivalent JSON.
All individual mappings and counts are saved in the JSON evidence.

Peak RSS is cumulative within one worker, not an independent peak per pass. The
post-metadata worker reports **1,376,272,384 bytes**; the initial intermediate worker
reported 1,447,526,400. The original baseline harness reports 1,202,143,232 but did not
retain ExportOracle, so it is **not directly comparable** with the newer oracle harness.
No overall memory improvement or complete attribution of the difference is claimed.
An isolated helper-only probe retained about 35.3 MB in ExportOracle and 36.3 MB in
Traffic. RSS grew by 38.3 MB on loading ExportOracle, 50.9 MB on loading Traffic and
62.7 MB during export verification. This establishes substantial measurement overhead,
but cannot explain/subtract the complete cross-process high-water difference. Runtime
versus model allocator versus oracle peak remains incompletely attributed.

## Artifact authorization diagnosis and verified fix

The initial intermediate first stage loaded artifact frontiers 4,078 times, roles
2,145 times and blob facts 1,061 times. `artifact-diagnosis.json` measures three
read-only validations of one actual saved event, then one generation guard, against
the completed dump. Each event validation reads the same 259 artifact IDs in five
frontiers (64/64/10/64/57), fetching 992,232 payload bytes. The guard fetches 805,081.
These are many modest payloads, not one multi-megabyte document.

The reviewed patch separates metadata authorization from explicit full reads.
`_resolve`/`_row` retain their full-row contracts; ordinary get still checks integrity.
Runtime/input/recursive/blob authorization uses one metadata-only ScopeAccess path.
Catalog VoiceProfile stock checking uses a narrow derived field. Snapshot-local
closure completion uses one flat marker set, with incremental denial propagation;
lookup work depends on requested IDs, not the count of already loaded artifacts.
No semantic body or cross-transaction permission cache was added.

| Three saved-event validations under cProfile | Before | After |
| --- | ---: | ---: |
| Wall seconds | 0.4441 | 0.1810 |
| Process CPU seconds | 0.4113 | 0.1552 |
| UUID hash calls | 816,186 | 82,206 |
| Event/guard artifact payload bytes fetched | 992,232 / 805,081 | 0 / 0 |

`artifact-metadata-diagnosis.json` preserves the new CPU profile and the same
frontier IDs. It distinguishes stored payload sizes, observed transferred payload
bytes and 3,781,777 avoided bytes across these four checks. Observed bytes derive
only from cursor columns actually returned, not the size of an unfetched body.
The diagnostic script emits those fields automatically on subsequent runs.

Metadata reference enumeration conservatively includes declared local input,
producer, runtime/blob references and effective edges. Foreign catalog configuration
is fully authorized (including its blobs, grant and stock restrictions) but its
bytes are not tenant-local indexed text and are excluded from the tenant purge
registry. Tests verify grant/blob revocation denies subsequent access, while a local
source blob withdrawal physically purges documents, chunks and results. Raw source,
request, response and export bodies remain unchanged as verified by the oracle.

For the read-only diagnosis, add `--operation artifact-diagnosis` and use the completed
dump instead of the pre-experiment dump. `memory_diagnosis.py --trace ... --export ...
--out ...` measures oracle overhead without a database or model inference.

Validation after the metadata patch: 76 affected core/catalog/index/export tests and
48 migration/recovery/immutable-ownership tests passed. Real migration round-trips
were enabled. Tests assert linear graph visits/bounded hash lookups, no semantic
payload in authorization facts, full-get hash rejection, catalog scope and fresh
revocation/rollback. Earlier batches are recorded above/in the commit history;
counts from overlapping batches are not combined into a unique-test total.
Ruff and diff checks passed.

Immutable runtime topology is capped at 4,096 nodes / 32,768 links, contains no body
or permission result, admits only committed read-only metadata and clears in finally.
All probe/replay PostgreSQL servers were task-owned, stopped and deleted by their
runners. The pre-existing original source server was neither started nor stopped.
No batch-write implementation or additional runs after this verified pass were performed.
