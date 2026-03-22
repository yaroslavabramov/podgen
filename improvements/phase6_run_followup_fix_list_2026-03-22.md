# Phase 6 Run Follow-Up Fix List

Date: 2026-03-22
Run: `fccc9c33-fd78-4da7-90ac-bf40c0177936`
Scope: `screenwriter` -> `evaluate_and_revolt` -> `audit_citations`

## Goal

Capture the actual remaining problems after the latest run.

This is not a generic plan.
It is the concrete backlog of what still needs to be fixed in Phase 6 after the latest implementation wave.

## Current State

What is already true:

- `dialogue_polish` now actually runs across all acts
- `citation_audit` no longer blocks the run everywhere
- `citation_repair` is less destructive than before
- metadata loss is only partially mitigated:
  - `scene_repair` is improved
  - `dialogue_polish` can still lose or misalign `emotion` / `ssml_hints` when line count or line alignment changes

What is still not good enough:

- `scene_opening_echo` is overflagged and under-modeled
- cross-act seams are not audited properly
- repeated conversational mechanisms still dominate the script
- opener distribution swung from "too much A" to "too much B"
- current `act_dialogue_audit` is too weak to justify trust in its output
- `dialogue_polish` metadata contract is still too weak for TTS-safe rewriting
- some citation-tail scenes still overclaim beyond available support

## Priority Order

## P0. Replace fake act audit with real full-act dialogue audit

Problem:

- current `act_dialogue_audit` only sees:
  - first 2 lines of each scene
  - last 2 lines of each scene
  - speaker distribution
  - local issue types
- it does **not** see the actual act
- it therefore cannot reliably detect:
  - repeated discourse mechanisms
  - repeated explanatory resets
  - real opener replay vs acceptable carry-over

Fix:

- replace edge-only act summaries with full scene dialogue per act
- keep one act-level pass per act
- feed it:
  - full scene text
  - scene jobs
  - local issue types
  - simple structured act context:
    - opener speaker counts
    - closer speaker counts
    - scene opening lines
    - scene closing lines
    - current local issue types

Success criterion:

- act audit can explain repetition patterns from real text, not just edge hints

## P1. Add explicit cross-act seam audit

Problem:

- current act audit cannot see:
  - `A1S6 -> A2S1`
  - `A2S6 -> A3S1`
  - `A3S6 -> A4S1`
- this is why real seam replay survives even when local/act audit exists

Concrete example:

- `A1S6`:
  - `Это меняет вопрос. Не "почему был хаос", а "кто теперь мешает этому банку печатать деньги без ограничений".`
- `A2S1`:
  - `...Кто им мешает напечатать сколько захотят?`

This is real replay across an act boundary.

Fix:

- add one cheap episode-level seam audit over the 3 act seams
- input:
  - last 2-3 lines of previous act final scene
  - first 2-3 lines of next act opening scene
  - scene jobs
- output:
  - `acceptable_handoff`
  - `replay_question`
  - `replay_punchline`
  - `weak_reset`
  - target scene to polish

Success criterion:

- act-boundary replay is no longer invisible to Phase 6

## P2. Make `dialogue_polish` metadata-safe by contract, not by best-effort merge

Problem:

- current metadata merge is position-based and best-effort
- when polish changes scene length or line alignment, `emotion` / `ssml_hints` can still be lost or drift
- this directly affects TTS quality

Fix:

- extend `POLISH_SYSTEM` JSON contract so each line can return:
  - `speaker`
  - `text`
  - `claim_ids`
  - `emotion`
  - `ssml_hints`
- make metadata preservation explicit in the rewrite contract instead of trying to recover it after the fact
- keep merge as a fallback, not the primary safety mechanism

Success criterion:

- accepted polished scenes preserve TTS metadata reliably, even when line count changes

## P3. Split "acceptable carry-over" from "bad replay"

Problem:

Current `scene_opening_echo` bucket mixes:

- acceptable continuity
- thesis -> example progression
- zoom-in from concept to case
- actual replay of same question or same punchline

This is why some flagged scenes are not actually bad.

Examples that are probably acceptable:

- `A1S4 -> A1S5`
- `A1S5 -> A1S6`

Examples that are genuinely bad:

- `A1S6 -> A2S1`
- `A2S5 -> A2S6`

Fix:

- rewrite the act/seam audit taxonomy so it distinguishes:
  - `acceptable_handoff`
  - `thesis_to_example_progression`
  - `replay_question`
  - `replay_punchline`
  - `repeated_explanatory_reset`

- use this taxonomy as the basis for:
  - act-audit recommendations
  - seam-audit recommendations
  - priority ranking
  - polish acceptance

- do not keep separate downstream logic that re-invents these categories in ad hoc ways

Success criterion:

- Phase 6 stops treating all thematic continuity as a defect

## P4. Let full-act audit output drive prioritization and opener-balance fixes

Problem:

- before polish, openers were skewed:
  - `A 18 / B 6`
- after polish, they flipped:
  - `A 6 / B 18`
- this means the current intervention logic is changing openers, but not solving distribution intelligently

Fix:

- do not treat opener-change as success by itself
- let the full-act audit explicitly identify:
  - dominant opener families
  - speaker-distribution imbalance
  - smallest set of scenes that would improve variation
- let priority score be derived from that output plus seam findings
- do not keep a separate pseudo-layer for opener balancing outside the act audit

Success criterion:

- polished scenes break repeated patterns without simply flipping the act into the opposite bias

## P5. Keep citation layer as guardrail, but fix the remaining content hotspots

Problem:

`citation_audit` is no longer the main blocker, but some scenes still overclaim.

Current real hotspots:

- `A1S5`
  - too-specific colonial banking examples beyond claim support
  - ideology/motivation overreach
  - new Hong Kong trade-bank assertion
- `A2S2`
  - explanatory mechanism extends beyond tightly grounded claim coverage
- `A2S4`
  - mechanism wording gets more specific than support warrants
- `A3S4`
  - attribution/timing rigor on Krugman and crisis framing
- `A4S1`
  - quote-like source framing without direct support

Fix:

- do not redesign citation layer again right now
- instead:
  - tighten claim coverage and source-framing on these scene types
  - improve local repair targeting for mechanism explanation scenes
  - keep citation repair focused on unsupported fragment, not whole-scene rewrite

Success criterion:

- citation failures become a short tail of real rigor cases, not a noisy second editor layer

## P6. Keep multilingual discipline strict

Problem:

- Phase 6 has repeatedly drifted toward:
  - language-specific phrase heuristics
  - case-shaped fixes
  - prompt examples that only help one language

Fix:

- keep all new repetition and seam logic:
  - structure-based
  - metadata-based
  - language-agnostic
- do not reintroduce:
  - Russian or English phrase lists
  - banned-word logic
  - corpus-specific shortcuts

Success criterion:

- all new Phase 6 logic remains portable across languages and corpora

## Immediate Implementation Order

1. Real full-act LLM dialogue audit with full act text
2. Echo taxonomy rewrite
3. Episode seam audit for act boundaries
4. Metadata-safe `dialogue_polish` contract
5. Only then rerun:
   - `write_screenplay`
   - `evaluate_and_revolt`
6. After that, inspect citation tail scenes before touching citation prompts/logic again

## What Not To Do

1. Do not solve repeated words with a blacklist.
2. Do not treat the current act-audit surrogate as "good enough".
3. Do not add another expensive pairwise scene-boundary pass per scene.
4. Do not redesign citation guardrails again before fixing writer/distribution/seam problems.
5. Do not accept opener rewrites just because the first line changed superficially.

## Bottom Line

The next real bottleneck is no longer `citation_audit`.

It is the writer/distribution layer:

- real act-level repetition
- real seam replay
- repeated opener mechanisms
- repeated explanatory moves

That is what needs to be fixed next.
