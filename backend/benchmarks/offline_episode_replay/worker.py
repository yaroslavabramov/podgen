"""Replay exact retained provider/source responses through the ordinary product."""

import functools
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from uuid import UUID

import httpx
import httpx2
from probe import Probe

from podgen.audio.synthesis.contracts import HttpReceipt
from podgen.domain.canonical import digest
from podgen.episodes.production import audio
from podgen.episodes.production.contracts import ProductionConfig
from podgen.episodes.production.export import export
from podgen.episodes.production.prepare import prepare
from podgen.episodes.production.service import EpisodeProduction
from podgen.persistence.database import Database
from podgen.persistence.repository import Repository
from podgen.research.hosted.transport import Response
from podgen.runtime.evaluation.fact_source_experiment.measurements import MeasuredObjects
from podgen.runtime.models.contracts import WireReply
from podgen.runtime.models.count_receipts import native_count
from podgen.runtime.models.messages_wire import messages_request
from podgen.runtime.models.responses_wire import responses_request
from podgen.sources.acquisition.contracts import FetchedDocument
from podgen.sources.acquisition.service import acquire_source
from podgen.sources.extraction.contracts import ExtractedDocument

out = Path(os.environ["PODGEN_EPISODE_REPLAY_OUT"])
fixtures = Path(os.environ["PODGEN_EPISODE_REPLAY_FIXTURES"])
state = json.loads((out / "state.json").read_text())
root = Path(os.environ["PODGEN_EPISODE_REPLAY_EXPORT"])
writing_root = root / "writing" / json.loads((root / "status.json").read_text())["writing_run_id"] / "archive"
db = Database(state["database_url"])
objects = MeasuredObjects(Path(state["objects"]))
repo = Repository(db, objects, UUID(state["actor"]), purpose="evaluation")
project = UUID(state["project"])
probe = Probe(db, out / "replay-profile.json")
config = ProductionConfig.model_validate_json((root / "private/manifest.json").read_bytes())


def forbidden(*args, **kwargs):
    raise AssertionError("Offline audit forbids HTTP requests")


httpx.Client.send = forbidden
httpx.AsyncClient.send = forbidden
httpx2.Client.send = forbidden
httpx2.AsyncClient.send = forbidden

fetches = json.loads((fixtures / "fetch-fixtures.json").read_text())
fetched = {(v["document"]["requested_url"], v["role"]): v for v in fetches if "document" in v}


class Fetcher:
    async def fetch(self, url, profile, headers=None):
        matches = [v for (key, role), v in fetched.items() if key == url]
        if not matches:
            raise AssertionError("Unrecorded source fetch: " + url)
        row = matches[0]
        return FetchedDocument(**row["document"], body=(fixtures / row["body_file"]).read_bytes())


class Extractor:
    async def extract(self, document, profile, role):
        return ExtractedDocument(**fetched[(document.requested_url, role)]["extracted"])


async def acquisition(repo, project, request):
    return await acquire_source(repo, project, request, fetcher=Fetcher(), extractors=Extractor())


research_root = root / "research" / json.loads((root / "status.json").read_text())["research"]["run_id"]


class Hosted:
    def __init__(self):
        self.calls = 0

    def step(self):
        self.calls += 1
        folder = research_root / f"exchange-{self.calls}"
        r = json.loads((folder / "receipt.json").read_text())
        return Response(
            r["status_code"], r["headers"], (folder / "response.bin").read_bytes(), r["complete"], r["error"]
        )

    def submit(self, *args):
        return self.step()

    def get(self, *args):
        return self.step()


