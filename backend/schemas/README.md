# Editorial input and writing contracts

An editorial packet describes one assignment and the documents that support it. The assignment sets the language, speakers, format, target duration and acceptable duration range. Each card records a claim and its supporting passages. A separate fixed-plan packet lets an operator compare authors on the same plan. An own-plan run starts from the shared packet alone.

## Published schemas

| Contract | Definition |
| --- | --- |
| Current shared dossier | [editorial-input.v2](editorial-input.v2.schema.json) |
| Fixed-plan import | [accepted forms](fixed-editorial-input.current.schema.json), [v2](fixed-editorial-input.v2.schema.json) |
| First-generation inputs | [shared v1](editorial-input.v1.schema.json), [example](editorial-input.v1.example.json), [fixed v1](fixed-editorial-input.v1.schema.json) |
| Planning model responses | [plan](planning-output.current.schema.json), [plan review](plan-review-output.current.schema.json) |

[Reusable cases](../../evals/editorial-inputs/) contain matching `shared.json` and `fixed-plan.json` files. A fixed plan includes the canonical digest of its shared input. The shared packet has no dependency on the fixed plan. The active run freezes its inputs, including their exact source references and bytes.

## Source admission

For each `source_path`, supply an authorized runtime record or source representation in `source-map.json`. Staged documents must come from the same import scope. A primary source must be eligible for generation. The importer validates quoted text and exact half-open Unicode code-point offsets `[start,end)` against the underlying document. It also checks the scope and digest when binding a fixed plan.

The [writing walkthrough](../WRITING-EVALUATION.md) shows how to import the bundled GPS dossier. For a new case, `editorial-source-import --file FILE --source-path PATH --scope SCOPE --purpose primary --out REF.json` registers one file. Use the same scope for related files and assign their actual purposes (`primary`, `brief`, `dossier` or `fixed_plan`). Map the packet's paths to the resulting references before running `editorial-import` or `fixed-editorial-import`.

In v2, `card_id` stays stable across display-name changes. `narrative_excerpt_indices` optionally selects narrative passages from a card's complete `excerpts` list. Entries are unique, nonnegative, zero-based indices. Omission or an empty list selects no narrative excerpts; `null` and out-of-range indices are invalid. For production author requests pinned to `selected-card-excerpts/1`, selected cards supply **all** their stored source excerpts for drafting and revision. Planning roles keep their own selection. See [card identity](card-identity.md).

## Preview inputs

`writing_cli preview` builds a role request locally. Use the common `--run-id`, `--case`, `--profile`, `--shared-ref` and `--out` options from the [walkthrough](../WRITING-EVALUATION.md). Supply contract objects in JSON files without an archive's outer `output` wrapper.

| Stage | Additional input |
| --- | --- |
| `plan` | The shared dossier |
| `plan-review` or `plan-review-revised` | `--candidate` |
| `plan-revise` or `plan-target-repair` | `--candidate` and `--plan-review` |
| `draft` | Exactly one of `--approved-plan` and `--fixed-ref` |
| `reader-review` or `fact-review` | `--draft` |
| `revise-1` or `revise-2` | `--draft`, `--reader-feedback` and `--fact-feedback`; a supplied `--composition` can carry forward the current narrative choices |
| Historical informed `review` or `revise` | One source of the plan and `--draft`; revision also requires `--feedback` |
| `anchor-repair` | `--draft` and `--feedback` |

Preview preserves the full request and reports its size. A live run also accounts for provider counts, output reservations and its pinned per-stage budget. Published limits and recovery options are in the [budget guide](../../docs/podcast-production/input-budget-recovery.md).

## Model-facing views and replay

The run manifest records its projection, role-specific schema versions, prompts and input bindings. A saved run keeps these versions on resume. Older manifests without `role_projection_version` use their original `/1` projection. Unsupported versions fail before a provider call. The submitted response and its validated, persistent representation remain available separately.

For the author, the accepted plan supplies the main question, listener promise, route and contributions expected of each speaker. For reviewers, the packet depends on the reading task and its pinned schema. See [independent readings](independent-readings.md), [addressed revisions](addressed-script-revision.md), [the v7 script handoff](../../docs/podcast-production/08-editorial-inputs.md#v7-audio-handoff) and [editorial choices and reference examples](../../docs/podcast-production/08-editorial-inputs.md#editorial-choices-and-reference-examples).
