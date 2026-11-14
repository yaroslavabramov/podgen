# Hieroglyphs script run: incomplete, waiting for credits

Run `1cd0ac76-3326-58bb-83f0-6ee76847cefe` is `needs_attention`:
OpenAI returned HTTP 429 / `insufficient_quota` / `credit_balance_exhausted`.
The first draft and reader review completed. Fact review completed nine physical
responses, then its source turn 9 was explicitly refused before generation.
The fact verdict, script revisions and final acceptance are **not complete**.
No TTS/audio was started. No request was repeated after the refusal.

## Saved work and recovery

- [Full first script](draft-attempt-0.md), [complete persisted draft](draft-attempt-0.json).
- [Complete reader review](reader-review-attempt-0.json): `revise`, five findings.
- [Provider refusal](credit-refusal.json), [precise recovery evidence](recovery.json).
- [User authorization](authorization.json), [accepted plan binding](planning-clearance.json).
- [Accounting](accounting.json), [count receipts](count-receipts.json),
  [successful physical generation receipts](generation-receipts.json).

The ordinary provider-free draft preview is
`tmp/hieroglyphs-30m-script-2026-11-15/draft-preview.json`. It uses Claude Opus,
the accepted plan `e9db563d-d646-56b9-9fce-fe551156767c`, and the saved
4066-word estimate. Full actual requests/responses, original Anthropic wire
responses, source tools, physical turns and failure receipts are in
`tmp/hieroglyphs-30m-script-2026-11-15/export/archive/`.
The frozen materials, prompts, model settings and limits remain unchanged.
Planning was reused, with no new research or planning generation.

The script has **3987 words**. The selected voice's retained calibration gives
about **29:25**, below the requested 30-minute minimum; this is a text estimate,
not audio measurement or final duration acceptance. The ordinary reader/fact and
revision cycle has not yet finished. No script revision has been spent;
the saved ceiling remains two.

The saved failure is unambiguous (`ambiguous=false`), operation `submit`,
source turn 9 of fact-review invocation
`e70688b0-e79a-5ffe-82ec-d436de3f4b86`. Existing `refusal_evidence` accepts its
receipt. **After credits are restored**, the ordinary `writing_cli resume`
with `--retry-credit-refusal e70688b0-e79a-5ffe-82ec-d436de3f4b86` can continue
from the rejected request. It must preserve the completed draft, reader review
and nine fact responses. This recovery has not been executed; root is awaiting
the user's confirmation of funding.

## New script work only

| Stage | Count calls | Completed generation responses | Explicitly refused submissions | Input usage | Output usage |
| --- | ---: | ---: | ---: | ---: | ---: |
| draft | 5 | 5 | 0 | 192,611 | 14,017 |
| reader-review | 1 | 1 | 0 | 14,205 | 11,279 |
| fact-review | 10 | 9 | 1 | 220,827 | 383 |
| Total | 16 | 15 | 1 | 427,643 | 25,679 |

The 16 exact count receipts measure 461,444 input tokens, including 33,801 for the
rejected generation request. Its generation usage is zero; count measurements
are separate from generation charges. Fifteen same-request count-reuse events
are not additional calls. Cache hits: 337,987 input tokens; cache writes: 80,440.
These counters are input subsets, not extra usage to add. Dollar charges for
count, generation and cache remain **unknown**; token usage is not an invoice.
No uncertain outcome or technical retry occurred. Prior completed planning
(27 counts/27 generations) is excluded from these new-work totals.

**Exporter accounting defect:** the ordinary `run.json` drops 220,827 input tokens
from the nine successful physical turns of this failed fact stage. It already
includes their 383 output tokens. This report counts unique physical receipts
and explicitly records the discrepancy; it does not rely on the incomplete
stage-level input sum. Synthetic stage aggregates are not added again.
Production code has not been changed for this defect.

The CLI ran about 339 seconds (5:39), including startup/final export; exact bounds
are in accounting.json. No heavy tests were run. The process exited after the
credit refusal. This is an interim checkpoint, **not a completed script cycle**.
