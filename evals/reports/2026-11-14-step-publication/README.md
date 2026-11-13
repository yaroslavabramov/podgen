# Atomic model-step publication — offline verification

Final measured code: `38413f36dac002b1fbc1ff0231476ddf71ef35ed`.
Core publication: `6175f3a4`; counter reset: `a451957f`; bounded audit: `7251fd2f`;
locked role reuse: `38413f36`. Previous receipts remain in
`../2026-11-13-runtime-offline-replay/` unchanged.

## Final tested results

| Operation | Previous metadata-only first | Final first | Final new repeat | Final completed |
| --- | ---: | ---: | ---: | ---: |
| Startup SQL | 1,178 | 1,175 | 638 | 611 |
| Fact stage SQL | 14,468 | 8,549 | 8,592 | 327 |
| Export SQL | 478 | 478 | 478 | 478 |
| Whole phase SQL | 16,128 | 10,206 | 9,712 | 1,420 |
| Whole seconds | 187.22 | 186.51 | 207.02 | 2.89 |
| Search seconds | 148.82 | 154.59 | 167.41 | 0 |
| Whole seconds excluding search | 38.39 | 31.92 | 39.61 | 2.89 |
| Count / submit mock calls | 43 / 43 | 43 / 43 | 43 / 43 | 0 / 0 |
| Actual tool results verified | 42 | 42 | 42 | 0 |


All three full offline passes verify native requests, actual tool results and all
370 exported files. Completed resume performs zero count, submit, tool or search
operations. SQL execution and driver-call totals match. Timing is reported without
a uniform-speedup claim; see the variance and limits below.

## Contract

`Publication` is an explicit, bounded phase, shared by ordinary and tool model
workflows. It stages complete object bytes outside the archive transaction,
validates the union of external references once before any runtime INSERT,
publishes runtime records/events/state together, classifies the union of new
records, and updates invocation-owned history only after commit. Intra-phase
references must resolve to an earlier record with the exact kind. Generic
`append_runtime` retains its authorization and replay contract.

The normal research turn has six publication phases: before-count intent; count
receipt; pre-submit; provider receipt; tool intent; tool result. Pre-submit contains
the context plan, physical request, visible measurement, reused exact count,
submit intent and status. Search diagnostics share the result phase. Provider ID,
physical usage/state and root usage projection commit together. Final output
publication includes its root usage and status. No transaction encloses provider
or object I/O. Scalar and grouped blob saves use the same `publish_staged` policy,
including streaming integrity checks, current ACL, withdrawal and legacy winners.

Every phase retains strict current input authorization. A production action guard
uses the same SQL snapshot, **after** classification, so newly protected SHA aliases
cannot evade the generation principal's checks. Classification walks the complete
forward/SHA-alias graph once and records an actually reaching root for each hash.
Metadata prefetch drains artifact-to-runtime links in bounded frontiers; it grants
no access. Dynamic admission refs are pinned in new physical request/count-intent
records without entering native model JSON. Existing records are not rewritten.

`TurnLedger` loads committed history once, then maintains usage incrementally and
checks submitted membership for one turn. Its in-transaction projection is pure.
Exceptional refusal evidence remains a post-commit operation under the existing
policy; any correction is persisted separately. Invalid or unrepresentable scalar
usage/counts retain their raw receipt and fail explicitly. Aggregate overflow also
retains the physical response. An uncertain submission is never automatically
sent again. Known preflight failures persist their staged request and metadata.

This change preserves the existing capture policy: withdrawal can still reject
capturing a response that has already arrived. No inbox, capture permission,
`skip_acl`, cross-snapshot authorization cache or new permission entity is added.
Such a submission without an accessible receipt remains unknown and cannot be
silently repaid. This limitation is not claimed solved.

## Growth checks

A complete counted turn with mocked provider and real PostgreSQL includes all six
publication phases, plus metadata/blob registration and exact-count validation.
The source documents are real `SourceRevision` artifacts with acquisition records.
The same test seeds zero or 128 already completed turns, then initializes the cold
history owner before measuring the new turn.

| Source documents | Completed turns | SELECT | INSERT | UPDATE | CTE (WITH) | SET | Union input checks |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 0 | 94 | 20 | 4 | 16 | 8 | 6 |
| 129 | 0 | 110 | 20 | 4 | 16 | 8 | 6 |
| 1 | 128 | 94 | 20 | 4 | 16 | 8 | 6 |
| 129 | 128 | 110 | 20 | 4 | 16 | 8 | 6 |

There is one exact current-turn lookup and zero historical turn rereads inside the
measured turn. The additional documents add bounded artifact-frontier reads,
not per-document acquisition queries. The initial growth probe exposed 1,040 CTE
queries at 129 documents; batched artifact-to-runtime metadata reduced this to 16.
An independent ledger test exercises one and 4,096 completed turns without
reloading historical rows or copying the submitted set on each update. Multi-row publication uses actual single SQL INSERTs,
not executemany calls disguised as one statement. Receipts report SQL executions
and driver calls separately; their equality is checked for the full replay.

