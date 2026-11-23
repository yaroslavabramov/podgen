# Podgen

Podgen turns a topic into a researched podcast. A planner chooses the story, a writer drafts the script, and two reviewers read the same version for different reasons: one follows the story as a listener, the other checks claims against source passages. Once both accept a version, the episode coordinator can send it to speech synthesis and assemble the recording.

The project is under active development. Its current interface is a Python CLI. A short Russian documentary about Egyptian hieroglyphs reached a 12-minute assembled recording. A longer experiment about the Atlantic telegraph cable reached an approved script and exposed a gap between editorial approval and the requested duration. [Read the evaluation results and the cable script](evals/reports/README.md).

## Try an offline example

From the repository root, with Python 3.12+ and [uv](https://docs.astral.sh/uv/) installed:

```sh
uv sync --locked --project backend
uv run --locked --project backend python -m podgen.package_cli validate contracts/examples/research-note-package.json
```

This validates a research-note package and its artifact dependencies locally. To inspect the episode commands, run:

```sh
uv run --locked --project backend python -m podgen.episode_cli --help
```

A live episode needs PostgreSQL, an object store, a provisioned project with model and voice profiles, and provider credentials. The [backend guide](backend/README.md) covers local setup and tests. The [episode guide](docs/podcast-production/episode-production.md) explains how to prepare, start, pause, resume and export a run.

## How it works

| Step | Result |
| --- | --- |
| Research | A report, a catalog of cited sources and locally retained passages. The catalog records which documents could be retrieved. |
| Plan | A proposed story and a separate editorial review. The approved plan is pinned for the writer. |
| Write and review | A draft, a listener's reading and a factual review. The author can revise the script twice within a standard episode. |
| Record | Speech in script order, an assembly manifest and a WAV master. Acoustic review remains a separate decision. |

PostgreSQL records the exact inputs, decisions and provider receipts behind a run. Larger source documents and audio live in object storage. This lets an operator inspect what a model saw and resume interrupted work without automatically repeating a paid request with an uncertain outcome.

## Explore the repository

- [Production architecture and implementation plan](docs/podcast-production/README.md)
- [Working prompts for the editorial roles](backend/src/podgen/writing/prompts/current/)
- [Writing evaluations and recovery](backend/WRITING-EVALUATION.md)
- [Data contracts and cross-language validation](contracts/README.md)
- [Evaluation cases, listening materials and operator tools](evals/README.md)
- [Design history and lessons from earlier versions](docs/history/README.md)

The documentation is in English. Recorded scripts, editorial evidence and source quotations retain their original languages and wording so they can be replayed and compared.
