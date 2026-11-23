# Full-operation persistence boundaries

This extends the existing source acquisition, retrieval, editorial and audio owners.
It does not replace the private execution journal or change model requests.

## Document acquisition and publication

An acquisition operation owns its received original, extraction and publication.
The original HTTP response becomes durable before extraction starts. A received
body and its verified content descriptor can be passed to the next local stage;
recovery loads the archived body instead of fetching it again.

Artifact publication prepares and verifies bytes before opening its SQL transaction.
Within the short transaction, it authorizes the unique dependency set and checks
every field-specific binding. Deduplicating reads must not collapse different
lineage, subtype or public/evaluator requirements on the same reference. A complete
document transition publishes its artifacts and checkpoint together. Failed
transactions expose neither half a representation nor a completed acquisition.
The composite acquisition owner uses the existing `archive_write` callback:
repeatable read, project fence and membership lock, with retries limited to
rolled-back SQL. Prepared bytes remain outside; descriptors escape only after commit.

Prepared bytes certify content and storage ownership, not access. Permissions and
withdrawals are checked again at publication. No SQL snapshot crosses provider or
remote storage I/O. The common package importer follows the same byte/publication
boundary; it is not a separate writer.

## Initial retrieval preparation

The writing operation prepares its execution inputs and binds lazy source text
before constructing retrieval. Index access uses that prepared input identity from
its first batch, with fresh authorization on each inference/write boundary.

Each existing bounded passage batch uses one multirow insert. Its completion cursor
advances in the same transaction. A failed insertion leaves both rows and cursor
unchanged. Reusable document identities, vectors, source coordinates, ranking and
run-specific source labels retain their existing contracts.

## Accepted planning selections

The accepted clearance owns a versioned immutable supplemental-selection result,
including an explicit empty selection. Its identity binds the exact clearance,
candidate plan and accepting review. Planning history is inspected once when that
result is published. A legacy clearance is adopted once without rewriting its
record or model request.

Stage preparation reads these selections; it does not replay tool history to
rediscover them. Continuations retain their parent run provenance and exact plan
hashes. Only the chosen plan's selected reports enter the author payload. Stored
event identities support audit; they do not manufacture generic runtime references
to private execution aliases.

## Accepted script validation and audio

Full structural validation belongs to editorial acceptance. A versioned immutable
proof binds the manifest, outcome, final script and final reviews. Ordinary reads
authorize those exact inputs and check their bindings. They do not replay all
revision and review logic. A legacy outcome without a proof must pass the complete
audit before adoption. Full replay remains an explicit audit operation.

One locked audio operation owns its immutable attempt/result/accounting descriptors.
Successful steps return them to render and accounting directly. Recovery reads the
existing journal in bounded batches; no additional audio state machine is added.
An uncertain submit never permits automatic resend. If a write acknowledgement is
lost, in-memory state cannot be treated as authoritative for another step.

## Qualification

Use a fresh pre-episode database snapshot, all retained source fetch/extraction
responses, the complete model/count trace, and all paid audio receipts. Run actual
acquisition, index preparation, tools, persistence, assembly and export with network
dispatch forbidden. Compare native requests, final script identity and master WAV
bytes. Repeat the completed operation to verify no new external work.

Measure each phase, SQL call sites, object I/O, elapsed time and peak memory on both
the baseline and candidate. Include first-use index construction. Retain failures
and recovery tests separately; do not compare an uninterrupted offline run directly
with a historical run containing provider waits and manual recovery gaps.

A historical replay of the hieroglyphs episode used the same pre-episode database, 54 archived source fetches and extractions, 84 model responses, 84 token counts, 28 research exchanges and two speech responses on each version. It ran local retrieval, FFmpeg, assembly and export with external dispatch disabled. Against baseline `b2e07b16`, client SQL fell from 61,312 to 37,518 and measured phase time from 572.90 to 537.39 seconds. The auxiliary cold outcome read cost 29 statements, compared with a prior measurement of 1,219. The runs reproduced script and master audio. The SQL counts exclude server-side trigger work and the timings represent one local run per version.
