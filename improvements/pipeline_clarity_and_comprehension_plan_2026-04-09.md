# Pipeline Clarity And Comprehension Plan

Date: 2026-04-09
Scope: `act_planner` -> `screenwriter` -> `writer_revolt`
Primary evidence run: `3870242f-5f13-4dcf-a74f-46dc76faf4d7`

## Goal

Make the pipeline produce episodes that are not only structurally grounded and stylistically polished, but also consistently understandable to a non-expert listener without manual rescue.

The current system is already decent at:

- claim grounding
- scene continuity
- seam repair
- repetition detection

The current system is still weak at:

- introducing new concepts with enough oxygen
- keeping causal chains explicit
- keeping actor / subject responsibility clear
- distinguishing summary from scene
- writing explanation scenes that feel understandable rather than compressed

This memo treats clarity and listener comprehension as a first-class quality target, not as an incidental side effect of good dialogue.

## Main Diagnosis

The latest screenplay confirms that the remaining product problem is not mostly factual accuracy.

It is listener comprehension.

Typical failures:

- abrupt causal jumps
- unexplained institutional terms
- undefined or shifting actors
- namedrops without grounding
- compressed history beats that skip setup
- thesis-heavy scenes that restate conclusions before the listener has enough concrete footing
- payoff acts that reopen explanatory material instead of landing the previous question

Examples from the latest screenplay:

- `Act 2` repeats the same mechanism thesis several times while introducing `currency board`, `liquidity`, `monetary base`, `Exchange Fund`, and `Certificate of Indebtedness` with too little breathing room
- `Act 3` opens with a compressed sweep through multiple coin systems and then jumps into institutional conclusions before the listener is properly oriented
- `Act 3 Scene 3` lands `440 thousand dollars in losses` before the mint setup is explained
- `Act 4` discusses rate pressure, corridor asymmetry, and 2005 reform without making the attack mechanism legible enough first
- `Act 5` reopens already-covered explanatory ground instead of cleanly paying off the question raised at the end of `Act 4`

## Why Existing Quality Layers Miss This

### `act_planner`

Current planning optimizes for:

- distinct scene moves
- claim grounding
- callback discipline
- overlap avoidance

It does not explicitly optimize for:

- what the listener knows before a scene
- what the listener should clearly understand after a scene
- what prior knowledge the scene is allowed to assume
- whether the scene introduces too many new moving parts at once

### `screenwriter`

Current writer behavior is too thesis-friendly.

It tends to:

- compress explanation into strong formulations
- prefer clean abstractions over concrete orientation
- rely on host roles to create movement even when the listener still lacks setup
- treat summary-like reframes as good scene endings

This produces text that sounds coherent inside the model but is often hard to follow in audio.

### `writer_revolt`

Current Phase 6 audits are good at:

- rhythm problems
- opener echoes
- repeated agreement / challenge shapes
- seam resets

They are not yet strong at:

- catching comprehension failures
- differentiating between acceptable density and missing explanation
- identifying when a scene is functionally a summary instead of a dramatized explanatory beat

## Product Principle

The pipeline should optimize for:

`A smart listener with no special prior knowledge can follow the logic of the episode in real time, without needing to stop and reconstruct hidden context.`

That means:

- every new actor, mechanism, and institutional label needs grounding
- every important causal turn needs an audible bridge
- every scene must earn its abstraction with enough concrete setup
- payoff scenes must move forward, not replay the base explanation

## Required Changes

## P0. Add a comprehension contract to `act_planner`

### Why

If planning does not specify what must become clear in each scene, writer and polish will keep producing scenes that are structurally distinct but cognitively muddy.

### Change

Augment scene briefs with clarity-oriented metadata, for example:

- `listener_state_before`
- `listener_state_after`
- `must_make_clear`
- `must_not_assume`
- `new_terms_to_introduce`
- `new_terms_forbidden`
- `required_bridge_from_previous_scene`

This should remain compact and machine-facing.

### Effect

This would prevent patterns like:

- introducing `corridor` before the listener understands the attack mechanics
- using `Hong Kong did X` where the actual actor is the colonial state, HKMA, or the banking regime
- re-asking a settled question instead of advancing the current one

## P0. Refactor `SCREENWRITER_SYSTEM` toward clarity-first writing

### Why

The writer currently over-rewards strong formulations, abstract symmetry, and efficient explanation.
That is good for clean-looking text and bad for actual listener comprehension.

### Change

Rewrite the prompt contract around these rules:

