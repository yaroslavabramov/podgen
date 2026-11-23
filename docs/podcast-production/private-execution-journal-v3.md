# Private execution journal v3 — implementation contract

Version 3 implements typed execution checkpoints and retains version 1 and 2 histories for archive reads. A historical offline test of commit `6e7d1508` replayed 43 model turns and 42 executed tools against a 51-document corpus. First-pass client SQL fell from 6,508 to 5,730, while completed-run read and export rose from 1,165 to 1,474. First-pass elapsed time increased from 161.37 to 168.70 seconds. Ordinary steps still took 67 client statements, and the first pass executed 24,004 server statements including triggers and functions. These measurements leave efficiency qualification open.

## Ownership and stored entities

Existing run ownership and immutable prepared input generations remain authoritative.
New execution storage has three ordinary tables and one semantic-boundary relation:

- `execution_heads`: PK `(project_id, run_id)`. One fixed-size current physical
  operation state, revision/fencing epoch, current attempt/step identity, request and
  configuration hashes, exact count/receipt locators, observation counters and a
  fixed accounting vector. No maps/lists of previous turns, calls or receipts.
- `execution_entries`: PK `(project_id, run_id, entry_id)`, unique
  `(project_id, run_id, commit_key)`. Immutable typed checkpoint, step/attempt IDs,
  input generation, operation identity, full data and named owned/borrowed byte
  slots. Attempt preparation and role output are entry kinds, not duplicate
  invocation payload rows. Indexes support attempt/step history, exact-count
  identity, provider observations, tool identity and charge corrections.
- `execution_objects`: unique `(project_id, sha256, media_type)`, immutable length
  and deterministic versioned content key. These are private execution bytes,
  separate from generic blob registration. Identical bytes reuse one object.
- `semantic_execution_links`: created only when publishing a real semantic result;
  links that result to its execution entry. No technical runtime stubs or per-entry
  generic reference edges are created.

A checkpoint owns one entry plus the corresponding atomic head change. Source
provenance is checked at input/admission, action and public-read boundaries. Typed
control references must name the actual step/request/receipt/call; same-run
membership alone never authorizes an unrelated reference.

## Stable operation and commit identities

`unique(run, operation_id, kind)` is insufficient. `operation_id` identifies an
external operation; `commit_key` identifies its particular durable observation:

| Operation | Deterministic identity |
|---|---|
| Count candidate | step ID + exact native-count/configuration identity + authorized recovery epoch |
| Submit | physical step ID + `submit`; a changed request requires a new step |
| Provider receipt | submit operation ID + observation number; submit response is observation zero |
| Tool operation | step ID + exact provider receipt ID + digest of full call ID + raw-arguments SHA |
| Tool observation | tool operation ID + observation number / terminal-result kind |
| Accounting correction | target operation + expected charge revision + correction command ID |

`commit_key` includes the typed checkpoint kind and all observation identities
included in that checkpoint. A count-receipt-plus-submit checkpoint therefore binds
both operation IDs. Retrying the same commit requires the same full content hash;
otherwise it is an explicit conflict. A committed identical retry returns the
previous result without another write or external call.

Different count candidates in one physical turn have different native/configuration
identities. Identical candidates reuse the exact receipt rather than issue another
count. A new count after an invalid/uncertain operation requires a durable explicit
recovery decision. Provider polling and pending tool observations have separate
observation numbers; they cannot overwrite earlier receipts. Ordinary replay looks
up the committed observation before dispatching anything.

## Atomic boundaries and byte lifetime

`CheckpointWriter.commit(typed_command)` replaces `Publication`'s records/events/
state/callback lists and arbitrary `phase.writes` SQL callbacks.

1. Compile exact candidate bytes and authorize the current run/input boundary.
2. Stage and upload a bounded group of complete byte slots outside SQL, using
   deterministic content keys and conditional-create/integrity verification.
3. In one short transaction, fence the head/byte-privacy mutation, register missing
   private objects, perform fresh ACL/input/SHA checks, validate the typed transition,
   insert one entry and CAS the single head. No network wait occurs in this scope.
4. Expose committed history/accounting to the caller only after commit.

The first durable count intent also creates/prepares the physical step, and stores
its full request candidate and exact native count request. `turn_preparation` no
longer performs an earlier blob/runtime/turn publication. A count receipt is stored
with submit intent or with blocked/failure state. Before a second count, the first
receipt is committed together with the next count intent. A provider receipt and
its state/accounting update are atomic. Read-only tools need one result checkpoint;
paid/mutating tools retain their prior durable operation intent.

Raw receipt retention precedes optional interpretation. Invalid JSON, duplicate
keys, oversized/NUL/surrogate identifiers and invalid numeric usage retain the full
raw body/wire bytes and an explicit interpretation error. No optional call/index
projection can roll back a valid raw capture. Semantic fields are never truncated.

The current step owner retains its captured response while executing its calls;
there is no seven-query provider-parent reread in continuous execution. Resume loads
and authorizes that receipt once. Raw buffers are released when the step no longer
needs them. Full history stays persisted, outside the fixed-size head.

A crash after durable submit intent and before receipt leaves an uncertain outcome;
it never authorizes resend. A stale worker cannot advance a fenced head. A matching
late receipt may reconcile its original operation through a typed CAS command,
subject to the unchanged live capture policy. Orphan uploaded bytes grant no access
and cannot make an operation appear complete.