## Batch-only full replay (intermediate)

The complete first/repeat/completed run on `a451957f` passed all native request,
actual tool result and export oracles. Receipts are retained as `batch-*.json`;
they do not claim that final validation had been optimized.

| Operation | Previous metadata-only first | Batch first | Batch new repeat | Batch completed |
| --- | ---: | ---: | ---: | ---: |
| Startup SQL | 1,178 | 1,178 | 641 | 613 |
| Fact stage SQL | 14,468 | 9,663 | 9,706 | 1,119 |
| Export SQL | 478 | 478 | 478 | 478 |
| Whole phase SQL | 16,128 | 11,323 | 10,829 | 2,214 |
| Whole seconds | 187.22 | 198.88 | 203.57 | 4.21 |
| Search seconds | 148.82 | 163.06 | 166.17 | 0 |
| Whole seconds excluding search | 38.39 | 35.82 | 37.40 | 4.21 |

Ordinary first-run turns cost 171, 189 after the additional source bindings, or
225 for search; the final turn costs 1,238. Each of 257 publication phases has
one external-input union validation. Inclusive phase SQL: before count 24; count
receipt 19; pre-submit 28; response receipt 20; tool intent 21; tool result 14
(with or without diagnostics); final response 18. Blob registration, explicit
receipt reads and tool internals are measured separately. The stage still has
2,948 metadata-frontier reads. New runs retain 43/43 count/submit, 42 verified tool
results and zero passage embeddings. Completed replay performs zero count,
submit, tools or search.

## Final-audit attribution

`finalization-diagnosis-before.json` restores the saved completed fixture and
executes the actual final audit read-only. It records SQL templates, exact frontier
IDs and nested evidence/locator/slice attribution. No provider, retrieval ranking,
publication or raw-input mutation occurs.

| Saved final audit | Unscoped body | Existing bounded source/journal scopes |
| --- | ---: | ---: |
| SQL executions | 890 | 107 |
| Read snapshots | 123 | 2 |
| Artifact metadata frontiers | 402 | 9 |
| Wall seconds (diagnostic, warm projections) | 1.379 | 0.145 |

Of the unscoped 890 statements, 558 occur in repeated VerifiedText slices (exact
selectors, retained checks and finding locators); 216 are repeated `locate`
material authorizations; 116 are current-catalog provenance and request/event
reads. With bounded scopes, the catalogue costs 99 SQL and the material guard 8;
subsequent local slices reuse the same snapshot facts. Normalized output and
complete audit proof hashes match. This is repetition within one local
verification, not a reason to weaken fresh external-boundary checks.

Production correction: `7251fd2fce3c32d882190577ac978b5107f66602`. It uses existing
`read_sources` plus `journal.read_operation` in final audit validation and
completed-output verification, including runtime admissions. Object I/O still
releases all owned snapshots and reauthorizes on return. A PostgreSQL remote
admission test verifies both repositories release snapshots before the read and
that withdrawn bytes are not returned. The affected audit/remote read/recovery
batch passes 76 tests.

`finalization-diagnosis.json` repeats the probe after correction and independently
checks the public production entry point: unscoped private body 890 SQL, explicit
scopes 107, public entry point 107, with equal output/proof hashes. The public
scoped probe takes 0.141 seconds; diagnostic timings are not an independent
end-to-end performance claim.

## Scoped audit full replay (before locked-role reuse)

`scoped-*.json` records all three complete verified passes on `7251fd2f`.
First/repeat/completed whole SQL is 10,540 / 10,046 / 1,422; fact stage SQL is
8,880 / 8,923 / 327. Final first-run turn falls from 1,238 to 455 SQL and
`fact_review:other` falls from 862 to 96. First time excluding search is 31.62s;
new repeat is 36.50s. This is a measured improvement with real inference variance,
not a claim that total elapsed time always decreases.

Exact attribution of the remaining 2,555 artifact-frontier SELECTs:

| Operation | Frontiers | Why a fresh boundary is retained |
| --- | ---: | --- |
| External input union, 257 publications | 1,328 | Current evaluation access before each atomic publication |
| Action generation guards | 512 | Fresh generation permission after classification, before count/submit/tool action |
| Local source/index tool operations | 404 | Current source and withdrawal-serialized retained-index access |
| Explicit request/receipt reads | 280 | Read exact durable evidence, including count reuse proof |
| Preparation, output and fixed checks | 31 | Stage binding, semantic output publication and catalogue checks |

