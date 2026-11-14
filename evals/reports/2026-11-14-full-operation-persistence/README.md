# Complete-operation persistence qualification

The audit identified repeated dependency resolution during acquisition, retrieval
construction before execution binding, repeated planning-history recovery, and
repeated full editorial validation during ordinary reads. This patch changes those
owners and transfers known audio descriptors between stages. Prompts, models,
revision limits, tool behavior and provider payload formats are unchanged.

## Comparable complete operation

Baseline `b2e07b16` and candidate use the same pre-episode database dump, 54 saved
source fetches/extractions, 84 model answers, 84 token counts, 28 research exchanges
and two speech responses. Real retrieval inference, tools, persistence, FFmpeg,
assembly and export execute; HTTP dispatch is forbidden. Only one heavy job runs.

| Client SQL | Baseline | Candidate |
|---|---:|---:|
| Preparation and setup | 129 | 129 |
| Hosted research persistence | 2,666 | 2,666 |
| Sources and material preparation | 21,253 | 13,369 |
| Writing, including fresh index construction | 27,163 | 17,469 |
| Audio preparation, processing and assembly | 5,208 | 870 |
| Complete technical export | 4,888 | 3,010 |
| Resume completed episode | 5 | 5 |
| **Total** | **61,312** | **37,518** |

Additional cold outcome read: **29 SQL**. The prior audit measured **1,219** on
the baseline completed episode. This additional probe is excluded from total.

Measured phase wall time is **572.90 → 537.39 seconds**; accumulated SQL execution
time **37.47 → 24.62 seconds**. Peak RSS is **1,279,279,104 → 1,263,337,472 bytes**.
Object reads total **872,085,341 → 715,198,973 bytes**; read calls **979 → 617**,
stream calls **793 → 739**. Published-byte accounting remains **414,302,686** on
both runs. This is object API traffic, not a physical disk-write measurement.

The historical 66,615 total included recovery attempts and is not the denominator
for this uninterrupted comparison. SQL counts exclude database trigger/function
internals. Timings are one local run per version, exclude provider latency, and
are not a statistical latency claim. Local retrieval and audio processing still
dominate elapsed time; SQL reduction does not imply a comparable overall speedup.

## Verification of the changed decisions

- First corpus construction: **10,124 → 3,341 SQL**. Chunk storage now executes
  **208 multirow inserts**, replacing **4,021 scalar inserts**. Inputs and lazy
  source access are bound before the constructor can build the index.
- Planning selection restoration: **3,735 → 297 SQL**. The new one-time selection
  capture costs **437 SQL** separately. Empty selections are persisted too.
- Full outcome-history validation across writing/audio/export: **5,771 → 961 SQL**.
  It now runs once at publication. Ordinary reads check the exact immutable proof
  and current access; full historical replay remains available explicitly.
- Native model/count requests matched their archived counterparts. Run metadata
  and opaque catalog/result identities were normalized; content, tools, model
  parameters and instructions were compared. All retained responses were consumed
  exactly once. The final script hash and master WAV bytes match the original.
- Completed resume made no extra provider calls. **Paid requests: zero.** Recorded
  model usage/costs in the replay are historical receipts, not a new bill.

These are subsets of phase totals and must not be added to them.

## Review findings resolved before acceptance

The first patch was not accepted on test counts alone. Review found and corrected:

1. Package preparation tried to decode binary representations as UTF-8. It now
   loads text only for actual text selectors, including external representations.
2. Lost audio write acknowledgements left a reusable in-memory ledger stale.
   The affected cell is invalidated and reloaded before further work.
3. An interrupted local reprocess could resume under the original processing key.
   Recovery now preserves its exact revision and never resubmits paid synthesis.
4. The new composite document transaction retained role checks under READ COMMITTED.
   It now uses the existing `archive_write` boundary: repeatable read, project fence,
   membership lock and SQL-only serialization retry. Returned descriptors belong
   only to a successful commit. Test instrumentation also covers this real boundary.
5. Outcome proof publication needed a fresh dependency check in its commit
   transaction, after the audit. Withdrawal at that boundary now blocks publication.

No second audio state machine/head was introduced. The existing archive is the
recovery authority. Common artifact field/subtype/lineage checks remain intact;
byte verification and remote storage stay outside publication transactions.

Checks include 35 source/publication tests after the boundary correction, five
guard controls, 64 outcome/audio/episode tests, and 20 final recovery/pagination
tests. These queues overlap; they are not a summed test count. The root index/annex
queue passed 23 cases and exposed an old pagination fixture failure, also reproduced
on baseline. The fixture repeated identical text that correctly grouped into one
passage; distinct passages restore the pagination test without changing retrieval.
Its corrected case passed in the final queue. Ruff and diff checks pass.

## Remaining costs and limits

37,518 SQL is the measured result, not a declared optimum. The generic document
writer still invalidates dependency/access facts after publications: the isolated
document probe records 22 artifact-frontier queries, including repeated checkpoint
walks. Simply retaining all AccessFacts is unsafe for classification, late aliases,
missing references and withdrawal semantics. No process-wide authorization cache
or weakened access check was added to hide that work. Hosted research persistence
and the remaining technical-export traversals were not redesigned in this patch.

No new model output quality or cloud-storage latency is claimed. Retry, withdrawal,
same-process lost-ACK and cold recovery are tested separately from the uninterrupted
replay. The first candidate replay was deliberately interrupted after the transaction
review finding; it is not included in qualification results.

## Reproduction and artifacts

Runner: `backend/benchmarks/offline_episode_replay/run.py` (see its README).
Use the same arguments for both checkouts:

```sh
backend/.venv/bin/python backend/benchmarks/offline_episode_replay/run.py \
  --state tmp/hieroglyphs-production-2026-11-14/reference-state.json \
  --dump tmp/longitude-corpus-2026-11-13/before-resume-2026-11-14.dump \
  --fixtures tmp/hieroglyphs-fullpass-fixtures-2026-11-14 \
  --export tmp/hieroglyphs-production-2026-11-14/export \
  --code-root /path/to/checkout \
  --out /path/to/new-output-directory \
  --pg-bin /opt/homebrew/opt/postgresql@17/bin
```

`measurements.json` contains phase metrics, request counts, diagnostic subsets and
equality oracles. `production-files.json` binds the exact changed production files;
their hashes were unchanged throughout replay. Full traces, completed database
dumps, objects and exports remain under the two local `tmp/hieroglyphs-fullpass-*`
directories named in the measurements. Private fixture payloads are retained in
the workspace with a checksum manifest and are not committed to Git.
