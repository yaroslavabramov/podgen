# Phase 6 Reality Check And Repair Plan

Date: 2026-03-22
Scope: `screenwriter` -> `dialogue_audit` (`writer_revolt`) -> `citation_audit` / `citation_repair`
Run used as primary evidence: `fccc9c33-fd78-4da7-90ac-bf40c0177936`

## Goal

Stop treating Phase 6 as "mostly solved except polish."

The current state is:

- `screenwriter` now produces a good content draft
- `dialogue_audit` diagnoses the right problems
- `citation_audit` mostly works as a rigor guardrail
- but the actual editor layers are still too weak and too inconsistent

The result is a script that is structurally good, but still has:

- boundary-level scene echo
- repetitive agreement / turn-taking patterns
- too little downstream dialogue polishing
- citation repairs that sometimes improve rigor while weakening drama
- re-audit noise and metadata loss in `scene_repair`

This memo defines what is actually broken, what is not broken, and what should be fixed next.

## Non-Negotiable Constraint

Phase 6 fixes must remain:

- universal
- multilingual
- portable across very different source corpora

That means:

- no system-level phrase dictionaries
- no hardcoded language-specific wordlists
- no case-specific hacks for one episode or one corpus

User/project-level controls such as `forbidden_phrases` in ShowBible are still valid.
But Phase 6 system behavior should be driven by:

- structured metadata
- prompt contracts
- issue taxonomy
- repair strategy
- acceptance logic

If a proposed fix depends on recognizing a small hand-built list of phrases in one language, treat it as the wrong abstraction by default.

## What Is Actually True Now

### 1. Planning is no longer the main bottleneck

The old planning problems were mostly upstream:

- cross-act duplication
- chronology wobble
- act-internal mechanism overlap

Those are no longer the dominant failure mode.

Current failures are mostly Phase 6 failures:

- writer surface/form
- scene boundary handling
- weak downstream polishing
- citation repair overreach / acceptance noise

### 2. `screenwriter` is good on content, weak on distribution

The latest writer draft is strong in macro sense:

- Act 1 is coherent and dramatically legible
- Act 2 explains the mechanism clearly
- Act 3 is the strongest act
- Act 4 lands the political framing well

But formal distribution is still weak:

- `scene_opening_echo` appears in most scenes
- `A` starts too many scenes
- `A` ends too many scenes
- `ABAB`-style rhythm still dominates
- `именно / то есть / подожди` remain episode-level tics

This is not a "writer is bad at facts" problem.
This is a "writer keeps choosing the same conversational move" problem.

### 3. `dialogue_audit` is a good detector, not a strong editor

Observed facts from the latest run:

- dialogue issues were found in `24/24` scenes
- polish was applied only to `4` scenes
- rewrite was requested for `0` scenes

So the current system already knows where the problems are.
It simply does not intervene enough.

### 4. `citation_audit` is no longer the main thing breaking the run

Current state:

- blockers are gone
- the step now mostly behaves like a quality guardrail
- many failures are non-blocking medium rigor issues

This is progress.

But `citation_repair` still has two real failure modes:

- it can weaken scene voice / drama
- `scene_repair` can drop line metadata (`emotion`, `ssml_hints`)

## Why It Feels Like "We Fixed It And It Came Back"

Because there are two different problems:

### Problem A: old structural duplication

This was the earlier issue:

- same meaning repeated across acts or adjacent planning beats

This was reduced mainly by `showrunner` and `act_planner`.

### Problem B: current boundary echo

This is the current issue:

- scene N does not fully duplicate scene N-1
- but it opens by paraphrasing the prior punchline instead of making a new move

So yes: the old overlap was fixed enough that a new, narrower problem became visible.

That is why it feels like regression when it is actually a change in failure mode.

## Current Phase 6 Problems

## P0. `writer_revolt` budget semantics are wrong in practice

### What is happening

`writer_revolt` uses a single `revolt_count` while comparing it to `max_writer_revolt_per_act`.

In practice this behaves like an episode-global cap, not a real per-act cap.

So:

- early scenes may get polished
- later scenes still get findings
- but no actual intervention happens

### Why this matters

This is a concrete reason why:

- the audit correctly flags `scene_opening_echo`
- but the final script still contains it almost everywhere

### Fix

Make polish budgeting truly local:

- track `revolt_count` per act, not per run
- optionally also track a smaller per-scene cap as now

### Expected outcome

More scenes in later acts actually receive `scene_polish`, especially:

- `A2S6`
- `A3S2`
- `A3S4`
- `A4S6`

## P1. Question handling in `citation_audit` is still wasting repair budget

### What is happening

The current question handling is safer than before, but still too narrow to be efficient.

