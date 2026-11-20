"""Native usage, repeated queries and read spans, including interrupted units."""

import json
from collections import Counter

from podgen.domain.canonical import digest
from podgen.domain.contracts.common import BlobRef, RuntimeRef
from podgen.persistence.private_journal.state import head
from podgen.runtime.evaluation.model_turns import events


def collect(journal, run_id):
    if journal.run(run_id) is None:
        return None
    row = journal.invocation(run_id, "fact-review")
    if row is None:
        return None
    seen = set()
    usage, tools = [], []
    for event in events(journal, row, kinds={"source_tool_result", "provider_response"}):
        data = event["data"]
        if event["event_kind"] == "source_tool_result":
            result = data["result"]
            passage = result.get("passage", {})
            tools.append(
                dict(
                    name=data["name"],
                    arguments=data["arguments"],
                    error=result.get("error"),
                    span={k: passage[k] for k in ("source_id", "start", "end") if k in passage},
                )
            )
        elif event.get("response_blob"):
            ref = RuntimeRef(record_kind="tool_execution", record_id=event["_record_id"])
            native = json.loads(journal.read_blob(ref, BlobRef.model_validate(event["response_blob"])))
            if native.get("status") != "completed" or native["id"] in seen:
                continue
            seen.add(native["id"])
            usage.append(dict(id=native["id"], model=native["model"], usage=native["usage"]))
    return dict(
        status=row["status"],
        charges=head(journal.repo, journal.project, run_id)["accounting"],
        usage=usage,
        tools=tools,
    )


def combined(units):
    charges = Counter()
    searches, reads = Counter(), Counter()
    for unit in units:
        if unit is None:
            continue
        charges.update(unit["charges"])
        for event in unit["tools"]:
            if event["name"] == "search_sources":
                searches[digest(event["arguments"])] += 1
            if event["span"]:
                reads[digest(event["span"])] += 1
    return dict(
        charges=dict(charges),
        searches=searches.total(),
        repeated_exact_searches=searches.total() - len(searches),
        reads=reads.total(),
        repeated_exact_reads=reads.total() - len(reads),
    )
