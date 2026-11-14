"""Narrow call proof derived from received response bytes before entering SQL."""

import hashlib
import json
from uuid import UUID


def valid_identifier(value, *, max_bytes):
    if not isinstance(value, str) or not value or "\0" in value:
        return False
    try:
        return len(value.encode("utf-8")) <= max_bytes
    except UnicodeError:
        return False


def project_calls(project, run_id, ref, payload, body):
    try:
        response = json.loads(body)
    except (ValueError, TypeError):
        return []
    output = response.get("output") if isinstance(response, dict) else None
    if not isinstance(output, list):
        return []
    data = payload["data"]
    physical = UUID(data.get("source_turn_id", payload["invocation_id"]))
    request = data.get("turn_request_ref", payload["request_ref"])
    rows = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue
        if not all(isinstance(item.get(key), str) and item[key] for key in ("call_id", "name", "arguments")):
            continue
        if not valid_identifier(item["call_id"], max_bytes=512) or not valid_identifier(
            item["name"], max_bytes=256
        ):
            continue
        try:
            for key in ("call_id", "name", "arguments"):
                if "\0" in item[key]:
                    raise ValueError("invalid_projection_text")
                item[key].encode("utf-8")
            arguments_hash = hashlib.sha256(item["arguments"].encode()).hexdigest()
        except (UnicodeError, ValueError):
            continue
        rows.append(
            dict(
                project_id=project,
                run_id=run_id,
                response_record_id=ref.record_id,
                physical_invocation_id=physical,
                request_record_id=UUID(request["record_id"]),
                call_id=item["call_id"],
                arguments_hash=arguments_hash,
                tool_name=item["name"],
            )
        )
    # Duplicate call IDs have no unambiguous binding. Preserve the raw response,
    # but register no proof from a malformed call list.
    if len({row["call_id"] for row in rows}) != len(rows):
        return []
    return rows
