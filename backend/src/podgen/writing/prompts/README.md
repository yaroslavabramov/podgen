# Whole-episode baseline A, instructions v1

Public API: `podgen.writing.roles.role_instructions(stage, format)`. Stages are
`draft`, `review`, `revise`; formats are `solo`, `dialogue`. The returned text is
the complete common + format + stage instructions, headed by
`ROLE_INSTRUCTIONS_VERSION`. UTF-8 assets are read with `importlib.resources`
from the installed package, independently of the working directory.

These instructions contain no episode data. The caller supplies complete pinned
brief, dossier and plan as separate inputs to drafting; the complete draft is
preserved before review. It creates a fresh review invocation, rather than
reusing the author's conversation or positive self-assessment. A reviewer who
already sees the plan/dossier records `reading_mode=informed`. A prompt alone
cannot implement blind or prefix-isolated first reading: the caller must enforce
input/tool access and save each observation before revealing the next phase.

Parse writing responses with `WriteOutput` and review responses with
`EditorialFeedback`. Review anchors address zero-based utterance positions and
exact text selections in the supplied draft. Call `feedback.validate_against(draft)`
before passing feedback to the reviser. The caller also validates the assigned
speaker identifiers against the actual episode inputs. Revision receives all
original inputs, the complete draft and the full feedback, and returns another
complete `WriteOutput`, including unchanged utterances. Old ordinal anchors do
not automatically bind to the new version.

The wire models do not replace canonical ScriptRevision, ResearchRequest,
EditorialReview or AcceptanceRecord. The caller binds outputs to its actual
artifacts, archives exact prompts/inputs/responses and model settings, handles
capacity/refusal/incomplete outcomes, and routes missing material to research.
An incomplete provider response is a failed attempt, not a complete short script.
Editorial recommendations do not automatically grant factual/audio acceptance.
Human listening and measured audio duration require real supporting artifacts.

Publish instruction changes in a new version directory and update the version
identifier/resolver together; do not silently replace a published version.
Qualify changes through real product runs on common inputs. Unit/schema checks
and installed-package checks establish contract integrity, not writing quality.
