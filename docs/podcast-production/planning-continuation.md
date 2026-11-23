# Continuing after the plan-revision limit

The operator-only `writing_cli continue-planning` command creates a child of an eligible own-plan run that stopped at `planning_revision_limit_exhausted` before writing began. It performs one additional `plan-revise` and one `plan-review-revised`. The ordinary writing chain may proceed only if that reviewer accepts the revised candidate. A request for further research has a different recovery path.

The first continuation requires `--max-additional-plan-revisions N`. It sets a ceiling for the complete chain of children. Each child consumes one additional review pair. If another revision is required, subsequent continuation targets that child and inherits the original ceiling. The deterministic UUID5 for the child includes the parent and `planning-continuation/1`. A repeat addresses the same child. The optional run ID asserts the expected identity.

The child inherits project, assignment, source material, prompts, models, schemas and limits. The parent’s exact final candidate and the review of that candidate determine the initial input. Validate those references at resume. Charge only the work incurred by the child. The parent retains its prior history and result.

```sh
uv run --project backend python -m podgen.writing_cli \
  --project PROJECT_UUID --actor ACTOR_UUID --objects /absolute/objects \
  continue-planning --source-run-id PARENT_RUN_UUID \
  --max-additional-plan-revisions 2 --export-dir /absolute/new-export
```

Use actual project and actor UUIDs and authorized local paths in place of placeholders. Add `--preview-only` to prepare the child and export its first request without creating a model client. Regular `episode start` does not invoke this operator command automatically.
