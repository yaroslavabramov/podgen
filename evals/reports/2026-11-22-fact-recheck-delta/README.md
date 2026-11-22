# Factual recheck /4

`reading_audit_consistency_contract=decision-findings/4` is active for new runs,
with `writing/prompts/current/fact-verification.md`. Frozen /2 and /3 keep their
original request and admission behavior. Existing local readmission grants and
the policy for granting new recovery remain separate.

The recheck receives the complete current script, exact before/after edit windows,
assignments, prior decisions, and authenticated evidence references. One explicit
confirmation retains unchanged decisions; changed, retired, or new decisions are
reported separately. Admission reconstructs and validates a complete report.
Split/merge successors must explicitly identify their own evidence; uncertain
anchors, missing evidence, and unchecked claims cannot silently produce keep.

## Saved representative case

Run `4801838f-73b2-44be-9145-a8c6ab9006d6`, archived `fact-review-2`:
66 current utterances, two changed utterances, 26 prior decisions. Only D13 and
D21 require explicit updates. Admission retains all 26 decisions and 12 physical
evidence records. Four needed excerpts stay inline; eight unchanged evidence
records expose exact optional `read_source` locations. All eight were read through
the real source tool and matched the retained text. Longer ranges use ordinary
pagination; ambiguous selectors and unavailable sources remain inline.

| UTF-8 bytes | Archived request | /4 candidate |
| --- | ---: | ---: |
| User content | 190,092 | 136,520 |
| Previous evidence | 92,952 | 35,823 |
| Output schema | 21,101 | 22,171 |
| Response | 53,028 | 15,071 |

The candidate response is an offline adaptation of the saved response, not a new
model review. The complete provider wire was captured with a local mock transport.
No provider generation, counting, search, or external network calls were made.

Warm preparation, compilation, and admission measured 478 SQL calls and two object
reads (391,695 bytes) for each contract. Times were 0.601 / 0.621 seconds; process
peak RSS was 162,824,192 bytes. These measurements do not cover complete episode
publication, physical object-store writes, or token/cache savings.

Local complete comparison and exact requests are in
`tmp/fact-recheck-delta-2026-11-22/`: `compare.py`, `before-request.json`,
`after-request.json`, `after-native-wire.json`, `after-offline-delta-response.json`,
`after-admitted-report.json`, `admission-proof.json`, `negative-cases.json`,
`exact-read-locations.json`, `payload-measurements.json`, and
`operation-measurements.json`. The original frozen run was not upgraded or edited.

## Verification

- 138 focused tests passed after the named-prompt migration.
- Disposable PostgreSQL: two tests passed, including the ordinary /4 production
  cycle with two rewrites, retained stable evidence, exact preview, and resume
  without another provider call. Shared heavy-test lock was held.
- Archived /3 full-cycle replay passed separately.
- Negative cases reject missing confirmation, omitted changed decisions, split
  successors without evidence, unknown H references, incorrect anchors, and
  unchecked coverage.
- Stable D2 explicitly uses supported, independent grounds and coverage; it no
  longer inherits the required negative D1 defect. Its anchor survives the cycle.

New runs use /4 through the ordinary entry point and preview. No temporary prompt
copy, manifest override, or substitute behavior is needed.

## Evidence references across rounds

On the same 66-utterance script, 17 of the 24 carried decisions under memory
projection `/1` named a different or missing passage in their prose. In D11 the
name H12 changed from Champollion's letter to an analysis of Cleopatra's name.

With memory projection `/2`, all 24 carried decisions identify their original
passages in both the actual request and the locally expanded report. All 23
decisions shown at the preceding recheck also matched their original sources.
The comparison checked each occurrence against the complete archived receipt,
and checked the corresponding inline passage or exact source range shown in
the request. The two reassessed decisions used the saved answer with citations
translated by receipt. The current run's reviewers made no new decisions.

Warm preparation, compilation and admission made 478 SQL calls and two object
reads (391,695 bytes) with either memory projection. There were no object writes
or provider calls. The prior archived request and the earlier delta candidate's
request, admitted report and admission proof were reproduced unchanged.
The local comparison and full candidate are in
`tmp/fact-recheck-citations-2026-11-22/`.
