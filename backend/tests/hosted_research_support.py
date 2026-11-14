import hashlib
import json
from contextlib import nullcontext
from copy import deepcopy
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
        self.projection = None

    def head(self):
        return self.projection

    def create_manifest(self, value):
        from podgen.research.hosted.projection import empty

        result = self.put("manifest", value)
        self.projection = empty()
        return result

    def adopt_head(self, value):
        if self.projection is None:
            self.projection = value
        return self.projection

    def exchange(self, key, value, previous, updated):
        assert self.projection == previous
        records = {name: deepcopy(self.records.get(name)) for name in (key, "job")}
        try:
            result = self.put(key, value)
            if updated["job_recorded"] and not previous["job_recorded"]:
                self._append_job(updated)
            self.projection = updated
            return result
        except BaseException:
            for name, previous_value in records.items():
                if previous_value is None:
                    self.records.pop(name, None)
                else:
                    self.records[name] = previous_value
            raise

    def _append_job(self, value):
        self.put(
            "job",
            dict(
                provider_job_id=value["current"]["id"],
                manifest_ref=self.ref("manifest").model_dump(mode="json"),
            ),
        )

    def ensure_job(self, previous):
        if not previous["current"] or previous["job_recorded"] or previous.get("integrity_error"):
            return previous
        self._append_job(previous)
        updated = deepcopy(previous)
        updated.update(revision=previous["revision"] + 1, job_recorded=True)
        self.projection = updated
        return updated

    def lock(self, *, write=True):
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
