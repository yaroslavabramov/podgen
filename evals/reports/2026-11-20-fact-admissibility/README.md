# Factual admission and recheck, provider-free verification

New production runs pin `reading_audit_consistency_contract=decision-findings/2`.
The base audit/schema keys and model profiles are unchanged. Old assets, `/1`
descriptions and validation remain available; archived runs do not acquire `/2`.

The saved hieroglyphs fact recheck after the first rewrite returned `keep` with
three explicit unchecked decisions: D4, D7 and D12. The new consistency policy
rejects that combination; the old policy still reproduces it. This checks an
actual recorded admission defect, without another factual generation.

Other contract tests cover required removal/correction of unsupported asserted
details, optional refinement of supported wording, preserving retained verdicts
and evidence aliases, and a concrete late reopening. Contextual admission cases
distinguish a temporal contrast within an investigation from global priority, and
“the same consonant” from “only one consonant.” These are contract/packet checks,
not evidence that a model will make the right semantic judgment.

## Actual model-facing comparison

The first fact review and recheck instruction bodies were compared with the last
verified hieroglyphs requests, including their complete supplied payload, history
and schemas. Applying the existing research adapter and the frozen tool contract
to each old asset reproduces the archived instruction bytes exactly.

Holding that complete input and its old tools fixed isolates the new instruction
and schema-description changes:

| Physical request | Canonical bytes before | After | Instruction bytes before | After |
| --- | ---: | ---: | ---: | ---: |
| First fact review | 171,401 | 174,089 | 15,229 | 17,245 |
| Factual recheck | 370,245 | 373,540 | 19,519 | 22,142 |

The longest retained physical request per role was used, rather than counting only
an opening prompt. Source text, script, prior decisions, tool declarations and
model parameters were held fixed. The new instructions ask for exact contextual
claims, concrete details, cross-passage consistency, quick lookup before deeper
research, and reading actual source passages. The recheck carries accepted proof
forward and checks changed/dependent meaning or a specifically justified reopening.

This is offline request assembly from preloaded files, not a live workflow
benchmark. Peak Python allocations were about 0.25/1.14 MiB for those assemblies.
SQL, network, provider generation and exact token-count calls: zero. Token counts,
paid latency, new tool execution and actual model quality remain unmeasured.
Full values, hashes, timings, method and paths are in [measurements.json](measurements.json).

In the later model comparison, inspect both real misses (the inconsistent
Alexander example and the two accounts of de Sacy's alphabetic assumption) and
false amplification (Snow's contextual contrast and the Ra/re sentence). The
new contract cannot independently extract omitted claims or adjudicate historical
truth; full model outputs must still be read against the actual speech and evidence.
