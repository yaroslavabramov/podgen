# Prompt Best Practices

General rules for writing production prompts in this project.

This document is intentionally generic. It does not define product-specific content policies.

## 1) Core principles

- Prefer behavior constraints over literal wording.
- Make outputs machine-parseable by default.
- Keep instructions non-ambiguous and non-contradictory.
- Treat prompts as contracts, not creative essays.
- Optimize for determinism first, style second.

## 2) Prompt structure

Use two layers:

1. `System` prompt:
   - role and scope
   - hard constraints
   - output contract
   - failure behavior (`return []`, `null`, explicit fallback)
2. `User` prompt:
   - task payload only
   - compact JSON input
   - no duplicated policy text from system prompt

Do not mix long context, examples, and constraints in one unstructured block.

**Caching rule:** keep system prompts static (no per-request data). Anthropic caches identical system prompts across calls — dynamic content in system defeats caching and increases cost. All variable data (IDs, payloads, context) belongs in the user prompt.

## 3) Output contract rules

- Always specify top-level type (`object` or `array`).
- Always specify required keys.
- For constrained fields, use explicit enum values.
- Define null/empty behavior explicitly.
- If strict JSON is required, explicitly forbid markdown/prose wrappers.
- Use consistent enum casing across all prompts. Default: **lowercase**, matching DB models.

When output is consumed by code, never rely on "best effort formatting".

**Schema enforcement:** prefer Anthropic tool_use with JSON schema over freeform "return JSON" instructions when the output structure is rigid and predictable. Tool_use guarantees valid JSON without relying on `parse_llm_json` workarounds. Reserve freeform JSON for cases where the schema is flexible or deeply nested.

## 4) Language and style control

- Pass output language explicitly as an input field (`output_language` in user payload).
- Require one output language for all human-readable fields.
- Avoid mixed-language output unless explicitly allowed.
- Keep tone constraints short and testable.
- Use "what to do" rules; avoid "how to phrase exact sentences".

**Multi-language strategy:** always write the system prompt in English and control output language via an explicit `output_language` field in the user payload. Do not write system prompts in the target language — this breaks caching (separate cached prompt per language) and makes prompts harder to maintain. The model should receive one instruction: "All human-readable output fields must be in {output_language}."

## 5) Avoid overfitting to canned phrasing

Do not include:

- literal sentence templates
- quoted example lines for final script text
- repeated hedge formulas to be copied verbatim
- long lists of narrow phrase bans that suppress natural speech

Instead:

- define abstract speaking behavior
- define banned pattern categories, not exhaustive phrase catalogs
- enforce with post-validation where possible

## 6) Context management

- Bound all long inputs (`[:N]`, top-K, summaries).
- Pass only task-relevant context.
- Separate source facts from stylistic instructions.
- Remove duplicate or overlapping constraints.

If a prompt grows large, split the task into smaller calls.

**Token budget awareness:** estimate input token count before calling. If input approaches 50% of the model's context window, the output may be truncated even with generous `max_tokens`. For large inputs: summarize, paginate, or split into multiple calls. Document expected input size ranges in a comment near the prompt constant.

## 7) Reliability and error handling

- Expect malformed outputs; parse defensively.
- Validate and normalize outputs in code.
- Use retries only for transient/model-format failures.
- Use deterministic fallback when safe.
- Log failure reasons in a compact, actionable form.
- Document all fallback behaviors explicitly — no silent grace-degrades hidden only in code.

Prompt quality is not a replacement for runtime validation.

## 8) Formatting best practices

- Use short numbered rules for critical constraints.
- Keep schema examples minimal and aligned with parser expectations.
- Keep one naming style for keys (snake_case recommended).
- Keep units explicit in fields (`seconds`, `pct`, `tokens`).
- Keep boolean semantics explicit (`true` means X, `false` means Y).

## 9) ID passthrough discipline

When a prompt must return identifiers (UUIDs, IDs) that reference existing objects:

- The prompt must explicitly state: "Only use IDs from the provided input. Do not invent new IDs."
- Code must validate returned IDs against the allowed set and discard unknown ones.
- Never ask the model to generate new UUIDs — create them in code and pass in.

## 10) Batching guidance

When sending multiple items for processing in one call:

- **Batch size 5-10** is the sweet spot for structured tasks (verification, classification). Larger batches increase the chance that one malformed item corrupts the entire response.
- **Never batch creative/generative tasks** (dialogue writing, planning). Each item needs full attention.
- Always include the item ID in both input and expected output so results can be mapped back.
- Parse batch results per-item: if one item is missing from the response, fall back to individual processing for that item, not the whole batch.

## 11) Temperature guidance

Match temperature to task type:

| Temperature | Use case | Examples |
|-------------|----------|----------|
| 0.0–0.2 | Classification, verification, extraction | verifier, evidence extractor, canonicalizer |
| 0.3–0.5 | Structured planning, analysis | decomposer, showrunner, act planner, conflict resolver |
| 0.5–0.7 | Creative writing with constraints | screenwriter, show bible generation |

Do not use temperature > 0.7 for tasks that produce structured JSON — high temperature increases malformed output risk.

## 12) Common anti-patterns

- One giant prompt doing planning + writing + auditing at once.
- Contradictory instructions ("be concise" and "be exhaustive").
- Asking model to invent runtime-owned identifiers.
- Excessive examples that become copied output.
- Hidden constraints only in code, not in prompt contract.
- Prompt-only safety without post-generation checks.
- Dynamic data in system prompt defeating prompt caching.
- Inconsistent enum casing between prompts targeting the same DB field.
- Asking for "exact verbatim quote" when input is truncated — model will hallucinate beyond the truncation boundary.

## 13) Change checklist

Before merging a prompt change:

1. Is the output contract explicit and parseable?
2. Are constraints atomic and non-conflicting?
3. Is language handling explicit (`output_language` field)?
4. Did you remove literal sentence templates/examples?
5. Is context size bounded and documented?
6. Is failure behavior defined in the prompt?
7. Is normalization/validation updated if schema changed?
8. Are trace/log payloads still compact and safe?
9. Did you test both nominal and malformed outputs?
10. Did you compare regression risk on downstream nodes?
11. Are enum values consistent with other prompts and DB models?
12. Is the system prompt static (no per-request data)?
13. Does `max_tokens` accommodate expected output size?
14. Are returned IDs validated against allowed sets in code?
