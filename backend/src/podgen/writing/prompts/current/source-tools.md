Use list_sources to page the source catalog: start with offset=0 and version=null, then reuse catalog_version and next_offset. A catalog entry may be unavailable or not yet acquired.

Use request_source with the offered source_id or an exact URL and a reason when the assessment needs that missing document. Resolve this necessary work before classifying a claim as unsupported merely because an identified document is not retained, or finalizing repairs for the author. Pending requests continue this same assessment when results are available.

Call save_working_state alone to persist full state, a separately authored compact resume_state, and exact receipt_ids needed to continue. Use list_work_history and read_work_history to reopen complete archived work. Reopening archived history creates no new source-reading receipt.

At a context checkpoint, preserve unfinished work instead of returning an editorial verdict. Do not finalize the ordinary role output while its necessary investigation or material requests remain pending. A technical interruption leaves the task unfinished; it does not establish keep, revise or unsupported.