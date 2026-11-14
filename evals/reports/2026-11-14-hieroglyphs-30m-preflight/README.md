# Hieroglyphs: 30-minute free preflight

Current prepared run: `9af453a9-9dc2-426c-9f79-01f4d93ec08b`.
The unpaid preflight `169ceb9c-ab3d-42dd-8aa4-6ef168bb8c4c` is superseded and
paused through the production control; its immutable configuration is unchanged.
Topic: «Как расшифровали египетские иероглифы». Russian solo, same audience,
models, research instructions, voice and budgets as the retained 12:11 episode.
Requested duration: **min 1800, target 1800, max 1980 seconds**. No lower threshold
was substituted to accommodate the existing estimate.

## Preparation result

**Native production prepare passed. No paid action occurred.** The new episode
and immutable research manifest are prepared; research has no approval or job.
HTTP sends and external dispatch were blocked in the preparation process.
Credentials exist locally; remote credentials/entitlements were not tested.

The first attempt exposed the local reference database at migration 0032. A fresh
67,220,736-byte custom pg_dump was saved before applying the already committed
0033–0035 migrations. The connection was verified as a local Unix socket. No old
snapshot was restored and no historical paid run was resumed. Preparation then
passed against the migrated database.

Files:

- [Current voice-bound evidence](voice-bound-preflight.json).
- [Initial preflight evidence](preflight.json) (superseded).
- Configuration: `tmp/hieroglyphs-30m-preflight-2026-11-14/voice-bound-config.json`.
- Native research request: `tmp/hieroglyphs-30m-preflight-2026-11-14/research-request.json`.
- Prepared status and migration/backup receipt are in the same directory.
- Ready prepare/run commands: `tmp/hieroglyphs-30m-preflight-2026-11-14/voice-bound-commands.txt`.
  The paid command has **not** been executed and must wait for root authorization.

## Actual configuration and capacity evidence

| Role | Frozen model | Context | Output per call |
| --- | --- | ---: | ---: |
| Planner | gpt-6.1-sol | 200,000 | 24,576 |
| Plan/listener/factual reviewer | gpt-6.1-terra | 200,000 | 24,576 |
| Writer/revision | claude-opus-5-5 | 200,000 | 24,576 |

These are the configured limits and actual native request parameters, not newly
queried vendor maximums. Anthropic uses `max_tokens=24576`, adaptive thinking,
medium effort; OpenAI uses `max_output_tokens=24576`, medium reasoning.

The retained writer's final draft call used **43,168 input / 5,378 output tokens**
for **1,598 words**. Configured usable input is 175,424 tokens; that saved request
leaves 132,256 input tokens beyond its measured input and reserved output.

The selected voice's retained final episode has **1,653 words / 731.72 seconds**,
about **135.54 words/minute**. At that rate 30 minutes corresponds to about
**4,066 words**. Scaling the saved writer usage gives roughly **13.7k output
tokens**, below 24,576. This is an archive-based estimate, not an exact count or
proof for a future answer. Fresh research and the new plan do not exist yet.
No old 13-minute plan has been passed off as the new plan.

The prepared research commission carries 1800/1800/1980 in its user payload.
It retains the instruction that episode duration is not a research word limit.
In the new voice-bound run, duration reaches the editorial Assignment, planning
duration context, author and listener review. The original production/1 planner
received the requested range in Assignment but no separate duration_context. Factual review receives the full script and factual remit rather
than an invented duration verdict. The author still writes one complete episode;
there is no newly introduced writing-by-parts workflow.

## Budgets and remaining risk

Writing retains 4,194,304 cumulative input tokens and 393,216 output tokens,
including cached/repeated inputs and tool turns. Elapsed allowance is 14,400
seconds per writing child run, not a ceiling on research plus the entire episode.
The historical episode used 2,171,884 input / 74,225 output tokens across 84 model
responses and 84 distinct count receipts. Longer factual work may exhaust the
input allowance; no larger budget was silently granted.

Planning allows one revision; the script allows two. Durable source tools use
`frozen-corpus-tools/7`: the old 12-turn/24-call limits do not apply. Context
checkpoints and the cumulative token/time budgets govern continuation.
Research allows two hosted jobs, including the initial one. It has no USD cap;
variable provider cost remains explicitly accepted in the unchanged settings.

Audio remains Gemini `gemini-3.8-flash-tts`, voice `ru-ru-concierge-1`, 24 kHz,
6,000 characters per part, one synthesis attempt per planned part, 900-second
request timeout, **$1 total allowance / $0.25 reservation per attempt**.
Scaling the entire retained episode gives about **26.5k characters**, at least
**five parts**, and **$0.409–0.450** for 30–33 minutes at the retained rates/usage.
This covers the whole proposed episode, not only its first part. Real partitioning
may add a part. Reservations are replaced by measured usage as parts complete;
if usage is unknown, four $0.25 reservations can block part five. The estimate is
not a guarantee of admission or an invoice cap.

The original estimate used **110 wpm from macOS Milena**, without a binding to
this Gemini voice: 3,300 suggested words would last about **24:21** at the retained
voice rate. The new optional reference corrects that estimate to **4,066 words**,
with **4,066–4,472** for the unchanged 30–33 minute range. This is approximate;
audio `assembled` status does not enforce duration. Inspect the measured master
and complete reviews after an authorized run. Paid execution has not occurred.

## Implemented contract

The optional production `duration_reference` keeps exact private assembly/script
references and the measured estimate. Preparation rederives it from ordinary
saved manifest/result/script metadata, with no WAV read. It requires complete
coverage, positive frames/rate, matching script digest, and exact synthesis plus
direction identity. Pending acoustic review is permitted: this is an estimate of
text speed, not a claim of listening acceptance or word fidelity.

The optional editorial `duration_estimate` contains only the versioned estimate
and speech identity, not private evaluator RuntimeRefs. The ordinary assignment
producer passes it to the common duration_context projection. Technical speech
identity stays outside model-facing assignment; the model sees word targets and
the limited measured basis. Missing optional fields are omitted from historical
serialization; the legacy branch remains exact. No new table, cache, prompt body,
model, workflow stage or duration bound is introduced.


## Verification and cost of preparation

44 targeted checks passed, including the real offline episode pipeline and older
full-request compatibility tests (`/tmp/podgen-duration-reference.xml`). After
adding production/1 preview and early language validation cases, the focused
contract suite passed 21 checks (`/tmp/podgen-duration-reference-preview.log`).
Ruff passes. Production/1 plan, plan-review and plan-revise previews receive the
new derived estimate; author, revision and listener compiler payloads do too.
The actual targeted-research path omits the private estimate while preserving
its existing assignment fields. Old absent optional fields retain serialization.
The word-based reference accepts supported ru/en units only, with matching solo
Assignment language; currently synthesis itself supports ru only.

Measured on the retained ordinary assembly: three scalar metadata reads cost
69 SQL statements; batching the manifest and script costs 48. Both read exactly
three JSON bodies, with no historical response bodies or WAV. The initial native
prepare used 99 SQL statements and one 2,868-byte profile object read. No network
or paid calls occurred. The evidence JSON records returned JSON bytes, object
bytes, elapsed time and Python peak allocations; SQL protocol bytes and process
RSS were not measured. This is one representative episode, not a history-size
scaling benchmark. Repeating prepare reused the same immutable configuration.

The settings comparison is exact after removing only the new duration reference:
models, prompt instructions, stage order, budgets and duration bounds are unchanged.
No commit or paid execution was made pending root review. A real 30-minute run
still needs authorization and complete script, review and audio inspection.
