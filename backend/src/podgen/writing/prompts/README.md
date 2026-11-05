# Writing instructions

## Edit one component

Edit the relevant UTF-8 Markdown file in `current/`:

| File | Applies to |
| --- | --- |
| `common.md` | Every stage and format |
| `solo.md`, `dialogue.md` | All stages of that format |
| `draft.md`, `review.md`, `revise.md` | That stage in both formats |
| `plan.md` | Initial own-plan proposal from the brief, dossier and permitted corpus |
| `plan-review.md` | Independent review of the initial or revised planning candidate |
| `plan-revise.md` | Complete replacement proposal after the plan editor requests revision |
| `anchor-repair.md` | The additional call that repairs review citation anchors |

For example, change `current/review.md` to change the editor's instructions.
That text edit is sufficient: do not copy a version directory, register a version,
change code, or regenerate unrelated files or test hashes. Existing runs keep
their saved instructions; the change applies to new runs. An installed deployment
must receive the updated package before it uses the new text.

Write system instructions in English. The user payload's explicit
`output_language` controls spoken words and other human-readable response fields,
including feedback. Preserve evidence quotes, anchor quotes, speaker identifiers,
schema keys and enum values verbatim. Episode material and evaluator answers do
not belong in these instruction files.

Responsibilities:

- The editor owns instruction wording and editorial assessment.
- The developer owns loading, storage, request assembly, compatibility and tests.
- The coordinating chat owns task assignment and acceptance of the result;
  it routes text changes to the editor and implementation changes to the developer.

## Assembly and saved runs

`podgen.writing.roles.role_instructions(stage, format)` returns the complete
current instructions: `common.md`, the format file, then the stage file. Stages
are `draft`, `review`, `revise`; formats are `solo`, `dialogue`. Resources load
from the installed package with `importlib.resources`, independently of the
working directory.

Each assembly gets a `whole-episode-a/sha256:...` content ID from the stage,
format, ordered component names and exact text. The generated header is outside
the hash input. Editing `review.md` changes only review assemblies; editing
`dialogue.md` changes only dialogue assemblies. There is no shared version number
to maintain.

For a new run, `load_instruction_set(format)` reads the applicable components
once and builds all three stages. The runner saves each complete body in a
`PromptDefinition`, with references and per-stage IDs in `manifest.json`
(`prompt_refs`, `role_instructions_versions`). Resume uses these saved bodies,
not `current/` or Git.

Own-plan runs use `load_instruction_set(format, include_planning=True)`, which
also assembles `plan`, `plan-review` and `plan-revise` from the same common and
format components plus their individual files. The two-argument
`role_instructions` API remains for `draft`, `review` and `revise` only.
Editing a planning file changes only that instruction assembly in new runs.

The repair appendix is pinned separately in the manifest's
`anchor_repair_instructions`; editing it affects repair calls only.

## Which plan and review apply

`fixed-plan` reads the supplied `author/<case>/plan.md` and runs
`draft -> review -> revise`; it does not invoke the new planning roles.
`own-plan` reads the brief, dossier and permitted source corpus, without the
supplied plan or evaluator answers. It starts with `plan`; an unblocked candidate
then goes to `plan-review`. The editor reviews that exact saved candidate against
the materials.

An `accept` permits writing with that candidate. If the decision is `revise`
and the revision budget allows it, `plan-revise` returns a complete new candidate.
The separate `plan-review-revised` call checks this new version using the same
`plan-review.md` body and content ID pinned at run creation. It receives the new
candidate's exact reference and hash; the previous review does not approve it.
The current runner permits at most one plan revision. A further required revision
stops the run rather than starting the writer.

All instruction bodies, including the optional revision and second review, are
pinned before the first call. Editing `current/` during a run changes none of
them. Every review is bound to the candidate it actually received.
Any blocking research request from the planner or plan editor is preserved and
prevents `draft`. The researcher is not implemented; recording a question does
not answer it or launch another agent. `PlanningOutput` can represent this as
`status=needs_research`, with `plan=null` when no meaningful plan is supported.
An explainable uncertainty alone does not require a blocker. Only a complete
`status=ready` proposal with an explicit editorial `accept` can reach the writer.
This planning decision is not independent factual or production approval.

## Read saved instructions

To see instructions actually used in an exported attempt, read its request:

```sh
python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["instructions"], end="")' \
  /path/to/export/archive/review/request.json
```

Use the relevant stage and, for an additional attempt, its
`archive/<stage>/attempt-N/request.json`. See `run.json` for the attempts.
The request also preserves the actual user payload, including `output_language`
for runs created with the explicit language contract. For a stage not yet invoked,
its pinned body is in the `PromptDefinition` referenced by the manifest.

The frozen directories `v1/`, `v2/`, `v3/` remain readable through
`role_instructions_for_version(stage, format, version="whole-episode-a/2")`.
V1 and v2 are Russian; v3 is English. Do not edit these historical files or raw
episode outputs. The resolver supports these numbered versions only: an old
content hash is not a globally retrievable snapshot. Read that run's saved body;
unknown versions fail explicitly.

## Runtime and output contracts

The caller supplies full pinned materials, preserves the complete draft, invokes
review separately, validates `EditorialFeedback` anchors against that exact draft,
and passes full feedback to revision. Anchors use zero-based utterance positions
and exact quotes; they do not automatically transfer to the revised text.
`WriteOutput` and `EditorialFeedback` schemas are unchanged.
Planning uses `PlanningOutput` and `PlanReviewOutput` from `planning_roles.py`;
review targets are JSON Pointers into the exact candidate, and material references
must match the supplied permitted catalog. These wire outputs are not canonical
production approvals.

Access isolation, blind-reading phases and complete request/response archives
are runtime responsibilities. A reviewer who sees the plan or dossier reports
`reading_mode=informed`. For missing material, the script editor can return
`research_requests` and recommend `needs_material`; this does not invoke a
research agent automatically. Incomplete provider output is a failed attempt.
Schema checks establish contract integrity, not writing quality, independent
factual approval, measured duration or human listening. Those claims require
their own evidence.