class Tape:
    def __init__(self):
        self.values = defaultdict(list)
        self.seen = Counter()
        self.versions = {}
        for stage in config_stage_names():
            for folder in [writing_root / stage, *sorted((writing_root / stage).glob("attempt-*"))]:
                events = folder / "events.json"
                if not events.exists():
                    continue
                for i, event in enumerate(json.loads(events.read_text())):
                    kind = event["event_kind"]
                    if kind not in ("count_response", "source_context_count", "provider_response"):
                        continue
                    prefix = folder / f"{i:03d}-{kind}"
                    p = Path(str(prefix) + "-response_blob.json")
                    if not p.exists():
                        continue
                    req = Path(str(prefix) + "-wire_request_blob.json")
                    wr = Path(str(prefix) + "-wire_response_blob.json")
                    self.values[(stage, "count" if kind != "provider_response" else "submit")].append(
                        (
                            p.read_bytes(),
                            req.read_bytes() if req.exists() else None,
                            wr.read_bytes() if wr.exists() else None,
                        )
                    )

    def factory(self, profile):
        tape = self

        class Provider:
            def step(self, body, kind):
                stage = body.get("metadata", {}).get("podgen_stage")
                f = sys._getframe(1)
                while stage is None and f:
                    local = f.f_locals
                    for field in ("root", "invocation"):
                        value = local.get(field)
                        if isinstance(value, dict) and value.get("stage") in config_stage_names():
                            stage = value["stage"]
                            break
                    if stage is None and local.get("stage") in config_stage_names():
                        stage = local["stage"]
                    f = f.f_back
                assert stage is not None, "Cannot locate current stage"
                key = (stage, kind)
                index = tape.seen[key]
                assert index < len(tape.values[key]), ("unexpected provider call", key, index)
                raw, native, wire = tape.values[key][index]
                if native:
                    expected = json.loads(native)
                    actual = (
                        native_count(body, profile.provider)
                        if kind == "count"
                        else messages_request(body)
                        if profile.provider == "anthropic"
                        else responses_request(body)
                    )
                    if "metadata" in expected:
                        expected["metadata"] = actual.get("metadata")

                    def versions(a, b):
                        if isinstance(a, dict) and isinstance(b, dict):
                            for k in a.keys() & b.keys():
                                if (
                                    k in ("catalog_version", "result_version")
                                    and isinstance(a[k], str)
                                    and isinstance(b[k], str)
                                    and len(a[k]) == len(b[k]) == 64
                                ):
                                    tape.versions[a[k]] = b[k]
                                else:
                                    versions(a[k], b[k])
                        elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                            for x, y in zip(a, b, strict=True):
                                versions(x, y)
                        elif isinstance(a, str) and isinstance(b, str):
                            try:
                                x, y = json.loads(a), json.loads(b)
                            except (ValueError, TypeError):
                                return
                            versions(x, y)

                    versions(expected, actual)
                    raw_expected = json.dumps(expected, ensure_ascii=False)
                    for old, new in tape.versions.items():
                        raw_expected = raw_expected.replace(old, new)
                    expected = json.loads(raw_expected)
                    if digest(expected) != digest(actual):
                        (out / "mismatched-request.json").write_text(
                            json.dumps(
                                dict(stage=stage, kind=kind, index=index, expected=expected, actual=actual),
                                ensure_ascii=False,
                            )
                        )
                        raise AssertionError(("Native request differs", stage, kind, index))
                    native = json.dumps(actual, ensure_ascii=False, separators=(",", ":")).encode()
                if kind == "submit":
                    value = json.loads(raw)
                    if "metadata" in value:
                        value["metadata"] = body["metadata"]
                    raw = json.dumps(value, ensure_ascii=False).encode()
                    for old, new in tape.versions.items():
                        raw = raw.replace(old.encode(), new.encode())
                tape.seen[key] += 1
                if kind == "submit" and index % 10 == 0:
                    print("replay", stage, index, "SQL", dict(probe.counts), flush=True)
                return WireReply(raw, request_body=native, wire_response=wire)

            def count(self, body):
                return self.step(body, "count")

            def submit(self, body, client_request_id):
                return self.step(body, "submit")

            def retrieve(self, *args):
                raise AssertionError("unexpected retrieve")

        return Provider()


def config_stage_names():
    return [
        "plan",
        "plan-review",
        "draft",
        "reader-review",
        "fact-review",
        "revise-1",
        "reader-review-1",
        "fact-review-1",
        "revise-2",
        "reader-review-2",
        "fact-review-2",
    ]


