"""Read a known generation before admitting a replacement; never submit from this module."""

import json

from podgen.runtime.evaluation.contracts import RunFailure

from .contracts import ProviderFault, WireReply
from .response_content import binding_error


def _archived_completion(journal, invocation):
    raw = journal.last_response(invocation)
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return WireReply(raw) if isinstance(value, dict) and value.get("status") == "completed" else None


def retrieve(gateway, invocation, profile, *, guard, full_response=False):
    identity = invocation.get("provider_response_id")
    journal = gateway.journal
    reply = _archived_completion(journal, invocation)
    if not identity and reply is None:
        return False
    guard()
    journal.event(
        invocation,
        "reconciliation_intent",
        data=dict(response_id=identity, previous_status=invocation["status"]),
    )
    provider = None
    if reply is None and identity:
        provider = gateway.provider_factory(profile)
        try:
            reply = provider.retrieve(identity)
        except ProviderFault as fault:
            journal.event(invocation, "reconciliation_error", reply=fault.reply, data=dict(code=fault.code))
            # An unavailable service or credentials do not establish that a known generation
            # is unrecoverable. Only a definitive absence permits a replacement.
            if fault.code not in ("provider_http_404", "provider_http_410", "response_not_retrievable"):
                raise RunFailure(
                    fault.code, run_id=invocation["run_id"], invocation_id=invocation["invocation_id"]
                ) from None
    if reply is None:
        return False
    body = journal.request(invocation)
    try:
        response = json.loads(reply.body)
        error = (
            binding_error(response, body, identity)
            if isinstance(response, dict)
            else "provider_malformed_response"
        )
    except (ValueError, TypeError):
        error = "provider_malformed_response"
    if error:
        journal.event(invocation, "reconciliation_rejected", reply=reply, data=dict(code=error))
        raise RunFailure(error, run_id=invocation["run_id"], invocation_id=invocation["invocation_id"])
    raw = gateway._archive_reply(invocation, reply, body, expected_id=identity)
    gateway._receive(
        invocation,
        profile,
        body,
        provider,
        raw,
        guard,
        full_response=full_response,
        reconciled=True,
    )
    journal.event(invocation, "reconciliation_completed", data=dict(response_id=identity))
    return True
