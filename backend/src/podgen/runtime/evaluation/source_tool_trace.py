"""Read receipts belong to the original fact invocation, including subsequent anchor repair."""

from podgen.domain.errors import Conflict

from .model_turns import events


def original_invocation(journal, invocation):
    current = invocation
    seen = set()
    while current["operation_kind"] != "role":
        if current["invocation_id"] in seen or current.get("parent_invocation_id") is None:
            raise Conflict("source_tool_origin_missing")
        seen.add(current["invocation_id"])
        current = next(
            (
                row
                for row in journal.attempts(current["run_id"], current["stage"])
                if row["invocation_id"] == current["parent_invocation_id"]
            ),
            None,
        )
        if current is None:
            raise Conflict("source_tool_origin_missing")
    return current


def successful_read_passages(journal, invocation):
    root = original_invocation(journal, invocation)
    return [
        record["data"]["result"]["passage"]
        for record in events(journal, root, kinds={"source_tool_result"})
        if record["event_kind"] == "source_tool_result"
        and record["data"]["name"] == "read_source"
        and "passage" in record["data"]["result"]
        and "error" not in record["data"]["result"]
    ]
