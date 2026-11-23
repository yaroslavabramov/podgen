# Cross-module design decisions

This record explains the main interfaces reconciled during the architecture review. Implementation details appear in the linked owner documents. The [acceptance matrix](07-implementation-plan.md#end-to-end-acceptance) describes how to exercise these decisions across boundaries.

| Decision | Resolution |
| --- | --- |
| R01. Discovering sources during a frozen task | Create a new immutable input manifest and retain the admission chain for each invocation. |
| R02. Reading versus delivery | A completed tool result becomes a delivery receipt only when it enters a dispatched model request. |
| R03. Domain and operational identity | Runtime tasks, searches and inbox entries have operational references. The editorial owner explicitly adopts their results. |
| R04. Previews | A preview derives from a complete note as a separate artifact with its own revision and coverage. |
| R05. Evidence coordinates | Use typed dataset selectors, zero-based page conventions, render coordinates and explicit audio time bases. |
| R06. Interfaces | The runtime owns the tool result and error envelope. Domain owners define the payloads. |
| R07. Cross-language hashes | A shared canonicalization contract and golden fixtures define serialization across Python and TypeScript. |
| R08. Baseline fairness | End-to-end competitors receive equal source access. An expert dossier belongs to a separate controlled writing comparison. |
| R09. Corrected versions | Separate historical provenance from current quality dependencies. Review the exact replacement. |
| R10. Invalidation | Each subject owner explains how a changed dependency affects the subject, including derived metadata or a dossier. |
| R11. Audio time | Master PCM frames determine the edit timeline. Resampling and encoded playback require explicit mappings. |
| R12. Reuse and acoustic approval | Reuse identical rendered bytes under a new binding when appropriate. Acoustic review remains bound to the edition it assessed. |
| R13. Qualification graph | Register immutable configuration, evaluate it, then attach qualification. A profile cannot depend on its own future evaluation. |
| R14. Evaluation access | Authorized evaluation can assemble a candidate before production qualification. That capability does not grant publication approval. |
| R15. Shared configuration | Expose permitted configuration kinds through a read-only catalog while preserving project rights for source material and recordings. |
| R16. Workflow state | Run status, editorial decision and current readiness answer different questions. Time-sensitive decisions include their temporal scope. |
| R17. Show notes | A separate review covers claims introduced by episode metadata. The release binds its exact result. |
| R18. Implementation order | Introduce common contracts and evaluation fixtures first so writing and verification can be exercised before autonomous research. |

A complete release crosses source → dossier → plan → script → review → audio → publication. Each handoff checks the exact version, admitted dependencies and the applicable authorization. Replay the binding at the point of integration, and exercise late replies, concurrent updates and withdrawal against the assembled result.
