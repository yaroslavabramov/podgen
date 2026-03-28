# Review findings — claim_builder/dedupe rerun clean (2026-03-28)

No new findings in the current changes.

## What I rechecked
- The manual `build_claims` rerun path now deletes the old claim pool before rebuilding, including the no-evidence early-return path.
- The new cross-branch semantic-dedupe safety gate still allows a clear positive duplicate merge while rejecting the newly added negative case.
- I did not reopen older residual follow-ups outside the current fix scope.

## Checks
- `uv run pytest tests/test_claim_builder_branch_batching.py tests/test_claim_dedupe.py tests/test_claim_dedupe_scope.py tests/test_claim_builder_debug_dump.py -q` → `39 passed, 6 warnings`
- `uv run ruff check app/pipeline/phase4_claims/claim_builder.py app/pipeline/phase4_claims/dedupe.py tests/test_claim_builder_branch_batching.py tests/test_claim_dedupe.py tests/test_claim_dedupe_scope.py tests/test_claim_builder_debug_dump.py` → `All checks passed!`
