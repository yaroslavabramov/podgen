# Hunter Query Variations (Experimental)

## Status
- Proposed, not enabled by default.
- Must be tested behind a feature flag.

## Idea
For each research question, generate one additional query variation and run retrieval on both:
- original question
- compact reformulation (noun-phrase style)

Merge both retrieval streams via RRF before extraction.

## Why this is not default yet
- Can increase recall, but can also add noise and query drift.
- Adds LLM cost and latency (even if batched).
- Effect is topic-dependent and language-dependent.

## Safe rollout plan
1. Add feature flag:
- `HUNTER_QUERY_VARIATIONS_ENABLED=false` (default)

2. Add lightweight batch rewriter:
- One Haiku call per batch of questions.
- Output exactly one reformulation per question.

3. Retrieval strategy under flag:
- run vector+keyword for original query
- run vector+keyword for reformulated query
- merge all hits with RRF
- keep existing diversity-aware selection

4. Guardrails:
- If reformulation is empty or near-duplicate, skip it.
- If reformulation changes named entities, skip it.

## Evaluation criteria (A/B)
- Asset coverage: share of questions where expected evidence types are found.
- Source diversity: unique `source_id` per question.
- Evidence usefulness: accepted EvidenceSpans per question.
- Cost/latency: end-to-end Hunter time and token usage.

## Success threshold to promote from experiment
- +10% asset coverage without:
  - >15% latency increase, and
  - >20% token-cost increase.

If threshold is not met, keep disabled.
