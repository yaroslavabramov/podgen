"""Shared exact no-generation predicates for capture, accounting and retry evidence."""

from podgen.runtime.models.messages_tool_policy import KEY, NAVIGATION
from podgen.runtime.models.messages_wire import messages_request

SCHEMA_MESSAGES = frozenset(
    {
        "The compiled grammar is too large, which would cause performance issues. Simplify your tool schemas or reduce the number of strict tools.",
        "Schema is too complex for compilation.",
    }
)


def credit(response):
    try:
        return (
            isinstance(response, dict)
            and response.get("id") is None
            and response.get("response_id") is None
            and response["error"]["code"] == "credit_balance_exhausted"
            and response["error"].get("response_id") is None
        )
    except (KeyError, TypeError, AttributeError):
        return False


def schema(response, request, native):
    try:
        return (
            response.get("type") == "error"
            and response.get("id") is None
            and response.get("response_id") is None
            and response.get("usage") is None
            and response["error"].get("type") == "invalid_request_error"
            and response["error"].get("message") in SCHEMA_MESSAGES
            and not any(response["error"].get(key) is not None for key in ("id", "response_id", "usage"))
            and request["model"].startswith("claude-")
            and KEY not in request.get("metadata", {})
            and any(
                tool.get("strict") is True and tool.get("name") in NAVIGATION
                for tool in native.get("tools", [])
            )
            and native == messages_request(request)
        )
    except (KeyError, TypeError, AttributeError, ValueError):
        return False


def incoming(journal, invocation, fault, operation):
    if operation != "submit" or fault.ambiguous or fault.reply is None:
        return None
    from .validation import load_unique_json

    try:
        response = load_unique_json(fault.reply.body)
        if fault.code == "provider_http_429" and fault.reply.status_code == 429 and credit(response):
            return "credit_refusal"
        if (
            fault.code == "provider_http_400"
            and fault.reply.status_code == 400
            and fault.reply.request_body is not None
        ):
            request = journal.request(invocation)
            native = load_unique_json(fault.reply.request_body)
            if schema(response, request, native):
                return "schema_refusal"
    except (ValueError, TypeError):
        return None
    return None
