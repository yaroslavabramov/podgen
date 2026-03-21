# Citation Audit Fail-Fast + Line Repair Plan

Date: 2026-03-22

## Goal

Make `phase6_writing.citation_auditor` cheap enough and reliable enough to run as a real pre-audio gate without burning multiple expensive reruns on one bad edge case.

Primary target:
- eliminate false blockers on question / transition lines like `A1S6`
- stop whole-scene rewrites for one flagged line
- stop auditing the rest of the episode once audio is already guaranteed to be blocked

---

## Current Failure Mode

The latest failed run shows a narrow but expensive problem:

1. Auditor over-interprets some question lines as factual assertions.
2. Repair rewrites the whole scene even when only one line is problematic.
3. Acceptance is still too scene-level for these cases.
4. The node keeps processing later scenes even after a blocker is already unresolved.

Result:
- the model may already produce a usable fix
- but acceptance rejects it for the wrong reason
- and the pipeline still spends money on the rest of the episode

---

## Design Principles

1. Prefer cheap deterministic post-filters over another LLM pass when the pattern is obvious.
2. Repair the smallest possible unit first.
3. Preserve scene function; do not let citation repair become scene re-authoring.
4. Fail fast in default runtime mode.
5. Keep blocking semantics conservative only for truly assertive lines.

---

## Target Behavior

Default `audit_citations` flow should become:

1. audit one scene
2. normalize + post-filter obvious false blockers
3. if needed, attempt `line_repair` first
4. only escalate to `scene_repair` when issues are broad enough to justify it
5. if blocker remains unresolved after allowed attempts, stop immediately

Not desired anymore:
- whole-episode full report on normal expensive runs
- whole-scene rewrite for one bad question
- scene-level claim-id preservation on non-assertive lines

---

## Plan

### P0. Add Non-Assertive Post-Filter

File:
- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Add a deterministic post-processing step after `_normalize_citation_issues(...)`:

- classify dialogue lines into rough surface functions:
  - question
  - transition
  - recap/synthesis
  - assertive statement
- for non-assertive lines:
  - downgrade `missing_claim_link` from `block` to at most `repair`, often `record`
  - downgrade `unverified_stated_as_fact` from `block` when the line is not actually asserting a concrete fact

Heuristic scope:
- line ends with `?`
- line starts with conversational bridge forms
- line is explicitly a recap/synthesis without introducing a new date, number, institution, or causal claim

Important:
- this is a narrow cost-protection filter, not a general semantic validator

Success criterion:
- `A1S6`-style bridge question does not become a blocker by default

---

### P1. Split Repair Modes: `line_repair` First, `scene_repair` Second

File:
- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Introduce two repair paths:

#### `line_repair`

Use when:
- 1-2 flagged lines
- issues are concentrated on specific `line_index` values
- no evidence that the whole scene is structurally wrong

Payload:
- original full dialogue
- exact flagged line indexes
- scene context
- available claims
- issues for those lines

Prompt rule:
- rewrite only the flagged lines
- preserve speaker and local intent
- do not expand into new exposition

Implementation detail:
- do **mechanical splice**, not free full-scene acceptance
- keep all unflagged lines from original dialogue exactly as they were
- replace only targeted lines with repaired lines

Why this is important:
- avoids exact-string comparison on non-flagged lines
- avoids accepting global drift just because the model rewrote the whole scene “nicely”
- matches the reviewer’s note: non-flagged lines should stay identical by construction

#### `scene_repair`

Use only when:
- issues are spread across many lines
- there are multiple distinct assertive factual problems
- line-local patch is unlikely to be enough

This remains the fallback, not the default.

Success criterion:
- one bad question no longer causes a whole-scene rewrite

---

### P2. Replace Scene-Level Claim Preservation with Line-Aware Preservation

File:
- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Current rule is too blunt:
- `original_claim_ids.issubset(repaired_claim_ids)` across the whole scene

Replace with:

- for `line_repair`:
  - unflagged lines are mechanically preserved
  - flagged assertive lines must preserve or improve factual grounding
  - flagged non-assertive lines may lose `claim_ids`

- for `scene_repair`:
  - preserve claim coverage for assertive content
  - do not require claim ids on question / transition / recap lines if they are no longer assertive

Practical rule:
- claim-id preservation should be tied to factual assertion preservation, not to raw scene union

Success criterion:
- repaired question lines can drop irrelevant `claim_ids` without false rejection

---

### P3. Tighten Acceptance by Repair Mode

File:
- `backend/app/pipeline/phase6_writing/citation_auditor.py`

#### Acceptance for `line_repair`

Accept if:
- original blockers on the targeted line(s) are gone or downgraded
- no new blockers anywhere
- unflagged lines remain mechanically unchanged
- targeted lines still preserve factual meaning

Do not require:
- full-scene issue-count improvement
- full-scene claim-id superset

#### Acceptance for `scene_repair`

Keep stronger checks:
- no new blockers
- blocker resolution
- bounded line drift
- no severe fact coverage collapse

This separates local patch economics from full rewrite economics.

---

### P4. Add Fail-Fast Mode to `audit_citations`

File:
- `backend/app/pipeline/phase6_writing/citation_auditor.py`

Default behavior should be:
- once a scene remains `blocking=true` after all allowed attempts, stop the loop immediately
- do not continue auditing and repairing later scenes

Optional later extension:
- add explicit `full_report` mode for manual QA/debug only

Why:
- current full-pass behavior is too expensive for a real audio gate

Success criterion:
- one unresolved blocker stops further spend on later scenes

---

### P5. Keep Bounded Retry, But Only Where It Pays

Files:
- `backend/app/pipeline/phase6_writing/citation_auditor.py`
- `backend/app/config.py`

Keep:
- bounded audit retry
- second repair attempt for blocking scenes

But pair it with fail-fast:
- retries should protect against provider flake
- they should not justify finishing the whole episode after a confirmed unresolved blocker

---

## Suggested Implementation Order

1. P0 non-assertive post-filter
2. P1 line-repair mode with mechanical splice
3. P2 line-aware claim preservation
4. P3 acceptance split by repair mode
5. P4 fail-fast loop behavior
6. run fixture-heavy tests
7. only then do one real `audit_citations` rerun

---

## Testing Plan

Primary tests:

- `A1S6` exact repro:
  - question initially flagged
  - `line_repair` changes only the question
  - blocker removed
  - repair accepted

- non-assertive line can lose `claim_ids` without false rejection

- assertive line still requires factual grounding after repair

- `scene_repair` still rejects large drift / coverage collapse

- fail-fast stops after first unresolved blocker

Files:
- `backend/tests/test_citation_auditor_prompt.py`
- add dedicated fixture tests if needed for `A1S6`-style cases

---

## What Not To Do

- do not add word blacklists
- do not switch the whole auditor back to Sonnet as first move
- do not keep whole-scene rewrite as the default repair mode
- do not require exact-string equality for non-flagged lines when a mechanical splice can guarantee identity
- do not run another expensive full audit before these changes are in

---

## Expected Outcome

After this change set:

- false blockers on bridge questions should drop sharply
- one-line rigor problems should become cheap to repair
- acceptance should stop rejecting good local fixes for scene-level reasons
- expensive citation runs should stop earlier when already doomed
- the next rerun should test the new architecture, not the same known failure again
