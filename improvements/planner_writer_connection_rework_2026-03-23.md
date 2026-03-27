# Planner → Writer Connection Rework

**Date:** 2026-03-23
**Status:** Implemented
**Scope:** act_planner.py, screenwriter.py
**Based on:** semantic analysis of run fccc9c33 (4 acts, 24 scenes)

---

## Context

Deep analysis of the planner→writer data flow revealed three layered problems:

1. **Planner beat cards can overlap semantically.** A1S4 description: "провал с фунтом — не управленческая ошибка, а следствие принципа: доктрина исключала госбанкинг." A1S6 description: "Валютный хаос — не провал управления. Это логичный итог доктрины: государство не должно печатать деньги." Same insight, different words. S6 had its OWN claims (HSBC/opium) that could have driven a genuinely new angle — but the description ignored them and restated S4's thesis. The beat card description and the beat card's claims were disconnected.

2. **Screenwriter is blind to its own narrative.** It sees `previous_scene.last_line` and bare claim statements from `already_covered_claims`. No dialogue, no arc, no framing. Even when the planner gives a good beat card, the writer can't know whether an earlier scene already made the same move. A1S6 faithfully executed its description — and produced a near-verbatim repeat of A1S4's twist, because it never saw S4's dialogue.

3. **Several prompt-level signals are broken or redundant.** `new_information_goal` duplicates claim statements. `callback_claim_ids` are bare UUIDs without details. `synthesize` as scene_job is interpreted as "recap" rather than "land through new angle." These compound problems 1 and 2.

---

## Changes

### 1. Ground beat card descriptions in scene claims (planner prompt)

**Problem:** Planner writes beat card descriptions as thesis summaries independent of the scene's claims. S6 got claims about HSBC/opium but its description was about the British doctrine (same thesis as S4). Claims and description are disconnected — the description drives the writer, the claims are decorative.

**Fix:** Update `ACT_PLANNER_SYSTEM`:

