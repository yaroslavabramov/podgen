# Historical context count replay

Base: `5486cc79`. No paid provider calls or production-run continuation.

The original implementation ignores historical `source_context_count` events without
`count_identity`. Two real-PostgreSQL regressions reproduce an ordinary interrupted
window for OpenAI and Anthropic through their actual adapters and local HTTP mocks.
The historical writer saves the original four data fields (`request_hash`,
`input_tokens`, `turn_index`, `label`) plus the original provider reply/wire archive;
it saves no modern count intent or identity. One completed source-reading turn is
followed by a crash after the next window's context count.

| Traffic per provider | Before resume | Original after resume | Fixed after resume |
| --- | ---: | ---: | ---: |
| Native count calls | 3 | 5 | 3 |
| Generation submissions | 1 | 2 | 2 |
| Source executions | 1 | 1 | 1 |

The original implementation fails both regressions (`5 != 3`). The fix passes.
Tests compare the actual historical context/gateway count requests and the saved
unfinished native count request with the physical request submitted after resume.
Completed replay adds no count, generation or source execution. All prior root
and physical-turn raw events remain unchanged.

Reuse requires a successful, unredacted provider reply, matching integer response,
matching canonical native request, same invocation/request binding, and the full
provider configuration loaded from the original request's immutable ModelProfile.
Canonical comparison preserves JSON types. Missing or mismatched evidence obtains
a new modern bound count; a hash/token pair alone is insufficient. The gateway
validates the original receipt again before adopting it for submission.

Validation: **32 passed in 3.10 s**, fresh PostgreSQL at current migration head:
`tests/test_legacy_count_receipts.py` and `tests/test_exact_count_receipts.py`.
Ruff and diff checks passed. Cases cover request/schema/tools, wire/value types,
response/status/redaction, missing profile/wire, provider/adapter, parameters,
timeouts and context/output limits. Only the existing Alembic deprecation warning
was emitted. SQL/byte/RSS performance was not measured by this narrow replay test.
Temporary owned PostgreSQL servers were stopped; no existing server was touched.
