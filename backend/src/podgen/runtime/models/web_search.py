"""Native search outcomes belong to their paid Responses turn. They are not source reads."""

from podgen.domain.errors import ContractError

KEY = "web_search_receipt_contract"
LEGACY = "responses-web-search-receipt/1"
CURRENT = "responses-web-search-receipt/2"
UNSUCCESSFUL = ("failed", "incomplete")
ACTIONS = ("search", "open_page", "find_in_page")


def _row(item, version):
    identity, status, action = item.get("id"), item.get("status"), item.get("action")
    if not isinstance(identity, str) or not identity:
        raise ContractError("provider_web_search_incomplete_or_invalid")
    if version == CURRENT and status in UNSUCCESSFUL:
        return dict(call_id=identity, status=status, action=action)
    if status != "completed" or not isinstance(action, dict) or action.get("type") not in ACTIONS:
        raise ContractError("provider_web_search_incomplete_or_invalid")
    row = dict(call_id=identity, action=action)
    if version == CURRENT:
        row["status"] = status
    return row


def outcomes(response, version):
    rows, error = {}, None
    output = response.get("output")
    if not isinstance(output, list):
        raise ContractError("provider_output_missing")
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "web_search_call":
            continue
        try:
            row = _row(item, version)
            if row["call_id"] in rows:
                raise ContractError("duplicate_provider_web_search_call")
            rows[row["call_id"]] = row
        except ContractError as exc:
            if version == LEGACY:
                raise
            error = exc
    return list(rows.values()), error


def counts(rows):
    completed = [r for r in rows if r.get("status") not in UNSUCCESSFUL]
    return dict(
        tool_calls=len(completed),
        search_calls=sum(r["action"]["type"] == "search" for r in completed),
        unsuccessful_tool_calls=len(rows) - len(completed),
    )


def record(journal, root, history, response, turn, version):
    rows, error = outcomes(response, version)
    if rows and turn not in history.web_search_turns:
        usage = counts(rows)
        if version == LEGACY:
            del usage["unsuccessful_tool_calls"]
        phase = journal.publication(root)
        phase.event(
            "source_web_search",
            data=dict(
                contract=version,
                turn_index=turn,
                provider_response_id=response["id"],
                calls=rows,
                **usage,
                relationship="navigation_not_source_evidence",
                billing="provider_tool_calls_separate_from_token_usage; amount_unknown",
            ),
            history=history,
        )
        phase.commit()
    if error:
        raise error
    return [r for r in rows if r.get("status") in UNSUCCESSFUL]


def continuation_response(response, failed):
    """Only the next model turn receives this view. The native receipt stays intact."""
    if not failed:
        return response
    # Responses accepts terminal search items as history when the native action
    # is present. Some failures omit it. Never invent one or promote its query
    # or source content into an instruction.
    output = [
        item
        for item in response["output"]
        if item.get("type") != "web_search_call"
        or item.get("status") not in UNSUCCESSFUL
        or isinstance(item.get("action"), dict)
        and item["action"].get("type") in ACTIONS
    ]
    notice = "A hosted web action failed or was incomplete. Continue this assessment using the available evidence or inspect another source."
    return dict(response, output=[dict(role="developer", content=notice), *output])


def context_limit(body, profile):
    # Responses hosted web search has its own 128k window, even for larger models.
    return (
        min(profile.context_limit, 128000)
        if any(t.get("type") == "web_search" for t in body.get("tools", []))
        else profile.context_limit
    )
