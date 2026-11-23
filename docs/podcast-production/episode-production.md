# Running an episode from a topic

The [episode CLI](../../backend/src/podgen/episode_cli.py) manages a solo episode from initial research to a WAV master. It requires an initialized PostgreSQL repository, the actor and project that own the episode, the object store for that project and a settings file referencing existing authorized profile revisions. The chosen voice must support the requested language. Setup and dependencies are in the [backend guide](../../backend/README.md).

From the repository root, activate the backend environment and export `PODGEN_DATABASE_URL`. Keep provider credentials in the environment or use the CLI’s global `--env-file` option. Set `PROJECT_ID`, `ACTOR_ID`, `OBJECT_STORE`, `SETTINGS_FILE` and `EPISODE_RUN_ID` to the project UUID, actor UUID, object-store directory, settings file and a new episode UUID. The [settings model](../../backend/src/podgen/episodes/production/contracts.py) defines the required fields. For example, after provisioning those inputs:

```sh
python -m podgen.episode_cli \
  --project "$PROJECT_ID" --actor "$ACTOR_ID" --objects "$OBJECT_STORE" \
  --run-id "$EPISODE_RUN_ID" prepare \
  --topic 'How engineers laid the Atlantic telegraph cable' \
  --output-language ru --min-seconds 1800 --target-seconds 1800 --max-seconds 1980 \
  --settings "$SETTINGS_FILE"
```

`prepare` is provider-free. It validates and saves the exact configuration, including the language, voice and duration. To execute that saved run, use the same global arguments followed by `run --export-dir tmp/episode-export`. This step can incur research, writing and synthesis charges. Alternatively, `start` takes the same topic and settings as `prepare`, requires `--export-dir` and performs both operations. A reference-state file may supply the project, actor, database and object-store settings through `--reference-state`. It belongs to the installation that created it.

| Command | Effect |
| --- | --- |
| `status` | Return the current phase, review count and reason for any stop in a bounded read. |
| `pause` | Persist a stop before the next external action. The current response can still be recorded. |
| `run` | Continue saved work, respecting a pause. |
| `resume` | Clear a pause and continue saved work. Uncertain paid submissions require reconciliation. |
| `export --export-dir DIRECTORY` | Export retained research and writing artifacts, and the master if it has been assembled. |

Research is polled every 15 seconds by default. `--step` returns control when the coordinator must wait for another poll. Reuse the episode UUID to inspect or resume that episode. A different topic or configuration requires a new UUID.

## Ownership and terminal states

The parent saves the configuration, phase and deterministic child identities before dispatch. Each child owns its request, response, charges and recovery history. A project-scoped lock and conditional state transitions prevent two executors from adopting conflicting work. Access is checked at external actions and public reads.

Supplemental research continues the same accepted plan. The ordinary process permits two completed script rewrites in total across material continuations. The settings pin the research-job allowance and the audio budget. A stop exposes the failed stage and reason. Replaying an uncertain paid operation as a new submission can bill it twice.

The synthesizer covers every utterance in order. Oversize utterances are split on sentence boundaries, then words. The assembler checks full character coverage against the exact source script. Under `ffmpeg-assembly-input/1`, local preprocessing retains the decoded PCM and applies loudness adjustment to the assembled master. A new processing revision may reuse a verified provider response without another TTS call.

`assembled` means the master exists and acoustic review is pending. `production_ready` remains false until the product’s release requirements have been satisfied.