The present `pure question` logic still leans on language-shaped heuristics.

That avoids unsafe fail-open behavior, but it leaves many harmless interrogative lines still flowing into:

- `missing_claim_link`
- `repair`
- unnecessary citation repair attempts

### Why this matters

This wastes expensive repair budget on lines that are conversationally doing one of these jobs:

- asking for explanation
- asking for consequence
- opening the next beat

The system should not treat all questions equally.

### Fix

Do **not** broadly exempt all questions.

Instead, split the policy by issue type and question function.

The first cheap step should happen inside the existing `citation_audit` call, not as a second classifier pass.

Extend the audit JSON contract with a structured field such as:

- `action_required: true|false`
- or `line_function: assertive|question_open|question_with_premise|recap|transition`

Then let post-filter use that structured signal from the same Haiku response.

This is preferable to:

- adding another LLM call
- parsing natural-language `suggestion` text like `no action required`
- keeping hand-built language heuristics for question prefixes

After that, keep the policy split by issue type and question function:

- For `missing_claim_link` on questions:
  - default to `record`, not `repair`, **unless** the question itself carries a concrete unsupported premise
- For `unverified_stated_as_fact` on questions:
  - keep actionable behavior when the question embeds a factual claim or recap premise

Operationally:

- pure/open informational questions should not trigger repair work
- loaded, recap, or confirmation questions should remain actionable

Implementation constraint:

- do not solve this with hardcoded Russian or English prefixes
- do not keep growing `_question_is_pure_request(...)` into a multilingual phrase dictionary
- do not parse free-form `suggestion` strings for control flow
- replace phrase-shape heuristics with a language-agnostic classifier contract:
  - does this line assert a concrete premise?
  - does this line recap a factual proposition?
  - is this line only requesting explanation / consequence / continuation?

### Important constraint

Do not go back to a broad question exemption.
We already know that loaded factual questions can fail open if this is too permissive.

## P2. `dialogue_audit` sees boundary problems but only with a tiny window

### What is happening

Current audit sees:

- the current scene
- `previous_scene.last_line`
- `previous_scene.last_lines`

This is enough to detect local opener echo.

It is not enough to detect:

- act-level repetition patterns
- episode-level overuse of one discourse move
- repeated speaker distribution across many scenes
- repeated openers / confirmations that are harmless locally but bad in aggregate
- repeated marker families that signal the same discourse move over and over

### Why this matters

The current defects are now largely distribution problems:

- too many scenes start with `A`
- too many scenes end with `A`
- too many scenes use the same confirm -> explain pattern
- the same agreement / interruption / reset markers keep resurfacing across an act

No scene-local audit will reliably catch those in aggregate.

### Fix

Add a new cheap pass after `screenwriter` and before or inside `writer_revolt`:

- `act_dialogue_audit`

This is also where marker-family repetition reporting should live.
Do **not** implement P4 as a second separate pass.
P4 is the repetition-reporting responsibility of this same `act_dialogue_audit`.

Inputs:

- all scenes of one act
- first 1-2 lines and last 1-2 lines per scene
- speaker pattern per scene
- opener shapes per scene
- full text optionally compressed

Outputs:

- scenes with repeated opener strategy
- scenes with repetitive agreement shape
- scenes with distribution imbalance
- marker-family overuse reports, for example:
  - explicit confirmation markers
  - interruption-first questions
  - explanation-reset markers
- scenes that should be polished first

The first version should explicitly surface which scenes are contributing most to:

- overused confirmation-first answers
- overused interruption-first questions
- overused explanatory resets

This is how repeated words get addressed without turning the system into a blacklist scrubber.

This should be an act-level prioritizer, not a full rewrite stage.

### Important constraint

Do not add an expensive full-scene multi-pass rewrite here.
The first version should only:

- rank scenes
- raise or lower polish priority

## P3. `screenwriter` anti-echo rules exist, but are too soft

### What is happening

The prompt already says:

- continue from `previous_scene.last_line`
- do not paraphrase previous scene's proposition
- open with consequence / contradiction / next question

But the model still often chooses the old safe move:

- explicit confirmation
- soft restatement
- then explanation

### Why this matters

This is the main reason `scene_opening_echo` remains the top issue type.

### Fix

Strengthen the contract with more operational guidance and examples:

- if previous scene ended with a conclusion, new scene must start with one of:
  - objection
  - consequence
  - narrower factual question
  - new pressure
  - cost
- not with:
  - confirmation
  - paraphrase
  - summary restatement

Add 3-4 contrastive micro-examples for:

- bad opener = restates prior landing
- good opener = starts from what changes next

### Note

This is still a prompt change, not a sufficient fix by itself.
It must be paired with stronger downstream polish.

