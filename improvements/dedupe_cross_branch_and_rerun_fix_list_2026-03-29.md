# Dedupe Cross-Branch And Rerun Fix List (2026-03-29)

## Scope

This file captures the next fixes for `dedupe_claims` after the latest rerun analysis.

Current observed problems:
- cross-branch semantic dedupe now produces real suggestions, but overlapping windows can emit contradictory keep/drop directions for the same pair
- apply logic can create merge cycles (`A <- B`, then `B <- A`)
- isolated `dedupe_claims` rerun still works from stale `state["claim_ids"]` instead of the previous-step output, so debug shows `requested > loaded` even when DB is healthy
- some cross-branch overmerges still come from the Haiku prompt itself, not from a deterministic veto layer

## Non-Goals

- Do **not** reintroduce a deterministic semantic veto after Haiku
- Do **not** add a new QC/judge pass
- Do **not** do manual claim canonicalization

## P0 — Fix Cross-Branch Apply Semantics

### 1. Prevent merge cycles

In `backend/app/pipeline/phase4_claims/dedupe.py`:
- before applying any cross-branch merge, resolve both `keep_id` and `drop_id` to their current live representative
- if `keep_id` is already dropped/redirected, retarget the merge to its live canonical keep
- if both sides resolve to the same canonical id, skip the merge
- never allow a claim that is already in `duplicate_map` as a drop to act as a later keep

Goal:
- no more `A -> B` and `B -> A` cycles in summary/debug

### 2. Consolidate overlapping-window suggestions before apply

Current issue:
- cross-branch Haiku runs on overlapping windows in parallel
- the same pair can be suggested multiple times, sometimes in opposite directions

Fix:
- gather all cross-branch suggestions first
- normalize them into pair families before applying anything
- deduplicate identical pairs across batches
- if both directions appear for the same pair, choose one canonical keep deterministically:
  - prefer higher `evidence_count`
  - then prefer more complete statement length
  - then stable tiebreak by `claim_id`
- only after consolidation, apply merges sequentially

Goal:
- overlapping windows improve recall without creating contradictory apply behavior

## P1 — Tighten Cross-Branch Prompt, Not Runtime Veto

In `backend/app/pipeline/phase4_claims/dedupe.py` `_DEDUPE_SYSTEM`:
- keep the current anti-overmerge guidance
- add sharper contrastive examples for these still-bad families:
  - `setup -> later outcome` is not a duplicate
  - `earlier mint/launch step -> later rejection/closure` is not a duplicate
  - `baseline fact -> comparative framing` is not automatically a duplicate
  - `baseline mechanism -> broader interpretation/evaluation` is not a duplicate
- explicitly say:
  - when one claim adds a later stage, consequence, comparator frame, or evaluative framing, keep both
  - cross-branch duplicates should be only near-paraphrases of the same proposition

Goal:
- reduce Haiku overmerges without adding a second local judge

## P1 — Fix Isolated `dedupe_claims` Rerun UX

Current issue:
- isolated rerun still starts from stale `state["claim_ids"]`
- after a prior dedupe pass, some of those ids are already deleted, so debug shows fake noise (`requested > loaded`, missing ids)

Desired behavior:
- isolated rerun of `dedupe_claims` should use the previous node output (`build_claims`) as its input boundary
- not the historical pre-dedupe snapshot from the dedupe node itself

Implementation direction:
- for step rerun of `dedupe_claims`, resolve input `claim_ids` from the last persisted output of `build_claims`
- only fall back to raw state if no upstream step output exists

Goal:
- rerunning dedupe means “run dedupe again on the output of claim builder”, not “replay an old stale dedupe input snapshot”

## P2 — Keep Debug Explicit

Debug is now much better and should stay explicit.

Preserve and extend:
- `suggested_merges`
- `applied_merges`
- `skipped_merges`
- `skip_reason`

Add if useful during implementation:
- `canonicalized_keep_id` / `canonicalized_drop_id` after consolidation
- `conflict_group_id` for opposite-direction pair proposals from overlapping windows

Goal:
- every rerun should answer:
  - did Haiku propose the merge?
  - did overlap create duplicate/conflicting proposals?
  - what canonical pair was finally applied?

## Tests

Add/update tests for:
- contradictory cross-branch pair proposals from overlapping batches do not create a cycle
- same pair suggested in multiple windows is applied once
- opposite-direction suggestions choose one deterministic keep
- isolated `dedupe_claims` rerun pulls input from previous-step output instead of stale dedupe snapshot

## Success Criteria

- no claim in dedupe summary ends up dropped into a claim that is itself dropped into the first claim
- cross-branch duplicates can still merge
- `setup -> outcome` and `baseline -> compare-tail` pairs stop overmerging as often
- isolated rerun debug no longer shows stale missing ids caused only by rerunning dedupe over its own old input snapshot
