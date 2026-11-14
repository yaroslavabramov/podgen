# Prepared execution journal verification

Measured production code: `a6b82c5af4a7423e2edac99adc5378bf203e7e06`.
Comparison: `45d0ee0c79d22c0b720d2188156b8d170807e300`, with its unchanged
[previous measurements](../2026-11-14-step-publication/README.md).

This qualifies the saved **initial fact-review**: 43 model turns replayed through
the SDK and MockTransport, each with an exact-count call and a submit call, real
SQL persistence, actual source tools and local ONNX retrieval, then final validation
and export. The input is the prepared 51-document / 1227-chunk
legacy corpus. No live provider or source service was called. This is not a
whole-product performance qualification.

## Outcome and scope

Attempt publication now uses immutable run/input-set ownership, a replaceable flat
authorization expansion and live conditions. Ordinary receipt/state/tool-result
writes do not rebuild or classify the semantic provenance graph. Input compilation
occurs once for initial material roots, once for the actual request context and once
for authenticated retained audit evidence. Generic semantic records retain their
existing repository policy. Legacy records remain unchanged.

All three passes verified 370 export files. First/completed: 322 byte-exact files
and 48 JSON files equal after enumerated technical substitutions. Repeat: 146/224.
The version-2 delta is explicit: `journal_version`, exact tool response/argument
proof fields, and 13 adjacent same-call diagnostic/call relocations. The verifier
checks the neighboring call/result identity and every event value; it never sorts
or drops semantic content. All native requests and all 42 tool results were exact.
First/repeat each made 43 count and 43 submit calls; completed made zero provider,
search or tool calls. No passage embeddings were recomputed.

## Client calls and time

| Pass | Previous SQL → current | Previous whole seconds → current | Search seconds, previous → current | Outside search, previous → current |
|---|---:|---:|---:|---:|
| First | 10206 → 6508 | 186.51 → 161.37 | 154.59 → 146.63 | 31.92 → 14.74 |
| Repeat | 9712 → 5712 | 207.02 → 159.34 | 167.41 → 145.31 | 39.61 → 14.04 |
| Completed | 1420 → 1165 | 2.89 → 3.37 | 0 → 0 | 2.89 → 3.37 |

Current startup / fact-review / export client SQL: first **998 / 5187 / 313**;
repeat **460 / 4929 / 313**; completed **526 / 316 / 313**.
Whole-review figures also include four surrounding status/coordination statements
and six measurement queries. Including the outer advisory-lock coordination, the
counts are **6511 / 5715 / 1168**. Driver calls and executions are both retained in
the raw JSON; no executemany calls hide extra executions.

Search time covers the real retrieval/embedding/ranking scope. Outside-search time
is the measured whole-review difference. Completed opening remains slower in this
sample despite lower SQL; it is not reported as an opening-speed improvement.

An intermediate implementation regressed to 14408 client SQL: 5216 exact text
binding queries for 2608 slices, repeated immutable control reads, and per-record
parent checks. Batching within the existing read snapshot and a bounded committed
metadata cache removed those costs. Completed then exposed another saved-event
loop: 92 scalar payload reads and 88 parent checks. The common bounded event reader
reduced these to 7 scalar payload reads, 2 batch payload reads and 5 parent checks
in completed fact-review; runtime-frontier queries fell 117 → 34. Full order and
payload preservation is covered by the 129-event regression test.

## Ordinary step obligations

A representative ordinary catalog-tool turn uses **96 client SQL**, compared with
165 previously. Its actual checkpoints are in `first.json:checkpoints`, including
every SQL template and count:

| Durable checkpoint | SQL |
|---|---:|
| Before-count intent | 15 |
| Count receipt + request/plan + validated submit/state | 19 |
| Provider receipt + exact call projection + usage/state | 17 |
| Replayable tool call fact/result | 10 |
| Four blob groups outside those commit transactions | 20 |
| Physical turn preparation | 4 |
| Fresh tool action authorization | 4 |
| Authorized full provider-parent read | 7 |
| Total | 96 |