## Accounting and cold recovery

Count calls, count reuse, generation input/output, provider cached input, rejected
requests, reservations and uncertain exposure are distinct fixed accounting fields.
Only the head contains current aggregate accounting; no per-turn root-invocation
status/usage update is written. Per-operation source facts remain in entries.

A correction appends `(target_operation, expected_charge_revision, old_charge,
new_charge, delta, evidence_locator)` and applies the delta to head totals in the
same CAS transaction. The writer verifies the prior effective charge by an indexed
latest-charge lookup for that operation. The old receipt and prior charge fact are
immutable. Duplicate corrections do not double-apply; competing corrections with
stale revisions fail. Cold recovery reads the head and indexed current operation /
receipt; rebuilding a corrupted projection is explicit, not a scan at each checkpoint.

One run has one active provider-generation coordinator. An uncertain submit never
authorizes automatic resend. Existing explicitly authorized technical recovery may
create a child attempt under its replacement limit while retaining the parent's
unknown charge/reservation and immutable return/recovery location. Reconciliation
of that parent adjusts accounting without rewinding the active child phase.
Concurrent callers serialize through head revision/fencing; separate runs remain
independent.

## SHA aliases, historical generations and semantic publication

A private namespace is not a privacy boundary by itself. Common generic/runtime/
catalog SHA checks must see `execution_objects` through the common indexed byte
privacy owner. First private SHA publication serializes with generic consumers;
preexisting aliases and their descendants receive the existing forward protection
closure only on that first-protection boundary. Private ownership is inherent in
the object catalog, without per-entry generic classification inserts.

Withdrawal follows source provenance into affected input generations, then affected
entries and their owned object SHA, then private/generic/catalog aliases and their
derivatives. Entry-owned object-use metadata is indexed in the entry (fixed typed
slot/hash descriptors), not copied to `runtime_blob_refs`. Borrowed source bytes
remain source dependencies and never become owned execution output by reference.
Historical generations remain exact; later admissions do not rewrite old entries.
Fresh permissions and SHA/withdrawal conditions are checked before and after I/O.

A validated role result is published once as a complete semantic result, with an
explicit execution provenance link. Draft import, accepted plan and finalization
consume that result, not technical entry stubs. Publication is idempotent and can
retry without repeating provider work. A failed semantic publication leaves the
captured private result recoverable and does not mark it published/accepted.

## Concrete replacement sites and compatibility

- `journal.prepare` / `turn_preparation.stage_prepare`: typed attempt/count/submit
  checkpoints replace generic blob save + runtime payload + invocation/turn writes.
- `Publication.commit`: replaced by the atomic typed writer. Remove new-path writes
  to `runtime_records`, `runtime_runtime_refs`, `runtime_blob_refs`,
  `evaluation_invocation_events`, `execution_provider_calls`,
  `execution_tool_results` and per-receipt `evaluator_blob_classifications`.
- `TurnJournal.write_transition` / `TurnLedger`: one head mutation replaces physical
  state plus root usage/status updates and cross-checkpoint descriptor warming.
- `ResearchContext.measured` / `ModelStep.invoke`: count intent, exact receipt reuse,
  cancellation capture and submit use the same writer and private byte slots.
- `gateway._archive_reply` / `tool_gateway._execute`: capture full receipt/result
  once; exact call proof is owned by the captured receipt, not a separate table.
- One `ExecutionArchiveAdapter` implements request/output/history/recovery/export
  views over v3 and read-only legacy archives. v3 transport/storage behavior is
  explicitly versioned; old previews/native requests are not silently rewritten.
- Remove `finalization.py`'s direct `runtime_records` existence query.
  `script_import.py`'s direct Repository read and accepted-plan/audit consumers go
  through the semantic-result/archive boundary. Legacy request/output FKs remain
  archival; new entries do not manufacture runtime rows to satisfy them.

Qualification must use the actual v2 `completed.dump` and objects, plus a genuinely
old-writer interrupted fixture. Test exact raw/native/export content, count cancel
and uncertain-submit recovery, CAS/lost-ack concurrency, remote-I/O ACL changes,
SHA aliases/descendants/borrowed bytes, historical generations and every semantic
publication consumer. Measure complete representative operations and real nested
SQL/bytes/memory growth; do not promise a numerical SQL minimum before measurement.

## Approved implementation invariants

The run head coordinates the current physical operation; it is not the history.
Nested parent/return locations and unresolved submit intents are immutable entry
references. Recovery addresses a concrete attempt/step, so moving the coordinator
cannot erase an older unresolved operation. Late reconciliation appends evidence
and a CAS accounting delta without rewinding the current head phase.

Archive references are stable and projection-versioned. Their indexed lookup is
owned by the archive adapter, not represented by generic runtime stubs. Semantic
publication commits the complete real result, provenance link and completion
atomically, or leaves an explicit idempotent publication recovery point. Generic
semantic payload uses a tagged execution locator, never a RuntimeRef to a private
UUID that the generic JSON projector could interpret as a runtime FK. The semantic
link participates in authorization and withdrawal and grants no private-byte access.

Provider observations, tool observations, count candidates and corrections have
distinct deterministic commit identities as specified above. The fixed-size head
contains no growing per-operation charge map. Correction lookup uses the indexed
latest effective charge for its target, checks old/new charge and expected revision,
and updates aggregate accounting once in the same transaction.
