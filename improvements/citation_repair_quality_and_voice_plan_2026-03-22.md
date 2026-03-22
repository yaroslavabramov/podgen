# Citation Repair Quality And Voice Plan

Date: 2026-03-22
Owner: phase6_writing / citation_auditor
Status: proposed

## Goal

Keep `citation_audit` as a useful rigor guardrail without letting `citation_repair` damage the podcast script.

Target outcome:

- no unresolved factual blockers proceed to audio
- repairs stay local and spoken
- repairs do not inject episode-meta nonsense like `об этом в следующий раз`
- repairs do not default to bureaucratic hedges like `по имеющимся данным`
- repairs preserve scene function, pacing, and handoff energy

This plan is explicitly about **repair quality**, not about larger planning changes.

## Current Diagnosis

The latest run shows the core safety logic is much better:

- false blockers dropped sharply
- line-level repair is working
- fail-fast is working
- full-episode spend is reduced

But the accepted repairs are still uneven.

### Good repairs

- `A1S2`: added a missing `claim_id` with almost no stylistic damage
- `A2S5`: cleaned an operational detail without flattening the scene
- `A4S5`: added legal precision without fully breaking the scene

### Bad or weak repairs

- `A2S2`: became academic and over-explained uncertainty
- `A2S6`: replaced a concrete internal handoff with `Об этом — в следующий раз`
- `A3S2`: removed unsupported Greenwood details, but also flattened the scene into dry summary

### Root causes

1. `LINE_REPAIR_SYSTEM` and `REPAIR_SYSTEM` optimize for factual safety but not for spoken style.
2. The current repair prompts allow deletion or teaser-replacement as an easy escape hatch.
3. Repair does not understand whether a scene is mid-episode or truly final.
4. Scene repair is too willing to turn agency and story into abstractions.
5. Post-filter still lets some question-shaped false positives enter repair.

## Design Principles

1. This is a spoken podcast, not an article, memo, or legal brief.
2. Local repair beats scene rewrite whenever possible.
3. Keep content if it can be hedged or narrowed; deletion is the last resort.
4. Preserve agency and dramatic structure.
5. Medium-level rigor fixes must not cost obvious script quality.
6. Prompt rules should do most of the work; only add cheap deterministic guards where the failure mode is obvious and expensive.

## Scope

