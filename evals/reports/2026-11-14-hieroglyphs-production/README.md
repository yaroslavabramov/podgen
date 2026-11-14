# Hieroglyphs: ordinary production run, 2026-11-14

User-selected topic: «Как расшифровали иероглифы». Russian solo episode,
12–15 minutes. The ordinary run reached `assembled`: **12:11.72** of audio.
This report evaluates the actual product output, not a manually rewritten
demonstration. Pronunciation and audible joins remain unreviewed.

- Production run: `63f3265a-1ff4-5526-85c7-92f723f435e7`.
- Research: `89a434cf-94b9-5545-ac3b-c749aaf8e0bf`.
- Writing: `27d83dae-baff-52a8-b792-e6dfd1a4c5b5`.
- Planner: Sol; reviews: Terra; writer: Claude Opus 5.5.
- Speech: Gemini 3.8 Flash TTS, `ru-ru-concierge-1`.
- Settings, exact requests and full technical export:
  `tmp/hieroglyphs-production-2026-11-14/`.

## Actual writing decisions

The ordinary chain ran research, source acquisition, planning, plan review,
drafting, reader/fact reviews and exactly two script rewrites. No manual content
edits, extra rewrite allowance, prompt changes, model substitutions or manually
constructed dossier were introduced during this run.

Read [the plan](plan.json) and [its separate review](plan-review.json), then
[the first script](first-script.md), [first revision](first-revision.md) and
[accepted final script](final-script.md). Reviews are retained separately in
`reader-review*.json` and `fact-review*.json`.

The plan was accepted without a rewrite. The first reviews required removing an
invalid inference about demotic writing, distinguishing language forms, correcting
the name-comparison explanation, and removing unsupported numerical claims.
The first rewrite corrected most of these but expanded the confusing explanation
of feather signs. The reader returned it again. The second rewrite removed that
comparison and retained P, L and O, with T as the next complication. Both final
reviews returned `keep`. This is evidence of their decisions, not a quality verdict
by the coordinator.

## Reading assessment

The opening establishes a place, an event and the significance of the stone.
The central comparison lets the listener follow the repeated signs in two names.
The move from foreign names to Ramses and Coptic gives the story an intelligible
next question. These are useful narrative decisions.

The accepted text still has concrete weaknesses:

1. The analogy with «молоко» explains one sound written with different signs by
   describing one letter pronounced differently, then adding «только наоборот».
   The listener has to reverse the analogy. This is less clear than the preceding
   direct statement. Both reviewing roles accepted it.
2. Attribution to Hartleben is repeated: a long explanation of the late biography,
   then «По словам Хартлебен», then «Но, повторю, это рассказ биографа».
   The last warning repeats information already established. The reader's final
   review explicitly approves its retention.
3. The ending moves through later corrections, the Nile expedition, priority
   disputes and posthumous publication before a general closing statement.
   It carries several successive endings. «Теперь главный герой» and
   «Задокументировано другое» also preserve a lecturer's manner.
4. «Догадки были … но проверить их было нечем» is an overbroad closing formulation
   after the episode has described earlier comparisons and checks. This is a
   narrative/meaning concern identified on reading, not an independently completed
   historical fact-check.

The first draft has 1,598 words, the first revision 1,662, and the final 1,653
(whitespace count). Revision did improve the name comparison; it did not establish
that all excessive explanation or a stretched ending had been resolved. No further
rewrite was added outside the configured limit.

## Recovery and intervention

Before starting, PostgreSQL was restored after a machine restart and the reference
state was pointed at a persistent local cluster. Existing historical paid runs
were not resumed from that older database snapshot.

Two implementation defects interrupted this new run:

- The first reader review had a quote-anchor mismatch. A stale in-memory invocation
  omitted an already persisted provider response ID, preventing the ordinary
  bounded anchor repair. Commit `c956b343` binds repair to the exact saved
  invocation. The original review remained archived; the repaired quote points
  to the actual paragraph. No replacement full review or draft was generated.
- The first successful TTS response was 377.72 seconds long. The comparison-sample
  loudness policy incorrectly applied to full-episode parts rejected its peaks.
  Commit `e626d4ca` gives full-render processing an explicit identity:
  `ffmpeg-assembly-input/1`. Parts retain decoded PCM; the assembly owns the one
  master gain. The saved response is reprocessed locally, preserving the failed
  processing revision and paid receipt. Comparison processing is unchanged.

Targeted recovery checks: 25 tests for the saved-invocation repair, 26 for audio
recovery/compatibility. The first live audio response also passed offline processing
with its PCM hash unchanged. No concurrent heavy test suites were run.

These required code fixes and explicit resume commands. The episode therefore did
not demonstrate an uninterrupted production run. Research-to-writing handoff and
editorial decisions themselves were handled by the ordinary product.

## Unresolved execution overhead

Source acquisition alone executed **21,253 client SQL statements** for 54 discovered
source URLs, admitting 52 texts (2,538,055 characters), in about 174 seconds.
This is an unresolved architecture defect, not an acceptable cost justified by the
number of sources.

