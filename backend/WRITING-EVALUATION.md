# Writing and editorial evaluation

Use `podgen.writing_cli` to run the writing stages against a saved dossier or research packet. It also supports replaying a specific draft or inspecting a model-facing request without calling a provider. For a new episode that needs research and audio, use the [episode coordinator](../docs/podcast-production/episode-production.md).

A writing run has a persistent ID. The planner proposes a plan, a separate editor reviews it, and the author receives the approved version. After the first draft, a listener editor reviews the spoken text and a fact reviewer checks its claims against the available sources. Both read the revised script before reaching a decision. Production uses the model profiles pinned to that run. The saved material, plan, scripts and both reviews are available individually.

## Import the example case

Start with the [local setup and editorial staging example](README.md#try-the-storage-and-editorial-contracts). Keep `PODGEN_DATABASE_URL` set to that database. The following shell commands run from the repository root and use `jq` and Python 3. The working directory is local and may contain private source material.

```sh
export PODGEN_DEMO_DIR="$(pwd)/tmp/editorial-demo"
PROJECT_UUID="$(jq -r '.project_id' "$PODGEN_DEMO_DIR/staging.json")"
ACTOR_UUID="$(jq -r '.actor_id' "$PODGEN_DEMO_DIR/staging.json")"
jq '.file_refs' "$PODGEN_DEMO_DIR/staging.json" > "$PODGEN_DEMO_DIR/source-map.json"

pgwrite() {
  uv run --directory backend python -m podgen.writing_cli \
    --project "$PROJECT_UUID" --actor "$ACTOR_UUID" \
    --objects "$PODGEN_DEMO_DIR/objects" "$@"
}

pgwrite editorial-import \
  --input ../evals/editorial-inputs/current/gps-clock/shared.json \
  --source-map "$PODGEN_DEMO_DIR/source-map.json" --out "$PODGEN_DEMO_DIR/shared-ref.json"

pgwrite fixed-editorial-import \
  --input ../evals/editorial-inputs/current/gps-clock/fixed-plan.json \
  --source-map "$PODGEN_DEMO_DIR/source-map.json" \
  --shared-ref "@$PODGEN_DEMO_DIR/shared-ref.json" --out "$PODGEN_DEMO_DIR/fixed-ref.json"
```

The shared packet contains the assignment and cited documents. The fixed plan binds to the digest of that exact packet. Each mapped path must resolve to an authorized source with the expected bytes. See [editorial schemas](schemas/README.md) for other topics, `solo` or `dialogue` formats, and importing a new source.

## Register profiles and preview the request

These commands record three profiles locally. The provider names, model IDs and supported roles below match the production behavior in this checkout.

```sh
pgwrite profile-create --provider anthropic --model claude-opus-5-5 \
  --context-limit 200000 --output-limit 24576 --reasoning-effort medium \
  --roles writer --out "$PODGEN_DEMO_DIR/writer.json"

pgwrite profile-create --model gpt-6.1-sol --context-limit 200000 \
  --output-limit 24576 --reasoning-effort medium --roles planner \
  --out "$PODGEN_DEMO_DIR/planner.json"

pgwrite profile-create --model gpt-6.1-terra --context-limit 200000 \
  --output-limit 24576 --reasoning-effort medium --roles editorial_reviewer plan_reviewer \
  --out "$PODGEN_DEMO_DIR/reviewer.json"

PREVIEW_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
pgwrite preview --run-id "$PREVIEW_UUID" --case gps-clock --stage draft \
  --shared-ref "@$PODGEN_DEMO_DIR/shared-ref.json" \
  --fixed-ref "@$PODGEN_DEMO_DIR/fixed-ref.json" \
  --profile "@$PODGEN_DEMO_DIR/writer.json" --out "$PODGEN_DEMO_DIR/draft-preview.json"
```

The preview contains the request body, its UTF-8 byte counts and the role-specific schema. It reads local records; the provider client and token-count endpoint are unused. To inspect a planning request, use `--stage plan` with the shared packet and planner profile. Omit `--fixed-ref`. To preview a later stage, supply its accepted input objects as described in [the stage reference](schemas/README.md#preview-inputs). A preview bound to an existing run uses its pinned schemas and instructions.

## Run an evaluation

Live model calls require provider credentials for the chosen profiles, available to the process as `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` or loaded using the global `--env-file` option. Create a fresh run ID for each new writing task and retain it for recovery.

```sh
RUN_UUID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
RUN_DIR="$PODGEN_DEMO_DIR/$RUN_UUID"

pgwrite run --run-id "$RUN_UUID" --case gps-clock --plan-mode own-plan \
  --author-ref "@$PODGEN_DEMO_DIR/shared-ref.json" \
  --writer-profile "@$PODGEN_DEMO_DIR/writer.json" \
  --reviewer-profile "@$PODGEN_DEMO_DIR/reviewer.json" \
  --planner-profile "@$PODGEN_DEMO_DIR/planner.json" \
  --plan-reviewer-profile "@$PODGEN_DEMO_DIR/reviewer.json" \
  --export-dir "$RUN_DIR"
```

- For a planning-only evaluation, add `--stop-after-plan` to this command before running it. When the plan passes, inspect `planning-clearance.json` and compare the plan with its separate review. A subsequent `resume` on the same ID begins writing.
- For an existing plan, use `--plan-mode fixed-plan --fixed-plan-input-ref "@$PODGEN_DEMO_DIR/fixed-ref.json"` and omit the two planning profiles.
- For a saved script, import a complete `WriteOutput` using `pgwrite script-import --file "$SCRIPT_FILE" --out "$PODGEN_DEMO_DIR/script-ref.json"`. Use a new run ID, `--plan-mode fixed-plan` and `--initial-script-ref "@$PODGEN_DEMO_DIR/script-ref.json"`. The cycle begins with the two readings of the supplied text. A fixed plan reference is optional on this path.

The standard allowance is one plan revision and up to two script revisions. For priced production profiles a fresh manifest pins a default **$5 writing budget** and the cumulative share reserved for each stage group. Hosted research and speech synthesis have separate accounting. Other runs use their pinned limits. The journal records provider counts, cache usage, reservations and completed receipts. See [budget and recovery](../docs/podcast-production/input-budget-recovery.md).

## Inspect results and recover a run

```sh
pgwrite inspect --run-id "$RUN_UUID" --export-dir "$RUN_DIR"
```

Read `review-result.md` for the episode decision, then compare `draft.txt` and `revise-1.txt` with the reader and factual reviews for those exact versions. Successful independent readings publish `final.txt` and `final.json`. The run records `review_status=keep` and `status=completed_unaccepted`. The episode coordinator can then synthesize audio; release approval is a separate decision. A request for more material or an exhausted revision allowance retains the candidate and the reason in `review-outcome.json`. Private exports carry the exact manifest, inputs, model responses and attempt history. Export directories are bound to one project and run.

`resume` can perform paid work. A completed run reuses its saved result. For an interrupted run, inspect `run.json` and the failed invocation first. A confirmed unsent refusal can qualify for a targeted retry. An uncertain submission remains blocked until the existing result is recovered or its fate is established. Existing recoveries include:

```sh
pgwrite resume --run-id "$RUN_UUID" --export-dir "$RUN_DIR"
pgwrite resume --run-id "$RUN_UUID" --retry-credit-refusal "$FAILED_INVOCATION_UUID" \
  --export-dir "$RUN_DIR"
```

Choose the command matching the saved state. An anchor repair selects from supplied passages only; a plan-target repair selects an existing plan address. Both preserve the original review and rejected response. For grants after a proven input or dollar refusal, use the limit and invocation specified in the [budget recovery guide](../docs/podcast-production/input-budget-recovery.md). See [technical recovery](../docs/podcast-production/technical-recovery.md) for paid submissions and [editorial continuation](../docs/podcast-production/editorial-continuation.md) for explicitly extending a finished evaluation.

Archived `informed-v1` runs retain their single informed review and original schema and request fingerprints. Earlier raw-input runs remain inspectable; `legacy_outbound_disabled_start_compact_run` identifies a raw-input run whose unfinished work requires a fresh supported evaluation.
