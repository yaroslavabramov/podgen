# Fact-only search and targeted source verification

Implemented `frozen-corpus-tools/8`. No paid API calls, model changes or production-run resumes.

## Ownership and invariants

- Physical Responses turns still own exact native counting, submission, provider receipts, budgets, credit recovery and uncertain outcomes. Native `web_search` uses the existing reviewer/model, not a parallel research provider. The request includes the provider's complete search source list.
- Only factual review receives search and anchored `request_research`. Complete plan/draft requests equal `/7` in regression tests. `/7` and earlier retain their declarations, instructions, commission and recovery behavior.
- Search output is navigation. Only retained `read_source` passages create E receipts. Search citations, X synthesis and validated excerpt locations cannot impersonate an E receipt.
- Both function requests and structured `research_requests` bind exact quoted anchors to the immutable caller script. Runtime verifies its hash and supplies full original turns plus one neighbor on either side. The private caller request retains the exact script reference; project-visible research receives verified text and the version hash, without a private journal reference.
- The research child, raw report, native request and acquired representations remain immutable. A new dedicated English commission replaces general episode research only for the new factual path. The output language stays in the user payload.
- Narrow answers contain at most six literal excerpts, a short answer and limitations. Explicit bounds reject invalid fields; nothing is truncated. Each excerpt must occur in the retained document, matched by URL and source path. The result supplies exact S ranges to read.
- Missing documents, malformed reports, absent evidence and invented quotes return explicit unavailable/unresolved results. No completed X answer is admitted. Retained documents remain usable in this same assessment. A repeated operation reuses its saved outcome and acquisition; concurrent completion uses the existing exclusive research-run lock, with an explicit busy rejection rather than duplicate work.

## Native search and recovery

`web_search_call + function_call`, `web_search_call + final message`, and restoration of the saved final are covered through the actual OpenAI SDK with a local transport. Search plus a later invalid output still retains search accounting. Successful search is not repeated after interruption or credit recovery; an ambiguous network result is not automatically resubmitted.

Search actions have separate `responses-web-search-receipt/1` records and `web-search.json` exports. New-run `run.json.web_search` distinguishes known calls from uncertain physical turns. Dollar amounts remain unknown; token/count/cache/refusal accounting stays in ordinary native receipts. Native search is never counted as a source-reading tool action.

Requests with hosted search enforce a 128,000-token window in checkpoint planning and final dispatch. Requests without it retain the saved model limit. This follows the [Responses web search limitation](https://developers.openai.com/api/docs/guides/tools-web-search#limitations), checked 2026-11-20. No profile is changed.

Version keys: `frozen-corpus-tools/8`, `targeted-script-context/1`, `targeted-research-handoff/2`, `targeted-research-answer/1`, `responses-web-search-receipt/1`.

## Complete-operation measurement

Real PostgreSQL, object storage, acquisition/extraction/admission and replay; local deterministic document transport; no paid research. Same discovery list and one necessary quoted document. Sizes 1/6/40 test growth. Raw measurements are in `measurements/`; test driver is `backend/tests/test_targeted_research_operation.py`.

| Discovered documents | Path | Fetches | SQL calls, resolve | Object bytes read | Bytes staged | Resolve seconds |
|---:|---|---:|---:|---:|---:|---:|
| 1 | legacy | 1 | 361 | 14,184 | 7,000 | 0.178 |
| 1 | anchored factual | 1 | 355 | 10,910 | 7,000 | 0.159 |
| 6 | legacy | 6 | 1,486 | 63,604 | 42,000 | 0.849 |
| 6 | anchored factual | 1 | 355 | 10,705 | 7,000 | 0.163 |
| 40 | legacy | 40 | 21,072 | 423,582 | 280,000 | 10.545 |
| 40 | anchored factual | 1 | 355 | 10,705 | 7,000 | 0.159 |

These are whole resolve operations, not isolated matching timings. Prepare/replay and SQL statement classes are recorded separately. Every replay made zero fetches and zero repository object-store reads. Object counters exclude cached Unicode projection reads; physical disk I/O was not measured. The process RSS high-water mark reached 181,600,256 bytes across these cases; it is not an independent per-variant memory comparison. Local timings are not a prediction of provider latency. The legacy path is intentionally frozen, including its existing growth; new narrow work does not acquire an unrelated full discovery corpus.

Still unverified: live provider acceptance/answer quality, behavior on long prior admission histories, provider-side search charges and network latency. These require a separately authorized paid experiment. The synthetic 1/6/40 fixtures measure operation growth; they do not demonstrate historical truth or editorial quality.

## Real saved script inspection

`real-script-commission.json` is an unsubmitted native research request compiled from the saved revised hieroglyphs script, utterance 58 and its two neighbors. It retains the actual comparison “Согласный на письме один и тот же”, rather than replacing it with a claim about the number of consonants. The profile equals the saved targeted-research profile. This inspects the actual model-facing commission, not an invented research answer.

## Checks

The targeted suite covers native SDK requests, strict legacy compatibility, exact full plan/draft equality, direct/structured binding, provider window admission, independent search accounting, interrupted/rejected/uncertain recovery, source-path ordering, malformed and fabricated excerpts, missing sources, concurrent completion and interrupted publication, plus full production continuation through planning and factual handoffs.

Final verification: **97 passed** on PostgreSQL, including all six complete-operation measurements and the native production handoff. Ruff and whitespace checks passed. No paid calls were made.

## Search continuation argument recovery

Follow-up pin: `source_tool_contract.search_argument_policy=recoverable-search-result-version/1`, enabled only for factual roles in newly prepared runs. Reusing an accurately copied result key and offset=6 with a rephrased query still fails in `StreamingSearchIndex`; the model receives a precise tool error and fresh-search arguments, without foreign hits. Only the new factual tool description clarifies that pagination preserves query and within verbatim; rephrasing starts at offset=0 with no key. No automatic search retry or fallback is performed. An authorization check precedes returning the error; storage/access failures and other conflicts still stop execution. Existing manifests without the pin retain the previous behavior.

Verification: one addressed PostgreSQL test through the real streaming index and native Responses loop (real previously issued key + offset=6 + changed query → delivered error → corrected next model call → successful search), including old behavior and access/storage propagation assertions. Additional compatibility checks: 44 passed, 1 database-dependent test skipped. Ruff and whitespace checks passed; no paid calls.
