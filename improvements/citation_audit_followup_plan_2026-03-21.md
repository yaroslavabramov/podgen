# Citation Audit Follow-Up Plan

Date: 2026-03-21
Scope: `backend/app/pipeline/phase6_writing/citation_auditor.py` and adjacent phase 6 quality flow

## Goal

Keep `citation_audit` as a real audio gate, but make it:

- faster
- less noisy
- less likely to rewrite scenes into worse dialogue
- more likely to resolve real blockers instead of spending repair budget on phantom issues

The target outcome is a phase 6 quality stack that protects factual rigor **without** flooding the script with dry attribution language or blocking audio on avoidable audit noise.

## Current Problems

### 1. Audit context is too large

Current state:

- each scene audit receives the full run-level `claims_map`
- in the latest run this was roughly `84` claims for a single scene
- this increases:
  - latency
  - timeout risk
  - `missing_claim_link` noise
  - claim matching ambiguity for Haiku

Observed symptom:

- `A3S1` failed with `TimeoutError`
- many scenes got medium/low citation noise on lines that were mostly rhetorical or synthetic

### 2. Re-audit is too broad and too noisy for repair acceptance

Current state:

- repair uses Sonnet
- then the entire repaired scene is re-audited
- `_citation_repair_improved(...)` compares:
  - worst severity
  - issue count
  - `fact_coverage_ratio`

Observed symptom:

- repair often fixes the original flagged issue
- re-audit then invents new medium/high issues on different lines
- repair gets rejected even when it likely removed the original blocker

This wastes repair budget and keeps scenes blocked for the wrong reason.

### 3. Repair prompt is too free

Current state:

- citation repair is supposed to be a rigor fix
- in practice it sometimes rewrites scene function or changes the beat shape

Observed symptom:

- some accepted repairs improve rigor but make the dialogue more academic
- at least one accepted repair moved the scene closer to explanation instead of just fixing citation/risk language

This means the current rollback criteria are not enough.

### 4. Citation policy is too aggressive on non-assertive lines

Current state:

- rhetorical questions
- clarifying questions
- synthesis lines
- listener-facing summary lines

often get `missing_claim_link` or related findings even when they do not introduce a new factual assertion.

This inflates failure counts and makes the audit less trustworthy.

### 5. Blocking flow is technically correct but operationally rough

Current state:

- unresolved `scope=block` issues now block audio correctly
- audit failure also blocks correctly
- but the graph still stops via exception rather than a graceful halted state

This is acceptable for safety but poor UX.

## Design Principles

- Keep fail-closed behavior for true audit failure or unresolved blocking issues.
- Prefer cheaper detection and more capable repair.
- Do not add banned-word lists or case-specific vocabulary rules.
- Do not remove post-repair checking entirely.
- Do not let citation repair change scene purpose, chronology, or dramatic function.
- Separate:
  - factual rigor fixes
  - dialogue polish

## Target Operating Model

Phase 6 citation quality should work like this:

1. Scene-level citation audit runs on a **scene-relevant claim subset**
2. If there are only `record` findings, scene passes
3. If there are `repair` or `block` findings, citation repair runs
4. Post-repair validation checks:
   - original blocking issues resolved or downgraded
   - no new blocking issues introduced
   - required claim coverage preserved
   - scene function not visibly altered
5. If unresolved `scope=block` remains, audio is blocked

## Execution Plan

### P0. Narrow Audit Context

Goal:

- reduce prompt size
- reduce phantom matching errors
- reduce timeout risk

Changes:

- Build a scene-local claim context instead of passing full run-level `claims_map`
- Include only:
  - claim IDs already referenced in `dialogue_json.claim_ids`
  - scene-assigned claims from manifest beat sheet
  - optionally a very small support halo:
    - callback/support claims from the same scene
    - no whole-episode spillover

Implementation direction:

- add manifest-backed lookup for scene claim ids
- add helper to compute:
  - `dialogue_claim_ids`
  - `scene_claim_ids`
  - merged deduped `audit_claim_ids`
- feed only those claims into `_build_citation_audit_prompt_payload(...)`

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`
- possibly shared helpers in `backend/app/pipeline/phase6_writing/dialogue_utils.py`

Expected impact:

- smaller audit payloads
- fewer false `missing_claim_link`
- fewer audit timeouts

### P1. Replace Full Re-Audit Acceptance with Targeted Blocking Re-Check

Goal:

- stop rejecting useful repairs because of fresh medium/low noise

Current problem:

- `_citation_repair_improved(...)` is too coarse
- it treats all new issues as equally important

New acceptance model:

- Keep a normalized record of original issues
- Separate original issues into:
  - `original_blocking_issues`
  - `original_repair_issues`
- After repair, run a **targeted re-check** focused on:
  - whether original blocking issues are still present in substance
  - whether repair introduced any new `scope=block` issues

Acceptance should succeed if:

- original blocking issues were removed or downgraded out of `scope=block`
- no new blocking issues appeared
- original claim coverage did not regress:
  - `original_claim_ids.issubset(repaired_claim_ids)`

Medium/low noise should not automatically reject repair.

Implementation options:

- Option A:
  - keep full re-audit, but change acceptance logic to compare only blocking issues
- Option B:
  - create a new targeted re-check prompt that only evaluates:
    - flagged issue list
    - repaired scene
    - whether those blockers remain

Preferred first move:

- Option A first, because it is cheaper to implement
- Option B only if Haiku remains too unstable after context reduction

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`
- tests in `backend/tests/test_citation_auditor_prompt.py`

