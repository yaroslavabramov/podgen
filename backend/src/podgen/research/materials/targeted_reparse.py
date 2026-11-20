"""Version local interpretation of saved research without replacing the paid archive."""

VERSION = "targeted-research-validation/2"
REPARSE_ERRORS = {
    "targeted_research_invalid_answer",
    "targeted_research_quote_not_in_source",
    "targeted_research_evidence_unavailable",
    "targeted_research_invalid_evidence_url",
    "targeted_research_evidence_not_retained",
}


def cached_result(store, key, name, arguments):
    result_key = key + ":result"
    saved = store.get(result_key)
    if (
        name == "request_research"
        and arguments.get("research_contract") == "targeted-script-context/1"
        and saved
        and saved.get("status") in ("unresolved", "unavailable")
        and saved.get("error") in REPARSE_ERRORS
        and saved.get("validation_contract") != VERSION
    ):
        result_key = key + ":validation:" + VERSION + ":result"
        saved = store.get(result_key)
        if saved is not None:
            from podgen.domain.errors import ContractError

            if (
                saved.get("contract") != VERSION
                or saved.get("original_result_ref") != store.ref(key + ":result").model_dump(mode="json")
                or not isinstance(saved.get("result"), dict)
            ):
                raise ContractError("targeted_research_reparse_provenance_mismatch")
            saved = saved["result"]
    return result_key, saved


def save_result(store, key, result_key, result):
    if result_key != key + ":result":
        store.put(
            result_key,
            dict(
                contract=VERSION,
                original_result_ref=store.ref(key + ":result").model_dump(mode="json"),
                result=result,
                operation="local_reparse_of_retained_research",
            ),
        )
    else:
        store.put(result_key, result)


def resolve_saved(resolver, store, key, arguments, guard):
    """Read the original completed child directly; never reconstruct its request."""
    from uuid import UUID

    from podgen.domain.errors import ContractError
    from podgen.research.hosted.journal import ResearchJournal
    from podgen.sources.material_requests import MaterialResolution

    from .targeted_resolution import resolve

    handoff = store.get(key + ":handoff")
    if not handoff or handoff.get("contract") != "targeted-research-handoff/2":
        raise ContractError("targeted_research_reparse_handoff_missing")
    child = ResearchJournal(store.repo, store.project, UUID(handoff["child_run_id"]))
    manifest, result = child.get("manifest"), child.get("result")
    if (
        not manifest
        or manifest.get("request_hash") != handoff["request_hash"]
        or not result
        or result.get("status") != "completed"
    ):
        raise ContractError("targeted_research_reparse_binding_mismatch")
    guard()
    resolution = resolve(resolver, store, key, child, arguments, guard)
    original = store.get(key + ":result")
    if "research_order_id" in original:
        return MaterialResolution(
            dict(resolution.result, research_order_id=original["research_order_id"]), resolution.admissions
        )
    return resolution
