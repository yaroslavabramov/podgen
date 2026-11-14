"""Suspend a check on a concrete research question and import its completed answer."""

import asyncio
from uuid import UUID, uuid5

from podgen.domain.errors import ContractError
from podgen.research.hosted.contracts import ResearchConfig
from podgen.research.hosted.corpus_acquisition import acquire_corpus, document_url
from podgen.research.hosted.journal import ResearchJournal
from podgen.research.hosted.service import prepare
from podgen.sources.material_requests import MaterialResolution

from .admissions import admit_outcome, admit_report
from .resolver import same_document
from .sections import source_mapping


def resolve_research(resolver, store, key, arguments, operation_id, guard):
    original = ResearchJournal(store.repo, store.project, UUID(resolver.preparation["research_run_id"]))
    parent = original.get("manifest")["config"]
    child = ResearchJournal(store.repo, store.project, uuid5(operation_id, "targeted-research"))
    episode_assignment = resolver.materials.packet.assignment.model_dump(mode="json")
    episode_assignment.pop("duration_estimate", None)
    config = ResearchConfig(
        run_id=child.run_id,
        instructions=parent["instructions"],
        profile=parent["profile"],
        assignment=dict(
            user_request=arguments["question"],
            output_language=resolver.materials.output_language,
            scope=dict(source_mode="open_web"),
            reason=arguments["reason"],
            episode_assignment=episode_assignment,
            task="Resolve this specific open question; do not repeat the general episode research.",
        ),
    )
    manifest = prepare(child, config)
    handoff = dict(
        contract="targeted-research-handoff/1",
        child_run_id=str(child.run_id),
        question=arguments["question"],
        reason=arguments["reason"],
        request_hash=manifest["request_hash"],
        parent_report_ref=original.ref("result").model_dump(mode="json"),
        request_ref=child.ref("manifest").model_dump(mode="json"),
    )
    store.put(key + ":handoff", handoff)
    if not child.get("result"):
        return MaterialResolution(
            dict(
                status="pending",
                handoff_ref=store.ref(key + ":handoff").model_dump(mode="json"),
                **handoff,
                next_action="Complete the prepared targeted research job through hosted research, then resume this same check.",
            )
        )
    guard()
    acquisition = resolver.config.model_copy(update={"reuse_selection_ids": []})
    acquired = asyncio.run(
        acquire_corpus(
            child, uuid5(operation_id, "targeted-research-sources"), acquisition, acquire=resolver.acquire
        )
    )
    report = child.bytes("result", child.get("result")["report_text_blob"]).decode("utf-8")
    numbered = source_mapping(report)
    source_map = []
    for row in acquired["sources"]:
        guard()
        try:
            url = document_url(row["reported_url"])
        except (ValueError, ContractError):
            url = row["reported_url"]
        known = next(
            (
                s
                for s in resolver.materials.packet.sources
                if s.document and same_document(s.reported_url, url)
            ),
            None,
        )
        admitted = None if known else admit_outcome(store, resolver.materials, url, row["acquisition"])
        source_map.append(
            dict(
                reported_url=row["reported_url"],
                report_numbers=[n for n, item in numbered.items() if item["url"] == row["reported_url"]],
                source_id=known.source_id if known else admitted.source_id if admitted else None,
                availability=known.status if known else row["acquisition"]["status"],
            )
        )
    admitted_report = admit_report(store, resolver.materials, child, arguments["question"], source_map)
    return MaterialResolution(
        dict(
            status="resolved",
            report_id=admitted_report.source_id,
            title=admitted_report.title,
            reported_source_map=source_map,
            relationship="research_context_not_primary_source_evidence",
        ),
        resolver.restore(run_id=store.run_id, guard=guard),
    )
