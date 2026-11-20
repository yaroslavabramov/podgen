"""Bind investigation to the immutable script, never to a reviewer's paraphrase."""

import json
from copy import deepcopy

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import ContractError

VERSION = "targeted-script-context/1"


def request_arguments(request, *, anchored):
    arguments = dict(
        question=request["question"],
        reason=request.get("why_needed") or request.get("reason") or json.dumps(request, ensure_ascii=False),
    )
    if anchored:
        arguments["anchors"] = deepcopy(request.get("anchors", []))
    return arguments


def bind(journal, root, arguments, *, request_policy=None):
    from podgen.research.materials.orders import KEY
    from podgen.research.materials.orders import VERSION as ORDER_VERSION

    if request_policy not in (None, ORDER_VERSION):
        raise ContractError("unsupported_research_request_contract")
    if request_policy is not None:
        arguments = dict(arguments, **{KEY: request_policy})
    request = journal.read(RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"]))
    context = request["context"]
    ref = context.get("script_ref")
    anchors = arguments.get("anchors", [])
    if ref is None:
        if root.get("stage", "").startswith("fact-review"):
            raise ContractError("research_script_missing")
        if anchors:
            raise ContractError("research_anchor_without_script")
        return dict(arguments, research_contract=VERSION, script_context=None)
    script = journal.read(RuntimeRef.model_validate(ref))["output"]
    if digest(script) != context["script_hash"]:
        raise ContractError("research_script_binding_mismatch")
    if not anchors:
        raise ContractError("research_script_anchors_required")
    rows, selected = script["utterances"], set()
    for anchor in anchors:
        index, quote = anchor.get("utterance_index"), anchor.get("quote")
        if (
            type(index) is not int
            or not 0 <= index < len(rows)
            or not isinstance(quote, str)
            or not quote.strip()
            or quote not in rows[index]["text"]
        ):
            raise ContractError("research_script_anchor_mismatch")
        selected.update(range(max(0, index - 1), min(len(rows), index + 2)))
    # The exact reference stays in the private caller request context. Public
    # research records carry the verified content hash and excerpts, never an
    # evaluator-only reference that a production researcher cannot authorize.
    return dict(
        arguments,
        research_contract=VERSION,
        script_context=dict(
            script_hash=context["script_hash"],
            anchors=deepcopy(anchors),
            utterances=[dict(utterance_index=i, **deepcopy(rows[i])) for i in sorted(selected)],
        ),
    )
