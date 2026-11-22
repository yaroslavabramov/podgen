# Addressed factual-review resume: paid 42 → prepared 43

Implemented and locally verified. No real HTTP, provider count or generation was
called. Original archived requests and model-facing contracts are unchanged.

## Scope and isolation

Baseline code: **4182edd6**. Real source run:
`4801838f-73b2-44be-9145-a8c6ab9006d6`, fact-review invocation
`733670e5-d4c3-58c4-a488-529db45cac35`.

Both variants use isolated PostgreSQL copies, identical materials and paid response
42, rewound to pending observation revision 179. The completed research child is
retained; its later local interpretation/call-result caches are removed in both
copies. Thus this measures first delivery and interpretation, rather than a
substituted cached result. Fixture preparation alone bypasses immutable-entry
triggers in disposable copies. Measured product execution uses normal checks.
Object writes go to separate overlays; originals are read-only.

The boundary includes the writing lock, material loading/restoration, fresh
access checks, corpus/index, history/ledger, paid response and material delivery,
then counting and durable preparation of request 43. It stops before model
submission. Export and subsequent verification reads are outside **both** SQL
and object counters. The local counter returns the original saved count; all
HTTP/submission/retrieval paths are poisoned. Baseline code is frozen, and the
comparison code contains only this change, excluding concurrent report-delivery
changes. The ordinary reading-stage entry is exercised in both variants.

## Complete-operation results

| Metric | Baseline | Addressed resume | Cold repeat |
|---|---:|---:|---:|
| SQL statements, including transaction settings | 5533 | 1865 | 1501 |
| Object reads / streams | 260 / 79 | 7 / 79 | 10 / 76 |
| Object bytes read | 57,270,516 | 6,664,329 | 7,034,927 |
| Object publications / bytes | 3 / 854,752 | 3 / 854,752 | 0 / 0 |
| Local count calls | 1 | 1 | 0 |
| Prepared physical turns | 0…43 | 42, 43 | 42, 43 |

Both initial preparations and the repeat produce exactly the saved continuous
request 43, after normal Responses wire conversion. Canonical wire SHA-256:
`94c0e1fc3818cb126c1d1f538ef8228d318bb481856445d451e8769be53c3a99`.
No generation intent is dispatched. The repeat reuses the bound count receipt;
it still performs normal bookkeeping, not a zero-write shortcut.

SQL ownership for the first delivery:

| Owner | Before | After |
|---|---:|---:|
| Materials | 177 | 177 |
| Material restoration | 409 | 409 |
| Corpus construction / refresh | 463 | 463 |
| Explicit authorization / input preparation | 52 | 52 |
| History | 845 | 34 |
| Physical ledger | 18 | 18 |
| Paid/current-next physical steps | 1455 | 29 |
| Material delivery and interpretation | 101 | 101 |
| Publication | 69 | 69 |
| Context, manifest, retry/budget setup | 1944 | 513 |

Remaining 513 setup statements are attributed in the raw `remaining_chains`:
input-recovery attempt discovery (71), research-annex manifest (44), current /
reading / prior-attempt headers (23 each), current-script binding (19), ordinary
manifest and request reads. Materials, corpus and publication were not optimized
by this change. There is no per-turn native-body history scan left in addressed
resume or its ancestor receipt-number restoration.

The identical history/ledger-only growth probe gives **22 → 863 SQL** for 0 → 42
turns before, and **24 → 52 SQL** after. Compact index hashes match the baseline
at both sizes, including E receipts, searches, exact counts and usage. Private
metadata is paged by 64 entries; historical bodies are not fetched. This probe
measures history growth, not the full operation on a synthetic empty corpus.

## Recovery and verification

The retained immutable request owns restart input. Its physical receipt and
request metadata must match the selected invocation/turn. Existing authorization,
counting, checkpoint and resolver operations remain responsible for their own
publication and failures. Shortened paid checkpoints restore their deferred
exchange before publishing context. Retry permission and exact failed-request
validation remain required; compact ancestor metadata only replaces the redundant
full-receipt reads. Legacy event prefixes use authorized bounded batch reads.

**30 tests pass** on migrated local PostgreSQL: direct/structured handoff, multiple
requests and adjacent calls in one answer, repeated pending/final resume, E IDs,
checkpoint selection, crash after a paid shortened checkpoint, credit retry,
access denial and corrupt/wrong-owner retained input. Full native request lists
match continuous execution. Lint and whitespace checks pass. One existing
Alembic configuration deprecation warning remains.

The original 235 execution entries captured before fixture preparation remain
byte-for-byte equivalent as decoded records. The parent scenario independently
appended 52 later entries; this check compares the original immutable prefix.
Our measurements wrote only to disposable databases and object overlays.

Limits: no live provider execution or concurrent resume race was added to this
measurement; existing invocation locks are retained. Peak RSS was not measured.
Fresh access guards remain in ordinary operations. Other remaining material,
manifest and authorization costs are visible above, not claimed fixed.

## Evidence

[Compact counters and hashes](proof.json). Private/raw artifacts stay in
[`tmp/research-handoff-resume-2026-11-22/`](../../../tmp/research-handoff-resume-2026-11-22/):

- [Complete measurement driver](../../../tmp/research-handoff-resume-2026-11-22/measure_boundary.py), [fixture preparation](../../../tmp/research-handoff-resume-2026-11-22/first_delivery.py).
- [Baseline](../../../tmp/research-handoff-resume-2026-11-22/boundary-before.log), [after](../../../tmp/research-handoff-resume-2026-11-22/final-after.log), [repeat](../../../tmp/research-handoff-resume-2026-11-22/final-repeat.log).
- [Native request 43](../../../tmp/research-handoff-resume-2026-11-22/podgen_handoff_final-wire.json), [growth](../../../tmp/research-handoff-resume-2026-11-22/after-growth.json), [tests](../../../tmp/research-handoff-resume-2026-11-22/tests-final.log), [original-prefix proof](../../../tmp/research-handoff-resume-2026-11-22/original-preserved.json).
