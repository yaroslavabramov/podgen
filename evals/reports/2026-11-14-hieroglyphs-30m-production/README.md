# Hieroglyphs: stopped 30-minute production run

Run `9af453a9-9dc2-426c-9f79-01f4d93ec08b` stopped at the configured planning
revision limit: `needs_attention / planning_revision_limit_exhausted`.
Research completed, the first plan was reviewed, its single allowed revision was
completed, and the second review again returned `revise`. No author draft,
script reviews, script revisions, TTS, audio assembly or listening copy exists.
The requested 1800/1800/1980-second range is untested in produced audio.

The code, English prompt bodies, selected models and budgets stayed unchanged.
No reviewer decision was bypassed; no additional planning revision or separate
author invocation was started. The prior unpaid preflight remains paused. The
current process has exited, and its heavy execution slot is free. This run is
finished for review; it has not been resumed or replaced by another paid run.

## Editorial stop

The first reviewer required a concrete explanation of the transition from royal
names to ordinary words. The revised plan added the word about birth and
preserved the chronological and evidentiary limits. The second reviewer accepted
that change but found a separate dependency gap: A5 describes the two final
`s` signs in Ramesses as already familiar, although the preceding demonstrations
introduce `p`, `t`, and `l`, without first teaching `s`. The reviewer requested
introducing that sound with Ptolemy or explaining its earlier recognition at A5.
No additional research was requested. This is an editorial request, not a provider
failure, context overflow or token-budget stop. Its importance and the complete
plan remain available for root's independent review; acceptance was not assumed.

## Complete artifacts

- [Initial full plan](checkpoints/plan-attempt-0.json).
- [First plan review](checkpoints/plan-review-attempt-0.json).
- [Revised full plan](checkpoints/plan-revise-attempt-0.json).
- [Second plan review](checkpoints/plan-review-revised-attempt-0.json).
- [Research report](/Users/yaroslavabramov/side/podgen/tmp/hieroglyphs-30m-preflight-2026-11-14/voice-bound-export/research/9165b204-80f8-5f0f-bcbf-ef8ee76450b0/report.md).
- [Readable generated plan](/Users/yaroslavabramov/side/podgen/tmp/hieroglyphs-30m-preflight-2026-11-14/voice-bound-export/writing/00131ed0-d0ae-5a00-9d6d-d719197e0ced/generated-plan.md).
- [Writing result and all stage summaries](/Users/yaroslavabramov/side/podgen/tmp/hieroglyphs-30m-preflight-2026-11-14/voice-bound-export/writing/00131ed0-d0ae-5a00-9d6d-d719197e0ced/run.json).
- [Receipt accounting](accounting.json), [count receipts](count-receipts.json),
  [generation receipts and full native usage](generation-receipts.json).
- [Coordinator events and exact timestamps](production-events.json).

The full native requests, responses, source-tool traces and per-turn states are
in `tmp/hieroglyphs-30m-preflight-2026-11-14/voice-bound-export/writing/00131ed0-d0ae-5a00-9d6d-d719197e0ced/archive/`.
The hosted research export retains its original request, approval, all 30 HTTP
exchange intents/receipts/raw responses, result and reported sources. Research
reports 273 source annotations covering 50 unique URLs. Source text and admission
artifacts are retained in the writing export's `research/` directory.

## Calls, usage and cost

| Stage | Native count calls | Generation calls | Generation input | Output | Cached input |
| --- | ---: | ---: | ---: | ---: | ---: |
| plan | 25 | 25 | 547,469 | 8,334 | 510,905 |
| plan-review | 22 | 22 | 610,177 | 3,000 | 571,037 |
| plan-revise | 17 | 17 | 382,232 | 7,983 | 349,955 |
| plan-review-revised | 23 | 23 | 624,233 | 2,908 | 591,753 |
| Total | 87 | 87 | 2,164,111 | 22,225 | 2,023,650 |

All 87 count receipts have distinct request/configuration identities. Their
2,164,111 measured input tokens equal the generation inputs. Each saved receipt
was reused once at submission: those 87 reuse events are not another 87 count
calls. Count measurements are separate from generation usage, not a second copy
of generation input charges. No completed historical generation was repeated.

Generation input includes 2,023,650 cache-hit tokens and 140,461 uncached tokens.
The provider also reports 140,200 cache-write tokens, all with 30-minute TTL;
these are a subset of input, not extra tokens to add to the total. Input/output
usage matches the ordinary exporter exactly. Synthetic `source_tools_final_response`
stage aggregates are excluded; only unique physical `provider_response` receipts
are summed. There were zero rejected requests,
unknown-usage invocations, uncertain outcomes or technical retries. Actual model
calls: `gpt-6.1-sol` 42, `gpt-6.1-terra` 45. The configured writer was never called.

Research: one submission and 29 retrieval exchanges, with provider-reported
729,783 input, 24,995 output, 2,048 cached tokens, 30,905 thought tokens,
92,504 tool-use tokens, 878,187 total tokens and 29 Google Search operations.
These fields are retained as reported; overlapping token categories are not
added together as separate charges. Research USD remains unknown.

**Dollar charges for generation, count, cache and research are unknown.** Native
receipts contain usage but no billed USD, and the frozen model profiles contain
no price schedule. No invoice was available, so neither a dollar total nor an
inferred zero was substituted. TTS has zero submissions and zero submitted work.
This is exact call/usage accounting, not a verified provider invoice.

## Time and verification limits

The coordinator ran from **2026-11-14 23:55:27 to 2026-11-15 00:16:50 Istanbul**:
**1282.89 seconds (21:22.89)**. Research took 470.99 seconds (7:50.99), material
preparation 118.43 seconds (1:58.43), and the writing/planning phase 693.36 seconds
(11:33.36). Per-stage generation spans and exact timestamps are in accounting.json;
these include waits/tools between the first and last native responses and are
not CPU-time measurements.

No audio duration or full-script output capacity was verified because the run
never reached those stages. Complete SQL/network totals for source acquisition,
full-run bytes and peak RSS were not instrumented. The native provider request
and receipt counts above were verified directly, without altering the live
execution behavior. No code changes or tests were run during paid execution.
