# Source-tool export input accounting regression

The ordinary export now reports **4,175,299 input / 87,801 known output** for
saved run `1cd0ac76-3326-58bb-83f0-6ee76847cefe`. Input increases by exactly
**1,332,350**; output, status, unknown-usage markers and all other fields remain
unchanged. The totals match 104 unique physical generation receipts, including
the already completed continuation planning. The historical production report
and its original export were not rewritten.

`export.py` now sums input from the same existing `charged_rows` list already
used for known output and unknown-usage markers. That function remains the owner
of submitted/unsent/refused/ambiguous classification. No second accounting path,
persistence changes, provider requests, budgets, contexts or prompts were added.
Completed aggregates are not added on top of physical turns. The non-tool legacy
branch is unchanged.

These inputs are **usage/conservative charged physical turns, not USD**.
Ambiguous submissions retain known request input and unknown-output markers;
known-unsent turns and proven no-generation refusals remain excluded. Usage is
not a verified invoice.

## Verification

[46 tests passed](test-result.json), [JUnit result](tests.xml).
Regressions execute real `export_run`
and its archive using a protocol-backed test journal. Cases cover completed
aggregate present/absent, budget refusal before submit, explicit credit refusal,
credit retry preserving the parent's paid turn, and an unknown outcome retaining
its marker. Existing source recovery/archive and non-tool credit tests pass.
Ruff and diff checks pass. The sole warning is an existing Alembic deprecation.

One sequential ordinary offline export was run before the edit and one after,
into separate fresh directories. HTTP/provider transports were guarded; no
provider was constructed. [Comparison and file inventories](comparison.json).
Only the numeric `run.json:input_tokens` replacement differs. The other **867
files / 90,364,720 bytes** are identical; all 868 filenames are unchanged.

| Measurement | Before | After |
| --- | ---: | ---: |
| Input | 2,842,949 | 4,175,299 |
| Known output | 87,801 | 87,801 |
| SQL calls, including SET | 2297 | 2297 |
| SQL mutating statements | 0 | 0 |
| Object reads | 606 | 606 |
| Object bytes read | 77,274,375 | 77,274,375 |
| Object stage/write calls | 0 | 0 |
| HTTP/provider attempts | 0 | 0 |
| Final export bytes | 90,383,747 | 90,383,747 |
| Wall seconds | 5.597 | 4.843 |
| CPU seconds | 2.244 | 2.030 |
| Peak process RSS, bytes | 115,539,968 | 115,572,736 |

[Before metrics](before-metrics.json), [after metrics](after-metrics.json).
Times are single observations in separate processes with warm OS/DB caches,
not evidence of a speedup or a document/history scaling benchmark. SQL protocol
bytes and temporary/staging filesystem writes were not measured. Export file
hashing occurred after the timed operation. New archives and streaming file
inventories remain under `tmp/export-input-regression-2026-11-15/`.

No generation, paid count, research or TTS was resumed. Root independently
reviewed the implementation and tests and verified the archive comparison and
unchanged I/O counters before committing the fix.
