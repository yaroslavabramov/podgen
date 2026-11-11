# Technical recovery of writing calls

The ordinary `resume` operation recovers the first submitted, technically interrupted stage in the same run. It skips absent optional planning stages and keeps completed plans, scripts and readings. No invocation UUID, additional permission or new run is required. The run advisory lock covers reconciliation, replacement admission and execution.

Recovery first checks the locally archived completed response, validating its model, metadata and known `provider_response_id` through the same gateway path as the original response. A valid local completion requires no provider access. If the archive is incomplete, recovery retrieves the known response. A transient retrieve failure stops recovery without creating a replacement. A definitive unavailable response (404, 410 or a provider's explicit unsupported retrieval result) permits replacement. Submitted calls without an acknowledgement can also receive a linked replacement. Output validation failures, refusals and exhausted content budgets do not qualify as technical failures.

The immutable replacement request records `technical-recovery/1`, the original request and invocation, physical-turn evidence, actor and a maximum of two technical replacements per stage. These replacements are separate from the two author revisions and any address repair. The original ambiguous attempt and its usage reservation remain intact. Every replacement is counted and checked against the existing context, role, run and elapsed budgets. Subsequent resumes reuse the admitted attempt; they do not create another child of the same parent.

For a tool-enabled stage, the replacement starts at the exact interrupted model turn. Its full stored conversation includes prior model output and completed tool results. Source receipts retain their E-identifiers; completed source reads are not executed again. Model-turn and tool-call limits continue from the original position.

Existing manifests, prompts, schemas and outputs are not upgraded. The recovery policy is pinned in the appended authorization record. Export includes every attempt, its events and replacement authorization, with unknown usage still reported. Apply migration `0021_technical_recovery` before using this recovery path.

Lightweight checks: `backend/tests/test_technical_recovery.py`, `backend/tests/test_source_tool_runtime.py`, `backend/tests/test_source_tool_recovery.py`. PostgreSQL integration checks: `backend/tests/test_technical_recovery_runtime.py`; run serially with the other Podgen database tests.
