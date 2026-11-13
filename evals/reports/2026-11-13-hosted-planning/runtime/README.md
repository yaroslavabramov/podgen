# Hosted research writing integration — offline handoff

13 November 2026. No model endpoint, token-count endpoint or new hosted research was called for this preparation.

The exact saved longitude report is admitted as ten complete M sections. The 62 S entries follow the final Markdown Sources numbering. Of 20 selected acquisitions, 15 have readable text: 14 available and S39 partial (retained chapter summary/metadata). Five attempted acquisitions are unavailable; 42 entries are not acquired. S42/S49 remain available catalogue documents; their historical evidentiary role is determined by reading them. S49's display title `K1` comes from the leading retained heading with its provenance; the source text is unchanged.

`plan.preview.json` is the actual initial request for run `293c8b5a-53df-5483-92ec-80c9bd7f2258`, using the supplied assignment unchanged. It contains assignment, research contents and source catalog, with no derived obligations or human factual errata. Current admission: `shared-ref.json`; all payload and source bytes are retained in the repository/object store.

The other seven previews use this same admitted research plus explicitly labelled **PREVIEW FIXTURE ONLY** plan/script/feedback in `fixtures/`. These fixtures test role inputs and instructions; they are not model-generated editorial work or the episode's accepted plan. Plans/reviews/scripts were validated against their actual output models, and wire plans, plan reviews and first audits against their actual response JSON schemas. The removed historical limitations fields are absent. The production reader's current General listeners audience fallback matches the live behavior profile when listener_profile is absent; no audience policy was changed.

`instructions/` contains full actual system messages. `instructions.diff` compares them to the archived verified baseline. `preview-summary.json` lists sizes, fields and tools. Research-only instruction adaptation, evidence-description adaptation, projection/sectioning and admission are frozen by `research_material_contract`; old inputs retain existing assets and tools.

The completed PostgreSQL workflow test covers plan → independent plan review → plan revision → second review → author → independent reader/fact audit → required rewrite → repeated audits, plus exact preview equality and resume without repeated calls. Source E2 survives as P1 and later H1; report R1 is excluded from factual evidence. Separate tests verify exact Unicode paging/context budgets, original/fallback provenance, sparse S numbering, unavailable overrides, project/blob withdrawal and durable report-read replay.

Run checks (one suite at a time): 22 passed for first admission/tool/legacy planning regression group; 38 passed for broader production, audit, recovery and frozen-schema checks; 7 passed after ACL/replay additions; 16 passed after schema adapter and native output validation. Final focused rerun after the exact editorial schema wording: 13 passed in 32.52 seconds. Ruff and git diff whitespace checks passed.

Commands from repository root:

```sh
bash evals/reports/2026-11-13-hosted-planning/runtime/commands.sh prepare
bash evals/reports/2026-11-13-hosted-planning/runtime/commands.sh preview
bash evals/reports/2026-11-13-hosted-planning/runtime/commands.sh run
bash evals/reports/2026-11-13-hosted-planning/runtime/commands.sh inspect
```

Prepare/preview are offline; run/resume use paid providers. `run` uses the prepared Opus writer, Sol planner and Terra reviewers at their existing effort/output/context settings, with the ordinary one plan revision/two script revision limits. `inspect` exports retained requests, replies, traces and final scripts. The additional full workflow/export and historical export checks passed: 3 tests in 41.31 seconds. `integration-fixture/` contains the exact executed factual-task handoff and its tool receipts. Coordinator owns the live run after reviewing the actual request and instruction changes.
