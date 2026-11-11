# Continue an exhausted editorial cycle

`continue-editorial` admits one additional author revision and both independent readings of its result. It requires an immutable `revision_limit_exhausted` outcome. A kept script, a request for material or an unfinished invocation cannot seed it. Technical interruption recovery remains the ordinary `resume` operation.

```sh
uv run --directory backend python -m podgen.writing_cli \
  --project PROJECT_UUID --actor ACTOR_UUID --objects OBJECT_DIRECTORY \
  continue-editorial --source-run-id SOURCE_RUN_UUID \
  --additional-input-tokens 524288 --additional-output-tokens 0 \
  --preview-only --export-dir EXPORT_DIRECTORY
```

Omit `--preview-only` to execute the admitted work, or use the ordinary `resume --run-id CHILD_UUID --export-dir EXPORT_DIRECTORY` after preparation. Preparation freezes the child manifest and exports the exact writer request without calling a model or token-count endpoint. The child ID is deterministic from its parent and `editorial-continuation/1`; repeated admission cannot branch or increase the allowances already frozen by the first preview.

The descendant starts with local `revise-1`, followed by `reader-review-1` and `fact-review-1`. It inherits the last full script and composition, the exact final reader/fact pair, accepted plan and clearance, materials, model profiles, prompt records, instruction versions and schemas. No planner, fresh draft or initial cold reading executes. A parent that itself continued planning is supported: its accepted plan is reused, its older planning costs are included, and its active planning-continuation command fields are cleared while ancestry remains available through the parent reference.

The global cap is three author revisions. A parent that completed two therefore permits one final revision. Local stage names do not reset the global script version in the writer operation, saved application verification or exported report. The informed reader gets the actual before/after, exact assigned work and the previous reader decision from the parent. The editorial cycle completes only when both readings return `keep` on the same complete revised version; this does not grant semantic acceptance or production readiness. Unresolved necessary work remains a failure to finish; no outcome is manually replaced.

All token usage and unknown-output reservations carry forward. `--additional-input-tokens` increases the parent's effective total input ceiling; `--additional-output-tokens` explicitly increases its output ceiling. Neither changes the model output limit, local role budget or context capacity. The manifest pins inherited usage, allowances and ceilings. `usage_ledger` separates inherited, own and total usage; current-run invocation counts and archives identify only actual new calls, including technical attempts. The normal budget checks include inherited usage before sending any new generation.

Elapsed time has a separate explicit policy, `per-admitted-run/1`: the child receives a new execution window of the same `max_elapsed_seconds`. Human waiting between terminal parent and child admission does not consume that window. Parent manifests, limits, outcomes, invocation history and archives are unchanged.

Historical runs with no pinned revision resources (`max_script_revisions=0`) are rejected with `editorial_continuation_revision_resources_missing`. The implementation does not obtain missing resources from the current production release.

Offline checks: `backend/tests/test_editorial_continuation.py`. PostgreSQL checks: `backend/tests/test_editorial_continuation_runtime.py`; run serially with the other database suites.
