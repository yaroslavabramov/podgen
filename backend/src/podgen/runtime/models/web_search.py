"""Responses-native search receipts: navigation and accounting, never source evidence.

The ordinary physical turn owns submission, count, uncertainty and recovery. This
adapter interprets only already-durable provider output; it performs no requests.
"""

from podgen.domain.errors import ContractError


def receipts(response):
    rows = []
    for item in response.get("output", []):
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action")
        if (
            not isinstance(item.get("id"), str)
            or item.get("status") != "completed"
            or not isinstance(action, dict)
            or action.get("type") not in {"search", "open_page", "find_in_page"}
        ):
            raise ContractError("provider_web_search_incomplete_or_invalid")
        rows.append(dict(call_id=item["id"], action=action))
    if len({row["call_id"] for row in rows}) != len(rows):
        raise ContractError("duplicate_provider_web_search_call")
    return rows


def record(journal, root, history, response, turn):
    rows = receipts(response)
    if not rows or turn in history.web_search_turns:
        return
    phase = journal.publication(root)
    phase.event(
        "source_web_search",
        data=dict(
            contract="responses-web-search-receipt/1",
            turn_index=turn,
            provider_response_id=response["id"],
            tool_calls=len(rows),
            calls=rows,
            search_calls=sum(row["action"]["type"] == "search" for row in rows),
            relationship="navigation_not_source_evidence",
            billing="provider_tool_calls_separate_from_token_usage; amount_unknown",
        ),
        history=history,
    )
    phase.commit()


def context_limit(body, profile):
    # Responses hosted web search has its own 128k window, even for larger models.
    # Freeze this behavior with the v8 tool declaration, without changing profiles.
    return (
        min(profile.context_limit, 128000)
        if any(t.get("type") == "web_search" for t in body.get("tools", []))
        else profile.context_limit
    )
