"""Report bound search outcomes, including the last turn of an interrupted checker."""

import json

from podgen.runtime.models import web_search


class SearchUsage:
    def __init__(self, recover_tail):
        self.counts = dict(tool_calls=0, search_calls=0, unsuccessful_tool_calls=0)
        self.recover_tail = recover_tail
        self.pending = None

    def observe(self, kind, data, response_path):
        if kind == "source_web_search":
            for key in self.counts:
                self.counts[key] += data.get(key, 0)
            if self.pending and data["provider_response_id"] == self.pending[0]:
                self.pending = None
        elif (
            self.recover_tail
            and kind == "provider_response"
            and data.get("turn_provider_response_id")
            and response_path is not None
        ):
            # A completed turn records its search outcomes before requesting the
            # next generation. Only the final interrupted turn can lack this event.
            self.pending = (data["turn_provider_response_id"], response_path)

    def result(self):
        if self.pending:
            provider_id, path = self.pending
            native = json.loads(path.read_bytes())
            if (
                native.get("id") == provider_id
                and native.get("status") in ("completed", "failed", "incomplete", "cancelled")
                and isinstance(native.get("output"), list)
            ):
                rows, _ = web_search.outcomes(native, web_search.CURRENT)
                for key, count in web_search.counts(rows).items():
                    self.counts[key] += count
        return self.counts
