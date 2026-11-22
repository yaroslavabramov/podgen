"""Deliver any completed textual research as context without acquiring its whole bibliography."""

from podgen.domain.errors import ContractError
from podgen.sources.material_requests import MaterialResolution

from .admissions import admit_report
from .report_navigation import (
    KEY,
    VERSION,
    excerpt_range,
    passage_sources,
    reading_links,
    reported_links,
    source_links,
)


def resolve(resolver, store, child, arguments, guard):
    result = child.get("result")
    if not result or result.get("status") != "completed":
        raise ContractError("research_report_delivery_requires_completed_result")
    text = child.bytes("result", result["report_text_blob"]).decode("utf-8")
    if not text.strip():
        raise ContractError("research_empty_document")
    mapping = reported_links(text, result.get("material", {}))
    guard()
    report = admit_report(
        store, resolver.materials, child, arguments["question"], mapping, delivery_contract=VERSION
    )
    structured = _structured(resolver, store, text, guard)
    admissions = resolver.restore(run_id=store.run_id, guard=guard)
    start, end = excerpt_range(text, arguments["question"])
    # The receipt owns this exact delivery. Later admissions may improve source navigation,
    # but cannot replace the report bytes, question or delivered excerpt.
    from types import SimpleNamespace

    view = source_links(
        mapping, SimpleNamespace(packet=resolver.materials.packet, runtime_admissions=admissions)
    )
    return MaterialResolution(
        dict(
            status="resolved",
            **{KEY: VERSION},
            report_id=report.source_id,
            title=report.title,
            report_excerpt=dict(text=text[start:end], start=start, end=end),
            **structured,
            sources=passage_sources(text[start:end], view),
            **reading_links(report.source_id, start, end),
            relationship="research_context_not_primary_source_evidence",
            next_action="Use validated excerpt ranges with read_source, or read the report and select an original. Report text and citations are leads; only read_source supplies E evidence receipts.",
        ),
        admissions,
    )


def _structured(resolver, store, text, guard):
    from .targeted import parse, validated_excerpts
    from .targeted_sources import originals

    try:
        answer = parse(text)
    except ContractError:
        return {}
    if not answer.evidence:
        return dict(limitations=answer.limitations)
    sources, _ = originals(resolver, store, answer, guard)
    excerpts, rejected = validated_excerpts(answer, sources)
    return dict(excerpts=excerpts, rejected_evidence=rejected, limitations=answer.limitations)


def terminal_result(delivery):
    """A delivered report remains readable even if its structured answer supplied no usable evidence."""
    if delivery.get(KEY) == VERSION and "limitations" in delivery and not delivery.get("excerpts"):
        return dict(delivery, status="unresolved")
    return delivery


def recover_saved(resolver, run_id, key, guard):
    """Explicit local upgrade, without invoking commission(), prepare() or hosted research.

    The caller opts in on a copy or current material scope. Historical requests,
    reviews and the original result remain unchanged; only an admission and its
    derived delivery receipt are appended.
    """
    from .targeted_reparse import cached_result, resolve_saved, save_result

    if resolver.report_delivery != VERSION:
        raise ContractError("research_report_delivery_opt_in_required")
    guard()
    store = resolver.store(run_id)
    with store.lock():
        handoff = store.get(key + ":handoff")
        if not handoff or store.get(key + ":result") is None:
            raise ContractError("research_report_delivery_saved_result_missing")
        arguments = dict(research_contract="targeted-script-context/1", question=handoff["question"])
        result_key, saved = cached_result(store, key, "request_research", arguments, delivery=VERSION)
        if saved is not None:
            guard()
            return MaterialResolution(saved, resolver.restore(run_id=run_id, guard=guard))
        resolution = resolve_saved(resolver, store, key, arguments, guard)
        guard()
        save_result(store, key, result_key, resolution.result)
        return resolution
