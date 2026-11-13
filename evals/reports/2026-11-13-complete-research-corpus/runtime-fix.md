# Research metadata index verification

Branch: `codex/research-metadata-index-20261113`; isolated worktree `/tmp/podgen-research-metadata-20261113`; base `6aece833`. Main checkout and live run were not modified. No paid provider calls.

## Change

ResearchHistory retains small count/plan/segment/submitted-turn/response-reference/receipt indexes, updated only after durable event writes. Full arguments, working state, source passages, transcripts and native response bodies remain lazy durable references. Response replay reads only the matching physical turn instead of scanning every later response.

Cold restore uses the existing full authorization graph reader within one local repeatable-read read-only snapshot. Full event bodies and directly linked physical requests are evicted after projection. Common provenance is memoized only for this restore. Raw history/response/blob reads and existing provider/tool guards still obtain fresh authorization; no snapshot crosses a provider/network wait. Prompt definitions, version pins, persisted event contents and native request serialization remain unchanged.

## Verification

- 80 related tests passed together on an owned temporary PostgreSQL instance: metadata index/restore/wire, research runtime, source-tool runtime/recovery, authorization, cache wire, navigation and prompt resources. The final added paused-run scenario and index test also passed separately (six tests). Ruff and diff whitespace checks passed.
- Native count/submit request bytes, client request IDs, tool execution and usage match linear metadata lookup behavior for ordinary, checkpoint, credit, pending-source and paused-run flows.
- Paused-run test completes turns 0..21, resumes with turn 22, and asserts the submitted sequence is exactly 0..23 with no repeated provider submissions or source reads. Existing real-DB tests cover credit recovery and technical count failure after checkpoint rotation.
- Cold restore test verifies common provenance is fetched once, full event/physical-request bodies are absent from the authorization memo after each projection, raw content stays exact, and source withdrawal denies both a subsequent raw read and a new restore.

## Read-only saved-live benchmark

Database connection enforced PostgreSQL `default_transaction_read_only=on`. Actual pre-change ResearchHistory was loaded from commit `6aece833`. Dataset: run `f7e9dbac-c64c-5698-807a-fa8b25e69a2f`, latest physical turn 21, 22 submitted turns, 18 read receipts.

| Operation | Before | After | SQL before / after |
| --- | ---: | ---: | ---: |
| Cold history restore | 32.3470 s | 0.9851 s | 362,562 / 10,229 |
| Latest count + context plan + submitted turns | 48.7028 s | <0.0001 s | 544,170 / 0 |

Decisions, receipt IDs, call IDs and event-reference order matched exactly. This measures the identified metadata component, not total role latency; material guards and fresh raw reads retain their existing costs. Native benchmark: `/tmp/podgen-metadata-benchmark-20261113.json`.

The live run remains paused. Merge/review/resume belong to the coordinating task; next paid physical turn must be 22.
