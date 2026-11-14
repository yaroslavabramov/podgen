# Private execution journal: implementation and remaining costs

Implementation reviewed through `0f0b6d8f`. Final consolidated test result is recorded below. No paid provider calls were made. Work stops here at the user's request; a new full replay on the final commit was not run.

## Implemented

- Typed atomic checkpoints and one fixed-size execution head replace per-receipt generic runtime records, provenance edges and root-state rewrites.
- Complete provider requests, replies and tool results belong to private execution storage. Semantic publication produces a real result with an explicit provenance link.
- Exact request/configuration-bound count reuse, uncertain-submit handling, late receipts and cold recovery preserve accounting and prevent accidental paid resubmission.
- Bounded operation-local archive caches retain fresh access checks around external I/O. Full bytes and historical archives are preserved.
- Production prompts, models and stage order are unchanged.

## Measured results, not an efficiency acceptance

The last full replay used `81a4af8c`, before final archive-cache and recovery fixes. It used 51 documents, 1,227 chunks, 43 recorded model turns and 42 actual tool executions. Provider responses were replayed offline.

| Measurement | Previous implementation | Measured v3 |
|---|---:|---:|
| First complete pass, client SQL | 6,508 | 5,816 |
| Repeat pass, client SQL | 5,712 | 5,186 |
| Completed-run read/export, client SQL | 1,165 | 1,562 |
| Ordinary step, client SQL | 96 | 67 |
| First pass, seconds | 161.37 | 174.10 |
| Repeat pass, seconds | 159.34 | 182.08 |

The 67 ordinary-step statements comprise 17 for count intent/request, 19 for count receipt/submit intent, 17 for provider receipt, 10 for tool result, and 4 external-action checks. These include ownership/access metadata, object registration, checkpoint insertion and head updates. This is **not** a claim that 67 is a justified minimum.

Server-side nested SQL is also counted: the measured v3 first pass executed 24,090 statements including triggers/functions, with 12,795 during startup, 10,654 during review, and 641 during export. Client counts alone understate database work. Detailed counters are in `measurements.json`.

A later completed-only check with the final archive cache reduced export from 564 to 480 client statements. The old export was 313; that regression remains. Its cold-process whole-operation count is not directly comparable with the earlier warm completed pass.

No speed or memory improvement is established. Search took 157 seconds of the measured 174-second first pass; process peak RSS was approximately 1.34 GB. Startup/source provenance and semantic finalization remain expensive. The broader efficiency complaint is **not resolved** by this patch.

## Storage and correctness evidence

In the full replay, legacy invocation/event/provider/tool tables did not grow and their fingerprints remained unchanged. Per run: 174 private entries, one head and four generic records for run/material context and the complete semantic output; there were no per-receipt generic stubs. Completed reads changed no storage.

The replay verified all 370 exported files: 278 byte-identical and 92 identical after explicit version/identifier metadata substitutions. Mutation checks reject changed semantic/accounting values. A separate genuine old completed archive exported all 370 files byte-identically, made zero provider calls and changed no storage. Eight tests cover genuine old interrupted archives.

Final consolidated suite on `0f0b6d8f`: **221 passed** in 70.84 seconds. One Alembic configuration deprecation warning; no test failures.

## Follow-up, not performed in this patch

Review startup/source graph publication and semantic finalization as whole operations, and resolve the export regression. Final-code full performance replay remains outstanding. Work is stopped; no further test loop was launched.