These counts are directly attributed in each receipt's `operations` entries,
including `artifact_frontier_selects` and `snapshot_sets`. Compared with the
batch-only stage, the 393 removed artifact-frontier SELECTs exactly match the
saved-final-audit diagnostic (402 minus 9). Remaining source closures within a
publication have one union owner; the last role-only duplication is described
below.

## Locked-role reuse

Archive publication and index access already acquire the current membership row
`FOR SHARE`. The final patch seeds that exact row (including a missing-row denial)
into the existing `AccessFacts.roles`. Reference/privacy classification invalidates
all reference, blob and successful-node facts, preserving only roles of the same
actor, purpose and SQL transaction. No role survives its transaction. Generation
and evaluation principals retain their independent checks.

Real PostgreSQL tests assert one locked membership SELECT and zero subsequent
scalar membership SELECTs in both boundaries, and verify that the next operation
denies deleted or demoted membership. Source/SHA withdrawal and remote-read tests
remain green. The final affected role/authorization/index/privacy batch passes
67 tests. Earlier affected suites (119, 98, 92 and 76 tests) overlap and are not
added together as a unique test total. Ruff and diff checks pass.

## Final verification details

`final-*.json` records the three sequential complete passes on
`38413f36dac002b1fbc1ff0231476ddf71ef35ed`. Each restores/uses the original ready
legacy corpus (51 documents / 1,227 chunks). The real SDK uses saved native
responses through MockTransport; real local tools and persistence execute.


The previous metadata-only new repeat took 186.56 seconds, including 147.71s search
and 38.86s outside search. Thus the final repeat is slower in both measured parts;
no general elapsed-time improvement is claimed from the lower SQL count. These
single runs establish exactness and operation counts, not a latency distribution.

Locked-role reuse removes exactly 334 first-run statements compared with the scoped
audit version: three startup membership reads and 331 fact-stage membership reads.
Artifact frontiers remain 2,555 with the attribution above. Whole SQL includes
literal transaction SETs and conflict-tolerant INSERT attempts. The three
advisory-lock/ACL coordination statements are separate; totals including them are
10,209 / 9,715 / 1,423. SQL execution counts equal driver-call counts in every
reported phase; no executemany result is counted as a single statement.

First-run ordinary turn costs are 165 (no source/index work), 182 (retained read),
215 (search), and 448 for the last turn including final validation/output. Final
phase SQL including nested checks is 23 before count, 18 count receipt, 27
pre-submit, 19 provider receipt, 20 tool intent, 13 tool result and 17 final
response. The two physical/root status-and-usage projections share their commits.
No historical-turn scan is repeated per physical step.

All native request JSON, count replies and 42 actual tool results are exact in
both new runs, with only allowed second-run routing identity substitutions. Each
export verifies all 370 files: first/completed 322 byte-exact and 48 JSON-equal
after explicit technical substitutions; repeat 146 byte-exact and 224 after those
substitutions. Files with no actual permitted substitution remain byte-exact.
The original oracle's content-verified blob-handle checks, creation-time and
transport request-ID normalization are unchanged. Output, source quotes, native
tools/prompts/schemas/limits, receipts and complete historical content are retained.
All passes have zero passage embedding calls. Completed resume has zero provider,
tool and search operations.

Raw object reads are 82,553,999 first-run bytes versus the previous 90,798,027:
43 avoidable request-body reads are gone, not semantic content. Unicode projection
bytes returned remain 16,807,696. First-run staging/publication remains 260 complete
objects / 36,016,771 logical bytes. Repeat stages 260 / 35,136,648 bytes and publishes
131 / 18,632,404, retaining existing deduplication. These are logical object API
counts, not physical writes; SQL wire bytes are not measured.

Worker peak RSS is 1,405,026,304 bytes, cumulative across passes. It includes local
model allocators, runtime and the retained export/native oracles. No memory
improvement or full attribution of RSS differences is claimed. The previous
scoped worker observed 1,647,132,672 bytes; this variation is not an independently
isolated runtime-memory comparison.

Reproduce the complete run with the command in the previous report's README,
using the saved pre-experiment dump and trace. For the read-only audit probe, use
the completed dump and `--operation finalization-diagnosis`. All verification is
offline; no paid or external provider/source API is used. Each runner owns its
temporary PostgreSQL server and object copy. All owned PostgreSQL/test/replay
processes were stopped and temporary database directories removed. Original
database servers, source objects and unrelated user files were not modified.

## Remaining costs and limits

Fresh strict source authorization is retained at every publication/action boundary;
the 2,555 metadata frontiers are attributed above. Real query ranking still dominates
new-run elapsed time. These single runs do not establish a latency distribution,
and runtime/oracle/model RSS contributions are not isolated. Late withdrawal can
still reject capturing an already received response under the existing policy;
an unknown submission is never silently repaid. **Paid calls: 0. External provider/source API calls: 0.**
