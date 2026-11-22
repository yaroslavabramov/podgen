"""Run-local interpretation of one shared research order, with separate call receipts."""

from uuid import UUID

from podgen.domain.canonical import digest
from podgen.sources.material_requests import MaterialResolution

from .orders import VERSION, commission
from .research_requests import resolve_research
from .targeted_reparse import cached_result, resolve_saved, save_result


def resolve(resolver, store, call_key, arguments, guard):
    order = commission(resolver, arguments, guard)
    key = "material-research-order:" + order["identity"]
    store.put(
        call_key + ":order",
        dict(contract=VERSION, research_order_id=order["identity"], operation_id=order["operation_id"]),
    )
    result_key, saved = cached_result(
        store, key, "request_research", order["arguments"], delivery=resolver.report_delivery
    )
    if saved is None:
        if result_key != key + ":result":
            resolution = resolve_saved(resolver, store, key, order["arguments"], guard)
        else:
            resolution = resolve_research(
                resolver, store, key, order["arguments"], UUID(order["operation_id"]), guard
            )
        saved = dict(resolution.result, research_order_id=order["identity"])
        if saved.get("status") == "pending":
            return MaterialResolution(saved, resolution.admissions)
        save_result(store, key, result_key, saved)
        admissions = resolution.admissions
    else:
        admissions = resolver.restore(run_id=store.run_id, guard=guard)
    guard()
    # Parsing upgrades are append-only. An old receipt still points at the exact
    # result it delivered, while a later validation version gets a new receipt.
    store.put(
        call_key + ":order-result:" + digest(result_key),
        dict(
            contract=VERSION,
            research_order_id=order["identity"],
            result_ref=store.ref(result_key).model_dump(mode="json"),
        ),
    )
    return MaterialResolution(saved, admissions)