## P3a. `language` is not propagated consistently through all Phase 6 editors

### What is happening

`citation_audit` and `citation_repair` already receive `language`.

`dialogue_audit` and `dialogue_polish` currently do not.

That creates an inconsistent contract:

- some Phase 6 components are explicitly language-aware
- other Phase 6 components are implicitly guessing style/register from the text alone

### Why this matters

For a universal multilingual system, language awareness should not exist only in the citation branch.

If `dialogue_audit` and `dialogue_polish` are expected to judge:

- opener naturalness
- spoken rhythm
- agreement/challenge variety
- conversational register

then they should receive `language` explicitly too.

Otherwise the system is mixing:

- explicit language-aware repair in one branch
- implicit language inference in another branch

### Fix

Propagate `language` through the full Phase 6 writing stack:

- `screenwriter`
- `dialogue_audit`
- `dialogue_polish`
- `citation_audit`
- `citation_repair`

And make the prompt contract explicit where relevant:

- write / judge in the same language as the dialogue
- preserve natural spoken register for that language
- avoid translated-from-English phrasing

### Important constraint

Do not solve this with language-specific style examples per locale.
The contract should stay abstract and portable.

## P4. Repeating words are symptoms of repeating discourse moves

### What is happening

Words like:

- `именно`
- `то есть`
- `подожди`

keep surviving because the system is not really editing enough scenes.

Current audits do not fail because they "cannot see the words."
They fail because:

- they detect discourse-shape issues abstractly
- but then polish too few scenes
- and they do not yet emit an explicit act-level repetition signal that says:
  - this agreement family is overused
  - this interruption family is overused
  - these specific scenes are causing most of it

### Why this matters

If we attack the words directly, the model will substitute another marker and preserve the same rhythm.

### Fix

Keep the abstraction level correct:

- agreement shape
- challenge shape
- opener function
- turn rhythm

But make repetition explicit inside `act_dialogue_audit` from P2, not in a second parallel mechanism.

That same act-level audit should emit a repetition report that tracks:

- confirmation-first replies that reuse the same marker family
- interruption-first questions that reuse the same marker family
- explanatory resets that keep using the same verbal habit

The words themselves are not the target.
They are a measurement surface for repeated conversational moves.

But increase actual rewrite coverage and add stronger acceptance for polish:

- if a scene was flagged for `repetitive_agreement_shape`, accepted polish should reduce that shape materially
- if a scene was flagged for `scene_opening_echo`, accepted polish should materially change the opener move
- if an act-level report says one marker family dominates an act, those scenes should be raised in polish priority instead of being treated as twenty unrelated local quirks

### How this should work in practice

The system should not ask:

- "ban `именно`?"

It should ask:

- "why are so many host answers still starting with explicit confirmation?"
- "why are so many challenger turns still opening with interruption?"
- "which scenes are reinforcing that distribution pattern?"

That gives the editor layer something concrete to act on without playing word whack-a-mole.

### Important implementation constraint

Do not build:

- one act-level pass for opener/distribution problems
- and another act-level pass for repeated marker families

That would duplicate cost and split ownership.

Build one `act_dialogue_audit` that outputs both:

- boundary/distribution findings
- marker-family repetition findings

### What not to do

Do not add a big blacklist of words.

## P5. `dialogue_polish` acceptance is too weakly tied to the issue it was supposed to fix

### What is happening

A scene can be polished, accepted, and still keep the core issue shape.

The system currently treats polish mostly as:

- valid JSON
- preserved claims
- no obvious regressions

It does not strongly ask:

- did opener echo actually go away?
- did the turn rhythm actually diversify?
- did the agreement shape actually change?

### Fix

Start with one cheap, high-confidence acceptance guard:

- if a scene was flagged for `scene_opening_echo`, accepted polish must materially change the opener move

Do **not** begin by trying to deterministically score:

- `repetitive_agreement_shape`
- `rigid_turn_taking`

Those are harder to evaluate cheaply and reliably.

## P6. `citation_repair` should not be trusted equally on all line types

### What is happening

`citation_repair` is currently acceptable on:

- local claim-link fixes
- local number/date/attribution fixes

It is not trustworthy enough on:

- handoff lines
- scene endings
- scene-shaping bridge lines
- high-drama interpretive lines

Example: a rigor-safe fix can still flatten a strong transition.

### Fix

Introduce line role awareness for citation repair:

- `bridge`
- `landing`
- `scene opener`
- `punchline`
- `setup question`

For these line roles:

- prefer line repair only
- avoid scene repair by default
- require stronger acceptance before replacing the final line

### Practical heuristic

If the flagged line is:

- the first line
- the last line
- or one of the last two lines

then require a stricter repair mode / acceptance path.