In scope:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`
- prompt text for `LINE_REPAIR_SYSTEM`
- prompt text for `REPAIR_SYSTEM`
- `scene_context` fields passed to repair
- one small post-filter code fix for question misfires
- repair-quality tests

Out of scope for this pass:

- redesigning `writer_revolt`
- adding a new `dialogue_polish` stage
- moving citation detection back to Sonnet
- broad acceptance-logic redesign unrelated to the specific quality regressions below

## Execution Plan

### P0. Rewrite The Repair Contract Around Spoken Voice

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Change both `LINE_REPAIR_SYSTEM` and `REPAIR_SYSTEM`.

Add explicit style contract:

- this is a spoken podcast line, not an article or legal memo
- preserve the original spoken tone and sentence shape
- do not introduce bureaucratic, legal, or academic phrasing
- do not replace direct agency with abstract institutional language
- repaired text must sound natural when read aloud
- the dialogue is in `{language}`; write naturally in that language and spoken register
- avoid translated-from-English phrasing or calqued sentence structure

Concrete anti-regression rules:

- do not default to formulas like `по имеющимся данным`, `в соответствии с`, `следует из`, `я должен оговориться`, unless the original line already used that style and the issue cannot be fixed another way
- do not replace `он предложил / он написал / чиновники прочли` with `суть механизма была / X стал ключевой фигурой`
- do not turn one sentence into a mini-paragraph

Why:

- current accepted repairs show clear drift into formal, explanatory prose
- this is the biggest quality problem now

### P1. Force A Better Repair Strategy Order

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Add explicit priority order to both repair prompts:

1. best: attach the correct existing `claim_id` if the support already exists in `available_claims`
2. next: keep the factual content but narrow or hedge the unsupported fragment
3. next: replace over-precise wording with a nearby supported version
4. last resort: remove the unsupported fragment

Important refinement:

- do not reward generic hedging as the default fix
- prefer short spoken narrowing over academic uncertainty markup
- if `claim.verification == "verified"`, repair may remove unnecessary caution rather than stacking more hedges
- if `claim.verification == "observed"` or `claim.verification == "unverified"`, repair must not increase confidence beyond the claim's support level

Examples of preferred behavior:

- `буквально через три недели` -> `вскоре после паники`
- `он написал статью, чиновники прочли и взяли за основу` -> keep Greenwood and the policy story, but remove the unsupported adoption chain
- `1983 год. Тогда якоря не было` -> keep the handoff idea without pretending the episode is ending

Explicitly forbid this fallback:

- replacing concrete internal scene-to-scene bridges with vague teaser lines like `об этом в следующий раз`

### P2. Teach Repair Where The Scene Sits In The Episode

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Extend `scene_context` with:

- `is_final_scene_in_episode`
- `is_final_scene_in_act`

Optional:

- `has_next_scene`

Then add prompt rules:

- if the scene is not the final scene of the episode, do not write sign-off or next-episode teaser language
- phrases like `об этом в следующий раз / выпуске` are allowed only in the final scene of the final act
- preserve intra-episode handoff energy if the original line was clearly bridging to the next scene or act

Why:

- `A2S6` shows that the model currently has no notion of episode position

### P3. Put Hard Bounds On Length And Shape

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Strengthen prompt constraints:

- repaired line should stay close to the original line length
- if the original line is one sentence, prefer one sentence in repair
- do not add parenthetical explanations or source-commentary unless the issue explicitly requires it
- do not add more than one short uncertainty marker to a single line

Keep current code-level line-drift guard, but make the prompt aware of it so the model does not keep fighting the acceptance logic.

Why:

- current repairs sometimes bloat one spoken line into a mini explanatory block

### P4. Protect Agency And Narrative Structure

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Add explicit narrative preservation rules:

- preserve named actors and actions if they are not themselves the unsupported part
- if the original line contains a story move, anecdote, or causal turn, keep that shape
- do not replace story with abstract explanation
- if one clause is unsupported, fix or remove that clause, not the entire dramatic move around it

For `scene_repair` specifically:

- if the blocker is on a specific claim inside a scene, keep the surrounding narrative intact
- focus edits on lines flagged as `block` or `repair`
- keep clean lines as unchanged as possible
- do not flatten a scene into generalized summary just to remove one unsupported assertion

Why:

- `A3S2` shows exactly this failure mode

### P5. Add A Small Few-Shot Layer Inside Repair Prompts

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Add 2-3 compact micro-examples to `LINE_REPAIR_SYSTEM` and possibly 1 to `REPAIR_SYSTEM`.

Examples should demonstrate:

- good local hedge without bureaucratic drift
- preserving a bridge into the next scene without fake episode sign-off
- preserving agency instead of abstracting it away

Suggested examples:

1. Unsupported date precision

- bad original:
  - `Рецепт появился буквально через три недели.`
- good repair:
  - `Рецепт появился вскоре после паники.`
- bad repair:
  - `Судя по имеющимся данным, рецепт появился через некоторый промежуток времени.`

2. Unsupported bridge fact

- bad original:
  - `1983 год. Тогда якоря не было — и посмотрим, что случилось.`
- good repair:
  - `А вот когда такого якоря ещё не было, всё выглядело совсем иначе.`
- bad repair:
  - `Об этом — в следующий раз.`

3. Unsupported agency chain

- bad original:
  - `Он написал статью, чиновники прочли и взяли её за основу.`
- good repair:
  - `Его позже назовут одним из архитекторов этой системы.`
- bad repair:
  - `Суть механизма была в следующем.`

4. Double hedging

- bad original:
  - `...вообще не было — хотя точный источник этих сведений установить затруднительно.`
- good repair:
  - `...судя по всему, вообще не было — хотя источник стоит перепроверить.`
- bad repair:
  - `...вообще не было — хотя я должен оговориться: точный источник установить затруднительно, так что это предположение, а не установленный факт.`

Why:

- the current prompt is all rules and no demonstrations
- a few small contrastive examples should steer Sonnet better than another paragraph of abstract instruction

### P6. Tighten Post-Filter For Question Misfires

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Implement a specific code fix:

- if `issue.type == "unverified_stated_as_fact"` and the flagged line is a pure question, downgrade it to `record/low`

Do not let that issue proceed into `line_repair`.

Why:

- a question cannot sensibly be “stated as fact”
- this is a cheap deterministic fix for an expensive misfire mode

This is intentionally narrow. Do not broaden the exemption to recaps again.

## Prompt Change Sketch

### LINE_REPAIR_SYSTEM should explicitly say

- keep the original line’s spoken rhythm and tone
- preserve whether the line is a question, bridge, recap, or answer unless the issue requires changing that
- prefer a local fix inside the line rather than deleting the line’s function
- do not introduce episode-meta phrases unless this is the final scene of the episode
- if the line names a person doing something, keep that agency if possible
- keep the line close in length to the original

### REPAIR_SYSTEM should explicitly say

- preserve the scene’s dramatic structure, not just its factual gist
- if only one clause is unsupported, repair that clause and leave the rest of the scene intact
- keep character action and causal motion where supported
- do not rewrite a vivid story beat into generalized exposition
- avoid legalistic and academic phrasing

## Tests

Primary test file:

- `backend/tests/test_citation_auditor_prompt.py`

Add tests for:

1. prompt contains spoken-style preservation rules
2. prompt forbids next-episode teaser language on non-final scenes
3. prompt strategy order prefers claim-link / local hedge / softening before deletion
4. `unverified_stated_as_fact` on pure question downgrades to `record/low`
5. prompt includes a contrastive example against double hedging
6. prompt instructs `scene_repair` to focus on flagged lines and keep clean lines stable
7. prompt mentions verification-level-sensitive confidence handling

Optional fixture-style tests:

- `A2S6` shape regression
- `A2S2` verbosity drift
- `A3S2` agency flattening

## Success Criteria

The next `audit_citations` run should show:

- no accepted repair that inserts `в следующий раз / выпуске` mid-episode
- fewer accepted repairs with obvious bureaucratic hedges
- `A2S6`-type bridges remain internal episode bridges, not fake sign-offs
- `A3S2`-type repairs preserve at least some narrative agency instead of collapsing to dry abstraction
- no regression in blocker safety

Qualitative success threshold:

- accepted repairs should sound like the original screenwriter voice under constraint, not like a compliance pass

## What Not To Do

- do not solve this by banning a giant list of words
- do not add another full pipeline stage yet
- do not move citation detection back to Sonnet before trying the cheaper prompt fixes
- do not relax rigor so far that unsupported factual bridges sneak back in
- do not accept “safe but dead” repairs as success

## Recommended Shipping Order

1. `P0 + P1 + P2 + P3 + P4 + P5` in one repair-prompt commit
2. `P6` small post-filter code fix
3. targeted tests
4. one new `audit_citations` rerun

## Expected Tradeoff

This plan may leave a few more medium-level issues unresolved.

That is acceptable.

The right tradeoff now is:

- fewer ugly accepted repairs
- fewer fake-safe text degradations
- slightly more residual non-blocking rigor notes

over:

- maximum repair aggressiveness at the cost of script quality
