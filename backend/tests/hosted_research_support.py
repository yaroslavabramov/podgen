import hashlib
import json
from contextlib import nullcontext
from types import SimpleNamespace
from uuid import uuid4, uuid5

from podgen.domain.contracts.common import RuntimeRef
from podgen.domain.errors import Conflict
from podgen.research.hosted.contracts import Approval, GeminiProfile, ResearchConfig
from podgen.research.hosted.service import approve, prepare
from podgen.research.hosted.transport import Response


class MemoryJournal:
    def __init__(self):
        self.run_id = uuid4()
        self.project = uuid4()
        self.repo = SimpleNamespace(actor_id=uuid4())
        self.records, self.blobs = {}, {}

    def lock(self):
        return nullcontext()

    def ref(self, key):
        return RuntimeRef(record_kind="tool_execution", record_id=uuid5(self.run_id, key))

    def get(self, key):
        return self.records.get(key)

    def put(self, key, value):
        if key in self.records and self.records[key] != value:
            raise Conflict("immutable_record_conflict")
        self.records[key] = json.loads(json.dumps(value))
        return self.ref(key)

    def blob(self, body, media="application/json"):
        sha = hashlib.sha256(body).hexdigest()
        self.blobs[sha] = body
        return dict(storage_key=sha, sha256=sha, byte_length=len(body), media_type=media)

    def bytes(self, key, blob):
        return self.blobs[blob["sha256"]]


class Provider:
    def __init__(self, *responses):
        self.responses, self.calls = iter(responses), []

    def execute(self, kind, *args):
        self.calls.append((kind, args))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def submit(self, *args):
        return self.execute("submit", *args)

    def get(self, *args):
        return self.execute("get", *args)

    def cancel(self, *args):
        return self.execute("cancel", *args)


def response(state="in_progress", **kwargs):
    return Response(200, {}, json.dumps(dict(id="job/one", status=state, **kwargs)).encode())


def ready(monkeypatch, *, approved=True):
    monkeypatch.setenv("TEST_RESEARCH_KEY", "not-a-real-key")
    journal = MemoryJournal()
    config = ResearchConfig(
        run_id=journal.run_id,
        assignment={
            "user_request": "Расскажи о часах",
            "output_language": "ru",
            "scope": {"source_mode": "open_web"},
            "full_context": "x" * 10000,
        },
        instructions="Research the assignment.",
        profile=GeminiProfile(agent="deep-research-preview-04-2026", credential_env="TEST_RESEARCH_KEY"),
    )
    value = prepare(journal, config)
    if approved:
        approve(
            journal,
            Approval(
                request_hash=value["request_hash"],
                reason="One research task",
                accepts_variable_provider_cost=True,
            ),
        )
    return journal, config