## P7. `scene_repair` loses metadata

### What is happening

`line_repair` uses metadata-preserving splice.

`scene_repair` returns normalized dialogue and drops:

- `emotion`
- `ssml_hints`

This is already visible in final DB state for accepted `scene_repair` scenes.

### Why this matters

Even when the text is acceptable, audio/rendering cues are silently degraded.

### Fix

Add metadata-aware merge for `scene_repair`:

- if a repaired line is textually identical to an original line, preserve original metadata wholesale
- if only `claim_ids` changed, preserve `emotion` and `ssml_hints`
- if text changed materially, allow regenerated line fields

Do not blindly preserve metadata for fully changed lines.

## P8. `citation_repair` acceptance is still too dependent on noisy re-audit

### What is happening

The repair quality itself improved.
But many repairs are still rejected because re-audit:

- finds new medium issues
- misreads added claim links
- escalates scope on untouched material

### Why this matters

This creates wasted spend and makes the system look less capable than it is.

### Fix

For non-blocking repairs:

- do not require full-scene re-audit stability
- accept if:
  - original actionable issue is gone
  - no new blockers appear
  - claims/line-role constraints hold

Also address the current hedge paradox explicitly:

- do not let repair invent unsupported source framing
- only use explicit source-attribution rhetoric when that attribution is actually supported by `available_claims`
- otherwise prefer:
  - claim reuse
  - local narrowing
  - short language-native uncertainty that matches support level

Implementation constraint:

- do not solve this with a banned phrase list per language
- enrich `available_claims` with attribution/source-support metadata
- make the contract semantic:
  - supported source framing
  - unsupported source framing
  - confidence level allowed by support
- acceptance should check changed lines for unsupported source-framing moves against claim metadata, not against a hand-built lexicon

For blocking repairs:

- keep a stricter re-check
- but bias it toward original blockers and changed lines

### Long-term direction

Move from full-scene re-audit toward:

- changed-line re-check
- blocker-focused re-check

## P9. We do not have an immutable writer baseline

### What is happening

Accepted repairs overwrite `script_segments.dialogue_json`.

A rerun then audits the already-repaired text, not the original writer draft.

### Why this matters

This makes diagnosis harder:

- some runs are being judged on repaired output
- not on the original writer behavior

### Fix

Persist a baseline:

- `writer_original_dialogue_json`

Then:

- `dialogue_audit` can compare current vs original
- `citation_audit` can compare current vs original
- manual recovery can restore baseline if a repair degraded the scene

## Priority Order

## P0: Do immediately

1. Fix `writer_revolt` budget semantics so polish budget is truly per act
2. Fix `scene_repair` metadata merge
3. Fix question handling so harmless interrogatives do not keep generating unnecessary `missing_claim_link -> repair` work

## P1: Next

4. Strengthen `screenwriter` anti-echo with contrastive examples
5. Make polish acceptance issue-aware for `scene_opening_echo`
6. Reduce dependence on noisy full re-audit for non-blocking citation repairs and fix hedge paradox in repair prompts
7. Propagate `language` consistently through all Phase 6 audit/polish payloads and prompts

## P2: After that

8. Add act-level `dialogue_audit` prioritizer with explicit repetition-distribution reporting, only if P0/P1 still leave strong distribution problems
9. Add line-role sensitivity to citation repair
10. Persist immutable writer baseline

## Success Criteria

The next meaningful success target is not "everything passed."
It is this:

- `scene_opening_echo` materially down from `22`
- more than `4` scenes polished when `24/24` are flagged
- `A` no longer starts ~75% of scenes
- `именно / то есть / подожди` materially down without blacklists
- no single confirmation / interruption / explanatory-reset family dominates an act
- repeated-marker scenes are being actively prioritized for polish instead of merely reported
- accepted `citation_repair` no longer strips metadata
- no accepted citation repair that obviously weakens a scene handoff
- all Phase 6 audit/polish prompts receive `language` explicitly instead of relying on implicit inference
- Phase 6 logic no longer depends on system-level language-specific phrase lists for question handling, recap detection, or repair acceptance

## What Not To Do

- Do not go back to planner-first fixes for these symptoms
- Do not use `citation_audit` to solve stylistic repetition
- Do not introduce large banned-word lists
- Do not reintroduce system-level language dictionaries through helper heuristics or prompt-side phrase lists
- Do not add another expensive pass before fixing the existing editor layers
- Do not trust scene-level local audit alone to solve act-level distribution problems

## Bottom Line

The system is no longer failing because it cannot write a coherent episode.

It is failing because:

- the writer draft still overuses a few conversational patterns
- the detector layers know that
- but the editor layers intervene too little and too inconsistently

That is the real next frontier.
