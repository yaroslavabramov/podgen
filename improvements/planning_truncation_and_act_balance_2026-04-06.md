# Planning Truncation Cleanup and Act Balance

**Date:** 2026-04-06
**Status:** Planned
**Scope:** act_planner.py, showrunner.py, planning_context.py, screenwriter.py
**Based on:** analysis of run 3870242f (Hong Kong dollar episode, 96 claims, 30 min)

---

## Context

Run 3870242f failed at Act 3 planning with `quality_gate_failed` after exhausting all retry attempts. Root cause analysis revealed a cascade:

1. Showrunner assigned 20 of 52 used claims (38%) to Act 3 with a 480s duration target — the largest act by far.
2. Act planner, forced to cover 20 claims across 6 scenes, generated descriptions up to 658 characters (4-5 sentences instead of the intended 1-3).
3. Quality gate (`_check_description_claim_grounding`) truncated descriptions to `[:400]` before sending them to Haiku for validation.
4. Haiku received mid-sentence-truncated descriptions (e.g. "...Каждая попытка стабилизации опирается ") and correctly flagged them as malformed → `description_claim_mismatch`.
5. Both retry attempts produced even longer descriptions → same truncation → same failure → run stopped.

Additionally, claim statement truncation (`[:300]`) in planning_context.py was cutting 2 out of 91 claims mid-word (max actual length: 310 chars). This was already patched to `[:500]` but the pattern of arbitrary character limits is systemic.

---

## Changes

### 1. Remove description truncation from quality gate (blocker)

**Problem:** `_check_description_claim_grounding` (act_planner.py, line ~1225) truncates description to `[:400]` before sending to Haiku. This creates false positives: Haiku sees an incomplete sentence and flags it as ungrounded. The quality gate's job is to validate the description — it must see the full text.

**Fix:** Remove `[:400]` truncation in the `scenes_for_check` construction:

```python
# Before
"description": description[:400],

# After
"description": description,
```

No replacement limit needed. Haiku's 200K context can handle 6 descriptions × 700 chars with no issue. The entire quality gate prompt stays under 10K tokens even for the largest acts.

**Files:** `act_planner.py` — `_run_llm_quality_checks()`

### 2. Cap claims per act in showrunner (root cause)

**Problem:** Showrunner assigned 20 claims to Act 3 while Acts 4-5 got 8 each. This cascades: dense act → long descriptions → truncation failures. It also produces an unbalanced episode — 38% of content in one act, followed by two thin acts.

**Fix:** Add explicit claim distribution guidance to the showrunner prompt. Compute a per-act ceiling and inject it:

```python
max_claims_per_act = max(8, int(len(available_claims) / act_count * 1.5))
```

For 52 claims / 5 acts → ceiling of 16. Add to the showrunner prompt:

```
Each act must use no more than {max_claims_per_act} claims. Distribute claims
proportionally to each act's duration target. If one branch has too many claims
for its act, select the strongest and leave the rest unused — an episode that
stages 12 strong claims well is better than one that rushes through 20.
```

Also add a post-LLM validation: if any act exceeds the ceiling, log a warning and redistribute excess claims to adjacent acts or drop them. This is a soft constraint — the showrunner may slightly exceed it, but the post-check prevents extreme imbalance.

**Files:** `showrunner.py` — `plan_episode()`, showrunner prompt template.

### 3. Tighten description length guidance in act planner prompt (symptom prevention)

**Problem:** `ACT_PLANNER_SYSTEM` says "1-3 sentence beat card" but does not enforce length. With 20 claims, the LLM writes 4-5 sentence mini-drafts (up to 658 chars). Long descriptions hurt downstream: the screenwriter tends to paraphrase the description instead of creating natural dialogue.

**Fix:** Add explicit constraint to `ACT_PLANNER_SYSTEM`:

```
Each scene description must be a beat card: 1-3 sentences, maximum 250 characters.
State what CHANGES in this scene — the shift in understanding, the new complication,
the consequence. Do not draft dialogue or narration. If a description needs more than
3 sentences, the scene is trying to do too much — split it or simplify.
```

250 chars is deliberately tight. The previous successful run (Apr 4) had median description length ~220 chars for Acts 2-5 when Act 3 had 14 claims. The outliers (400+ chars) all came from the overloaded 20-claim Act 3.

No code-level truncation of description in the act planner output — if the LLM still writes long descriptions after the prompt change, that's a signal the act is overloaded (fix 2 should prevent this).

**Files:** `act_planner.py` — `ACT_PLANNER_SYSTEM` prompt.

### 4. Remove cargo-cult statement truncations

**Problem:** Claim statements are truncated at arbitrary character limits in multiple places. These limits were set without empirical basis and cause downstream data loss:

| Location | Current limit | Max actual data | Effect |
|---|---|---|---|
| `planning_context.py:29` | `[:500]` (was `[:300]`) | 310 chars | No current effect, but masks upstream issues |
| `showrunner.py:144` (fallback) | `[:500]` (was `[:200]`) | 310 chars | Same |
| `screenwriter.py:747` | `[:500]` (was `[:300]`) | 310 chars | Same |
| `verifier.py:927` | `[:500]` | 310 chars | No effect |

**Fix:** Remove truncation from all four locations. If claim statements grow beyond acceptable length in the future, the fix belongs in the claim builder prompt ("write claims under 300 characters"), not in downstream consumers silently cutting text.

Keep truncation only in places where it serves a genuine compression purpose:
- `screenwriter.py` `_build_covered_claims_payload()` with `statement_chars=160` — this is a compact "what was already said" reference, not full claim text. Keep as-is.
- `dedupe.py` debug dump `[:300]` — debug-only, doesn't affect runtime. Keep as-is.

**Files:** `planning_context.py`, `showrunner.py`, `screenwriter.py`, `verifier.py` (if applicable).

---

## Execution order

1. **Fix 1** (quality gate truncation) — unblocks the current run immediately
2. **Fix 2** (showrunner claim cap) — prevents the root cause from recurring
3. **Fix 3** (description length guidance) — prevents symptom even if cap is slightly exceeded
4. **Fix 4** (statement truncation cleanup) — hygiene, no urgency

After fixes 1-2, re-run showrunner + act planner and verify:
- No act has more than ~16 claims
- All acts pass quality gate on first or second attempt
- Description lengths stay under 300 chars with balanced acts
- No truncated claim statements in planning or writing prompts
