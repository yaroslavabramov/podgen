"""Versioned operational adapter; the literary instruction body remains unchanged."""

BODY = """Use list_research_results to discover supplemental reports and list_research_sources to map their local citations to retained S identifiers.

Use list_sources to page the source catalog: start with offset=0 and version=null, then reuse catalog_version and next_offset. A catalog entry may be unavailable or not yet acquired.

Use request_source with the offered source_id or an exact URL and a reason when the assessment needs that missing document. Use request_research with a specific question and reason when additional investigation is needed instead. Resolve this necessary work before classifying a claim as unsupported merely because an identified document is not retained, or finalizing repairs for the author. Read returned X reports through read_research_result and inspect their underlying documents with the source readers. Pending requests continue this same assessment when results are available.

Call save_working_state alone to persist full state, a separately authored compact resume_state, and exact receipt_ids needed to continue. Use list_work_history and read_work_history to reopen complete archived work. Reopening archived history creates no new source-reading receipt.

At a context checkpoint, preserve unfinished work instead of returning an editorial verdict. Do not finalize the ordinary role output while its necessary investigation or material requests remain pending. A technical interruption leaves the task unfinished; it does not establish keep, revise or unsupported.

During planning, use select_research_result to explicitly select any complete supplemental X report the author needs if this exact plan is accepted. This delivers its full text alongside selected M sections without treating synthesis as original-source evidence. Merely opening a report does not select it.
"""


ANCHORED = """For request_research during a script assessment, supply anchors with exact quote and utterance_index from the supplied script. The runtime attaches the complete original turns and adjacent context; ask about their actual meaning without strengthening the claim. Use retained sources first. During factual review, use the Responses web_search tool for a narrow missing fact before requesting deeper investigation. Search results and citations are navigation, never E evidence: obtain the exact document with request_source and read its retained text with read_source before citing it. Deep research is for questions requiring comparison or unresolved conflicting accounts. Its synthesis also needs original-source reading."""


def attach(body, *, anchored=False):
    return (
        body + "\n\n# Durable research tools and context\n\n" + BODY + ("\n" + ANCHORED if anchored else "")
    )
