# Full offline fact-review replay — intermediate

This is an intermediate engineering result, **not acceptance of the complete runtime cost**.
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

| Operation | Baseline first | Intermediate first | New repeat | Completed resume |
| --- | ---: | ---: | ---: | ---: |
| Startup SQL | 100,150 | 1,226 | 640 | 612 |
| Stage SQL | 65,375 | 14,956 | 15,214 | 1,211 |
| Export SQL | 47,155 | 478 | 478 | 478 |
| Whole phase SQL | 212,684 | 16,664 | 16,336 | 2,305 |
| Whole phase seconds | 394.58 | 217.11 | 212.97 | 6.40 |
| Count / submit mock requests | 43 / 43 | 43 / 43 | 43 / 43 | 0 / 0 |
| Real tool results verified | 42 | 42 | 42 | 0 |

SQL includes literal transaction-SET statements and logical INSERT
attempts, including ON CONFLICT. Each whole phase excludes three advisory-lock/ACL
coordination statements; `sql_including_coordination` includes those three. The
baseline templates likewise total 212,687 including coordination.

A normal first physical turn costs 1,081 SQL before and 279 after this patch; ordinary
later turns cost 298 after it. Last-turn attribution also includes final publication
and audit work, and is reported separately in each JSON's `turn_sql`. The first
intermediate stage still spends 60.45 seconds outside its 151.97 seconds of retrieval
search. Both new full passes perform zero passage-embedding calls. Repeated new-run
query ranking remains real CPU inference, not a replayed tool answer.

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

Every export contains 370 files. First baseline and intermediate first verify 322
files byte-for-byte; 48 verify as JSON after explicit technical substitutions.
Substitutions pair event IDs by ordered event identity, derived run/request/output
IDs, the newly created prompt reference and derived hashes. Changed blob references
are allowed only after their actual exported content matches. Only the manifest's
creation time and technical event-envelope HTTP request IDs are normalized. Files
without an actual allowed substitution must retain bytes, not just equivalent JSON.
All individual mappings and counts are saved in the JSON evidence.

Peak RSS is cumulative within one worker, not an independent peak per pass. It grew
from baseline 1,202,143,232 to 1,447,526,400 bytes; **no memory improvement is claimed**.
An isolated helper-only probe retained about 35.3 MB in ExportOracle and 36.3 MB in
Traffic. RSS grew by 38.3 MB on loading ExportOracle, 50.9 MB on loading Traffic and
62.7 MB during export verification. This establishes substantial measurement overhead,
but cannot explain/subtract the complete cross-process high-water difference. Runtime
versus model allocator versus oracle peak remains incompletely attributed.

## Remaining repeated work and next review

The first stage still loads artifact frontiers 4,078 times, actor roles 2,145 times
and blob facts 1,061 times. `artifact-diagnosis.json` measures three read-only input
validations of one actual saved event, then a generation guard, against the completed
dump. Each event validation reads the same 259 artifact IDs in five frontiers
(64/64/10/64/57), transferring 992,232 payload bytes unnecessarily for authorization.
The guard reads another 805,081 payload bytes. This is many modest payloads, not one
multi-megabyte document.

Three validations took 0.444 s wall / 0.411 s process CPU under cProfile.
`ArtifactFacts.load` was entered 2,847 times; UUID hashing 816,186 times. Its set
subtraction repeatedly walks already loaded IDs. Metadata/full-body separation and
snapshot-local completed-closure reuse are the proposed next patch. No semantic
payload cache or permission cache across transactions is proposed.

For the read-only diagnosis, add `--operation artifact-diagnosis` and use the completed
dump instead of the pre-experiment dump. `memory_diagnosis.py --trace ... --export ...
--out ...` measures oracle overhead without a database or model inference.

Validation so far: 107 core tests; 40 migration/index/export tests (including real
migration round-trips); 13 provenance/streaming checks. Some files overlap across
batches. Ruff and diff checks passed. The source-admission fixture correction uses
project-visible provenance; it does not weaken evaluator-only access protection.
Runtime immutable topology is capped at 4,096 nodes / 32,768 links, stores no payload
or permission result, admits only committed read-only metadata and clears in finally.
