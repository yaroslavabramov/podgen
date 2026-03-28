# Review findings — build_claims rerun review (2026-03-28)

## Finding 1
**File:** `/Users/yaroslavabramov/side/podgen/backend/app/pipeline/phase4_claims/claim_builder.py:136`

**[P1] Manual `build_claims` rerun forces full rebuild without deleting old claims**

The new `_should_force_full_rebuild_for_node_rerun(...)` path nulls `rebuild_scope` for manual step reruns of `build_claims`. But `_delete_claims(...)` is only reached inside the targeted-rebuild branch, so this forced full-rebuild path skips deletion of the existing `state["claim_ids"]` entirely and then writes a fresh claim set on top. Focused repro confirmed the path: with a rerun forcing `full_rebuild`, `deleted == []`, `rebuild_mode == "full_rebuild"`, and the node returned only the new claim ID. That means old claim rows remain in the DB as stale duplicates after a manual claim-builder rerun.

## Checks
- `uv run pytest tests/test_claim_builder_branch_batching.py tests/test_claim_dedupe.py -q` → `34 passed, 1 warning`
- `uv run ruff check app/pipeline/phase4_claims/claim_builder.py app/pipeline/phase4_claims/dedupe.py tests/test_claim_builder_branch_batching.py tests/test_claim_dedupe.py` → `All checks passed!`
