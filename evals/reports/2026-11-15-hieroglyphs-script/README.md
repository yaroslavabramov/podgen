# Hieroglyphs script: final fact review stopped at the input budget

**`needs_attention / run_token_budget_exceeded`. No final fact verdict or overall
keep outcome exists.** Latest full text: **4146 words**, approximately **30:36**
using the retained voice estimate, not measured audio. Two permitted rewrites
were used. No third rewrite/research job, budget increase or TTS was started.

Run `1cd0ac76-3326-58bb-83f0-6ee76847cefe` retained the accepted plan, materials,
models, prompts and limits. The final reader decision is `keep`; this is not
factual or publication acceptance. Read the [independent assessment](independent-reading.md):
unreconciled claims about de Sacy, premature disclosure of Ramesses, repeated
attribution interruptions, and unresolved factual questions remain. The generated
research synthesis is not proof; its citation confusion and excessive conclusions
are documented there. These observations were not inserted into model inputs.

## Full texts and reviews

| Version | Complete text | Words | Reader | Fact |
| --- | --- | ---: | --- | --- |
| Draft | [first text](draft-attempt-0.md) | 3987 | [revise](reader-review-attempt-0.json) | [revise, anchors repaired](fact-review-attempt-2.json) |
| Rewrite 1 | [full text](revise-1-attempt-0.md) | 4122 | [revise](reader-review-1-attempt-0.json) | [keep](fact-review-1-attempt-0.json) |
| Rewrite 2 | [latest text](revise-2-attempt-0.md) | 4146 | [keep](reader-review-2-attempt-0.json) | Incomplete |

Complete persisted JSON accompanies each text. [Script hashes and estimates](script-versions.json),
[reading index](readings-index.json), [final checkpoint](final-checkpoint.json)
and [execution state](execution-final-status.json) preserve the result. The
unvalidated fact response remains `fact-review-attempt-1-invalid-output.json`;
no feedback or script was edited by hand. Full native requests, responses, tool
traces and physical turns are in
`tmp/hieroglyphs-30m-script-2026-11-15/export/archive/`.

## Exact stop and receipt accounting

Saved cumulative input ceiling: **4,194,304**. Completed physical responses in
this run used **4,175,299**, including **672,067** from earlier continuation
planning. Remaining: **19,005**. Next exact count: **76,239**; submission would
reach **4,251,538**, over by **57,234**. Fact-review-2 turn 19 was counted but
**never submitted for generation**. The per-request context limit was not the
blocker. [Exact count receipt and unsent proof](budget-stop.json).

| Scope | Count calls | Completed generations | Input usage | Output usage |
| --- | ---: | ---: | ---: | ---: |
| Script cycle, excluding earlier planning | 78 | 77 | 3,503,232 | 77,123 |
| Subset after funding confirmation | 62 | 62 | 3,075,589 | 51,444 |
| Subset after targeted research | 8 | 7 | 494,001 | 428 |

Script-cycle cached input: **3,061,876**; cache writes: **398,465**. These are input
subsets, not extra usage. Count receipts measure **3,579,471** inputs, including
the unsent request; measurements are separate from generation charges. One
credit submission was explicitly refused without generation; its saved count
was reused after funding. Zero uncertain outcomes occurred. Synthetic stage
aggregates are excluded.

**Generation/count/cache/research USD remain unknown.** Usage is not an invoice.
[Complete accounting](accounting.json), [generation receipts](generation-receipts.json),
[count receipts](count-receipts.json), [funding-recovery delta](recovery-1/accounting-new-work.json)
and [exact budget evidence](budget-stop.json) separate new work from planning.

**Exporter defect:** `export/run.json` reports **2,842,949** input, omitting
**1,332,350** from successful turns of incomplete/failed fact stages: 220,827
from the first fact attempt and 1,111,523 from the final one. Known outputs are
already included. The receipt accounting and live budget ledger include these
inputs; the discrepancy is not another charge.

## Targeted research, recovery and performance limits

One specific prepared research job used the remaining second slot under the
saved limit of two and variable-cost acceptance; general research/planning were
not repeated. It completed with **1 submission / 26 retrievals**, reporting
**1,047,417 input / 23,442 output**, 200,641 cached tokens and 37 searches.
[Authorization and exact question](targeted-research-2/authorization-and-preflight.json),
[research accounting](targeted-research-2/accounting.json),
[complete report](/Users/yaroslavabramov/side/podgen/tmp/hieroglyphs-30m-script-2026-11-15/targeted-research-export/report.md).
Afterward seven physical model answers read report X1 and searched/read S57,
S68 and S74. They produced tool actions, not a fact verdict.
[Full post-research tool trace](after-targeted-research-tool-trace.json).

[Preservation proof](preservation-proof.json) verifies original draft/reader/plan
outputs and all nine pre-refusal fact responses. Credit recovery began at turn 9.
Two reviewed technical compatibility fixes retained saved requests: roleless
conversation decoding (`c711c4b0`, 35 checks) and strict source-recovery lineage
(`1ed228ea`, 28 checks). [Offline replay](recovery-1/recheck-lineage-replay.json)
verified five completed audits and unchanged request hashes without API calls.

Publication also hit PostgreSQL `40001` after the existing three SQL-only
retries. One ordinary resume reused durable receipts and completed the operation;
it did not repeat model/research generations. The original competing writer
could not be established from unlabeled historical backend logs.
[Failure/retry](targeted-research-2/source-publication-retry.json),
[traceback](targeted-research-2/source-publication-serialization-traceback.txt).

All **40 URLs** reached outcomes: 30 available, 9 unavailable, 1 unconfirmed.
Forty fetch receipts retain 40 redirects and **29,438,695** final-body bytes.
[Measurements](targeted-research-2/source-operation-metrics.json) record a
255.55-second first-to-last receipt span, including processing and the retry
pause. Two CLI sessions span 580.65 active seconds, also covering replay, later
fact calls and export. Complete source-network/SQL counts were not instrumented.
A macOS sample observed about 1.6 GB peak physical footprint for one process,
not whole-run peak RSS. [All CLI session bounds](execution-timing.json).

The processes have exited; the heavy slot is free. No further paid calls/tests
are running. This is a complete latest text with an **unfinished final factual
assessment**, not a completed or publication-ready episode.
