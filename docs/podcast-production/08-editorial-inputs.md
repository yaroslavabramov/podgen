# Inputs for writing and review

The `editorial-role-packets/1` contract describes how Podgen compiles role-specific tasks from complete saved material. The archive retains the full inputs and exact model request. The role sees the information needed for its decision. [Writing behavior profiles](writing-behavior-profiles.md) describe version selection. [Research tools](research-materials-runtime.md) describe the hosted-report path.

## Inputs and identity

An evaluation case may supply a prepared dossier with evidence cards. Its persistent card identifiers survive reordering and renaming. An execution can offer shorter aliases and resolve them back to the exact stored identity. The hosted-research path supplies a report in sections, a catalog of sources and tools for reading retained passages. Anecdotes, examples and prohibitions in an evaluation fixture are properties of that fixture. Production should derive the assignment from the user’s request.

Each projection pins the language, format, duration and speaker configuration. Source quotes and identifiers keep their original spelling. The backend returns an explicit error when a request exceeds a pinned limit. It must not discard the end of a source or a speech field to make the request fit.

## Role contracts

| Role | Input and decision |
| --- | --- |
| Planner | Receives the brief and admitted research, selects relevant material, builds the opening, route, acts and ending, or requests material on a concrete gap. |
| Plan editor | Reads the candidate as a complete story. Refers to specific plan sections when asking for a change. Acceptance applies to that candidate. |
| Writer | Receives the accepted plan and selected evidence. Writes the complete spoken script. An editorial suggestion identifies a problem; a supplied source supports a factual assertion. |
| Listener-oriented reader | Reads the speech in order, with the listening target and estimated duration. Locates an actual breakdown in orientation, development, language or closure. |
| Fact-checker | Checks consequential statements in context against retained evidence. A source receipt identifies what was delivered. The checker still judges the passage's scope. |
| Reviser | Receives the previous script, the assigned changes and the source support needed to carry them out. Can resolve overlapping tasks through a single coherent edit. |
| Recheckers | Inspect the actual result and consequences for nearby and later speech. Both recommendations bind to the same version. |

A no-call preview uses the same request compiler as the normal stage. Compare the complete instructions, role input, schema and tools with the last verified behavior when changing an agent. Inputs to an archived run use the saved behavior and source selections.

## V7 audio handoff

<a id="передача-сценария-в-v7"></a>

A native `keep` from both readers can freeze the current script as the writing outcome. A `revise` with allowance remaining commissions another edit. A material request routes through the relevant research handler. Exhaustion, refusal and an unvalidated response remain distinct outcomes.

The synthesis stage receives the exact final script and speaker bindings through the saved outcome. It checks authorization at the external action and verifies the script hash used by the voice and assembler. Read the [outcome and audio descriptors](validated-outcome-and-audio-descriptors.md) for the validation and recovery boundaries.

## Editorial choices and reference examples

<a id="авторские-решения-и-справочные-образцы"></a>

A story can use an illustrative example or a clearly framed reconstruction. It must preserve the factual account, and the listener must understand whose perspective or knowledge it represents when that matters. A sample passage can demonstrate timing or a particular editorial choice. It must not supply compulsory facts or fixed scene directions for unrelated episodes.

The [editorial practice guide](03-editorial-practice.md) gives worked examples. [Independent reading schemas](../../backend/schemas/independent-readings.md) and the [reading audit](reading-audit-contract.md) cover the structured replies.
