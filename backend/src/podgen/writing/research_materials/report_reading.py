"""Read reports as synthesis, with source leads only on explicitly versioned admissions."""


def read(corpus, args, read_id, text_page):
    source = corpus.supplemental_reports.get(args["result_id"])
    if source is None:
        return dict(error="unknown_research_result")
    page = text_page(source.text, args["start"], args["end"])
    if "error" in page:
        return page
    result = dict(
        material_passage=dict(
            read_id="R" + read_id[1:],
            material_id=args["result_id"],
            title=source.title,
            text=page["text"],
            start=page["start"],
            end=page["end"],
        ),
        next_start=page["next_start"],
        length=page["length"],
        kind="research_synthesis_not_source_evidence",
        source_mapping=dict(
            tool="list_research_sources", result_id=args["result_id"], offset=0, version=None
        ),
    )
    from podgen.research.materials.report_navigation import KEY, VERSIONS, passage_sources, source_links

    catalog = corpus.admission_catalog.get(args["result_id"], {})
    if catalog.get(KEY) in VERSIONS:
        result["sources"] = passage_sources(
            page["text"], source_links(catalog["reported_source_map"], corpus._materials)
        )
    return result
