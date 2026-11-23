# Implementation and qualification plan

The roadmap describes the work needed to release a complete, listenable episode. The [episode CLI](episode-production.md) already connects hosted research, planning, writing, independent reviews and assembly. A new capability reaches a release milestone only when the associated end-to-end case succeeds under its pinned conditions.

## Milestones and work packages

| Milestone | Objective and evidence |
| --- | --- |
| M0 — Contracts and measurement | Durable identity, frozen evaluation cases, reproducible requests and separation of evaluator-only data. |
| M1 — Whole-episode quality | Compare complete scripts and actual audio from the same expert dossier. Calibrate the reviewers and evaluate the findings against human listening. |
| M2 — Independent production | Start from a topic, perform research, repair substantive gaps and produce an episode with the same access granted to the baseline. |
| M3 — Reliable user workflow | Exercise revisions, cancellation, resume, review, playback and export through real application boundaries. |
| M4 — Qualification | Establish which languages, formats, durations, models and voices pass the agreed criteria on held-out cases. Exercise access withdrawal and restore. |
| M5 — Cutover | New work selects qualified profiles, old work remains reproducible, and rollback is tested against in-flight runs. |

| Package | Responsibility and exit criterion |
| --- | --- |
| WP-01 | Domain and persistence. Shared identities, canonical hashes and selectors round-trip intact across languages. |
| WP-02 | Corpus and evaluation runner. Separate development material and hidden annotations. Reproduce the evaluated inputs and blind the comparison. |
| WP-03 | Model gateway, context compiler and tools. Record exact requests and outcomes. Distinguish counted, dispatched, refused and uncertain work. |
| WP-04 | Ingest and retrieval. Bind quotations to retained revisions and demonstrate correct ranges for difficult HTML, PDFs, charts and scans. |
| WP-05 | Verification. Detect consequential errors, distinguish alternatives from contradictions and tie a decision to the script that was read. |
| WP-06 | Story and writing. Compare whole and act-based drafts, revisions and continuity from the actual words. |
| WP-07 | Audio. Render complete scripts, measure actual speech, audition joins and recover a local defect without regenerating unrelated speech. |
| WP-08 | Research and editorial integration. Admit material discovered during a task and resolve a targeted question in the same editorial story. |
| WP-09 | Durable execution. Retry and cancellation preserve paid work, exact bindings and one adopted outcome under concurrency. |
| WP-10 | Release and invalidation. Assemble one compatible version set and re-evaluate current eligibility after later changes. |
| WP-11 | User interface. A user can request an episode, inspect progress, listen, request a correction and export it without managing agents. |
| WP-12 | Operations and isolation. Exercise withdrawal, deletion, restart, backup, restore and project isolation. |
| WP-13 | Qualification. Establish final quality, cost, reliability and supported combinations on held-out episodes. |
| WP-14 | Cutover. Rehearse rollout and rollback with old and new saved operations. |

### Delivery dependencies

Evaluation and the model gateway both depend on the domain contracts. Source acquisition follows the gateway. Source work enables the verification and writing branches. Audio needs both. Editorial research integration needs source access and a working writing path. Durable execution builds on the model gateway. Release assembly depends on verification, audio and durable execution. The user workflow builds on release. Operations builds on the execution layer.

Qualification combines the evaluation harness, independent editorial research, the user workflow and operational recovery. Cutover follows qualification of the specific configurations offered to users.

## Experiments that decide the architecture

| Experiment | Controlled comparison |
| --- | --- |
| EXP-01 | Change one role's model, assignment or tools while preserving its inputs and downstream assessment. Inspect the actual wire request. |
| EXP-02 | Vary extraction and retrieval on the same documents. Verify original anchors and downstream factual judgments. |
| EXP-03 | Compare sequential and independent research workers on the same questions, corpus and source access. |
| EXP-04 | Compare whole-draft and act-based writing. For dialogue also compare direct writing, direct writing with revision and adaptation of researched prose. Use blind listening, whole-episode coherence and a professional reference where available. |
| EXP-05 | Compare voice, provider, conditioning and boundaries on the same speech. Listen to joined passages and full episodes. |
| EXP-06 | Calibrate evaluators on known real defects, clean controls and harmless variation. Include false-positive and missed-error rates. |
| EXP-07 | Compare the full product to a strong model with simple tools, matching assignment, source access and independent final QA. Give both writers an expert dossier only in the separate writing experiment. |
| EXP-08 | Interrupt work around dispatch, storage, adoption and release. Verify resumability, actual charges and the consistency of public output. |

Record the baseline, exact configuration, all attempts, relevant cost and resources, the observed difference, and what remains unverified. If a simpler pipeline wins, retain the data and recovery guarantees while simplifying editorial orchestration.

## End-to-end acceptance

| Case | Behavior to verify |
| --- | --- |
| AC-01 | The final paragraph, Unicode ranges and canonical hashes survive every interface. Oversize input returns an explicit failure. |
| AC-02 | A foreign project reference, forged catalog kind or private voice reference cannot expose another project's material. |
| AC-03 | Removing an unsupported claim ends its active quality dependency after review. Historical provenance and privacy restrictions survive. |
| AC-04 | Discovery, acquisition and model-visible delivery are distinct. Cancellation between reading and sending cannot invent a receipt. |
| AC-05 | A lost acknowledgement, duplicate action or late reply yields one adopted result with honest accounting of uncertainty. |
| AC-06 | Incorrect OCR, a table with a lost unit and an ambiguous quotation lead back to inspection of the original. |
| AC-07 | Contradictory accounts, copies and later discoveries can change an important scene without giving its actors hindsight. |
| AC-08 | A causal explanation present only in a stage direction fails a cold reading of the spoken text. |
| AC-09 | A factual correction preserves a useful callback and verifies both the removal of a placeholder and resolution of a material request. |
| AC-10 | An invented host memory is repaired. A genuine unknown remains audible to the degree necessary for the argument. |
| AC-11 | A long dialogue can exceed the output window, resume a partial act, reorder acts and recompute continuity without duplicating turns. |
| AC-12 | A reported audio defect produces an exact proposal. Changing the source interval invalidates its prior approval. |
| AC-13 | Splicing rejects a different master. A changed interval length updates the timeline, rejoins both sides and retains the original. |
| AC-14 | Crashes around upload, transaction commit, outbox, integration and acceptance recover without dangling references or duplicate chapters. |
| AC-15 | Cancellation, UI reconnect and resume respect mutation identity and prevent late adoption. |
| AC-16 | Editing the script or policy during release assembly cannot produce a mixed-version release. |
| AC-17 | Withdrawing evaluator annotations or private originals also removes them from prompts, retrieval, exports and restored systems. |
| AC-18 | Blind listeners compare the dialogue alternatives on equal briefs, dossiers and plans, including a professional reference where possible. |
| AC-19 | A user can listen, correct and export a complete episode in each advertised language and format. |
| AC-20 | Deploy, outage, restore and rollback preserve the user's original intent, entitlement and pinned behavior. |
