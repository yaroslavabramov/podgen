"""Acquire only the narrow answer's cited documents and validate its literal excerpts."""

from podgen.domain.errors import ContractError
from podgen.sources.material_requests import MaterialResolution

from .admissions import admit_report
from .targeted_reparse import VERSION


def resolve(resolver, store, key, child, arguments, guard):
    if getattr(resolver, "report_delivery", None) is not None:
        from .report_delivery import resolve as deliver_report

        return deliver_report(resolver, store, child, arguments, guard)
    from .targeted import handoff, parse, validated_excerpts

    report = child.bytes("result", child.get("result")["report_text_blob"]).decode("utf-8")
    try:
        answer = parse(report)
    except ContractError as error:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                status="unresolved",
                error=error.code,
                question=arguments["question"],
                next_action="The saved research answer failed validation. Continue this assessment with source discovery and reading; no completed answer was admitted.",
            ),
        )
    if not answer.evidence:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                status="unresolved",
                question=arguments["question"],
                answer=answer.answer,
                limitations=answer.limitations,
                next_action="No original-source excerpt was supplied. Continue verification; no completed answer was admitted.",
            ),
        )
    from .targeted_sources import originals

    sources, source_map = originals(resolver, store, answer, guard)
    unavailable = [row for row in source_map if row["source_id"] is None]
    excerpts, rejected = validated_excerpts(answer, sources)
    if rejected:
        return _unfinished(
            resolver,
            store,
            guard,
            dict(
                contract="targeted-research-partial-evidence/1",
                status="unresolved" if excerpts or not unavailable else "unavailable",
                error="targeted_research_evidence_unavailable" if unavailable else rejected[0]["error"],
                question=arguments["question"],
                unverified_answer=answer.answer,
                limitations=answer.limitations,
                excerpts=excerpts,
                rejected_evidence=rejected,
                sources=source_map,
                relationship="validated_source_locations_not_read_receipts",
                next_action="The answer remains unverified. Read the validated excerpt ranges with read_source and resolve the rejected evidence before deciding the claim; no completed answer was admitted.",
            ),
        )
    summary = handoff(answer, excerpts)
    store.put(key + ":answer", summary)
    admitted_report = admit_report(store, resolver.materials, child, arguments["question"], source_map)
    return MaterialResolution(
        dict(
            status="resolved",
            validation_contract=VERSION,
            report_id=admitted_report.source_id,
            title=admitted_report.title,
            **summary,
        ),
        resolver.restore(run_id=store.run_id, guard=guard),
    )


def _unfinished(resolver, store, guard, result):
    return MaterialResolution(
        dict(result, validation_contract=VERSION), resolver.restore(run_id=store.run_id, guard=guard)
    )