Each checkpoint retains its transaction/project/member fence, current input and
parent privacy checks, exact collision/binding validation, batched record/projection
inserts and own-SHA protection where bytes exist. Count/submit adds the physical
turn and usage projections; provider capture adds its call proof and usage state.
Tool results have no owned-byte registration/classification. Blob groups keep
pre-stage and pre/post-upload access checks and the exact deduplication winner.
These are measured obligations, not a claim that 96 is an architectural minimum.

Fresh read text binding batches fell from 5216 to 32 in fact-review. Immutable
run/set ownership reads fell 174 → 2; invocation binding reads 173 → 1. Normal
checkpoints do not reload their immutable parent bundle. Descriptor ownership is
proved and stored only after commit in a 512-entry / 2 MiB serialized-metadata
cache with eviction; permissions and withdrawal/SHA facts never cross snapshots.

The parent lookup EXPLAIN cases keep two bounded metadata queries at histories
0/128/4096. Actual runtime/event rows visited were **5 / 266 / 3**. PostgreSQL chose
sequential/hashed plans for the tiny 128-row case and exact index scans at 4096;
these observed scan costs are reported rather than inferred from query count.

## Server work, including triggers

`pg_stat_statements.track=all` measures nested SQL as well as top-level statements:

| Pass/phase | All server statements | Nested statements |
|---|---:|---:|
| First startup | 12691 | 11556 |
| First fact-review | 14810 | 7877 |
| First export | 406 | 0 |
| Repeat startup | 10600 | 10037 |
| Repeat fact-review | 14028 | 7361 |
| Repeat export | 406 | 0 |
| Completed startup | 3927 | 3351 |
| Completed fact-review | 404 | 0 |
| Completed export | 406 | 0 |

First startup includes **8008 JSON helper statements** from the generic semantic
manifest/provenance projection and 3107 FK checks. First fact-review nested work
includes **7210 FK checks**, **399 event/turn projections**, **260 protected-hash
projection inserts**, and 8 other statements. This work remains visible. The normal
execution-record insert skips the recursive JSON reference projection trigger.
Full server templates, calls, returned/modified rows, time and buffer counters are
in each pass's `server_sql`.

Server totals independently include transaction commands that client executor
counting omits, so they are not summed with client counts. The old baseline did
not measure server nested statements; no server-total reduction is claimed.

## Bytes, memory and validation

Whole-review object bytes read: first **82553999 → 66049755**; repeat
**79833890 → 63329646**; completed **29007904 → 29007904**. Raw object staging
remains **36016771 / 35136648 / 0** bytes. Source text, count/native request bytes,
provider bodies, full tool results and final meaning remain complete.

The observed process high-water was **1304018944 bytes**, versus the previous
1405026304. It is cumulative across the three passes, not independent per-pass
peaks. This single local observation is not a robust memory-improvement claim.

Validation evidence is included: 188 targeted checks; 73 overlapping saved-event,
legacy/checkpoint and reading-audit checks; the 129-event full-payload/order case;
and three actual query-plan cases. These cover fresh ACL/withdrawal/SHA checks,
privacy-only parent links, cached-header paths, commit/rollback cache behavior,
bounded text/parent batches, strict malformed receipt retention, exact counts,
legacy and structured research decisions, dedup winner races and network-free SQL
transactions. No full-product suite or live-provider qualification is claimed.

Reproduce with the repository virtualenv:

```sh
/Users/yaroslavabramov/side/podgen/backend/.venv/bin/python backend/benchmarks/offline_fact_replay/run.py \
  --state /Users/yaroslavabramov/side/podgen/tmp/longitude-corpus-2026-11-13/state.json \
  --dump /tmp/podgen-fact-primary-only-20261113/tmp/source-only-validation/source.dump \
  --trace evals/reports/2026-11-13-fact-review-source-only/live/trace.tar.gz \
  --out /tmp/podgen-execution-journal-batched-final-20261114-new \
  --pg-bin /opt/homebrew/opt/postgresql@17/bin
```

The harness owns its Unix-socket PostgreSQL instance and stops it in `finally`.
The actual completed dump and hard-linked objects are retained locally under
`/tmp/podgen-execution-journal-batched-final-20261114/` for further read-only replay.
Exports, database dumps and object contents are excluded from this Git report.
