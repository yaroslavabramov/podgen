"""Offline provider oracle: replay saved bytes and reject any model-facing change."""

import json
import tarfile
from copy import deepcopy
from uuid import uuid5

import httpx2

from podgen.domain.canonical import digest
from podgen.runtime.models.openai_provider import OpenAIProvider


class Traffic:
    def __init__(self, archive, run_id):
        self.run_id = run_id
        with tarfile.open(archive) as trace:
            root = "export/archive/fact-review/"
            self.initial = json.load(trace.extractfile(root + "request.json"))
            self.events = json.load(trace.extractfile(root + "events.json"))
            self.counts, self.responses = [], []
            for index, event in enumerate(self.events):
                if event["event_kind"] not in {"source_context_count", "provider_response"}:
                    continue
                prefix = root + f"{index:03d}-{event['event_kind']}"
                wire = json.load(trace.extractfile(prefix + "-wire_request_blob.json"))
                response = trace.extractfile(prefix + "-response_blob.json").read()
                target = self.counts if event["event_kind"] == "source_context_count" else self.responses
                target.append((wire, response))
        assert len(self.counts) == len(self.responses) == 43
        self.seen_counts, self.seen_submits = [], []
        self.tool_results = [
            e["data"]["result"] for e in self.events if e["event_kind"] == "source_tool_result"
        ]
        self.seen_tools = 0
        self.client = httpx2.Client(transport=httpx2.MockTransport(self.handle))

    @property
    def approved_hash(self):
        body = deepcopy(self.initial)
        body["metadata"].update(
            podgen_run_id=str(self.run_id),
            podgen_invocation_id=str(uuid5(self.run_id, "invocation:fact-review")),
        )
        return digest(body)

    def handle(self, request):
        body = json.loads(request.content)
        counting = request.url.path.endswith("/input_tokens")
        seen = self.seen_counts if counting else self.seen_submits
        saved = self.counts if counting else self.responses
        assert len(seen) < len(saved), "unexpected additional provider request"
        expected, response = saved[len(seen)]
        expected = deepcopy(expected)
        if not counting:
            index = int(expected["metadata"]["podgen_source_turn"])
            root = uuid5(self.run_id, "invocation:fact-review")
            expected["metadata"].update(
                podgen_run_id=str(self.run_id),
                podgen_invocation_id=str(uuid5(root, f"source-turn:{index}")),
            )
        assert digest(body) == digest(expected), (
            "native_request_changed",
            counting,
            len(seen),
            digest(body),
            digest(expected),
        )
        if not counting:
            decoded = json.loads(response)
            if "metadata" in decoded and decoded["metadata"] != body["metadata"]:
                decoded["metadata"] = body["metadata"]
                response = json.dumps(decoded, ensure_ascii=False, separators=(",", ":")).encode()
        seen.append(digest(body))
        return httpx2.Response(200, content=response, headers={"content-type": "application/json"})

    def provider(self, profile):
        return OpenAIProvider("offline-key-never-sent", http_client=self.client)
