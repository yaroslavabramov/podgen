# Bound research across native and terminal episode requests

No paid provider calls or model/profile changes. Tests use the normal episode service, real PostgreSQL/private execution journal, and local research/model/audio providers.

## Baseline and correction

The former terminal handler dropped anchors, combined unrelated review questions, and used a new operation identity. A question already investigated through a tool or structured review could therefore trigger another broad research job. Reusing only the child job still reread its report and revalidated all excerpts on a second caller.

New writing manifests explicitly pin `research_request_contract=bound-research-orders/1`. Saved manifests without this field retain the previous terminal and research behavior. Provider requests and stored reports are not rewritten.

- All three entrances—function call, structured review request and terminal `needs_material`—use the same exact script binding. Terminal requests retain a reference to their immutable originating review; question, anchors, script reference/hash and caller are checked before dispatch. The complete original utterances and neighbors are preserved.
- The original material scope owns one research order by question, bound script/context, output language, instructions and provider configuration. Anchor ordering and duplicate anchors do not change this work identity. Caller rationale and exact original arrays remain in each call's provenance; changing rationale cannot authorize duplicate payment.
- A writing run owns one persisted interpretation per order, including unresolved answers. Additional callers reference this same result after fresh authorization; they do not reopen the report, revalidate excerpts or fetch source objects. Another writing run imports the retained research into its own S/X namespace rather than borrowing local names.
- A prepared order survives interruption before submission, and a completed child survives interruption before progress publication. Existing child submission/reconciliation rules still apply. Concurrent preparations yield the same child identity, with the normal explicit busy rejection when a competing lock is held.
- Terminal questions are processed separately, with per-run progress/results. A remaining unresolved question is preserved and stops continuation. Completed research is not a factual verdict: original `needs_material` and the unanswered question remain in the archive; incomplete work is not published as keep.
- Local parsing upgrades use the shared versioned interpretation mechanism and retain links to the previous result. An old paid receipt is not repeated or overwritten.

`provider_evidence.register_structured()` now reconstructs expected calls with the anchored mode from the saved source-tool contract. The PostgreSQL episode test exercises the real registration and execution tables; it does not replace registration with a FakeJournal stub.

## Addressed checks

The tests cover tool/structured/final-request entry, two questions with one remaining unresolved, reordering/repeating anchors, changed rationale, restart after a completed answer but before the progress checkpoint, continuation of the original script when all materials are available, run-local X1/X2 mapping, fresh denial of access, and concurrent order preparation. The legacy final-request path is exercised with an actually persisted manifest lacking the new pin.

Observable repeat checks count report opens, parsing, literal-excerpt validation, object-store reads and SQL calls around complete resolver operations. Both resolved and unresolved cases require zero repeated parsing, excerpt validation, report reads, object reads and network acquisition; authorization is still performed. Raw measurements are retained in `measurements/`.

The sample is a retained document and three original utterances. It verifies operation behavior, not live provider quality, historical truth or production latency. Object counters exclude cached Unicode projection reads; process RSS is a high-water mark, not an independent memory comparison. Long admission-history performance is not measured here.

## Measurements and result

| Saved answer | First materialization: SQL driver calls / object bytes read | Same-order caller: SQL driver calls / object bytes read | Repeated report parse / excerpt validation / network |
|---|---:|---:|---:|
| Resolved | 206 / 1,991 | 81 / 0 | 0 / 0 / 0 |
| Unresolved | 116 / 102 | 78 / 0 | 0 / 0 / 0 |

These are complete resolver calls, including order lookup, authorization, local admission and separate caller receipts. SQL remains intentionally nonzero for fresh access checks and provenance. Counts are client driver calls, not server-internal statements. The first call materializes a retained answer; the second consumes that saved interpretation, so this is evidence of eliminated repeated work, not a provider-latency or cold/warm speed comparison.

Final addressed verification: **10 passed** (the nine lifecycle/compatibility/concurrency cases, plus shared cached-error reinterpretation). The two observable-measurement cases were rerun after instrumentation and both passed. All PostgreSQL runs used the common `tmp/audit-2026-11-21/check.py` slot. Ruff and whitespace checks passed. No paid calls, model changes, or live generation resumes. Live quality/cost and long-history/full-episode SQL performance remain unmeasured.
