# Local recovery of an unsent input-budget rejection

`resume --retry-input-budget INVOCATION_UUID` authorizes one new physical attempt of the exact archived request. Set `--max-visible-bytes` and/or `--max-input-tokens`; omitted thresholds inherit the target's existing allowance. Limits may only increase and must fit the recorded rejected request/count. Defaults for every other invocation remain 131072 visible bytes and 32768 input tokens. The run manifest, semantic request, source script, reviews, schema and profile are unchanged; only attempt metadata receives a new identity.

Admission requires a modern role invocation rejected by the local byte or token guard, with no submission intent, provider generation ID, output or ambiguous provider event. A byte rejection must precede even token counting. A token rejection must have the archived successful count response matching the recorded count. The immutable authorization records the actor, source invocation, failure receipt, measured components and both thresholds. A unique child attempt and deterministic identity make repeated authorization idempotent. Run locking remains mandatory at the public service boundary. Paid, uncertain, context-rejected or total-budget-rejected requests are ineligible.

The new attempt always performs the provider's real count. Both local limits, model context capacity, output reservation, total run token budgets and the existing source/time guard are checked before generation. Another local rejection needs a new explicit UUID-targeted action; an unknown submission is never retried. An allowance follows only exact-request operational credit retries, not another stage or a semantic repair. Proven unsent input-budget failures consume no generation reservation; their counts and events remain archived.

Apply migration `0016_input_budget_retry` before use. For the already archived Snow request, append the following to the normal existing project/actor/object-store CLI prefix:

```sh
resume --run-id 1fa0cbbd-6702-5f53-b164-3173bc6216d5 \
  --retry-input-budget 8ad0c870-a389-57ff-919b-eb29a8263e43 \
  --max-visible-bytes 262144 --max-input-tokens 65536 \
  --export-dir EXISTING_EXPORT_DIRECTORY
```

Its saved measurement is 134082 bytes (instructions 3455, input 126808, schema 3819). No provider count or generation was made for that rejected attempt. These thresholds are a local operational allowance, not a new writing behavior or an editorial instruction. The operator performs the resumed count/generation; implementation checks do not call external APIs.

`test_input_budget_recovery.py` is one serial SQL/gateway test, using synthetic provider replies. It covers byte rejection before provider construction, a real count exceeding the original token threshold, a separate token allowance, request/history preservation, one successful submit and idempotent reuse, conservative source refusal, and retained model-context/input/output-total guards.
