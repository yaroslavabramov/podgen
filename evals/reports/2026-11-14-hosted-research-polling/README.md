# Hosted research polling

`hosted-research-head/1` stores one bounded operational projection per project/run,
owned by its immutable manifest. The archive remains authoritative and unchanged.
Intent/head, first successful receipt/job/head and legacy job repair/head commit
atomically. A run lock plus revision comparison rejects competing writers.

Current polling reads the head, manifest/approval and fixed result records, never
previous exchanges or raw HTTP bodies. The first valid snapshot binds the job.
Failed GETs retain that snapshot; a changed provider job is a durable integrity
error. Unknown submission outcomes never cause another submit. Legacy replay is
once-only; export deliberately remains linear in archive size.

## Complete fake-provider operations

Compared the complete `advance` and `status` paths against commit `05b7d855`, with
the same immutable request and 4670-byte fake responses. No real provider calls.
The in-memory journal records every read/write and bytes returned. This measures
the service operation, not SQL or real object-storage performance.

| Prior polls | Old next-poll record reads | Current | Old blob reads | Current |
|---:|---:|---:|---:|---:|
| 1 | 34 | 6 + one head | 9 | 1 |
| 20 | 205 | 6 + one head | 66 | 1 |
| 100 | 925 | 6 + one head | 306 | 1 |

The current blob read is always the 10473-byte original request. Old next-poll
blob bytes rise from 47833 to 1434823. Current status reads one head and two
records, with zero blob reads; old status reads 16/92/412 records and 3/22/102
blobs. Each current poll writes two exchange records and one 4670-byte response;
the job is written once, together with its first successful receipt. The head has
fixed fields; numeric counters and selector suffixes grow logarithmically.

Full measurements, timings and Python peak allocations: `measurement.json`.
Python allocation peaks are not process RSS. Reproduce the same check:

```sh
PYTHONPATH=backend backend/.venv/bin/python \
  backend/benchmarks/hosted_research_polling.py \
  --baseline-ref 05b7d855 \
  --out /tmp/podgen-hosted-polling.json
```

## Verification

32 lightweight tests pass across `test_hosted_research.py`,
`test_hosted_research_cli.py` and `test_hosted_research_head.py`: recovery after
intent/receipt, unknown outcomes, failed GET, changed job, terminal and completed
status, one-time legacy adoption, one-time job persistence/repair, complete report
and export preservation, and bounded work at 1/20/100 polls.

`test_hosted_research_head_persistence.py` is prepared for the coordinator's sole
PostgreSQL test queue. It checks complete SQL/row/object-I/O work at 1/20/100
polls, atomic rollback, reopen after receipt, revision conflicts, simultaneous
advance, once-only adoption, current receipt revocation and viewer status access.
It has not been run in this chat. SQL/real storage counts, actual peak RSS and
migration execution remain unverified here; fake-provider timings do not establish
end-to-end production performance. `runtime.dispatch.before_external` is retained.
