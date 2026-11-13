# Local legacy-index reuse and bounded export

Scope: ready-corpus preparation and export on saved longitude data, starting at root `687aca39` with the agreed `5271829a` runtime batch-read API (local cherry-pick `f98825f0`). No provider calls, generation, prompt/model/profile changes or ranking changes. Persistence graph/write work and the full offline 43-response/42-action first/repeated replay remain with the integration owner. These measurements do not establish readiness of that whole path.

## Ownership and invariants

Legacy computation is selected by project, source content hash, Unicode length, language and complete pinned profile. An indexed content lookup in migration `0026_legacy_source_lookup` replaces the erroneous new-run selector. Completed candidates must have cursor equal to source length; copied chunks must match the declared count, valid coordinates and language. Destination content locks and one authorized transaction cover validation, copying and publication. A partial legacy build can resume only in its original scope; an already progressing reusable projection is never overwritten. Current run memberships, labels and access remain independent of the original run.

Preparation binds at most 64 source descriptors per metadata operation. Completed immutable membership revisions reauthorize without repeating detection, lookup and rebinding. Missing work reads one bounded tokenization window and passes those exact texts to inference and publication, avoiding per-chunk source rereads. Its authorization snapshot ends before inference. Model loading is deferred until inference is needed. The narrow shared `persistence.source_reading.read_sources` helper groups source reads and also replaces the existing duplicate repository grouping in SourceCorpus. It is not a global cache or a guarantee that arbitrary callers never perform local computation inside a read scope.

Export pages metadata and full bodies in groups of at most 64 using `Repository.get_runtime_many`. Full payloads are caller-owned and never retained in authorization facts. Event arrays are streamed atomically in the original JSON format. A temporary per-turn spool partitions events once; turn export does not scan the full event history for every turn or keep all event bodies in memory. Pagination fences each selected history at its initial sequence/turn high-water mark.

Each metadata batch is consumed before blob I/O. Remote I/O releases the SQL snapshot, individual reads reauthorize on return, and the entire batch is checked afterward, including records without their own blob. Source exports also recheck their frozen material root after an earlier remote read may have reset the operation. No authorization transaction is retained through remote storage waits.

## Representative saved-data checks

The isolated PostgreSQL clone restored the original 0022 dump, then migrated to the current schema. Its original 51 complete legacy source labels cover 1,227 chunks. Ready-corpus preparation ran with inference model loading forbidden; all labels/chunks were reused with **zero model loads and zero embedding calls**. The repeated refresh also did no embedding or source-object read.

The second database restored the saved completed fact review. Both exports were compared against the original trace manifest: **370 files in each pass, zero differing bytes or paths**, including complete requests/responses, event order, optional files, turn states, attempts, outputs and unknown-usage fields.

| Operation | Seconds | SQL calls |
| --- | ---: | ---: |
| Original full-run startup receipt | 100.514 | 100,150 |
| New cold ready-corpus preparation | 2.243 | 4,381 |
| New unchanged-corpus refresh | 0.080 | 57 |
| Original full-run export receipt | 56.306 | 47,155 |
| New first export | 2.831 | 2,585 |
| New repeated export | 2.544 | 2,585 |

The original values come from `../2026-11-13-fact-review-source-only/live/run-measurements.json`. The new phase receipts, object counters and process peak RSS are in `index-probe.json` and `export-probe.json`. The first export additionally cold-read the Unicode source projections; the original full-run export and the repeated export had those projections warm. Object counters remain logical repository reads/staging/publication, not physical disk I/O. These are targeted operations, not a substitute for the integrated whole-path replay. Residual graph/authorization work is visible in the SQL counts and is not declared solved here.

## Validation

64 tests passed in 15.06 seconds; complete output is `tests.txt`. The single addressed suite included new legacy cross-scope reuse and corrupt projection rejection, bounded preparation and exact Unicode passages, cold/partial/repeated/concurrent builds, project and membership withdrawal, atomic ordered runtime batch reads, exact streamed JSON/native bytes, empty histories, out-of-order turn events, the remote A-read/B-withdrawal regression, existing remote read cleanup, existing research workflow/input export and unchanged retrieval/replay contracts. Ruff and whitespace checks passed.

All owned PostgreSQL databases at port 55471 were stopped after validation. No original database was changed or stopped. The isolated dumps, expanded exports and small measurement drivers remain under `/tmp/podgen-local-index-export-20261113/tmp/local-index-export-validation/` for inspection. The heavy-test slot was explicitly released for the integration owner's whole-path replay.
