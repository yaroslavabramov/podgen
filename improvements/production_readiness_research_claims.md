## Production Readiness Plan: Research -> Claims -> Verify Loop

### Scope

This plan captures the remaining work needed before the research / claims / verifier slice can be called production-quality for a universal podcast pipeline.

It reflects:
- current code behavior
- the latest debug dumps
- agreed review feedback

The plan intentionally separates:
- hard blockers
- important quality work
- deferred items that are real but not required before the next production-readiness checkpoint

---

## Current Assessment

The pipeline is already strong at:
- building a usable research backbone
- turning evidence into structured claims
- preserving narrative hooks better than before
- verifying claims with a mostly sensible hybrid verifier

The main remaining issues are no longer foundational data-model problems.
They are:
- loop correctness
- semantic precision
- intent-specific quality on non-history questions

---

## P0: Must Fix Before Production Readiness

### 1. Replace full claim rebuild after verifier fast-web loop with an incremental update path

Current behavior:
- `verify_claims` can send the graph back to `research_fast_web`
- `research_fast_web` then flows unconditionally into `build_claims`
- `build_claims` rebuilds claims from the full `state["evidence_ids"]` set

Why this is a blocker:
- wastes LLM calls
- discards previous `claim_ids` from state
- creates poor loop semantics for small evidence deltas
- risks orphaned or stale claim rows in the database

Required end state:
- fast-web research after verifier must update only the impacted branch/question groups
- existing unaffected claims must stay stable
- state must preserve the full active claim set
- only impacted claims should be re-deduped / re-verified

Preferred direction:
- track impacted provenance groups from verifier gaps
- create a targeted claim rebuild node or targeted claim update mode
- merge updated claims back into the active claim set instead of replacing the whole set

### 2. Tighten claim-builder semantic precision

Observed failure mode:
- some claims become slightly stronger or more specific than the evidence allows
- examples seen in logs:
  - explicit dates not fully present in the quote
  - motives / intentions inferred from contextual evidence
  - contextual or partial evidence turned into a stronger factual proposition

Why this is a blocker:
- this directly affects factual trustworthiness
- verifier catches some cases, but not all semantic drift should be delegated downstream

Required end state:
- claim builder emits only minimal auditable propositions
- no dates, motives, causal links, or evaluative framing unless explicitly supported
- contextual evidence should stay contextual unless it truly carries the proposition

Preferred direction:
- prompt hardening
- more explicit positive / negative examples
- stronger instructions around partial quotes, contextual evidence, and “do not upgrade the source claim”

### 3. Improve quality for non-history intents

Current state:
- `history` and `explain` perform better than `debate`, `compare`, and `case_study`
- debate-style branches currently drift toward historical background instead of producing the intended argumentative structure

Why this is a blocker:
- the product is supposed to work on arbitrary user corpora and arbitrary topic structures
- strong performance only on history / mechanism topics is not enough for production quality

Required end state:
- Hunter retrieval / extraction should honor intent, not just topic similarity
- Claim Builder should preserve the semantic shape of the question intent
- debate / compare / case-study branches should generate claims that match their editorial purpose

Preferred direction:
- tighten prompt wording around intent usage in Hunter and Claim Builder
- add intent-specific examples, especially for `debate`, `compare`, and `case_study`
- make Hunter query generation intent-aware, not only extraction-aware:
  - for `debate`, queries should surface evaluative and pro/con positions, not only factual descriptions
  - for `compare`, queries should surface explicit contrasts and alternatives, not only adjacent mechanisms
  - for `case_study`, queries should prefer concrete episodes with consequences and actors, not only general background

---

## P1: Important Quality Work After the P0 Fixes

### 4. Clean up verifier reasoning quality

Observed issue:
- verifier reasoning is sometimes directionally correct but contains nonsense details
- example from logs: treating a 2026 date as a “future date” even though the run is in 2026

Why it matters:
- not a core correctness blocker today because reasoning is internal/debug-only
- becomes a real issue if reasoning is ever exposed in UI, explainability flows, or shownotes

Required end state:
- reasoning should stay short, grounded, and evidence-bound
- no temporal speculation or unsupported source commentary

Preferred direction:
- prompt tightening only
- no additional deterministic layer needed for now

### 5. Improve illustrative-evidence behavior

Current state:
- `illustrative_evidence_ids` is used materially, but not optimally
- narrative evidence still too often becomes factual support when it would be better used mainly as illustration

Why it matters:
- this affects story quality more than factual correctness
- the feature works, but can work better

Required end state:
- ANECDOTE / CASE / EXPERT_QUOTE should default to illustrative unless they clearly carry an atomic proposition
- planning and writing should receive a cleaner split between factual backbone and narrative hooks

Preferred direction:
- prompt tuning in Claim Builder
- examples that distinguish:
  - support only
  - illustrative only
  - both

### 6. Run cross-topic production-readiness evaluations

Why it matters:
- one good historical / institutional topic is not enough evidence of universal readiness

Required end state:
- run and inspect at least a few intentionally different corpora
- confirm quality on:
  - history-heavy material
  - debate / argument-heavy material
  - contemporary / mixed-quality web-heavy material

Deliverable:
- short evaluation notes with failure modes and whether P0 work generalized

---

## Deferred: Real Work, But Not Required Before the Next Checkpoint

### 7. Route verifier gaps by gap type

The idea is correct:
- weak source support
- truncated / partial quote
- claim overshoot

But this is not a small fix.
It requires:
- explicit `gap_reason` generation
- a quote/span expansion path
- targeted claim repair semantics

Decision:
- keep this deferred until after the incremental loop is fixed
- revisit once the loop has a stable targeted-update architecture

---

## Non-Goals / Not Bugs

These should not drive the next work round:

- raw `claims > evidence` on the first claim-builder pass is not inherently wrong
- prefiltered verifier claims not using special source-strength logic is not a known correctness bug
- `illustrative` is not broken; it is under-optimized

---

## Recommended Execution Order

1. Incremental verifier -> fast-web -> claims loop
2. Claim-builder precision hardening
3. Non-history intent quality improvements
4. Verifier reasoning cleanup
5. Illustrative-evidence prompt tuning
6. Cross-topic evaluation runs
7. Typed gap routing after the targeted loop architecture is stable
