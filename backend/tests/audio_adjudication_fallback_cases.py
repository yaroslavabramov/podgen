"""Persisted /2 CLI path uses real HTTP routing with provider responses doubled."""

import json
from uuid import uuid4

import httpx

from podgen import audio_adjudication_cli
from podgen.runtime.evaluation.audio.adjudication.attempts import history
from podgen.runtime.evaluation.audio.adjudication.contracts import prefix
from podgen.runtime.evaluation.audio.adjudication.transport import ReviewerTransport
from tests.audio_adjudication_doubles import configuration, openai_response


def assert_persisted_fallback(journal, old_config, common, folder, monkeypatch):
    config = configuration(old_config.model_copy(update=dict(review_id=uuid4())))
    path = folder / "review-v2-config.json"
    path.write_text(config.model_dump_json())
    output = folder / "review-v2-export"
    args = ["--config", str(path), "--export-dir", str(output)]
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.host == "generativelanguage.googleapis.com":
            assert request.headers["x-goog-api-key"] == "fixture-google-secret"
            assert "authorization" not in request.headers
            return httpx.Response(503, content=b"service unavailable")
        assert request.headers["authorization"] == "Bearer fixture-openai-secret"
        assert "x-goog-api-key" not in request.headers
        return httpx.Response(200, content=openai_response(request.content))

    monkeypatch.setenv("JEMINI_API_KEY", "fixture-google-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-openai-secret")
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        monkeypatch.setattr(
            audio_adjudication_cli,
            "ReviewerTransport",
            lambda config: ReviewerTransport(config, client=client),
        )
        assert audio_adjudication_cli.main([*common, "prepare", *args]) == 0 and not calls
        assert audio_adjudication_cli.main([*common, "run", *args]) == 0
        manifest = journal.get(prefix(config.review_id) + ":manifest")
        assert len(calls) == len(manifest["windows"]) + 1
        rows = history(journal, manifest["windows"][0]["key"])
        assert [r["prepared"]["provider"] for r in rows] == ["gemini", "openai"]
        assert rows[0]["receipt"]["status_code"] == 503
        monkeypatch.delenv("JEMINI_API_KEY")
        monkeypatch.delenv("OPENAI_API_KEY")
        assert audio_adjudication_cli.main([*common, "resume", *args]) == 0
        assert audio_adjudication_cli.main([*common, "reprocess", *args, "--reason", "Local replay"]) == 0
        assert len(calls) == len(manifest["windows"]) + 1
    status = json.loads((output / "status.json").read_text())
    assert status["active_provider"] == "openai" and status["all_cases_resolved"]
    artifacts = "".join(p.read_text() for p in output.rglob("*.json"))
    assert "fixture-google-secret" not in artifacts and "fixture-openai-secret" not in artifacts