- Add rule: **"Each scene's description must be grounded in the claims assigned to that scene. The description should explain what those specific claims reveal, change, or complicate — not restate the act's general thesis."**
  - Bad: S6 has HSBC/opium claims, description says "monetary chaos was not failure but policy" (thesis from S4's claims)
  - Good: S6 has HSBC/opium claims, description says "a bank born from opium money fills the void that doctrine left open — the system didn't fail, it produced exactly this"
- Add rule: **"Each scene must make a different move from every other scene in the act. If two descriptions land on the same insight, one of them is wrong — rewrite it to use its own claims."**
- Redefine `synthesize` guidance: instead of "compress what changed in understanding" → **"land the shift through this scene's own material — show why what the listener just heard means something different or bigger than they thought. Do not restate earlier scenes' conclusions."**
- Redefine `takeaway`: **"a takeaway uses its own claims to recontextualize the act — not summarize it."**
- When the next act benefits from unresolved tension, use a **forward hook** — a question or tension that pulls forward. But don't force it: sometimes a hard landing creates its own pull. Encode in `transition_note` when present.

**Validation:** Prompt alone is not a contract — the planner already ignores similar high-level rules. Add two LLM-based post-checks in `_collect_beat_sheet_quality_issues` (Haiku call, cheap):

1. **Claim-description grounding.** For each scene: do the assigned claims substantively drive the description? If the description's core insight doesn't follow from its claims (like S6 describing doctrine while holding HSBC claims), flag as `description_claim_mismatch` (severity: structural → triggers retry with feedback).

2. **Cross-scene insight dedup.** For all scene pairs: do any two descriptions land the same insight? If yes, flag as `description_overlap` (severity: structural). Feedback to planner: "S4 and S6 both land 'chaos was policy'; rewrite S6's description to use its own claims (HSBC/opium) for a different move."

Both checks feed into the existing retry loop (`quality_failures` / `ACT_PLANNER_QUALITY_RETRIES`). The planner already retries on structural issues — these just add two new issue types.

**Files:** `act_planner.py` — `ACT_PLANNER_SYSTEM` prompt, `_collect_beat_sheet_quality_issues()`.

### 2. Pass prior act dialogue to screenwriter

**Problem:** Screenwriter sees only `previous_scene.last_line` + `already_covered_claims` (bare fact statements from DB). Cannot reframe, contrast, or land material it hasn't read. Cannot detect that an earlier scene already made the same move.

**Fix:** In `_build_scene_prompt_payload`, add `prior_scenes_dialogue` — compact representation of all preceding scenes' dialogue within the current act. For scene N, include scenes 1..N-1.

Format per scene:
```json
{
  "scene_number": 3,
  "scene_type": "case_study",
  "lines": [
    {"speaker": "A", "text": "..."},
    {"speaker": "B", "text": "..."}
  ]
}
```

Cost: ~2-3K tokens for the last scene of a 6-scene act. Well within budget.

Source: `existing_segments` dict already available in the `write_screenplay` loop. Extract `dialogue_json` and pass speaker+text only (no claim_ids/ssml/emotion — irrelevant for narrative context).

Update `SCREENWRITER_SYSTEM` prompt: instruct the writer to read prior dialogue, avoid repeating insights/formulations already landed, and build on the narrative arc rather than writing in isolation.

**Files:** `screenwriter.py` — `write_screenplay()` loop (build context), `_build_scene_prompt_payload()` (accept + include), `SCREENWRITER_SYSTEM` prompt.

### 3. Remove `new_information_goal`

**Problem:** Duplicates claim statements already present in the `claims` field. The "don't repeat" function is already served by `already_covered_claims` + the system prompt rule (line 90). For final scenes it actively misleads: "Advance the listener with what is NEW" when the scene should land existing material through a new angle.

**Fix:** Remove `_build_new_information_goal()` and the `new_information_goal` field from prompt payload.

**Files:** `screenwriter.py` — delete `_build_new_information_goal()` (lines 908-949), remove from `_build_scene_prompt_payload()`, remove from `write_screenplay()` call site.

### 4. Pass callback claim details to screenwriter

**Problem:** `callback_claim_ids` are passed as bare UUID list — no statement, no verification level. Screenwriter must cross-reference them against `already_covered_claims` by ID to understand what they mean.

**Fix:** In `_build_scene_prompt_payload`, resolve callback claims against `act_claim_lookup` and pass as `callback_claims` with full details (same format as `claims`).

```python
# current
"callback_claim_ids": _scene_callback_claim_ids(scene),

# new
"callback_claims": [
    act_claim_lookup[cid]
    for cid in _scene_callback_claim_ids(scene)
    if cid in act_claim_lookup
],
```

Update `SCREENWRITER_SYSTEM` prompt: `claims` = new material to introduce, `callback_claims` = established material available for reference (not re-explain from zero).

**Files:** `screenwriter.py` — `_build_scene_prompt_payload()`, `SCREENWRITER_SYSTEM`.

### 5. Validate screenwriter output claim_ids

**Problem:** Sonnet occasionally corrupts UUIDs (3/81 = 4% in this run): 1-char typos, franken-UUIDs (concatenation of two UUIDs), hallucinated UUIDs. Corrupted IDs pollute `covered_claim_ids` tracking and can cause citation audit issues.

**Fix:** Post-validation after screenwriter output parse:
```python
for line in dialogue:
    validated = []
    for cid in line.get("claim_ids", []):
        if cid in global_claim_lookup:
            validated.append(cid)
        else:
            logger.warning("Dropping invalid claim_id %s from screenwriter output", cid)
    line["claim_ids"] = validated
```

Strict: exact match → accept, anything else → drop + log. No fuzzy repair — auto-correcting by edit distance risks silently attaching a line to the wrong claim.

**Files:** `screenwriter.py` — after dialogue parse loop (lines 396-413).

### 6. Simplify `already_covered_claims` once prior dialogue is available

**Problem:** With full prior dialogue in the prompt (change #2), `already_covered_claims` becomes partially redundant as narrative context. However, claim IDs are still needed for the writer to reference them in output `claim_ids`.

**Fix:** Keep `already_covered_claims` but simplify: just `[{id, statement_short}]` as a claim ID reference table — not as narrative context. Remove `mechanism_anchors` extraction (complex regex, fragile, now unnecessary with full dialogue available).

**Files:** `screenwriter.py` — `_build_already_covered_claims_payload()`, `_collect_mechanism_anchors()`, `_extract_mechanism_anchors()`.

---

## What NOT to change

- **`already_covered_claims_global`** (cross-act) — still needed for first scene of each act, where prior act dialogue is not available. Keep as-is.
- **`previous_scene`** — still useful as a focused pointer to the immediately preceding scene. Keep but can simplify (last_line is the most valuable part).
- **`recent_scene_starts`** — still useful for opener variety. Keep.
- **Citation auditor** — no changes needed. `_select_scene_claims_map` already merges primary+callback+claim_ids via `scene_manifest_claim_ids`. Current run (post-fixes) passed audit with 0 blockers.
- **`scene_job` values** — keep all. The issue was prompt guidance, not the taxonomy.
- **Final-scene claim assignment** — new claims in final scenes are fine when they serve the landing. No need to force callbacks or restrict primaries. Fix #1 ensures descriptions are grounded in those claims; fix #2 ensures the writer sees the narrative context.

---

## Dependency order

1 (planner prompt) is independent — fixes the source of overlap.

2 (prior dialogue) is independent — gives writer narrative context.

3 (remove new_information_goal) is independent.

4 (callback details) is independent.

5 (UUID validation) is independent.

6 (simplify already_covered) depends on 2.

**1 through 5 can all be done in parallel.** Then 6.
