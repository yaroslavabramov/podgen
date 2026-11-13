# Bounded Anthropic grammar and addressed recovery

Worktree: `/tmp/podgen-anthropic-grammar-recovery-20261113`, branch `codex/anthropic-grammar-recovery-20261113`, base `94ba1332`. Main and live storage unchanged. No paid calls or provider network tests.

## Diagnosis

Actual refusal: run `f7e9dbac-c64c-5698-807a-fa8b25e69a2f`, revise-1 invocation `ffb97724-8950-5436-a690-b6847d6032b6`, provider_error `88f310d2-f455-41d5-a8f4-3b2e014dfc67`. HTTP 400 invalid_request_error, grammar too large, ambiguous:false, no generated response. Native request: `/Users/yaroslavabramov/side/podgen/tmp/longitude-corpus-2026-11-13/rejected-revise-native-request.json`.

13 strict tools and 13 combined union nodes were below documented explicit limits, but Anthropic documents additional internal compilation limits and recommends keeping only critical tools strict. count_tokens does not compile the grammar. Source: https://platform.claude.com/docs/en/build-with-claude/structured-outputs#schema-complexity-limits .

## Implementation

Production and initial regression commit: `70fdf97d`. Test-only follow-up `65d79372` updates four OpenAI HTTP mocks to httpx2; Anthropic mocks remain on httpx.

`locally-validated-navigation/1` disables native strict sampling only for ten explicitly named read/navigation tools. Three critical tools stay strict: request_source, request_research, save_working_state. Their complete canonical JSON Schemas and the existing Draft202012Validator-before-execution gate stay intact. Unknown future tools keep their existing strict settings. Native output_config.format is unchanged.

New manifests pin messages_tool_policy within their provider compilation contract. Normal stage and snapshot-preview compilers read this pin explicitly. Missing pin in an old manifest means legacy strict wire, including previously unprepared stages; simply installing new code does not upgrade them. Unmarked archived requests retain exact native translation.

Existing addressed technical recovery recognizes only an immutable exact Anthropic grammar rejection: one matching submit, one nonambiguous HTTP400 submit error with recognized invalid_request_error text, no response/output/usage, matching saved native request, and at least one strict navigation tool that the new policy changes. Generic400, ambiguous outcomes, changed request/evidence, generated responses, already-upgraded policies and output-only complexity do not qualify. A durable child technical attempt changes only standard attempt identity metadata plus the explicit native policy marker. Failed parent, exact inputs, schema, instructions, context, response archives and prior paid receipts remain immutable. No implicit transport retry. Existing maximum technical replacements and two semantic rewrites remain intact.

Proven no-generation grammar rejection contributes zero generated usage; prior completed physical turns retain their accounting. The coordinator independently checked the production evidence reader on the actual saved refusal and obtained the expected event ref/policy without writes or API calls.

## Verification

- Real PostgreSQL test: completed paid read -> grammar400 -> addressed child retry -> invalid non-strict read args -> explicit tool error -> final response. No repeated source execution, rejected generation not charged, parent events/request unchanged, full prior passage preserved, completed replay sends nothing.
- Snapshot-preview test verifies an old manifest stays strict, a new provider policy pin changes only request metadata, and a new preview uses the new policy.
- Rejection regressions cover ambiguous failures, arbitrary400, wrongstatus, paid response ID/output, changed/missing native request, already-current policy, no navigation tools, response events and duplicate submits.
- 28 archived historical Podgen requests were serialized with both the original messages_wire from base94ba1332 and the new translator. All56 count/submit native request bodies matched byte-for-byte.
- 131 tests passed with four pre-existing cases deselected before the mock fix; the final strengthened grammar module passed16 tests. Four baseline failures were reproduced on `/tmp/podgen-research-metadata-20261113` before this grammar patch (same installed SDK); each failed with httpx/httpx2 response-type incompatibility, not production behavior. Final full set result will be appended below.

Native comparison artifact: `/tmp/podgen-grammar-native-comparison-20261113.json`. Actual rejected request changes13->3 strict tools and13->4 constrained union nodes. All top-level fields except tools, every tool parameter schema/description, and output schema remain identical.

Provider compilation acceptance itself is not locally measurable and was not tested live; the user requested no further generation.

## Final result

**136 passed in109.55seconds**, one isolated pytest run on an owned migrated PostgreSQL instance, including all four corrected compatibility scenarios. No deselections, failures or paid provider calls. Ruff passed; worktree clean. Apply `70fdf97d` then `65d79372`.

## Integration and stop

Integrated as `69a5c227` and `96ff5759` after coordinator review, completing the current code work under the user’s instruction to stop further generation. No live retry was submitted; provider acceptance of the revised grammar remains unverified.
