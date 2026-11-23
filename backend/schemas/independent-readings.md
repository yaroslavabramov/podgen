# Independent script readings

After drafting, Podgen asks two reviewers to read the same saved version. Each has a separate request, evidence boundary and report. The listener editor assesses the spoken story. The fact reviewer checks what the speakers claim against the source material. The production manifest pins `review_mode=independent-readings/1`, the model profiles and the contract for each reading.

| Role | Input and responsibility |
| --- | --- |
| `reader-review` | The script in speaking order, audience, genre, format, language and duration context. The editor assesses how the episode opens, develops and ends, including clarity, repetition, voice and idiom. The reader cannot inspect the source documents or the fact review. |
| `fact-review` | The same script, its factual remit and the sources available to the run. In a tool-enabled run, the reviewer can search and read the admitted evidence. It checks facts, causality, chronology and the scope of generalizations. It records which sources support each decision. |

Each role anchors findings to the current spoken text. Production reports record coverage and distinguish checked decisions from unchecked areas. Evidence aliases resolve to the exact packet passage or archived source read. The run archives complete judgments and rationales. See the [reading audit contract](../../docs/podcast-production/reading-audit-contract.md) for response fields, coverage rules and version history.

## How revisions work

1. A request for missing material takes precedence over revision. Required findings from either role trigger revision. Both roles must accept the current version for a positive outcome. The coordinator combines their decisions deterministically.
2. Each reviewer chooses a scope and identifies the affected passages. Local changes, sequence replacement and a whole-script rewrite use the [addressed revision contract](addressed-script-revision.md). The writer receives actionable assignments and relevant sources. A finding diagnoses the problem; its suggested wording does not become a new source of evidence.
3. Both roles recheck the new script. Each sees the assignments sent to the writer, the actual edits and its own prior judgments. Factual rechecks bind retained claims to their evidence. Reviewers may retain, reassess or retire a prior decision and must account for every assigned repair.
4. `max_script_revisions` accepts 0, 1 or 2. The default is 2. On exhaustion, the run retains the latest candidate, both reviews and the reason. A `keep` publishes the exact version that passed both readings. No further writing call is needed.

Each review binds to the script reference and digest, prompt, schema, model profile and manifest. Anchor repair can fix an address by selecting an offered passage while preserving the reviewer's judgment. The run keeps the original submission and repair history. A completed invocation is reused on resume. A submission of uncertain outcome requires recovery before any retry.

## Run or inspect a saved script

`script-import --file script.json --out script-ref.json` accepts a complete `WriteOutput` object with speaker-attributed utterances. It preserves the submitted bytes and a semantic digest. In a new run, pass `--initial-script-ref @script-ref.json`, the shared input and the writer and reviewer profiles. The first calls read the supplied script. If available, provide its fixed plan as context. See the [writing walkthrough](../WRITING-EVALUATION.md) for the command sequence.

A local `preview --stage reader-review` or `--stage fact-review` accepts `--draft script.json`. For `revise-1` or `revise-2` also pass `--reader-feedback` and `--fact-feedback`. Saved runs use their pinned request layout.

The export includes individual scripts and reports, `reading-round-N.json`, `review-outcome.json` and the readable `review-result.md`. Accepted writing output uses `final.json` and `final.txt`; a withheld version uses `candidate-final.json` and `candidate-final.txt`. A completed accepted writing evaluation records `status=completed_unaccepted` and `review_status=keep`. Production or release approval is separate. The writing CLI returns exit code 2 for a terminal editorial refusal.

## Missing material and historical runs

In projection v9 the writer can return `{"material_request":{"question":"…","why_needed":"…"}}` inside its `result` instead of a draft or revision. The writing evaluation records `failure_code=writer_needs_material`, the full question and `last_script_ref`. Resume reproduces that saved outcome. An operator can import an amended dossier and start a new run, supplying the saved script if one exists. The full [episode coordinator](../../docs/podcast-production/episode-production.md) manages its own supplementary research workflow.

Runs created with `informed-v1` retain their original single informed review. Historical independent readings retain the schemas, projections and prompts in their manifests. The current production profiles use `listener-experience/14`, `fact-revision/5` and `editorial-recheck/3`. Historical reports cannot acquire newer audit decisions through replay.