- a scene is successful only if a non-expert listener can track who is acting, what changed, and why it matters
- do not introduce a new term unless you can either explain it immediately or make it unnecessary
- do not use a named person, institution, or legal event as a shorthand for meaning; ground it first
- do not let a host summarize an implication before the concrete setup is legible
- do not open a new act by replaying base explanatory material that has already been established
- avoid empty authority phrases like `people often miss`, `what matters here is`, `this is important`, unless the next line cashes them out concretely

### Additional requirement

Scene jobs should have different writer contracts:

- `hook`: wonder / anomaly / orientation, not taxonomy
- `mechanism`: stepwise causal clarity, not slogan chains
- `history`: staged chronology, not compressed historical dump
- `crisis`: event logic first, abstract takeaway second
- `payoff`: answer the current question, do not reopen the base explanation

## P0. Add `comprehension_audit` to Phase 6

### Why

The current quality loop can tell us that a scene is repetitive, but not that it is hard to understand.
That is the wrong failure prioritization.

### New issue taxonomy

Add a dedicated internal audit for comprehension failures such as:

- `undefined_actor`
- `unexplained_term`
- `namedrop_without_grounding`
- `causal_gap`
- `missing_bridge`
- `assumed_context`
- `summary_instead_of_scene`
- `compressed_history_dump`
- `payoff_restarts_explanation`

### Output shape

Keep it compact:

- at most `2-3` issues per scene
- short English descriptions
- stable scope labels:
  - `scene_rewrite`
  - `scene_polish`
  - `act_warning`

### Routing policy

- rhythm-only issues stay in `dialogue_polish`
- comprehension issues should escalate earlier to scene rewrite or cluster rewrite
- do not ask `dialogue_polish` to rescue a scene that is structurally unclear

## P1. Add explicit timing checks before audio generation

### Why

The current screenplay can come out materially shorter than the intended episode length without triggering any meaningful warning.

### Change

Track:

- target duration by episode
- estimated duration by act
- estimated duration by screenplay draft

If the screenplay falls below the acceptable lower bound, surface a blocking or near-blocking warning before downstream generation.

### Suggested implementation

- use language-aware words-per-minute heuristics as a first pass
- compare both episode-level and act-level budgets
- flag acts that are too short because they collapsed into summary rather than scene work

## P1. Make Phase 6 escalation less polish-centric

### Why

We already learned that scene polish is useful for rhythm, not for deep meaning repair.

### Change

Use these escalation rules:

- `dialogue_polish` for rhythm, opener echo, repeated agreement shapes
- `multi_scene_polish` for contiguous monotony clusters
- `scene rewrite` for comprehension failures
- `act-level rewrite` only when several comprehension failures share one root cause across the act

This keeps polish in its correct lane.

## P1. Strengthen the planner-writer handoff

### Why

Some of the current listener confusion comes from the writer inheriting scene descriptions that are structurally valid but not pedagogically sequenced.

### Change

Carry more listener-facing structure from Phase 5 to Phase 6:

- current act question
- current scene answer
- unresolved question to hand off
- required bridge from previous scene
- allowed abstractions for the scene

This should reduce cases where the writer restarts from a broader question instead of continuing the current one.

## What Not To Do

- do not solve this with language-specific banned phrase lists
- do not keep stacking more generic style bullets onto `SCREENWRITER_SYSTEM`
- do not expect `dialogue_polish` to fix explanation density after the fact
- do not use episode-specific heuristics
- do not treat rhetorical smoothness as a proxy for understanding

## Implementation Order

1. Add `comprehension_audit` in Phase 6.
2. Refactor `SCREENWRITER_SYSTEM` around clarity-first scene writing.
3. Extend `act_planner` scene briefs with listener/comprehension metadata.
4. Add screenplay duration guardrails.
5. Re-run the same evidence episode and compare:
   - comprehension issue count
   - number of namedrops without grounding
   - number of unexplained term flags
   - number of summary-instead-of-scene flags
   - total estimated runtime vs target runtime
6. Only after that decide whether a broader act-rewrite layer is necessary.

## Success Criteria

The plan is working if, on a rerun of the same episode:

- `Act 2` becomes materially easier to follow without manual explanation
- `Act 3` no longer makes large causal jumps between setup and consequence
- `Act 4` attack / corridor mechanics are understandable on first listen
- `Act 5` pays off the prior act instead of reopening explanation
- screenplay duration lands within the intended runtime band
- the number of purely rhetorical thesis restatements drops

## Short Version

The next meaningful improvement is not another rhythm tweak.

It is to teach the pipeline to ask:

`Will a normal listener actually understand this scene in real time?`

Until that becomes an explicit contract in planning, writing, and Phase 6 audit, manual editorial rescue will keep finding problems that the system currently treats as acceptable.
