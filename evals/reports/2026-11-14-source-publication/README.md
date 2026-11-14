# Source/artifact publication

Owner: the existing Repository writer commits artifacts; AcquisitionStore groups
one document transition with its checkpoints using that writer and append_runtime.
No alternative persistence owner, process cache or retained authorization grant.

SourceRevision/checkpoint commit together before extraction. Metadata revision,
extractor profile, representation and outcome commit together after a separately
saved extraction checkpoint. Retry after rollback reuses the saved HTTP body and
extractor output. The current operation passes immutable source/byte descriptors
instead of rereading them from storage. HTTP bytes are retained before extraction.

The compound publication and checkpoint put reuse existing archive_write:
REPEATABLE READ, project fence, membership FOR SHARE and SQL-only serialization
retry. Byte preparation is outside the callback. Every attempt constructs its own
new descriptors, returned only after commit. Repository/package/branch transaction
policy and general AccessFacts invalidation have not been widened or bypassed.

The common writer deduplicates reads before resolution, authorizes dependency
frontiers in batches, loads unique business bodies in bounded batches, and checks
every field's subtype/lineage semantics. New revisions preserve both ordinary and
lineage roles when explicitly requested; retries of existing revisions do not
rewrite historical edges. Bytes and evidence text are verified outside SQL through
object_io. Fresh publication checks still reject withdrawn access and newly
classified evaluator SHA aliases. Package imports prepare only required text
selectors and support binary/direct-blob/external-representation inputs.

## Measured complete operation

One actual acquisition with a fake fetch/extractor, including all checkpoints and
the metadata-bearing completed document, after fixture setup:

| Work | Count |
|---|---:|
| SQL executions | 145 |
| Membership role queries | 12 |
| Artifact-frontier queries | 22 |
| Artifact `SELECT *` statements | 13 |
| File object streams | 2 |
| File bytes read | 3030 |
| Additional object reads for artifact creation | 0 |

`document-transition.json` records totals and per-stage counts. Publication facts
still invalidate after writes; source/representation/outcome checkpoints each
read three frontiers. This remaining repeated work is explicit, not hidden by a
cache or removed access checks.

The 13 artifact statements are four operation-id lookups (misses), four input-body
batches, four reads of newly created result rows, and one acquisition profile read;
they are not 13 repeated body downloads. A pre-correction RC probe measured 139
SQL. Its counter was superseded after review required the real RR/member-lock
boundary; no comparison with the old 54-source acquisition is inferred from it.

For an artifact with 1/20/100 repeated occurrences of the same dependency, each
publication executes 27 SQL statements, one business-body batch and one final
artifact-row read. Every field contract is still checked.

The audit's 21253-SQL baseline covers 54 acquired/admitted sources. It is a
different denominator: this one-document probe does not establish the final
end-to-end reduction. Root acceptance remains the same complete offline replay,
including all sources, all editorial roles, two audio requests and export.

## Checks and limits

The final boundary queue passed 35 tests. Five transaction-guard follow-ups also
passed (four overlap the boundary queue). The guard observes actual SQLAlchemy
begin/commit/rollback events, excluding AUTOCOMMIT advisory locks, and its negative
control detects deliberate object I/O inside archive_write. The initial related
queue had 54 passes and one instrumentation assertion failure (the test matched
pre-driver parameter spelling), subsequently corrected. Covered paths include
source acquisition/resume, complete report bytes, package atomicity, binary/direct
BlobRef import, external text evidence, field subtypes, lineage roles, immutable
retry, object I/O transaction boundaries, outer remote snapshot release, ACL and
SHA withdrawal, and post-read evaluator classification. No paid API was used.
Concurrent ACL deletion/demotion blocks until document publication/put completes;
the next action denies the revoked/demoted actor. Forced serialization failures at
source/outcome rollback partial refs and retry SQL without another fetch/extraction.

Root's reusable-document/annex queue passed 23 cases and exposed an old pagination
fixture failure, reproduced on baseline and corrected with distinct passages.
The corrected case passed. The complete candidate replay also passed with identical
native requests, final script and WAV. Its sources/material phase uses 13369 SQL
versus 21253 on baseline; see the [full-operation report](../2026-11-14-full-operation-persistence/README.md)
for comparable totals, memory, timing and limitations. This local probe alone does
not qualify cloud latency or process peak RSS.