### P2. Tighten Citation Repair Guardrails

Goal:

- make repair fix rigor without re-authoring the scene

Prompt changes:

- repair is not allowed to change scene function
- repair is not allowed to move explanation earlier than planned
- repair is not allowed to turn question/setup beats into explanation beats
- repair should prefer:
  - adding/adjusting claim links
  - adding modest uncertainty markers
  - tightening overconfident wording
  - replacing unsupported precision with supported precision
- repair should avoid:
  - expanding the scene
  - introducing new exposition
  - changing the landing

Payload additions:

- `scene_job`
- `scene_type`
- maybe compact scene brief / description

Implementation note:

- do not duplicate manifest loading logic here
- reuse the same scene-job lookup path already introduced for dialogue polish
- preferred location for shared lookup/helper logic:
  - `backend/app/pipeline/phase6_writing/dialogue_utils.py`

Validation additions:

- check claim coverage preservation:
  - `original_claim_ids.issubset(repaired_claim_ids)`
- add simple structural guard:
  - line count should stay near original:
    - `abs(len(repaired_lines) - len(original_lines)) / max(len(original_lines), 1) <= 0.3`
  - opening line function should not be radically different

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`

### P3. Relax Audit Expectations for Non-Assertive Lines

Goal:

- reduce noisy failures on natural dialogue moves

Prompt changes for citation auditor:

- not every line needs a claim link
- rhetorical questions do not need claim ids
- clarifying questions do not need claim ids unless they introduce new factual content
- synthesis lines may summarize supported claims without repeating explicit citation on every clause
- interpretation should be flagged only when it presents unsupported factual certainty, not whenever it compresses prior material

This should specifically reduce:

- `missing_claim_link` on question lines
- `missing_claim_link` on synthesis lines
- over-flagging of conversational transitions

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`
- tests in `backend/tests/test_citation_auditor_prompt.py`

### P4. Add One Extra Repair Attempt for Real Blocking Scenes

Goal:

- give genuinely fixable blockers one more shot

Rule:

- only scenes with unresolved `scope=block` after first repair get a second repair attempt
- max citation repair attempts per scene: `2`
- no infinite loops

Why:

- blocking scenes are few
- second attempt is worth the cost only for those scenes
- this is cheaper than re-running the whole writing step

Files:

- `backend/app/pipeline/phase6_writing/citation_auditor.py`
- TODO/docs/tests

### P5. Graceful Audio Halt

Goal:

- keep safety
- improve UX

Current state:

- unresolved blockers raise `RuntimeError`

Target:

- graph stops before audio with a durable halted state
- UI can show:
  - `audio_blocked = true`
  - blocking scenes
  - reason

This is lower priority than P0-P4 because current behavior is ugly but safe.

Files:

- `backend/app/pipeline/graph.py`
- worker/run state handling
- frontend/API only if needed

## What Not To Do

- Do not revert to fail-open on audit exceptions.
- Do not remove all post-repair checking.
- Do not switch everything back to Sonnet before reducing prompt scope.
- Do not use word blacklists or surface-style fixes inside citation repair.
- Do not let citation repair become a hidden dialogue-polish pass.

## Test Plan

### Unit tests

- scene-local claim subset builder only includes relevant claim ids
- `_has_blocking_citation_issues(...)` stays scope-based
- `_citation_repair_improved(...)` accepts `block -> repair` and `block -> record`
- targeted acceptance ignores new medium/low noise if blockers are resolved
- repair guardrails reject scene-function regression where possible

### Prompt tests

- citation auditor prompt explicitly exempts rhetorical/clarifying lines from universal claim-link requirement
- citation repair prompt explicitly preserves scene function / scene job

### Integration-like tests

- audit exception still blocks audio
- large-run scene with no blocking issues but some record issues does not block
- repaired scene with downgraded blocker passes even if record issues remain
- blocking scene gets at most two repair attempts

## Success Criteria

We should consider the follow-up successful if a comparable rerun shows:

- no audit timeout on normal scene payloads
- materially fewer phantom `missing_claim_link` findings
- higher repair acceptance rate on scenes with genuine fixes
- fewer accepted repairs that make dialogue stiffer or more explanatory
- audio blocked only by:
  - real unresolved factual blockers
  - genuine audit failure

## Recommended Implementation Order

1. P0 narrow audit context
2. P1 targeted blocking-focused acceptance
3. P3 relax non-assertive line policy
4. P2 stricter repair guardrails
5. rerun `audit_citations`
6. only then decide whether P4 second repair attempt is still necessary
7. P5 graceful audio halt last
