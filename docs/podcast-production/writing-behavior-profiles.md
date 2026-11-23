# How a writing run works

A writing run starts with an assignment, a dossier and four model-profile references. The behavior profile determines the instructions, schemas and handoffs. `production/1` is the default for the ordinary writing CLI. Model references determine which provider serves each role. The Snow evaluation used Sol for planning, Opus for writing and Terra for the reviews. [Writing CLI](../../backend/WRITING-EVALUATION.md)

| Stage | Input and decision |
| --- | --- |
| Plan | The planner chooses the story, its scope, scene progression and ending. The plan editor reads the plan for orientation, development and resolution. A requested revision returns to the planner and the editor reads the new version. |
| Draft | The author receives the accepted story, scene order, ending, host roles, selected material and the planner's choices about what to leave out. The author returns a complete script. |
| Read and verify | The reader follows the spoken text in order. A separate fact reviewer checks claims against evidence in context. The reader marks both ends of a passage that needs moving or shortening. Each reviewer records its own decision. |
| Rewrite | The writer receives the current script and the required work, including the location, defect, requested change and meaning to preserve. A local edit receives the editorial direction needed for that passage. A whole-script rebuild also receives the detailed plan. The archive keeps each full review and the writer's change report. Both reviewers read the revised script. |
| Finish | Acceptance requires both reviews of the same script. When the rewrite limit is exhausted, the export contains the last script, reviews and stop reason. The production writing cycle allows two rewrites. |

## Evidence and review

The fact reviewer checks the claim as heard in its scene. It can inspect retained passages, source metadata and signatures attached to the quoted passage. Research notes about the limits of the source remain distinct. A required factual assignment identifies the listener-facing claim, the discrepancy and its consequence for the story.

The draft author, rewrite author and fact reviewer can search and read sources pinned to the episode. Source readings have exact text ranges, so a reviewer can read a neighboring paragraph or identify a signatory. The search contract depends on the manifest pin. The runtime verifies evidence receipts against that review's own trace. For a required factual correction the writer receives the passage, source title, range and assignment reference. Shared evidence travels once. [Reading tools](source-search.md) · [Reading audit](reading-audit-contract.md)

## Reproducibility and cache

A new run records the behavior profile, exact instructions, schemas, source inputs and model profiles in its manifest. Saved preview and resume use those pins. The research CLI exposes historical behavior profiles for controlled comparisons.

The adapter places reusable material at the beginning of the request. For rewrites this can include the plan's source passages and, for a whole-script rebuild, the accepted plan. The current script, required edits and newly supplied evidence follow. Drafting uses a different instruction and schema. The review roles have their own prefixes. Provider cache reuse depends on the role, request, provider and timing. Inspect cache creation, cache reads and ordinary input in the actual usage record.
