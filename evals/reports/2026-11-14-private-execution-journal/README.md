# Private execution journal: implementation and remaining costs

Implementation reviewed through `0f0b6d8f` and integrated as `6e7d1508`. The requested final offline acceptance on that exact production commit completed successfully. No paid provider calls were made.

## Implemented

- Typed atomic checkpoints and one fixed-size execution head replace per-receipt generic runtime records, provenance edges and root-state rewrites.
- Complete provider requests, replies and tool results belong to private execution storage. Semantic publication produces a real result with an explicit provenance link.
- Exact request/configuration-bound count reuse, uncertain-submit handling, late receipts and cold recovery preserve accounting and prevent accidental paid resubmission.
- Bounded operation-local archive caches retain fresh access checks around external I/O. Full bytes and historical archives are preserved.
- Production prompts, models and stage order are unchanged.

## Final acceptance on `6e7d1508`

One existing offline replay covered first execution, a second independent execution, and reopening the first completed execution. It used 51 documents, 1,227 chunks, 43 recorded model turns and 42 actual tool executions per independent run. All native model requests and tool results matched the saved trace. This is execution/storage qualification, not a fresh assessment of model output quality.

| Measurement | Previous implementation | Final v3 |
|---|---:|---:|
| First complete pass, client SQL | 6,508 | 5,730 |
| Second independent pass, client SQL | 5,712 | 5,100 |
| Completed-run read/export, client SQL | 1,165 | 1,474 |
| Ordinary step without search, client SQL | 96 | 67 |
| Export, client SQL | 313 | 480 |
| First pass, seconds | 161.37 | 168.70 |
| Second independent pass, seconds | 159.34 | 163.38 |
| Completed-run read/export, seconds | 3.37 | 3.64 |

The completed pass made **zero count, generation or tool calls**, and added no rows to the audited tables. Each independent execution replayed exactly 43 counts and 43 generation responses; those are distinct runs, not accidental retries.

All three passes made **zero passage-embedding calls**. The first pass imported 49 unique legacy document indexes without recomputing them. The second and completed passes performed no document-index writes. Query embeddings still occur for the 13 searches in each independent execution.

The 67 ordinary-step statements comprise 17 for count intent/request, 19 for count receipt/submit intent, 17 for provider receipt, 10 for tool result, and 4 external-action checks. These include ownership/access metadata, object registration, checkpoint insertion and head updates. This is **not** a claim that 67 is a justified minimum. Other turns cost 75 or 93 client statements; finalization is more expensive.

Server-side nested SQL is also counted: the first pass executed **24,004 statements** including triggers/functions: 12,795 during startup, 10,652 during review, and 557 during export. Client counts alone understate database work. Final measurements and verification evidence are in `final-acceptance.json`; `measurements.json` retains the previous baseline/intermediate evidence.

### Remaining defects and limits

- Export regressed from 313 to 480 client statements; completed-run access also remains more expensive than the baseline.
- Startup/source provenance and semantic finalization still perform substantial metadata work. Private execution storage did not remove that cost.
- No speed improvement is established. Search took 153.61 seconds of the 168.70-second first pass. Peak RSS was approximately 1.28 GB; this is cumulative process high-water, not a controlled independent memory comparison.
- This replay does not simulate every interruption. Recovery coverage comes from the consolidated tests and genuine legacy fixtures below.

Technical acceptance of preserved behavior is complete. The broader efficiency complaint is **not resolved**. No SDK migration or further refactoring was started.

## Storage and correctness evidence

In the full replay, legacy invocation/event/provider/tool tables did not grow and their fingerprints remained unchanged. Per run: 174 private entries, one head and four generic records for run/material context and the complete semantic output; there were no per-receipt generic stubs. Completed reads changed no storage.

The replay verified all 370 exported files: 278 byte-identical and 92 identical after explicit version/identifier metadata substitutions. Mutation checks reject changed semantic/accounting values. A separate genuine old completed archive exported all 370 files byte-identically, made zero provider calls and changed no storage. Eight tests cover genuine old interrupted archives.

Final consolidated suite on `0f0b6d8f`: **221 passed** in 70.84 seconds. One Alembic configuration deprecation warning; no test failures.

## Follow-up, not performed in this patch

The remaining performance issues above are recorded, not silently accepted. The requested final-code replay is complete; no further test loop was launched. Return to the ordinary episode workflow before selecting another infrastructure project.
