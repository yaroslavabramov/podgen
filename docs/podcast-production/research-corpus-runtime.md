# Preparing and reading a research corpus

`research-material-preparation/2` accepts the assignment, a completed hosted research run and acquisition settings (`profile_ref`, `rights`). Its preparation command processes every reported link. Supply prior `acquisition.reuse_selection_ids` to reuse the saved documents or failed attempts with matching settings and retention policy.

The preparer merges transport-equivalent URLs by hostname case, default port and fragment. It preserves path, query and each original occurrence. Documents have separate, stable operation IDs. A retry resumes unfinished documents. Report sections refer to exact ranges of the original text. A link reported by the service, bytes obtained locally and evidence read from those bytes have distinct identities. Historical `research-material-preparation/1` inputs retain their own preparation rules.

## Reading beyond the first context window

`frozen-corpus-tools/7` introduced continuation by context window for the research-capable roles. The provider's native count determines whether the actual request fits with room for the response. Time and spending limits can still stop a run.

Before switching windows, the agent stores its full notes and a compact continuation state. The next request receives the assignment, continuation state and references to reopen earlier material. Full messages, readings and notes remain in the journal. R and E receipts survive a change of window. A technical stop leaves the review open.

The source catalog and search return versioned pages. The index processes text and embeddings in bounded batches, persists progress and only publishes a complete document. Range reads check access at the time of reading. [Source search](source-search.md)

## Additional documents and reports

- `request_source` accepts a known S ID or a URL with a reason. Acquisition and extraction use the ordinary source pipeline. A new document joins this run's journal. The tool returns a status and the acquisition or supplement reference. The reviewer can then open a passage.
- `request_research` records a specific question for hosted research. Preparing an order leaves payment pending. Approval and resume use the hosted research service. The review remains open until the result arrives. A subsequent resume imports the result and returns control to that reviewer.
- `list_research_results` lists supplementary reports, each with an X ID. `list_research_sources` lists their reported links. `read_research_result` produces an R receipt for report text. Individual source documents have S IDs and produce E receipts when read. The stored mapping connects the report's link numbers to those documents.
- A planning role can select a whole supplementary report through `select_research_result` for this version of the plan. The writer receives the selected X reports and the selected M sections of the original report. Reading additional material alone does not commission it for the script.

A continuation or imported accepted plan freezes the S/X additions visible at that point. Subsequent additions to the parent cannot change the child. Newly admitted documents receive the next free aliases. Existing receipts, text and bindings keep their meanings.

## Verification

Check resume between documents, reuse under matching policies, the E-to-P/H handoff to the writer, the distinction between R and E, reproduction of old requests, reads beyond the old corpus boundary and recovery after a context switch. Evaluate the narrative separately by reading the plan, plan review, first draft, final script and both script reviews for the same episode.
