"""Freshly bind server-produced material references before a tool checkpoint."""

from podgen.domain.references import walk_values


def bind_material_result(journal, root, name, result):
    if name in ("request_source", "request_research"):
        journal.execution.prepare(root["run_id"], evaluation=[ref for _, _, ref in walk_values(result)])


def prepare_result(
    journal, root, data, result, *, history=None, diagnostics=None, record_call=False, search_delivery=None
):
    bind_material_result(journal, root, data["name"], result)
    phase = journal.publication(root)
    if data["name"] == "search_sources" and diagnostics is not None:
        phase.event("source_search_diagnostics", data=dict(call_index=data["call_index"], **diagnostics))
    if record_call:
        phase.event("source_tool_call", data=data, history=history)
    record = dict(data, result=result)
    if data["name"] == "search_sources" and search_delivery is not None:
        record["search_delivery_identity"] = search_delivery
    result_ref = phase.event("source_tool_result", data=record, history=history)
    return phase, result_ref