Inspection found repeated authorization/dependency work around small acquisition
checkpoints, rereads of just-saved bodies, repeated blob validation and reloading
sources/text to recover titles during admission. The future correction belongs in
the existing document acquisition/checkpoint owner: bounded batched reads, one
short transaction per durable document transition, descriptor reuse, and fresh
authorization across network waits. No parallel acquisition implementation or
unbounded cache was added during this run.

The hosted-research head now has bounded cost as history grows, but its measured
75 SQL statements per poll are not evidence of good absolute efficiency either.

Measurements count client SQL executions, not database trigger internals. Object
measurements exclude projection mmap and OS caches. The HTTP observer instruments
`httpx`; current OpenAI transport uses `httpx2`, so its HTTP count is incomplete.
Generation accounting must use saved provider receipts, not that observer.

## Completed audio

The final resume reused the first paid receipt and submitted only the second part.
The two parts last 377.72 and 354.00 seconds. Each has one synthesis take; the first
has two local processing revisions. Both are bound to the accepted final script.
The assembly covers all 32 utterances with no missing or duplicate character
ranges. That verifies the submitted text coverage, not the fidelity of spoken words.

The master is 48 kHz mono PCM, 35,122,560 frames, −21.3 LUFS, −1.3 dBTP.
It uses one −0.1 dB gain across both parts; no per-part gain or tempo correction.
The actual 731.72-second duration meets the requested 720–900-second range.

- WAV: `tmp/hieroglyphs-production-2026-11-14/export/audio/master.wav`.
- Listening MP3: `tmp/hieroglyphs-production-2026-11-14/export/audio/master.mp3`.
- Join preview: `tmp/hieroglyphs-production-2026-11-14/export/audio/join-1.wav`.
- [Product status](production-status.json), [audio measurements](audio-status.json).

`acoustic_review` remains `pending`; `production_ready` remains `false`.
The MP3 is a local listening copy, not a new synthesis. Exported plan/reviews and
all three script versions were compared against this report and match exactly.

## Cost and request accounting

[Accounting](usage-cost.json) includes all 84 saved model generation receipts,
with no unknown writing usage. Aggregate `source_tools_final_response` copies are
excluded so the final model response is not counted twice. There were also 84
token-count receipts; those are not 84 additional generations.

| Stage | Model responses | Estimated USD |
| --- | ---: | ---: |
| Plan | 23 | 0.196209 |
| Plan review | 24 | 0.083884 |
| First script | 8 | 0.396577 |
| First reader review, including anchor repair | 2 | 0.050117 |
| First fact review | 21 | 0.142514 |
| First rewrite | 1 | 0.288356 |
| Its reader review | 1 | 0.062789 |
| Its fact review | 1 | 0.090053 |
| Second rewrite | 1 | 0.059411 |
| Final reader review | 1 | 0.053273 |
| Final fact review | 1 | 0.088826 |
| **Writing total** | **84** | **1.512010** |
| Speech | **2** | **0.166264** |

The 84 writing responses include the steps around 72 source-tool executions,
the final role answers and one anchor repair. They do not represent 84 rewrites.
The receipt totals are 2,171,884 input tokens and 74,225 output tokens. Of input,
1,853,857 were cache reads, 295,207 cache writes, and 22,820 uncached tokens.
Cache writes and cache reads are priced separately using the recorded TTL.

**Known writing + speech subtotal: $1.678274. Research is additional.** Rates were
verified the same day and copied with their official sources in [pricing.json](pricing.json).
These are usage-based estimates, not a billing invoice.

Research was one hosted job, 27 status polls and 35 internal Google searches.
Its receipt reports 1,033,269 aggregate tokens, including 866,826 input tokens and
89,921 cached tokens. The hosted-agent receipt does not report an actual USD bill
or enough per-model pricing detail to calculate one reliably. Google's agent
pricing combines underlying model and tool charges; account-level free allowances
are also unknown. Research USD and the grand total are therefore left unknown,
not replaced with the earlier generic $1–3 estimate.
[Google agent pricing](https://aistudio.google.com/docs/pricing#pricing-for-agents).

## Complete-operation measurements

Three ordinary CLI attempts together took **2,403.62 seconds (40:03.62)**,
including provider waits and research polling delays. Debugging, tests, database
restoration and gaps between attempts are excluded. Full wall time was longer.

| Phase | Client SQL executions |
| --- | ---: |
| Startup/resume | 223 |
| Hosted research | 2,666 |
| Source acquisition/admission | 21,253 |
| Writing and reviews | 29,899 |
| Audio processing and assembly | 7,686 |
| Full technical export | 4,888 |
| **Total** | **66,615** |

The audio phase includes the failed local processing attempt and its recovery.
Export is a complete technical archive, not simply downloading the MP3. These
distinctions explain scope; they do not justify the query volume. This result must
not be presented as an efficient or production-ready architecture.

Raw measurements for all three attempts and cumulative object I/O are retained in
[runtime-metrics.json](runtime-metrics.json). Maximum observed process RSS was
1,312,735,232 bytes (about 1.22 GiB). This is a full-operation measurement, not a
before/after performance comparison with older, differently scoped fact-check runs.
