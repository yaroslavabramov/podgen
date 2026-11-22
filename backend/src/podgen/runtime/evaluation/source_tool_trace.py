"""Read receipts belong to the original fact invocation, including subsequent anchor repair."""

from podgen.domain.errors import Conflict

from .model_turns import events


def original_invocation(journal, invocation):
    current = invocation
    seen = set()
    # A local readmission reuses its parent's response; the parent lineage owns the work.
    while current["operation_kind"] != "role" or current.get("attempt_kind") == "local_readmission":
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
    from .source_tool_recovery import lineage

    return [
        record["data"]["result"]["passage"]
        for ancestor in lineage(journal, root)
        for record in events(journal, ancestor, kinds={"source_tool_result"})
        if record["event_kind"] == "source_tool_result"
        and record["data"]["name"] == "read_source"
        and "passage" in record["data"]["result"]
        and "error" not in record["data"]["result"]
    ]


def commission_invocation(journal, invocation):
    """The first role attempt owns the semantic assignment across operational retries."""
    root = original_invocation(journal, invocation)
    from .source_tool_recovery import continuation, lineage

    ancestors = lineage(journal, root)
    if len(ancestors) > 1:
        from podgen.domain.contracts.common import RuntimeRef

        from .source_tool_contract import applies, from_manifest

        record = journal.read(RuntimeRef(record_kind="tool_execution", record_id=root["request_record_id"]))
        authorization = record.get("authorization") or {}
        contract = from_manifest(journal.manifest(root["run_id"]))
        if "source_tool_resume" in authorization or applies(root["stage"], contract):
            if "source_tool_resume" not in authorization:
                raise Conflict("source_tool_resume_authorization_mismatch")
            continuation(journal, root)
    return ancestors[0]
