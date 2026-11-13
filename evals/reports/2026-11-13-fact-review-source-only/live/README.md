# Completed retained-sources-only fact review

One initial fact-review completed on 2026-11-13. Attempt index 0; no retry, unknown API outcome, supplemental source request, new research or rewrite. The saved root request retains approved hash `400d5c617fd04a13c41edfef0ce8692ea8ac6e891f4c35da36981ae0c93dee1d`.

The complete result is `fact-review-output.json`; `audit.json` contains its complete contextual audit. The model returned `revise` with four important findings and 16 audit decisions covering all 26 utterances. This records the model's verdict for independent editorial review, not acceptance of its findings. The ordinary stage validation completed. Run status is `completed_unaccepted`; the combined-reading export says `incomplete` because this isolated experiment does not run a listener review or accept an episode.

## Trace

`trace.tar.gz` preserves the full ordinary export: 370 files, 35,241,950 uncompressed bytes. `trace-manifest.json` gives every file's hash and size; every archived file was read back and verified. The expanded files remain at the manifest's absolute `raw_trace_directory`, including `archive/fact-review/events.json`, `source-tools.json`, all 43 native turn requests and every provider count/generation response. Complete passages and native usage remain in the trace. The archive also contains the frozen input artifacts for technical provenance; their presence in the archive does not grant the model report-reading access.

`verification.json` records equality of the actual compiled initial request to the approved hash, native usage to journal totals, all-utterance coverage and empty `support_labels`. The full final output was read after completion. Source-read calls inspected 17 distinct retained documents through 18 passages. No source was acquired or added.

## Tool behavior compared with the saved source run

| Tool | Original fact review | Source-only fact review |
| --- | ---: | ---: |
| `read_material` | 4 | 0 |
| `read_research_result` | 0 | 0 |
| `list_research_results` | 1 | 0 |
| `list_sources` | 1 | 11 |
| `read_source` | 14 | 18 |
| `search_sources` | 13 | 13 |
| Total tool calls/results | 33 | 42 |
| Completed physical count responses | 68 (event-bound) | 43 (wire-verified) |
| Completed generation responses | 34 (event-bound) | 43 (wire-verified) |

The baseline performs two physical counts per turn: 34 `source_context_count` plus 34 `count_response` events, totaling 68, separately from 34 generation responses. Its fact-review wire request/response blobs are absent from this experiment archive and the saved longitude export (whose archive contains only the plan); the baseline figure is established by the saved invocation event binding, not wire verification. The new run has 43 `source_context_count` calls, zero `count_response` calls and 43 `count_receipt_reused` events. Reuse adds no physical request. All 43 new count and 43 generation request/response pairs were verified against their archived blob hashes and successful HTTP status; the 43 count provider request IDs are distinct.

The original model actually consumed four successful report readings: M6 (6,596 characters), M7 (4,605), M8 (4,085), M3 (2,847), totaling 18,133 Unicode characters. It made no M-scoped search. Thus the variant removes access to synthesis that was actually read in the baseline, alongside report navigation/catalog fields. `source-run-tool-counts.json` records the original invocation and exact report-reading arguments/results. These counts were obtained read-only after the new review finished; no baseline findings entered its context.

## Usage and operation measurements

`usage.json` retains each native generation receipt with its path, response ID, usage and estimate. Totals: 1,723,875 input tokens; 1,661,670 cached tokens; 62,076 cache-write tokens; 13,443 output tokens. All 43 generation responses have usage; there are no unknown usage turns.

Estimated generation cost is **USD 0.22802250**, using the copied `pricing.json`, the saved 2026-11-08 rates. This is not a billing invoice or a claim about current prices. Formula per response: `(regular_input × 1 + cached × 0.05 + cache_write × 1.25 + output × 5) / 1,000,000`, where regular input excludes cached and written input. Native count receipts are preserved separately; no price for count requests is inferred.

`run-measurements.json` contains exact phase counters:

| Phase | Seconds | SQL calls |
| --- | ---: | ---: |
| Startup | 100.514 | 100,150 |
| Fact review | 512.089 | 65,375 |
| Export | 56.306 | 47,155 |
| Whole operation | 668.910 | 212,684 |

The 13 local searches took 152.047 seconds inside the fact-review phase. Process peak RSS was 1,208,057,856 bytes. Repository reads totaled 90,798,027 bytes; 260 logical staging/publication operations each accounted for 36,016,771 bytes. These are logical repository operations, not physical disk writes; cached Unicode projection reads are outside these object counters. SQL volume is high and remains a measured limitation of this approved runtime; no performance change was introduced during the experiment.

After exporting and backing up the completed database, the owned source-copy PostgreSQL at port 55468 was stopped. Its data directory and `tmp/source-only-validation/completed.dump` remain in the worktree. The original source database was not changed or stopped.
