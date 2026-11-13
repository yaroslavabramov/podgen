# Reusable source document projections

Engineering audit item 3, 2026-11-13. Document computation and run authorization
must have different identities. No prompts, model-facing references, retrieval
weights, ranking rules or archived requests change.

## Identities

* Project-local document projection: exact source SHA-256, character length,
  complete pinned retrieval profile (including encoder weights/tokenizer hashes,
  prefixes, pooling, chunk windows/overlap and lexical configuration), detected
  language and explicit projection implementation version. No run or S ID.
* Run membership: the existing immutable run scope + retrieval profile + local
  S ID points to that projection. The same S ID in another run is unrelated.
* Search result: remains in the existing run-scoped immutable result store;
  query version and ordering are not shared or recomputed for later pages.

## Storage and lifecycle

New tables hold project-local document build state and chunk projections,
run memberships, and the run scope's authorization dependency hashes. Original
texts remain in the source store. Chunks/vectors are never copied per run.
Existing v1/v2 projection tables remain available for legacy replay. A v2
projection already built by this run can be imported server-side once into the
shared document store, including its durable cursor; no embedding call is needed.

One session advisory lock per project/document projection serializes builders
across runs, without holding a database transaction during inference. A competing
builder receives an explicit retryable technical-unavailability result. Each
bounded batch atomically writes chunks and advances its cursor. Searches join
only authorized run memberships to **completed** document projections.

Every operation uses the existing `IndexAccess.transaction()` authorization and
withdrawal serialization; no new source-access API is required. Run scope
withdrawal removes its memberships/results and conservatively purges affected
reusable projections. Revoking the source text hash also purges its reusable
chunks. Publication triggers reject withdrawn text/dependencies. Membership loss
never grants another run access to a projection.

## Validation before merge

Real PostgreSQL tests with counted cheap embedding doubles: second run/alias
reuses exact chunks with zero document inference; isolated S-ID bindings; changed
text or processing identity requires new computation; interrupted shared build
resumes across runs and is not searchable early; stable existing result pages;
withdrawal blocks cached results/rebuild and removes derived text/vectors;
legacy completed/partial projections are adopted without re-embedding old batches.
No paid calls. The shared heavy-test slot must be assigned before execution.

## Implementation review status

Isolated branch `codex/reusable-document-index-20261113` from `b3d49764`. Migration
`0024_reusable_source_documents` follows the separately owned
`0023_turn_event_selection`. Static syntax, Ruff and whitespace checks pass.
PostgreSQL tests and representative before/after operation measurements are
**pending allocation of the shared test slot**, not reported as passed.

Source access uses the unchanged `IndexAccess` interface. The whole pinned
retrieval profile participates in document identity; historical profiles and
run result versions are untouched. Completed reusable projections live until
project deletion or content/dependency withdrawal, even if one run is deleted.
