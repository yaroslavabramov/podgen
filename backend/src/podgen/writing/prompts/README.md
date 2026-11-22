# Working writing instructions

**Edit `current/` only for the active production behavior.** Runtime and fresh
preview use the same named Markdown composition. Each role has one readable
file; related source variants remain conditional sections in that file.

| Role | File |
| --- | --- |
| Story planner / planning revision | `planner.md` / `plan-revision.md` |
| Plan editor | `plan-editor.md` |
| Author / independent-reading revision | `author.md` / `author-revision.md` |
| Listener / factual reading | `listener-review.md` / `fact-review.md` |
| Repeated listener / factual reading | `revision-verification.md` / `fact-verification.md` |
| Single-editor historical workflow | `script-editor.md` / `script-revision.md` |
| Script / plan address repair | `anchor-repair.md` / `plan-target-repair.md` |

`common.md`, `solo.md`, `dialogue.md` and the three substantial research blocks
are shared only where explicitly included. `durable-tools.md` owns operational
instructions, including the conditional planning selection and factual research
paragraphs. `research-navigation.md` explains retained investigations and search
history. `research-continuation.md` owns checkpoint/deferred-exchange and
material-return messages; new research manifests pin this bundle and old manifests
read its frozen v1 counterpart. `source-tools.md` is the retained-source-only operational variant.
`style-reference-ru.md` supplies the author's separate Russian craft reference.
System instructions remain English; `output_language` selects generated language.

## Composition

The composer preserves UTF-8 text, whitespace and CRLF bytes. No English sentence
is found or replaced in Python. Its only syntax is:

- `{{include:common.md}}` and `{{include:{format}.md}}` explicitly include blocks.
- `{{if sources=research}}…{{elif sources=sources}}…{{else}}…{{endif}}` selects a
  source variant. `cards`, `research` and `sources` are explicit caller inputs.
- `{{section task}}…{{endsection}}` defines a named block within a role file.
  `{{include:planner.md#task}}` reuses its task without adding its reading suffix.
- `|rstrip` is an explicit whitespace filter on an include, used to preserve the
  published source-only framing. Boolean conditions select `anchored` and
  `planning_selection` in the operational block.

Unknown directives/conditions, missing sections, invalid paths and cycles fail
explicitly. There is no process-wide prompt cache. A new manifest or fresh preview snapshots
local Markdown once for that bounded assembly; nested includes reuse its bytes.
A later assembly observes a new edit.

`load_role(role, format, sources=...)` returns the complete semantic role body.
The ordinary manifest and preview choose this context from admitted materials,
then attach only the operational blocks selected by their tool contract.
`load_instruction_set`, `role_instructions` and `load_reading_instructions`
inspect those same active bodies. A fresh manifest hashes the **complete final
instruction text**, including operational blocks, into its prompt version.
A new research manifest additionally pins exactly the initial `fact-review`
source-only variant in `fact_source_instruction_variants`, bound to the complete
original instruction fingerprint. Report-off preparation and its ordinary request
compiler use this saved pair even after later edits. No unused recheck variants
are persisted. Older manifests without this field use their frozen fingerprint
adapter; they never fall back to reading current text.

## Saved runs and history

Admission pins the complete instruction text, schema, craft reference and prompt
identity. Saved preview/resume read the manifest and saved definitions; they do
not reconstruct prompts from `current/`, Git or a release hash. Editing a file
never migrates a saved run. Model-facing instructions contain no generated hash
header or file paths.

`history/profiles/` and named files in `history/releases/` preserve previous
behavior profiles. `history/numbered/` preserves published numbered APIs with
unchanged universal-newline semantics. Tests' prior comparison bodies and unshipped samples live
under `backend/tests/fixtures/writing-instructions/` with descriptive names;
unreferenced drafts remain available in Git, not the runtime package.
Unused opaque hash-named files are removed.

Historical research experiments use a frozen fingerprint lookup in
`history/research-adapters.json` or `history/source-adapters.json`. An unknown
archived body is rejected; it is never interpreted by replacing English phrases
or searching candidate current role texts. Active source variants use the
explicit role/format/source context instead.

Wheel builds include both current Markdown and required historical resources.
Use the offline writing preview to inspect the **complete** final request before
running a provider. Validate changes against full instructions and actual stage
payloads; schema success alone establishes no editorial or factual acceptance.
