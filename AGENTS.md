# Working agreement

Be concise and direct. Write review findings as plain text, not inline comments.

Build a complete, reliable product. Do not use an MVP mindset or leave a workaround
where the task requires a proper solution. Follow the architecture and work order
in `docs/podcast-production/`.

## Engineering decisions and review

- Define ownership, invariants, failure handling, and data lifetimes before changing
  a subsystem. Fix the underlying operation; do not hide repeated work behind an
  unbounded cache or add a parallel implementation of the same responsibility.
- Keep cohesive modules and explicit interfaces. Split growing modules when the
  responsibility becomes unclear, rather than stacking special-case branches.
- Review the entire execution path, including database queries, dependency walks,
  object storage, indexing, provider requests, recovery, and model-facing context.
  Passing isolated tests is not evidence that the complete operation is efficient.
- For changes to these paths, measure a complete operation on representative data:
  SQL and network calls, repeated computation, bytes read/written, and peak memory
  where relevant. Check how work grows with documents and history. Record the
  baseline, resulting measurements, and what remains unverified.
- Reuse immutable results by content and configuration identity. Keep reusable
  computation separate from run-specific names, membership, and authorization.
- Batch related reads within a bounded operation. Preserve fresh authorization at
  external-action and public-read boundaries; never hold an authorization snapshot
  across network waits or replace access checks with a process-wide cache.
- Persist an exact provider count receipt once and reuse it only for its bound
  request and provider configuration. Distinguish counting, generation, cached
  input, rejected requests, and uncertain outcomes in accounting and reports.
- Verify retries and concurrent execution explicitly. Completed paid work must not
  be repeated accidentally; incomplete work must not be published as complete.
- Inspect changes before merging, and commit completed work in coherent units.
  Preserve unrelated changes. Report known defects instead of declaring readiness
  merely because tests pass.
- Write full evaluation exports and raw provider responses to `tmp/`. Keep
  conclusions and the fixtures needed to reproduce checks in `evals/`.

Run only one heavy Podgen test suite on this machine at a time. Coordinate the slot
with other agents and chats. Do not resume paid generation unless the current user
request authorizes it.

## Agent contracts

- Write production system prompts in English. Set the language of scripts and
  other generated text through `output_language` in the user payload. Preserve
  source quotations and identifiers exactly.
- Never truncate semantic fields during persistence. Return an explicit validation
  error when a field exceeds its contract.
- Keep compact model-facing views separate from complete persisted artifacts.
  Expose useful actions, results, and evidence references to models; keep database
  envelopes, provider IDs, duplicate encodings, and transport logs in the technical
  archive rather than automatically placing them in model context.
- When debugging an agent, inspect its actual prompt, payload, tool contract, and
  trace first. Do not add semantic gates before establishing that the agent received
  a sound task, sufficient context, and understandable tools.
- When reviewing a prompt, compare the complete model-facing request with the last
  verified version. State which decisions should change and inspect the resulting
  output at that stage. For planning, read both the plan and its separate review.
- Preserve reproducibility of saved requests. Version changed model-facing or
  transport behavior explicitly; do not silently alter archived preview or resume.
