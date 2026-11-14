# Bound execution journal

The execution journal owns attempts, exact wire requests/receipts, usage, tool results
and recovery. Semantic artifacts retain their generic provenance repository. A normal
journal checkpoint must not reconstruct the semantic graph or classify its ancestors.

## Stored contract

- `execution_input_sets`: immutable run-owned typed roots, purpose and identity;
  a monotonic generation advances only when actual inputs/admissions expand.
- `execution_input_expansions`: one replaceable derived authorization projection per
  set, with versions of every relevant owner/catalog project. This is metadata, not
  a saved permission or historical semantic payload.
- `execution_input_resources`: the flat read/privacy dependencies of that projection.
  Privacy-only bindings never confer read access. Full source BlobRefs remain here,
  outside evaluator runtime payloads.
- `runtime_records.execution_run_id/execution_input_set_id`: both NULL for old/generic
  records, both present for execution records; composite foreign keys bind project,
  run and set. Raw payloads and identities remain immutable.
- Existing invocation/event/turn tables are projections of the journal. Explicit own
  byte bindings remain in `runtime_blob_refs`; new execution records skip recursive
  JSON reference projection.

A trusted binder builds dependencies using the existing complete ScopeAccess rules.
Unknown refs cannot be attached by the writer: they require an explicit input/admission
binding. Same-run technical refs require exact kind and an input-set generation already
covered by the selected set. Legacy records retain their original read/recovery policy.

## Freshness and privacy

`projects.validity_version` fences mutable effective provenance, acceptance revocations
and catalog publications/revocations, across all reached owners. Runtime append,
classification and blob registration do not bump it. Memberships, grants, audience and
SHA flags are checked live; successful checks never survive their snapshot. A stale
expansion is rebuilt, not accepted or copied into historical request payloads.

Generic graph and flat projection form one forward privacy closure:
legacy artifact/runtime → input resource → set → execution record → own bytes → aliases.
This includes revalidation links added after preparation. Source withdrawal therefore
reaches old sets through their retained roots as well as direct resources.

New evaluator own SHA bindings are marked directly. First protection of bytes already
used by other consumers also closes their forward/alias component with a real witness.
Every evaluator derivative protects its own bytes; public derivatives of protected
inputs are denied. All consumer/classification writers acquire the project write lock
in the same order before SQL writes; no SHARE-to-UPDATE upgrade or network wait occurs.

Capture keeps current ACL/SHA and withdrawal policy. No inbox or capture capability is
introduced. Missing receipts after uncertain submit never authorize another submit.

## Checkpoints

1. Exact count intent before count.
2. Count receipt plus validated submit intent/request/status, or receipt plus failure.
   A second external count requires checkpointing the first receipt before that call.
3. Provider receipt, provider ID, usage and state atomically.
4. Replayable read result bound to physical response, call ID and argument hash.
   No separate durable pre-execution read intent is necessary.

Paid/mutating tools retain durable operation intents/idempotency. A committed result
is reused; an unfinished replayable read is repeated only after fresh authorization.
Unknown generation is reconciled or requires explicit recovery, never silently resent.

Native model requests/responses, tool results and final meaning remain exact. A new
journal/archive contract explicitly describes technical deltas; old preview/resume
remains exact. Verification counts driver and nested server statements, projection
work, rows/resources visited, bytes and RSS, including rare alias/withdrawal work.
