# Import the Snow materials

From the repository root, configure `PODGEN_DATABASE_URL` and provide an actor with access to the project. Use a fresh scope and output directory.

```sh
uv run --project backend python evals/editorial-inputs/current/snow-pump/import_case.py \
  --project "$PODGEN_PROJECT_ID" --actor "$PODGEN_ACTOR_ID" \
  --objects "$PODGEN_OBJECTS_DIR" --scope snow-materials-2026-11-10 \
  --out-dir "$SNOW_WORK_DIR"
```

The importer admits the four files listed in the [case guide](README.md), checks source hashes and all 68 excerpts, then imports the 24-card `shared.json` package. It calls no models. The output directory contains `D1-ref.json` through `D4-ref.json`, `source-map.json` and `shared-ref.json`.

For a new writing run use `--author-ref "@$SNOW_WORK_DIR/shared-ref.json" --case snow-pump --plan-mode own-plan` with a new run ID and export directory. For a historical import, select `--input evals/editorial-inputs/current/snow-pump/history/before-material-rebuild-2026-11-10.json`. The importer reads that snapshot's own document list, including its v4 dossier.