samples = json.loads((fixtures / "audio-fixtures.json").read_text())


class Speech:
    def __init__(self):
        self.calls = 0

    def submit(self, request, credential):
        row = next(v for v in samples if digest(v["request"]) == digest(request.model_dump(mode="json")))
        self.calls += 1
        r = row["receipt"]
        return HttpReceipt(
            r["status_code"],
            r["headers"],
            (fixtures / row["body_file"]).read_bytes(),
            r["elapsed_ms"],
            r["complete"],
            r["error"],
        )


for key in [config.settings.research_profile.credential_env, config.settings.audio_profile.credential_env]:
    os.environ[key] = "offline-placeholder"
tape = Tape()
hosted = Hosted()
speech = Speech()


def observed(cls, name, phase):
    orig = getattr(cls, name)

    @functools.wraps(orig)
    def wrapped(*args, **kwargs):
        with probe.scope(phase):
            return orig(*args, **kwargs)

    setattr(cls, name, wrapped)


for method in ("_research", "_materials", "_writing", "_terminal_materials"):
    observed(EpisodeProduction, method, method[1:])
original_render = audio.render


def render(*args, **kwargs):
    with probe.scope("audio"):
        return original_render(*args, **kwargs)


audio.render = render
try:
    with probe.scope("prepare"):
        prepare(repo, project, config)
    runner = EpisodeProduction(
        repo,
        project,
        config.run_id,
        research_provider=hosted,
        model_provider=tape.factory,
        audio_transport=speech,
        acquire=acquisition,
        sleep=lambda _: None,
    )
    result = runner.run()
    with probe.scope("export"):
        export(runner.journal, out / "replay-export")
    # A completed ordinary run is a read: no provider calls and no new work.
    calls_before = (dict(tape.seen), hosted.calls, speech.calls)
    with probe.scope("completed_resume"):
        assert runner.run() == result
    assert (dict(tape.seen), hosted.calls, speech.calls) == calls_before
    from podgen.runtime.evaluation.journal import EvaluationJournal
    from podgen.runtime.evaluation.reading_outcome import read_outcome

    # An additional public read is reported separately from the complete run.
    with probe.scope("cold_outcome_read"):
        cold = read_outcome(EvaluationJournal(repo, project), UUID(result["progress"]["writing_run_id"]))
        assert cold["status"] == "keep"
    from hashlib import sha256

    def checksum(path):
        value = sha256()
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                value.update(chunk)
        return value.hexdigest()

    actual_export = out / "replay-export"
    writing_id = result["progress"]["writing_run_id"]
    original_id = json.loads((root / "status.json").read_text())["writing_run_id"]
    original = json.loads((root / "writing" / original_id / "review-outcome.json").read_text())
    current = json.loads((actual_export / "writing" / writing_id / "review-outcome.json").read_text())
    oracle = dict(
        same_final_script_hash=original["script_hash"] == current["script_hash"],
        same_master_wav_sha256=checksum(root / "audio/master.wav")
        == checksum(actual_export / "audio/master.wav"),
        completed_resume_repeated_calls=False,
        expected_generation_counts={str(key): len(values) for key, values in tape.values.items()},
        actual_generation_counts={str(key): value for key, value in tape.seen.items()},
    )
    assert oracle["same_final_script_hash"] and oracle["same_master_wav_sha256"]
    assert oracle["expected_generation_counts"] == oracle["actual_generation_counts"]
    (out / "oracle.json").write_text(json.dumps(oracle, indent=2))
    print("COMPLETE", result["status"], dict(probe.counts), flush=True)
finally:
    probe.save()
    (out / "replay-calls.json").write_text(
        json.dumps(
            dict(
                models={str(k): v for k, v in tape.seen.items()},
                research=hosted.calls,
                speech=speech.calls,
                objects=dict(objects.counts),
            ),
            indent=2,
        )
    )
    db.engine.dispose()
