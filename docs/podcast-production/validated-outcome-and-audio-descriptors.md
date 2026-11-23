# Validated reading outcomes and audio results

## Reading outcomes

`reading_outcome.audit_outcome` replays the exact scripts, both independent reviews from each round, edit assignments and rewrite results. The reading-cycle owner calls `reading_validation.certify` on completion. Its immutable `validated-reading-outcome/1` record binds versions and hashes for the outcome, manifest, final script and both final reviews. The certificate confirms the structure of the archived run. Editorial acceptance and production readiness are recorded independently.

A legacy outcome without a certificate undergoes one full replay before adoption. Certification uses its own lock and rejects conflicting bindings. Ordinary `read_outcome` resolves the certified records in a batch. Callers can pass the resulting immutable `ValidatedOutcome` between stages. At a new read or action boundary, `authorize` checks current access to each bound reference and verifies integrity. Publication rechecks the proof dependencies inside its transaction, so withdrawal between audit and commit takes effect. Evaluation access still requires an appropriate purpose and an owner, evaluator or operator role.

## Audio

The immutable journal is the source of audio history. `AudioJournal.attempt_keys` uses the existing consumer index on `source_ref` to address attempts. Processing revisions use the same index keyed by receipt. Bodies are loaded in batches of up to 64 references.

`execute_sample` returns a `SampleResult` with status, completed result reference, baseline and accounted cost. `process_result` returns the known decode and processing results. Rendering and accounting reuse these descriptors. Assembly gets exact references and checks their bindings against a fresh batch read. Status-only interfaces wrap the same operations.

An `OperationLedger` lasts for one locked operation. A normal transition updates the cost of the current take. After an exception, the affected entry is marked dirty and recovered from the durable journal before further work. If submit acknowledgement is lost, the attempt stays `outcome_unknown`. On receipt, the provider response is saved before local processing.

Unfinished local processing resumes with the same `processing_key`, including after restart between a processing record, a decoded result and final publication. Losing acknowledgement of a reprocess write does not create another paid attempt. Export and explicit audit can still read the full history. Baseline selection, PCM and master normalization remain separate from recovery.
