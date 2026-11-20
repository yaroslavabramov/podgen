"""One immutable research commission per exact question, script and configuration.

The original material scope owns paid computation. Writing runs own admissions
and call provenance; their local names and retries do not create a new order.
"""

from uuid import UUID, uuid5

from podgen.domain.canonical import digest
from podgen.domain.errors import ContractError
from podgen.research.hosted.journal import ResearchJournal

KEY = "research_request_contract"
VERSION = "bound-research-orders/1"


def policy(manifest):
    value = manifest.get(KEY)
    if value is not None and (
        value != VERSION or manifest.get("source_tool_contract", {}).get("version") != "frozen-corpus-tools/8"
    ):
        raise ContractError("unsupported_research_request_contract")
    return value


def commission(resolver, arguments, guard):
    if arguments.get(KEY) != VERSION or arguments.get("research_contract") != "targeted-script-context/1":
        raise ContractError("unsupported_bound_research_request")
    context = arguments.get("script_context")
    if (
        not context
        or not context.get("anchors")
        or not context.get("script_hash")
        or _anchors(arguments.get("anchors", [])) != _anchors(context["anchors"])
    ):
        raise ContractError("bound_research_requires_script_context")
    from .targeted import INSTRUCTIONS

    ledger = ResearchJournal(
        resolver.journal.repo, resolver.journal.project, UUID(resolver.preparation["research_run_id"])
    )
    guard()
    original = ledger.get("manifest")["config"]
    # Reason explains why a caller needs this work. It is retained in each call,
    # but is not a second research question or permission to repay the same work.
    scope = dict(
        contract=VERSION,
        work=_work(arguments),
        output_language=resolver.materials.output_language,
        instructions=INSTRUCTIONS,
        profile=original["profile"],
    )
    identity = digest(scope)
    operation_id = uuid5(ledger.run_id, "bound-research-order:" + identity)
    key = "research-order:" + identity
    with ledger.lock():
        guard()
        saved = ledger.get(key)
        if saved is None:
            saved = dict(
                contract=VERSION,
                identity=identity,
                scope=scope,
                operation_id=str(operation_id),
                arguments=arguments,
            )
            ledger.put(key, saved)
        if (
            saved.get("contract") != VERSION
            or saved.get("identity") != identity
            or saved.get("scope") != scope
            or saved.get("operation_id") != str(operation_id)
            or _work(saved.get("arguments", {})) != _work(arguments)
        ):
            raise ContractError("bound_research_order_binding_mismatch")
    guard()
    return saved


def _anchors(rows):
    return [
        dict(utterance_index=index, quote=quote)
        for index, quote in sorted({(row["utterance_index"], row["quote"]) for row in rows})
    ]


def _work(arguments):
    # The call and original commission retain their exact ordering. Only the
    # reusable work identity treats duplicate/reordered anchors as the same set.
    context = arguments.get("script_context") or {}
    return dict(
        {
            key: value
            for key, value in arguments.items()
            if key not in {"reason", "anchors", "script_context"}
        },
        script_context=dict(context, anchors=_anchors(context.get("anchors", []))),
    )
